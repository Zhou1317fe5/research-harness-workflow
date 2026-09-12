#!/usr/bin/env python3
"""Commit named task paths without disturbing unrelated staged changes."""

from __future__ import annotations

import subprocess
import os
import re
from pathlib import Path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=False)
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout).decode(errors="replace").strip()
        raise RuntimeError(message or f"git {' '.join(args)} failed with {proc.returncode}")
    return proc


def index_patch(repo: Path) -> bytes:
    return _git(repo, "diff", "--cached", "--binary").stdout


def staged_paths(repo: Path) -> set[str]:
    output = _git(repo, "diff", "--cached", "--name-only", "-z").stdout
    return {item.decode(errors="surrogateescape") for item in output.split(b"\0") if item}


def _normalized_relative(repo: Path, path: Path) -> str:
    candidate = path if path.is_absolute() else repo / path
    try:
        relative = candidate.resolve().relative_to(repo.resolve()).as_posix().rstrip("/")
    except ValueError as exc:
        raise RuntimeError(f"task path escapes repository: {path}") from exc
    if not relative or relative == ".":
        raise RuntimeError("task path must not be the repository root")
    return relative


def _paths_overlap(left: str, right: str) -> bool:
    if os.name == "nt":
        left, right = left.casefold(), right.casefold()
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def verify_commit(repo: Path, commit: str, paths: list[Path] | None = None) -> str:
    """只读核验不可变 Git 对象和适用路径；不会把当前 HEAD 猜作任务提交。"""
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit or ""):
        raise ValueError("git_commit_missing_or_invalid")
    resolved = _git(repo, "rev-parse", "--verify", commit).stdout.decode().strip()
    if _git(repo, "cat-file", "-t", resolved).stdout.strip() != b"commit":
        raise ValueError("git_object_is_not_commit")
    changed = {
        x.decode(errors="surrogateescape")
        for x in _git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", resolved).stdout.split(b"\0") if x
    }
    for path in paths or []:
        relative = _normalized_relative(repo, path)
        exists = _git(repo, "cat-file", "-e", f"{resolved}:{relative}", check=False).returncode == 0
        if not exists and relative not in changed:
            raise ValueError(f"git_commit_path_missing:{relative}")
    return resolved


def row_git_errors(csv_path: Path, row: dict[str, str], *, workdir: Path | None = None) -> list[str]:
    if row.get("git_state") != "已提交":
        return []
    from mission_completion import parse_note_tags

    try:
        tags = parse_note_tags(row.get("notes", ""))
        commit = row.get("commit_hash", "").strip() or tags.get("commit_hash", "")
        location = _git(csv_path.parent, "rev-parse", "--show-toplevel", check=False)
        if location.returncode:
            location = _git(workdir or Path.cwd(), "rev-parse", "--show-toplevel")
        repo = Path(location.stdout.decode().strip())
        project = repo
        if tags.get("git_repo"):
            selected = (project / tags["git_repo"]).resolve()
            # 结果行也可关联 RunID；用途决定 Git 命名空间，不能只看 RunID 是否为空。
            if not selected.is_relative_to(project.resolve()) or row.get("phase") not in {"artifact", "analysis", "review"}:
                raise ValueError("git_repository_identity_invalid")
            actual = Path(_git(selected, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()
            if actual != selected:
                raise ValueError("git_repo_must_identify_repository_root")
            repo = selected
        paths = []
        for ref in row.get("refs", "").split(";"):
            value = re.sub(r":\d+(?:-\d+)?$", "", ref.strip().split("#", 1)[0])
            if not value or "://" in value or value.startswith(("command:", "manual:", "session:")):
                continue
            candidate = Path(value)
            if candidate.suffix or "/" in value:
                candidates = [candidate] if candidate.is_absolute() else [project / candidate, repo / candidate, csv_path.parent / candidate]
                if any(char.isspace() for char in value) and not any(p.is_file() for p in candidates):
                    continue  # refs 也允许命令；只有显式文件路径才用于 Git 核验。
                existing = {p.resolve() for p in candidates if p.is_file()}
                if len(existing) > 1:
                    raise ValueError(f"git_reference_ambiguous:{value}")
                local = next(iter(existing)) if existing else candidates[0]
                if local.resolve().is_relative_to(repo.resolve()):
                    # 原始产物不进 Git，科研身份另由 ingestion 校验。
                    if "remote_artifacts" not in local.resolve().relative_to(repo.resolve()).parts:
                        paths.append(local)
        resolved = verify_commit(repo, commit, paths)
        if row.get("commit_hash") and tags.get("commit_hash") and verify_commit(repo, tags["commit_hash"]) != resolved:
            raise ValueError("git_commit_reference_conflict")
        expected = tags.get("pre_run_code_commit")
        if repo == project and row.get("run_id") and expected and verify_commit(repo, expected) != resolved:
            raise ValueError("git_reviewed_source_mismatch")
    except (OSError, RuntimeError, ValueError) as exc:
        return [f"git_evidence_invalid:{row.get('id', '')}:{exc}"]
    return []


def commit_paths(repo: Path, paths: list[Path], message: str) -> str:
    repo = repo.resolve()
    task_paths = [_normalized_relative(repo, path) for path in paths]
    if not task_paths:
        raise ValueError("task paths must not be empty")
    parent_result = _git(repo, "rev-parse", "--verify", "HEAD", check=False)
    parent = parent_result.stdout.decode().strip() if parent_result.returncode == 0 else None
    branch = _git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout
    staged = staged_paths(repo)
    overlap = sorted(
        staged_path
        for staged_path in staged
        if any(_paths_overlap(task_path, staged_path) for task_path in task_paths)
    )
    if overlap:
        raise RuntimeError("task path already has staged changes: " + ", ".join(overlap))
    before = index_patch(repo)
    new_paths = [
        item.decode(errors="surrogateescape")
        for item in _git(
            repo, "ls-files", "--others", "--exclude-standard", "-z", "--", *task_paths
        ).stdout.split(b"\0")
        if item
    ]
    try:
        # --only 直接读取已跟踪路径；新文件只登记 intent，失败时可精确撤回。
        if new_paths:
            _git(repo, "add", "--intent-to-add", "--", *new_paths)
        _git(repo, "commit", "--only", "-m", message, "--", *task_paths)
    except Exception:
        if new_paths:
            _git(repo, "update-index", "--force-remove", "--", *new_paths)
        if index_patch(repo) != before:
            raise RuntimeError("commit failed and index delta could not be preserved exactly")
        raise
    if index_patch(repo) != before:
        raise RuntimeError("unrelated staged patch changed during task commit")
    commit = verify_commit(repo, _git(repo, "rev-parse", "HEAD").stdout.decode().strip(), paths)
    parents = _git(repo, "rev-list", "--parents", "-n", "1", commit).stdout.decode().split()[1:]
    if parents != ([parent] if parent else []) or _git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout != branch:
        raise RuntimeError("commit succeeded but parent/branch context changed; inspect Git before retrying")
    changed = _git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", commit).stdout
    if any(not any(_paths_overlap(task, path.decode(errors="surrogateescape")) for task in task_paths) for path in changed.split(b"\0") if path):
        raise RuntimeError("commit contains paths outside the task")
    if _git(repo, "diff", "--exit-code", commit, "--", *task_paths, check=False).returncode:
        raise RuntimeError("committed task content differs from the working tree")
    return commit
