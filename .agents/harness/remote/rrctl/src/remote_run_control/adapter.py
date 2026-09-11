"""Process-isolated project adapter protocol."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RRCError
from .jsonutil import canonical_bytes
from .processes import process_identity
from .security import redact


@dataclass(frozen=True, slots=True)
class AdapterResult:
    healthy: bool
    complete: bool
    progress: dict[str, Any]
    observations: dict[str, Any]
    artifacts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "rrctl.adapter.v1",
            "healthy": self.healthy,
            "complete": self.complete,
            "progress": self.progress,
            "observations": self.observations,
            "artifacts": list(self.artifacts),
        }


def parse_adapter_result(value: Any) -> AdapterResult:
    if not isinstance(value, dict):
        raise RRCError("adapter_json_type", "adapter output must be a JSON object", "adapter")
    if value.get("protocol") != "rrctl.adapter.v1":
        raise RRCError(
            "adapter_protocol",
            "adapter protocol must equal rrctl.adapter.v1",
            "adapter",
        )
    healthy = value.get("healthy")
    complete = value.get("complete")
    if not isinstance(healthy, bool) or not isinstance(complete, bool):
        raise RRCError(
            "adapter_verdict_type",
            "adapter healthy and complete must be booleans",
            "adapter",
        )
    if complete and not healthy:
        raise RRCError(
            "adapter_verdict_inconsistent",
            "adapter cannot report complete=true with healthy=false",
            "adapter",
        )
    progress = value.get("progress", {})
    observations = value.get("observations", {})
    artifacts = value.get("artifacts", [])
    if not isinstance(progress, dict) or not isinstance(observations, dict):
        raise RRCError(
            "adapter_payload_type",
            "adapter progress and observations must be objects",
            "adapter",
        )
    if not isinstance(artifacts, list) or any(
        not isinstance(item, str) or not item for item in artifacts
    ):
        raise RRCError(
            "adapter_artifacts_type",
            "adapter artifacts must be a string array",
            "adapter",
        )
    return AdapterResult(healthy, complete, progress, observations, tuple(artifacts))


def _stop_checker(process: subprocess.Popen) -> None:
    """超时只停止本次检查器的子树，保持 worker/workload 的进程组存活。"""
    identities = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            with suppress(RRCError):
                identity = process_identity(int(path.name))
                identities[identity["pid"]] = identity
    descendants = []
    parents = {process.pid}
    while parents:
        children = [item for item in identities.values() if item["parent_pid"] in parents]
        descendants.extend(children)
        parents = {item["pid"] for item in children}
    for item in reversed(descendants):
        with suppress(RRCError, ProcessLookupError, PermissionError):
            if process_identity(item["pid"])["start_ticks"] == item["start_ticks"]:
                os.kill(item["pid"], signal.SIGKILL)
    with suppress(ProcessLookupError):
        process.kill()
    process.wait(timeout=5)


def run_adapter(
    argv: tuple[str, ...],
    context: dict[str, Any],
    *,
    timeout_seconds: int,
    cwd: Path,
    environment_command: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> AdapterResult | None:
    if not argv:
        return None
    command = [*environment_command, *argv]
    # 临时文件避免子进程继承 stdout pipe 后让 communicate 在超时清理中继续阻塞。
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=env or os.environ.copy(),
        ) as process:
            try:
                process.communicate(canonical_bytes(context) + b"\n", timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _stop_checker(process)
                raise RRCError(
                    "adapter_timeout",
                    f"adapter exceeded {timeout_seconds}s timeout",
                    "adapter",
                ) from exc
            return_code = process.returncode
        stdout.seek(0)
        output = stdout.read()
        stderr.seek(0, 2)
        stderr.seek(max(0, stderr.tell() - 4000))
        error_text = stderr.read().decode("utf-8", errors="replace")
    if return_code != 0:
        raise RRCError(
            "adapter_exit",
            f"adapter exited with code {return_code}",
            "adapter",
            details={"stderr": redact(error_text)},
        )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RRCError(
            "adapter_json",
            f"adapter emitted invalid JSON: {exc}",
            "adapter",
            details={"stdout": redact(output.decode("utf-8", errors="replace")[-4000:])},
        ) from exc
    return parse_adapter_result(value)
