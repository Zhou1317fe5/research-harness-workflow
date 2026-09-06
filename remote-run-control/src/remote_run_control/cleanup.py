"""Run-bound output cleanup for disposable pre-review smoke workloads."""

from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import RRCError
from .jsonutil import atomic_write_json, utc_now
from .models import RunSpec

SUMMARY_PATH = "smoke_summary.json"


def _relative(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or value in {"", "."} or ".." in path.parts:
        raise RRCError(
            "cleanup_path",
            f"{field} must be a non-empty relative path without '..': {value}",
            "cleanup",
        )
    return path.as_posix()


def _matches(relative: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns)


def _tree_size(path: Path) -> int:
    if path.is_symlink():
        return path.lstat().st_size
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        current = Path(root)
        directories[:] = [name for name in directories if not (current / name).is_symlink()]
        for name in files:
            candidate = current / name
            total += (
                candidate.lstat().st_size if candidate.is_symlink() else candidate.stat().st_size
            )
    return total


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def cleanup_output(spec: RunSpec, *, terminal_state: str) -> dict[str, Any] | None:
    policy = spec.output_cleanup
    if policy is None:
        return None
    if policy.mode != "pre_review_smoke":
        raise RRCError("cleanup_mode", "unsupported output cleanup mode", "cleanup")
    if spec.metadata.get("execution_purpose") != "pre_review_smoke":
        raise RRCError(
            "cleanup_purpose",
            "output cleanup is restricted to pre_review_smoke runs",
            "cleanup",
        )
    output_root = Path(spec.remote.output_root)
    if spec.run_id not in output_root.as_posix():
        raise RRCError(
            "cleanup_run_binding",
            "output root must contain the bound run_id",
            "cleanup",
        )
    if output_root.is_symlink():
        raise RRCError("cleanup_symlink", "output root cannot be a symlink", "cleanup")
    output_root.mkdir(parents=True, exist_ok=True)
    root = output_root.resolve()
    retain = {_relative(value, field="output_cleanup.retain") for value in policy.retain}
    retain.add(SUMMARY_PATH)
    patterns = tuple(
        _relative(value, field="output_cleanup.delete_globs") for value in policy.delete_globs
    )

    candidates: list[tuple[str, Path]] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        for name in [*directories, *files]:
            candidate = base / name
            relative = candidate.relative_to(root).as_posix()
            if relative in retain:
                continue
            matches_pattern = _matches(relative, patterns)
            matches_size = (
                policy.delete_files_larger_than_bytes is not None
                and candidate.is_file()
                and not candidate.is_symlink()
                and candidate.stat().st_size > policy.delete_files_larger_than_bytes
            )
            if matches_pattern or matches_size:
                candidates.append((relative, candidate))
        directories[:] = [
            name
            for name in directories
            if not any(
                relative == (base / name).relative_to(root).as_posix()
                for relative, _ in candidates
            )
        ]

    deleted_paths: list[str] = []
    released_bytes = 0
    for relative, candidate in sorted(
        candidates, key=lambda item: item[0].count("/"), reverse=True
    ):
        if not candidate.exists() and not candidate.is_symlink():
            continue
        released_bytes += _tree_size(candidate)
        _remove(candidate)
        deleted_paths.append(relative)

    remaining: list[str] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        for name in [*directories, *files]:
            relative = (base / name).relative_to(root).as_posix()
            if _matches(relative, patterns):
                remaining.append(relative)

    summary = {
        "schema_version": "rrctl.smoke-summary.v1",
        "run_id": spec.run_id,
        "terminal_state": terminal_state,
        "checkpoint_cleanup_completed": not remaining,
        "checkpoint_paths_remaining": sorted(set(remaining)),
        "deleted_paths": sorted(set(deleted_paths)),
        "released_bytes": released_bytes,
        "retained_evidence_paths": [
            str(Path(spec.remote.control_root) / "console.log"),
            str(Path(spec.remote.control_root) / "status.json"),
            str(root / SUMMARY_PATH),
        ],
        "updated_at": utc_now(),
    }
    atomic_write_json(root / SUMMARY_PATH, summary)
    if remaining:
        raise RRCError(
            "cleanup_incomplete",
            "smoke checkpoint cleanup left matching paths",
            "cleanup",
            details={"checkpoint_paths_remaining": sorted(set(remaining))},
        )
    return summary
