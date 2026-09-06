"""Secret detection/redaction and path confinement helpers."""

from __future__ import annotations

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
    result = text
    for value in sorted({value for value in explicit_values if value}, key=len, reverse=True):
        result = result.replace(value, "[REDACTED]")
    result = SECRET_ASSIGNMENT.sub("credential=[REDACTED]", result)
    result = SECRET_FLAG.sub("--credential=[REDACTED]", result)
    result = PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", result)
    return result


def redact_data(value: Any, explicit_values: Iterable[str] = ()) -> Any:
    if isinstance(value, dict):
        return {key: redact_data(item, explicit_values) for key, item in value.items()}
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
