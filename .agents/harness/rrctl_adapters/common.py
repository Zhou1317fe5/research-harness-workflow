"""Dependency-free helpers shared by rrctl adapters."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path, PurePosixPath
from typing import Any


class AdapterContractError(Exception):
    """A stable, user-facing adapter contract failure."""


def read_context() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise AdapterContractError(f"context_json_invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise AdapterContractError("context_type_invalid: expected an object")
    if value.get("protocol") != "rrctl.adapter.context.v1":
        raise AdapterContractError("context_protocol_invalid")
    if value.get("phase") not in {"first_step", "periodic", "completion"}:
        raise AdapterContractError("context_phase_invalid")
    if not isinstance(value.get("metadata"), dict):
        raise AdapterContractError("context_metadata_invalid")
    return value


def require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdapterContractError(f"{field}_type_invalid: expected an object")
    return value


def require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AdapterContractError(f"{field}_type_invalid: expected non-empty text")
    return value


def require_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise AdapterContractError(f"{field}_type_invalid: expected a string array")
    return value


def relative_path(value: Any, field: str) -> PurePosixPath:
    text = require_text(value, field)
    path = PurePosixPath(text)
    if path.is_absolute() or text in {".", ""} or ".." in path.parts:
        raise AdapterContractError(f"{field}_not_confined: {text}")
    return path


def confined_path(
    output_root: Path,
    value: Any,
    field: str,
    *,
    must_exist: bool = True,
) -> Path:
    relative = relative_path(value, field)
    if output_root.is_symlink():
        raise AdapterContractError("output_root_symlink_rejected")
    resolved_root = output_root.resolve()
    unresolved = output_root / relative
    current = output_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AdapterContractError(f"{field}_symlink_rejected: {relative}")
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise AdapterContractError(f"{field}_escape_rejected: {relative}") from exc
    if must_exist and not resolved.exists():
        raise AdapterContractError(f"{field}_missing: {relative}")
    return resolved


def load_json_object(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdapterContractError(f"{field}_read_failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AdapterContractError(f"{field}_json_invalid: {exc}") from exc
    return require_mapping(value, field)


def load_jsonl_last_object(path: Path, field: str) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            buffer = b""
            raw_line: bytes | None = None
            while position > 0 and raw_line is None:
                size = min(8192, position)
                position -= size
                handle.seek(position)
                buffer = handle.read(size) + buffer
                lines = buffer.split(b"\n")
                # The first item may be a partial line until the file start is reached.
                complete = lines if position == 0 else lines[1:]
                raw_line = next(
                    (line for line in reversed(complete) if line.strip()), None
                )
    except OSError as exc:
        raise AdapterContractError(f"{field}_read_failed: {exc}") from exc
    if raw_line is None:
        raise AdapterContractError(f"{field}_jsonl_empty")
    try:
        value = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterContractError(f"{field}_jsonl_invalid: {exc}") from exc
    return require_mapping(value, field)


def load_structured_object(path: Path, field: str, format_name: str = "json") -> dict[str, Any]:
    if format_name == "json":
        return load_json_object(path, field)
    if format_name == "jsonl_last":
        return load_jsonl_last_object(path, field)
    raise AdapterContractError(f"{field}_format_invalid: {format_name}")


def dotted_value(value: dict[str, Any], dotted: str, field: str) -> Any:
    current: Any = value
    remaining = dotted
    while True:
        if not remaining or not isinstance(current, dict):
            raise AdapterContractError(f"{field}_missing: {dotted}")
        # Prefer an exact remaining key. This preserves decimal or dotted literal
        # keys such as thresholds["2.0"] while retaining nested dotted paths.
        if remaining in current:
            return current[remaining]
        head, separator, tail = remaining.partition(".")
        if not separator or not head or head not in current:
            raise AdapterContractError(f"{field}_missing: {dotted}")
        current = current[head]
        remaining = tail


def require_finite_fields(
    value: dict[str, Any], fields: list[str], source: str
) -> None:
    for dotted in fields:
        item = dotted_value(value, dotted, source)
        if (
            not isinstance(item, int | float)
            or isinstance(item, bool)
            or not math.isfinite(float(item))
        ):
            raise AdapterContractError(f"{source}_non_finite: {dotted}")


def require_identity_fields(
    value: dict[str, Any],
    expected: dict[str, Any],
    source: str,
) -> None:
    for dotted, expected_value in sorted(expected.items()):
        actual = dotted_value(value, dotted, source)
        if actual != expected_value:
            raise AdapterContractError(
                f"{source}_identity_mismatch: {dotted} expected={expected_value!r} actual={actual!r}"
            )


def emit_success(
    *,
    healthy: bool,
    complete: bool,
    progress: dict[str, Any],
    observations: dict[str, Any],
    artifacts: list[str],
) -> None:
    value = {
        "protocol": "rrctl.adapter.v1",
        "healthy": healthy,
        "complete": complete,
        "progress": progress,
        "observations": observations,
        "artifacts": artifacts,
    }
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def emit_error(exc: BaseException) -> int:
    value = {
        "protocol": "mission.rrctl-adapter-error.v1",
        "error": str(exc),
    }
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
    )
    return 2
