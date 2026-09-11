"""标准库实现的控制输出预算；可复用于只读远端环境探针。"""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

CONTROL_OUTPUT_LIMIT = 256 * 1024
_MARKER = b"\n[... control output truncated ...]\n"


class TailBuffer:
    def __init__(self, limit: int | None):
        self.limit = limit
        self.total = 0
        self.head = bytearray()
        self.tail = bytearray()

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        if self.limit is None:
            self.head.extend(data)
            return
        split = self.limit // 2
        take = min(len(data), max(0, split - len(self.head)))
        self.head.extend(data[:take])
        self.tail.extend(data[take:])
        excess = len(self.tail) - (self.limit - split)
        if excess > 0:
            del self.tail[:excess]

    @property
    def truncated(self) -> bool:
        return self.limit is not None and self.total > self.limit

    def bytes(self) -> bytes:
        if not self.truncated:
            return bytes(self.head + self.tail)
        available = self.limit - len(_MARKER)
        first = available // 2
        return bytes(self.head[:first]) + _MARKER + bytes(self.tail[-(available - first) :])


class OutputCapture:
    def __init__(self, limit: int | None, redactor: Any = None):
        self.raw = TailBuffer(limit)
        self.safe = TailBuffer(limit) if redactor is not None else None
        self.redactor = redactor

    def feed(self, data: bytes) -> None:
        self.raw.feed(data)
        if self.safe is not None:
            self.safe.feed(self.redactor.feed(data))

    def finish(self) -> None:
        if self.safe is not None:
            self.safe.feed(self.redactor.feed(b"", final=True))

    def preview(self) -> bytes:
        return self.safe.bytes() if self.safe is not None else self.raw.bytes()

    def metadata(self, *, complete: bool) -> dict[str, Any]:
        return {
            "truncated": self.raw.truncated,
            "limit_bytes": self.raw.limit,
            "original_bytes": self.raw.total if complete else None,
            "observed_bytes": self.raw.total,
        }


@dataclass
class CapturedCommand:
    returncode: int | None
    stdout: OutputCapture
    stderr: OutputCapture
    timed_out: bool


def run_bounded(
    argv: list[str],
    *,
    input_data: bytes | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 180,
    stdout_limit: int | None = CONTROL_OUTPUT_LIMIT,
    redactors: tuple[Any, Any] = (None, None),
) -> CapturedCommand:
    """同时排空两个管道并分块写 stdin；持续大输出不会扩张控制缓存。"""
    stdout = OutputCapture(stdout_limit, redactors[0])
    stderr = OutputCapture(CONTROL_OUTPUT_LIMIT, redactors[1])
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    offset = 0
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, capture in ((process.stdout, stdout), (process.stderr, stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, capture)
            if input_data:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, None)
            elif process.stdin:
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    if key.data is None:
                        try:
                            offset += os.write(
                                key.fd, memoryview(input_data)[offset : offset + 65536]
                            )
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            offset = len(input_data)
                        if offset >= len(input_data):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        try:
                            data = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if data:
                            key.data.feed(data)
                        else:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
            if not timed_out:
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
    finally:
        if process.poll() is None:
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        stdout.finish()
        stderr.finish()
    return CapturedCommand(process.returncode, stdout, stderr, timed_out)
