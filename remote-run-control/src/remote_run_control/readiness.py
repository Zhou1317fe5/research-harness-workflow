"""Deterministic local readiness validation for a RunSpec."""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .jsonutil import sha256_file
from .models import RunSpec
from .profiles import Profile, ProfileStore
from .security import confined_relative_path, contains_secret, contains_secret_data
from .source_identity import source_content_sha256

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, order=True, slots=True)
class Diagnostic:
    code: str
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    errors: tuple[Diagnostic, ...]
    warnings: tuple[Diagnostic, ...]
    run_spec_sha256: str
    profile: Profile | None = None
    source_content_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "errors": [item.to_dict() for item in self.errors],
            "warnings": [item.to_dict() for item in self.warnings],
            "run_spec_sha256": self.run_spec_sha256,
            "source_content_sha256": self.source_content_sha256,
            "profile": self.profile.name if self.profile else None,
        }


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _path_allowed(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    for pattern in patterns:
        candidate = pattern.replace("\\", "/").lstrip("./")
        if normalized == candidate or normalized.startswith(candidate.rstrip("/") + "/"):
            return True
        if fnmatch.fnmatch(normalized, candidate):
            return True
    return False


def _changed_paths(repo: Path, commit: str) -> tuple[list[str], str | None]:
    changed: set[str] = set()
    diff = _git(repo, "diff", "--name-only", commit, "--")
    if diff.returncode != 0:
        return [], diff.stderr.strip() or "git diff failed"
    changed.update(line.strip() for line in diff.stdout.splitlines() if line.strip())
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode != 0:
        return [], status.stderr.strip() or "git status failed"
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        raw = line[3:]
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        changed.add(raw.strip().strip('"'))
    return sorted(changed), None


def validate_run_spec(
    spec: RunSpec,
    *,
    profile_store: ProfileStore | None = None,
    load_profile: bool = True,
) -> ReadinessResult:
    errors: list[Diagnostic] = []
    warnings: list[Diagnostic] = []
    resolved_source_content_sha256: str | None = None

    def error(code: str, field: str, message: str) -> None:
        errors.append(Diagnostic(code, field, message))

    def warning(code: str, field: str, message: str) -> None:
        warnings.append(Diagnostic(code, field, message))

    if spec.schema_version != "rrctl.run.v1":
        error("schema_version", "schema_version", "must equal rrctl.run.v1")
    for field, value in (
        ("run_id", spec.run_id),
        ("project", spec.project),
        ("session.name", spec.session.name),
    ):
        if not IDENTIFIER_RE.fullmatch(value):
            error("identifier_invalid", field, "must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")

    if spec.session.backend != "tmux":
        error("session_backend", "session.backend", "v1 only supports tmux")
    if spec.environment.kind != "conda":
        error("environment_kind", "environment.kind", "v1 only supports conda")
    if contains_secret_data(spec.to_dict()):
        error("secret_in_spec", "run_spec", "RunSpec appears to contain a credential")

    workload_cwd = PurePosixPath(spec.workload.cwd)
    if workload_cwd.is_absolute() or ".." in workload_cwd.parts:
        error("workload_cwd", "workload.cwd", "must stay within the staged repository")
    conda_sh = PurePosixPath(spec.environment.conda_sh)
    if not conda_sh.is_absolute() or spec.environment.conda_sh == "/" or ".." in conda_sh.parts:
        error("conda_sh", "environment.conda_sh", "must be a specific absolute path")

    command_text = " ".join(spec.workload.argv)
    if contains_secret(command_text):
        error("secret_in_argv", "workload.argv", "workload argv appears to contain a credential")
    for phase_name in ("first_step", "periodic", "completion"):
        phase = getattr(spec.health, phase_name)
        if contains_secret(" ".join(phase.adapter_argv)):
            error(
                "secret_in_argv",
                f"health.{phase_name}.adapter_argv",
                "adapter argv appears to contain a credential",
            )
        if phase.poll_interval_seconds <= 0:
            error("health_interval", f"health.{phase_name}.poll_interval_seconds", "must be > 0")
        if phase.adapter_timeout_seconds < 1:
            error("adapter_timeout", f"health.{phase_name}.adapter_timeout_seconds", "must be >= 1")
        if phase.low_gpu_limit_seconds is not None and phase.gpu_min_percent is None:
            error(
                "low_gpu_without_threshold",
                f"health.{phase_name}.low_gpu_limit_seconds",
                "requires gpu_min_percent",
            )
        if phase.gpu_utilization_policy == "required" and (
            phase.gpu_min_percent is None or phase.low_gpu_limit_seconds is None
        ):
            error(
                "required_gpu_window",
                f"health.{phase_name}.gpu_utilization_policy",
                "required policy needs gpu_min_percent and low_gpu_limit_seconds",
            )
        if phase.progress_path:
            try:
                confined_relative_path(
                    phase.progress_path, field=f"health.{phase_name}.progress_path"
                )
            except Exception as exc:
                error("progress_path", f"health.{phase_name}.progress_path", str(exc))
        for pattern in phase.fatal_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                error("fatal_pattern", f"health.{phase_name}.fatal_patterns", str(exc))

    remote_paths = {
        "remote.stage_root": spec.remote.stage_root,
        "remote.repo_root": spec.remote.repo_root,
        "remote.control_root": spec.remote.control_root,
        "remote.output_root": spec.remote.output_root,
    }
    normalized_paths: dict[str, PurePosixPath] = {}
    for field, value in remote_paths.items():
        path = PurePosixPath(value)
        if not path.is_absolute() or value == "/" or ".." in path.parts:
            error("remote_path", field, "must be a specific absolute path without '..'")
        else:
            normalized_paths[field] = path
    items = sorted(normalized_paths.items())
    for index, (left_field, left) in enumerate(items):
        for right_field, right in items[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                error(
                    "remote_path_overlap",
                    f"{left_field},{right_field}",
                    f"remote lifecycle roots must not overlap: {left} / {right}",
                )

    artifact_paths: set[str] = set()
    for index, artifact in enumerate(spec.artifacts):
        try:
            normalized = str(
                confined_relative_path(artifact.path, field=f"artifacts[{index}].path")
            )
            if normalized in artifact_paths:
                error("artifact_duplicate", f"artifacts[{index}].path", "duplicate artifact path")
            artifact_paths.add(normalized)
        except Exception as exc:
            error("artifact_path", f"artifacts[{index}].path", str(exc))

    if spec.output_cleanup is not None:
        cleanup = spec.output_cleanup
        if cleanup.mode != "pre_review_smoke":
            error("cleanup_mode", "output_cleanup.mode", "must equal pre_review_smoke")
        if spec.metadata.get("execution_purpose") != "pre_review_smoke":
            error(
                "cleanup_purpose",
                "metadata.execution_purpose",
                "output cleanup is allowed only for pre_review_smoke",
            )
        output = PurePosixPath(spec.remote.output_root)
        if spec.run_id not in output.as_posix():
            error(
                "cleanup_run_binding",
                "remote.output_root",
                "must contain run_id when output cleanup is enabled",
            )
        for field, values in (
            ("retain", cleanup.retain),
            ("delete_globs", cleanup.delete_globs),
        ):
            for index, value in enumerate(values):
                path = PurePosixPath(value)
                if path.is_absolute() or value in {"", "."} or ".." in path.parts:
                    error(
                        "cleanup_path",
                        f"output_cleanup.{field}[{index}]",
                        "must be a confined relative pattern",
                    )
        if "smoke_summary.json" not in cleanup.retain:
            error(
                "cleanup_summary_retention",
                "output_cleanup.retain",
                "must retain smoke_summary.json",
            )
        if not cleanup.delete_globs:
            error(
                "cleanup_patterns_missing",
                "output_cleanup.delete_globs",
                "must declare disposable smoke output patterns",
            )

    anchor_names: set[str] = set()
    remote_anchor_paths: set[str] = set()
    for index, anchor in enumerate(spec.anchors):
        prefix = f"anchors[{index}]"
        if not IDENTIFIER_RE.fullmatch(anchor.name):
            error("anchor_name", f"{prefix}.name", "must be a safe identifier")
        if anchor.name in anchor_names:
            error("anchor_duplicate", f"{prefix}.name", "duplicate anchor name")
        anchor_names.add(anchor.name)
        if not SHA256_RE.fullmatch(anchor.sha256):
            error("anchor_sha", f"{prefix}.sha256", "must be 64 lowercase hexadecimal characters")
        local_path = Path(anchor.local_path).expanduser()
        if not local_path.is_file():
            error("anchor_missing", f"{prefix}.local_path", f"file does not exist: {local_path}")
        elif SHA256_RE.fullmatch(anchor.sha256):
            actual = sha256_file(local_path)
            if actual != anchor.sha256:
                error(
                    "anchor_sha_mismatch",
                    f"{prefix}.sha256",
                    f"expected {anchor.sha256}, got {actual}",
                )
        remote_path = PurePosixPath(anchor.remote_path)
        if not remote_path.is_absolute() or anchor.remote_path == "/" or ".." in remote_path.parts:
            error("anchor_remote_path", f"{prefix}.remote_path", "must be a specific absolute path")
        elif any(
            remote_path == lifecycle_root or lifecycle_root in remote_path.parents
            for lifecycle_root in normalized_paths.values()
        ):
            error(
                "anchor_remote_overlap",
                f"{prefix}.remote_path",
                "must not be inside a lifecycle root",
            )
        if anchor.remote_path in remote_anchor_paths:
            error(
                "anchor_remote_duplicate", f"{prefix}.remote_path", "duplicate remote anchor path"
            )
        remote_anchor_paths.add(anchor.remote_path)

    commit_valid = bool(COMMIT_RE.fullmatch(spec.source.commit))
    if not commit_valid:
        error("commit_format", "source.commit", "must be a full 40-character lowercase Git commit")
    if spec.source.transport_bundle_sha256 and not SHA256_RE.fullmatch(
        spec.source.transport_bundle_sha256
    ):
        error(
            "bundle_sha",
            "source.bundle_sha256",
            "must be empty or 64 lowercase hexadecimal characters",
        )
    if spec.source.source_content_sha256 and not SHA256_RE.fullmatch(
        spec.source.source_content_sha256
    ):
        error(
            "source_content_sha",
            "source.source_content_sha256",
            "must be empty or 64 lowercase hexadecimal characters",
        )
    repo = Path(spec.source.repo_root).expanduser()
    if not repo.is_dir():
        error("repo_missing", "source.repo_root", f"directory does not exist: {repo}")
    elif _git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        error("repo_invalid", "source.repo_root", "not a Git work tree")
    elif commit_valid:
        resolved = _git(repo, "rev-parse", "--verify", f"{spec.source.commit}^{{commit}}")
        if resolved.returncode != 0 or resolved.stdout.strip() != spec.source.commit:
            error(
                "commit_missing", "source.commit", "commit is not resolvable in source repository"
            )
        else:
            try:
                resolved_source_content_sha256 = source_content_sha256(repo, spec.source)
                if (
                    spec.source.source_content_sha256
                    and spec.source.source_content_sha256 != resolved_source_content_sha256
                ):
                    error(
                        "source_content_sha_mismatch",
                        "source.source_content_sha256",
                        "reviewed source content does not match the frozen identity",
                    )
            except Exception as exc:
                error("source_content_identity", "source.commit", str(exc))
        branch_format = _git(repo, "check-ref-format", "--branch", spec.source.branch)
        branch = _git(repo, "rev-parse", "--verify", f"refs/heads/{spec.source.branch}")
        if branch_format.returncode != 0:
            error("branch_format", "source.branch", "branch name is invalid")
        elif branch.returncode != 0:
            error("branch_missing", "source.branch", "local branch is not resolvable")
        elif (
            _git(
                repo,
                "merge-base",
                "--is-ancestor",
                spec.source.commit,
                f"refs/heads/{spec.source.branch}",
            ).returncode
            != 0
        ):
            error("branch_commit", "source.commit", "commit is not reachable from branch")
        changed, git_error = _changed_paths(repo, spec.source.commit)
        if git_error:
            error("git_inspect", "source.repo_root", git_error)
        else:
            unexpected = [
                path
                for path in changed
                if not _path_allowed(path, spec.source.allowed_post_commit_paths)
            ]
            if unexpected:
                error(
                    "source_drift",
                    "source.allowed_post_commit_paths",
                    "runtime-relevant paths differ from reviewed commit: " + ", ".join(unexpected),
                )

    for executable in ("git", "tar"):
        if shutil.which(executable) is None:
            error(
                "tool_missing",
                executable,
                f"required local executable is unavailable: {executable}",
            )

    profile: Profile | None = None
    if load_profile:
        try:
            profile = (profile_store or ProfileStore()).load(spec.remote.profile)
            if profile.kind == "ssh" and shutil.which(profile.ssh_argv[0]) is None:
                error(
                    "tool_missing",
                    "profile.ssh_argv",
                    f"SSH executable is unavailable: {profile.ssh_argv[0]}",
                )
            if profile.password_env and shutil.which("sshpass") is None:
                error("tool_missing", "profile.password_env", "password profile requires sshpass")
        except Exception as exc:
            error("profile", "remote.profile", str(exc))
    else:
        warning("profile_skipped", "remote.profile", "credential profile was not loaded")

    return ReadinessResult(
        ready=not errors,
        errors=tuple(sorted(errors)),
        warnings=tuple(sorted(warnings)),
        run_spec_sha256=spec.digest,
        source_content_sha256=resolved_source_content_sha256,
        profile=profile,
    )
