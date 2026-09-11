"""Secret detection/redaction and path confinement helpers."""

from __future__ import annotations

import codecs
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import RRCError

SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:password|passwd|pwd|api[_-]?key|access[_-]?token|secret[_-]?key)"
    r"\s*(?:=|:)\s*['\"]?[^\s'\"]+"
)
SECRET_FLAG = re.compile(
    r"(?i)--(?:password|passwd|api-key|access-token|token|secret)(?:=|\s+)"
    r"['\"]?[^\s'\"]+"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")
SECRET_KEY = re.compile(
    r"(?i)^(?:password|passwd|pwd|api[_-]?key|access[_-]?token|secret[_-]?key|auth[_-]?token)$"
)


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in (SECRET_ASSIGNMENT, SECRET_FLAG, PRIVATE_KEY))


def contains_secret_data(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and SECRET_KEY.fullmatch(key)) or contains_secret_data(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(contains_secret_data(item) for item in value)
    return isinstance(value, str) and contains_secret(value)


def redact(text: str, explicit_values: Iterable[str] = ()) -> str:
    return StreamRedactor(explicit_values).feed(text.encode("utf-8"), final=True).decode("utf-8")


def redact_data(value: Any, explicit_values: Iterable[str] = ()) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if isinstance(key, str) and REDACTION_KEY.fullmatch(key)
            else redact_data(item, explicit_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_data(item, explicit_values) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, explicit_values) for item in value)
    return redact(value, explicit_values) if isinstance(value, str) else value


def confined_relative_path(value: str, *, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or value in {"", "."} or ".." in path.parts:
        raise RRCError(
            "path_not_confined",
            f"{field} must be a non-empty relative path without '..': {value}",
            "schema",
        )
    return path


def ensure_within(root: Path, candidate: Path, *, field: str) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise RRCError(
            "path_escape",
            f"{field} escapes root {resolved_root}: {resolved_candidate}",
            "artifact",
        ) from exc
    return resolved_candidate


def secure_file_mode(path: Path) -> bool:
    return not bool(os.stat(path).st_mode & 0o077)


# 这些规则只影响输出脱敏，不改变 RunSpec 的凭据检测或主机密钥策略。
_CREDENTIAL_BASE = (
    r"(?:password|passwd|pwd|api[_-]?key|access[_-]?token|"
    r"auth[_-]?token|secret[_-]?key|token)"
)
_CREDENTIAL_NAME = r"(?:[a-z0-9_]+_)?" + _CREDENTIAL_BASE
REDACTION_KEY = re.compile(_CREDENTIAL_NAME, re.IGNORECASE)
_CREDENTIAL_HEADER = re.compile(
    r"(?:[\"']?" + _CREDENTIAL_BASE + r"[\"']?\s*[:=]\s*|"
    r"--(?:password|passwd|api-key|access-token|token|secret)(?:=|\s+))",
    re.IGNORECASE,
)
_PARTIAL_HEADER = re.compile(r"[\"']?" + _CREDENTIAL_BASE + r"[\"']?\s+$", re.IGNORECASE)
_PRIVATE_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_PRIVATE_END = re.compile(r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")


class StreamRedactor:
    """先跨块脱敏再进入头尾缓存，避免截断边缘留下半个口令。"""

    def __init__(self, explicit_values: Iterable[str] = ()):
        values = {value for value in explicit_values if value}
        # JSON 转义后的口令仍可被还原，诊断预览也必须屏蔽这些表示。
        values.update(json.dumps(value, ensure_ascii=True)[1:-1] for value in tuple(values))
        values.update(json.dumps(value, ensure_ascii=False)[1:-1] for value in tuple(values))
        self.values = sorted(values, key=len, reverse=True)
        self.keep = max(128, max(map(len, self.values), default=0))
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending = ""
        self.mode: str | None = None
        self.escaped = False

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        self.pending += self.decoder.decode(data, final=final)
        output = []
        while self.pending:
            if self.mode == "private":
                end = _PRIVATE_END.search(self.pending)
                if end is None:
                    self.pending = "" if final else self.pending[-96:]
                    break
                self.pending = self.pending[end.end() :]
                self.mode = None
                continue
            if self.mode == "value_start":
                self.pending = self.pending.lstrip()
                if not self.pending:
                    break
                if self.pending[0] in "\"'":
                    self.mode, self.pending = self.pending[0], self.pending[1:]
                else:
                    self.mode = "unquoted"
                continue
            if self.mode == "unquoted":
                end = re.search(r"[\s\"']", self.pending)
                if end is None:
                    self.pending = ""
                    break
                self.pending = self.pending[end.start() :]
                self.mode = None
                continue
            if self.mode in {'"', "'"}:
                end = None
                for index, char in enumerate(self.pending):
                    if self.escaped:
                        self.escaped = False
                    elif char == "\\":
                        self.escaped = True
                    elif char == self.mode:
                        end = index + 1
                        break
                if end is None:
                    self.pending = ""
                    break
                self.pending = self.pending[end:]
                self.mode = None
                continue
            cut = len(self.pending) if final else max(0, len(self.pending) - self.keep)
            candidates = []
            for value in self.values:
                start = self.pending.find(value)
                if start >= 0:
                    candidates.append((start, start + len(value), "explicit"))
            for pattern, kind in ((_CREDENTIAL_HEADER, "value_start"), (_PRIVATE_BEGIN, "private")):
                found = pattern.search(self.pending)
                if found:
                    candidates.append((found.start(), found.end(), kind))
            found = min(candidates, default=None, key=lambda item: (item[0], -item[1]))
            if found is not None and (found[0] < cut or final):
                start, end, kind = found
                output += [self.pending[:start], "[REDACTED]"]
                self.pending = self.pending[end:]
                self.mode = None if kind == "explicit" else kind
                continue
            partial = _PARTIAL_HEADER.search(self.pending)
            if partial and partial.start() < cut:
                # 超长空白不是值；保留短的完整键，下一块仍能识别赋值。
                output.append(self.pending[: partial.start()])
                self.pending = self.pending[partial.start() :].rstrip() + " "
                break
            if cut:
                output.append(self.pending[:cut])
                self.pending = self.pending[cut:]
            break
        return "".join(output).encode("utf-8")
