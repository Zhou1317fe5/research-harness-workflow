"""Confined artifact manifest generation and verification."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import RRCError
from .jsonutil import atomic_write_json, load_json, sha256_file, utc_now
from .models import ArtifactSpec
from .security import confined_relative_path, ensure_within


def _files_for_path(output_root: Path, relative: str) -> list[Path]:
    confined_relative_path(relative, field="artifact.path")
    if output_root.is_symlink():
        raise RRCError("artifact_symlink", "output root cannot be a symlink", "artifact")
    unresolved = output_root / relative
    if unresolved.is_symlink():
        raise RRCError("artifact_symlink", f"artifact cannot be a symlink: {relative}", "artifact")
    candidate = ensure_within(output_root, unresolved, field="artifact.path")
    if candidate.is_file():
        return [candidate]
    if candidate.is_dir():
        files: list[Path] = []
        for path in sorted(candidate.rglob("*")):
            if path.is_symlink():
                raise RRCError(
                    "artifact_symlink", f"artifact tree contains a symlink: {path}", "artifact"
                )
            if path.is_file():
                ensure_within(output_root, path, field="artifact.path")
                files.append(path)
        return files
    return []


def build_artifact_manifest(
    *,
    run_id: str,
    output_root: Path,
    declared: tuple[ArtifactSpec, ...],
    adapter_paths: Iterable[str] = (),
    destination: Path | None,
) -> dict[str, Any]:
    requested: dict[str, bool] = {item.path: item.required for item in declared}
    for value in adapter_paths:
        requested.setdefault(value, True)
    entries_by_path: dict[str, dict[str, Any]] = {}
    for relative, required in sorted(requested.items()):
        files = _files_for_path(output_root, relative)
        if required and not files:
            raise RRCError(
                "artifact_required_missing",
                f"required artifact does not exist or is empty: {relative}",
                "artifact",
            )
        for path in files:
            relative_file = path.relative_to(output_root.resolve()).as_posix()
            entries_by_path[relative_file] = {
                "path": relative_file,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
    manifest = {
        "schema_version": "rrctl.artifacts.v1",
        "run_id": run_id,
        "generated_at": utc_now(),
        "output_root": str(output_root),
        "entries": [entries_by_path[key] for key in sorted(entries_by_path)],
    }
    if destination is not None:
        atomic_write_json(destination, manifest)
    return manifest


def build_diagnostic_snapshot(
    *,
    run_id: str,
    control_root: Path,
    output_root: Path,
    output_paths: Iterable[str],
    max_file_bytes: int = 1_048_576,
    max_total_bytes: int = 16_777_216,
) -> dict[str, Any]:
    """保存有限大小的日志快照；不改变 workload 或运行终态。"""
    if control_root.is_symlink() or (control_root / "diagnostics").is_symlink():
        raise RRCError("diagnostic_symlink", "diagnostic root cannot be a symlink", "artifact")
    snapshot_id = uuid.uuid4().hex
    destination = control_root / "diagnostics" / snapshot_id
    destination.mkdir(parents=True, exist_ok=False)
    remaining = max_total_bytes
    copied: list[dict[str, Any]] = []
    skipped: list[str] = []
    seen: set[tuple[str, str]] = set()
    roots = (
        (
            "control",
            control_root,
            (
                "status.json",
                "console.log",
                "events.jsonl",
                "health.jsonl",
                "health_state.json",
                "worker.log",
                "preflight.log",
                "preflight.json",
                "monitor.json",
                "monitor-events.jsonl",
                "health-latest",
                "completion.json",
                "workload-exit.json",
                "finalization-error.json",
            ),
        ),
        ("output", output_root, tuple(output_paths)),
    )
    for label, root, paths in roots:
        for relative in paths:
            for path in _files_for_path(root, relative):
                relative_file = path.relative_to(root.resolve()).as_posix()
                key = (label, relative_file)
                if key in seen:
                    continue
                seen.add(key)
                name = f"{label}/{relative_file}"
                # 诊断只取文本证据，不随目录声明下载 checkpoint 等大文件。
                if (
                    path.suffix.lower() not in {".log", ".txt", ".json", ".jsonl", ".csv", ".tsv"}
                    or remaining <= 0
                ):
                    skipped.append(name)
                    continue
                with path.open("rb") as source:
                    source.seek(0, 2)
                    size = source.tell()
                    count = min(size, max_file_bytes, remaining)
                    source.seek(size - count)
                    data = source.read(count)
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                remaining -= len(data)
                copied.append(
                    {
                        "path": name,
                        "source_bytes": size,
                        "copied_bytes": len(data),
                        "truncated": len(data) < size,
                    }
                )
    atomic_write_json(
        destination / "snapshot.json",
        {
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "files": copied,
            "skipped": skipped,
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
        },
    )
    build_artifact_manifest(
        run_id=run_id,
        output_root=destination,
        declared=(
            ArtifactSpec("control", False),
            ArtifactSpec("output", False),
            ArtifactSpec("snapshot.json"),
        ),
        destination=destination / "artifact_manifest.json",
    )
    return {"run_id": run_id, "snapshot_id": snapshot_id}


def load_artifact_manifest(path: Path, *, expected_run_id: str | None = None) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict) or value.get("schema_version") != "rrctl.artifacts.v1":
        raise RRCError("artifact_manifest_invalid", "invalid artifact manifest", "artifact")
    if expected_run_id and value.get("run_id") != expected_run_id:
        raise RRCError("artifact_run_mismatch", "artifact manifest run id mismatch", "artifact")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise RRCError("artifact_entries_invalid", "artifact entries must be an array", "artifact")
    seen: set[str] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise RRCError("artifact_entry_invalid", f"entry {index} must be an object", "artifact")
        relative = item.get("path")
        if not isinstance(relative, str):
            raise RRCError("artifact_entry_path", f"entry {index} path is invalid", "artifact")
        confined_relative_path(relative, field=f"entries[{index}].path")
        if relative in seen:
            raise RRCError("artifact_entry_duplicate", f"entry {index} is duplicated", "artifact")
        seen.add(relative)
        if (
            not isinstance(item.get("size"), int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
        ):
            raise RRCError("artifact_entry_size", f"entry {index} size is invalid", "artifact")
        sha = item.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise RRCError("artifact_entry_sha", f"entry {index} SHA is invalid", "artifact")
    return value
