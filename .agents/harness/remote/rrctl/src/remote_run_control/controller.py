"""Local rrctl orchestration and recovery index."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import load_artifact_manifest
from .doctor import connection_doctor
from .errors import RRCError
from .jsonutil import atomic_write_json, load_json, sha256_file, sha256_json, utc_now
from .models import RunSpec
from .monitor import OBSERVE_MAX_SECONDS
from .monitor import protocol as monitoring_protocol
from .profiles import DEFAULT_PROFILE_PATH, ProfileStore
from .readiness import ReadinessResult, validate_run_spec
from .security import confined_relative_path, redact, redact_data
from .source_identity import source_content_sha256
from .transport import CommandResult, Transport, download_timeout_seconds, transport_for
from .zipapp_builder import build_worker_zipapp

DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "rrctl" / "runs"


@contextmanager
def _pull_lock(destination: Path):
    # Reject symlink ancestors as well as the final lock/staging name.
    for ancestor in (destination.parent, *destination.parent.parents):
        if ancestor.is_symlink():
            raise RRCError("pull_symlink", "pull parent cannot be a symlink", "artifact")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep the inode: unlinking a lock file lets a later caller lock a different inode.
    lock_path = destination.parent / f".{destination.name}.pull.lock"
    if lock_path.is_symlink():
        raise RRCError("pull_symlink", "pull lock cannot be a symlink", "artifact")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            # Separate open() descriptions also exclude threads in this process.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RRCError(
                "pull_busy", "另一个拉取正在写入同一目标，请稍后重试", "artifact",
                details={"destination": str(destination)}, retryable=True,
            ) from exc
        yield
    finally:
        os.close(fd)


def _pull_path(root: Path, relative: str) -> Path:
    for ancestor in root.parents:
        if ancestor.is_symlink():
            raise RRCError("pull_symlink", "pull parent cannot be a symlink", "artifact")
    path = root
    for part in ("", *confined_relative_path(relative, field="pull.path").parts):
        path = path / part
        if path.is_symlink():
            raise RRCError("pull_symlink", f"pull path cannot be a symlink: {path}", "artifact")
    return path


def _verified_pull_file(root: Path, entry: dict[str, Any]) -> bool:
    path = _pull_path(root, entry["path"])
    return (path.is_file() and path.stat().st_size == entry["size"]
            and sha256_file(path) == entry["sha256"])


class _PullProgress:
    def __init__(self, run_id: str, manifest: dict[str, Any], timeout: float):
        self.run_id = run_id
        self.total_files = len(manifest["entries"])
        self.total_bytes = sum(item["size"] for item in manifest["entries"])
        self.timeout = timeout
        self.started = self.last = time.monotonic()
        self.files = self.bytes = self.resumed_files = self.resumed_bytes = 0
        self.last_files = 0

    def emit(self, event: str = "progress", current_bytes: int = 0) -> None:
        now = time.monotonic()
        if event == "progress" and now - self.last < 30 and self.files - self.last_files < 32:
            return
        elapsed = max(now - self.started, 0.001)
        message = {"start": "开始拉取并校验产物", "progress": "正在拉取产物",
                   "end": "产物拉取完成", "error": "拉取中断，已校验文件保留供重试"}[event]
        transferred = self.bytes + current_bytes
        speed = max(0, transferred - self.resumed_bytes) / elapsed
        def human_bytes(value: float) -> str:
            for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
                if value < 1024 or unit == "TiB":
                    return f"{value:.1f} {unit}"
                value /= 1024
            return ""  # unreachable
        message += (f"：文件 {self.files}/{self.total_files}，"
                    f"已传 {human_bytes(transferred)}/{human_bytes(self.total_bytes)}，"
                    f"速度 {human_bytes(speed)}/s")
        value = {
            "schema_version": "rrctl.progress.v1", "run_id": self.run_id,
            "stage": "pull", "event": event, "message": message,
            "files": self.files, "bytes": self.bytes + current_bytes,
            "total_files": self.total_files, "total_bytes": self.total_bytes,
            "resumed_files": self.resumed_files, "resumed_bytes": self.resumed_bytes,
            "speed_bytes_per_second": round(
                max(0, self.bytes + current_bytes - self.resumed_bytes) / elapsed, 1),
            "elapsed_seconds": round(elapsed, 1),
            "download_timeout_seconds": self.timeout, "budget_scope": "per_file",
        }
        print("[rrctl-progress] " + json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True)
        self.last, self.last_files = now, self.files

_RECOVERY_SCRIPT = r'''\
import hashlib, json, os, shutil, sys, uuid
from pathlib import Path

control_root = Path(sys.argv[1]).resolve()
output_root = Path(sys.argv[2]).resolve()
paths = json.loads(sys.argv[3])
run_id = sys.argv[4]
run_spec_sha256 = sys.argv[5]
expected_exit_code = int(sys.argv[6])
allow_nonzero_exit = sys.argv[7] == "1"
binding = json.loads((control_root / "binding.json").read_text())
status = json.loads((control_root / "status.json").read_text())
exited = json.loads((control_root / "workload-exit.json").read_text())
if (binding.get("run_id") != run_id or binding.get("run_spec_sha256") != run_spec_sha256
        or binding.get("control_root") != str(control_root)
        or status.get("run_id") != run_id
        or status.get("state") not in {"workload_complete", "completed", "failed"}
        or exited.get("run_id") != run_id
        or exited.get("run_spec_sha256") != run_spec_sha256
        or type(exited.get("exit_code")) is not int
        or exited["exit_code"] != expected_exit_code
        or status.get("detail", {}).get("exit_code") not in (None, expected_exit_code)
        or (expected_exit_code != 0 and not allow_nonzero_exit)):
    raise SystemExit("recovery_exit_identity_invalid")
if not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths):
    raise SystemExit("recovery_paths_invalid")

def safe_relative(value):
    p = Path(value)
    if p.is_absolute() or ".." in p.parts or not value or value.startswith("~"):
        raise SystemExit("recovery_path_not_confined:" + value)
    return p.as_posix()

def files_for(value):
    rel = safe_relative(value)
    source = output_root / rel
    if source.is_symlink():
        raise SystemExit("recovery_symlink:" + rel)
    resolved = source.resolve()
    if output_root not in resolved.parents and resolved != output_root:
        raise SystemExit("recovery_path_escape:" + rel)
    if source.is_file():
        return [(rel, source)]
    if source.is_dir():
        out = []
        for item in sorted(source.rglob("*")):
            if item.is_symlink():
                raise SystemExit("recovery_symlink:" + item.relative_to(output_root).as_posix())
            if item.is_file():
                item.resolve().relative_to(output_root)
                out.append((item.relative_to(output_root).as_posix(), item))
        return out
    raise SystemExit("recovery_path_missing:" + rel)

entries = []
# Rebuild after de-duplication without mutating the traversal list.
unique = []
seen = set()
for value in paths:
    for rel, source in files_for(value):
        if rel not in seen:
            seen.add(rel)
            unique.append((rel, source))
if not unique:
    raise SystemExit("recovery_no_files")
recovery_id = uuid.uuid4().hex
root = control_root / "recovery" / recovery_id
(root / "output").mkdir(parents=True, mode=0o700)
total = 0
for rel, source in unique:
    target = root / "output" / rel
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise SystemExit("recovery_destination_collision")
    size = source.stat().st_size
    total += size
    if total > 536870912:
        raise SystemExit("recovery_size_limit")
    shutil.copyfile(source, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    entries.append({"path": "output/" + rel, "size": size, "sha256": digest})
manifest = {
    "schema_version": "rrctl.artifacts.v1",
    "run_id": run_id,
    "run_spec_sha256": run_spec_sha256,
    "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    "output_root": str(root),
    "entries": entries,
    "source_root": str(output_root),
    "exit_code": expected_exit_code,
    "terminal_state": status["state"],
    "completion_contract_passed": False,
    "recovery_id": recovery_id,
}
(root / "artifact_manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + chr(10), encoding="utf-8")
print(json.dumps({"recovery_id": recovery_id, "root": str(root), "entries": len(entries)}, sort_keys=True))
'''


@dataclass(frozen=True, slots=True)
class RunRef:
    run_id: str
    profile: str
    stage_root: str
    control_root: str
    worker_path: str
    remote_python: str
    run_spec_sha256: str
    source_content_sha256: str = ""
    transport_bundle_sha256: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunRef:
        required = {
            key: value[key]
            for key in (
                "run_id",
                "profile",
                "stage_root",
                "control_root",
                "worker_path",
                "remote_python",
                "run_spec_sha256",
            )
        }
        return cls(
            **required,
            source_content_sha256=str(value.get("source_content_sha256", "")),
            transport_bundle_sha256=str(value.get("transport_bundle_sha256", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "profile": self.profile,
            "stage_root": self.stage_root,
            "control_root": self.control_root,
            "worker_path": self.worker_path,
            "remote_python": self.remote_python,
            "run_spec_sha256": self.run_spec_sha256,
            "source_content_sha256": self.source_content_sha256,
            "transport_bundle_sha256": self.transport_bundle_sha256,
        }


class RunIndex:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(os.environ.get("RRCTL_STATE_ROOT", DEFAULT_STATE_ROOT))

    def save(self, ref: RunRef, spec: RunSpec, status: dict[str, Any] | None = None) -> None:
        destination = self.root / ref.run_id
        destination.mkdir(parents=True, exist_ok=True)
        atomic_write_json(destination / "run_ref.json", ref.to_dict())
        atomic_write_json(destination / "run_spec.json", spec.to_dict())
        if status is not None:
            atomic_write_json(destination / "last_status.json", status)

    def load_ref(self, run_id: str) -> RunRef:
        path = self.root / run_id / "run_ref.json"
        if not path.is_file():
            raise RRCError(
                "run_ref_missing", f"local run index does not contain {run_id}", "recovery"
            )
        value = load_json(path)
        if not isinstance(value, dict):
            raise RRCError("run_ref_invalid", f"invalid local run reference: {path}", "recovery")
        return RunRef.from_dict(value)

    def load_spec(self, run_id: str) -> RunSpec:
        return RunSpec.from_path(self.root / run_id / "run_spec.json")


def _parse_worker_result(
    result: CommandResult, *, phase: str, secret_values: tuple[str, ...] = ()
) -> dict[str, Any]:
    if result.stdout_truncated:
        raise RRCError(
            "control_output_truncated",
            "worker response exceeded the control output budget",
            phase,
            details={"control_output": result.diagnostics(secret_values)},
            retryable=False,
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RRCError(
            "worker_output",
            f"worker emitted invalid JSON during {phase}",
            phase,
            details={"control_output": result.diagnostics(secret_values)},
        ) from exc
    value = redact_data(value, secret_values)
    if result.returncode != 0 or (isinstance(value, dict) and value.get("ok") is False):
        raise RRCError(
            "worker_command",
            f"worker command failed during {phase}",
            phase,
            details={"returncode": result.returncode, "worker": value},
        )
    if not isinstance(value, dict):
        raise RRCError("worker_output", "worker output must be an object", phase)
    return value


class Controller:
    def __init__(
        self,
        *,
        profiles_path: Path | None = None,
        state_root: Path | None = None,
    ):
        self.profile_store = ProfileStore(profiles_path or DEFAULT_PROFILE_PATH)
        self.index = RunIndex(state_root)
        self.control_output: dict[str, Any] | None = None

    def doctor(
        self, profile_name: str, *, env_file: Path | None = None, offline: bool = False
    ) -> dict[str, Any]:
        return connection_doctor(
            self.profile_store, profile_name, env_file=env_file, offline=offline
        )

    def _parse_result(
        self, result: CommandResult, *, phase: str, secret_values: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        if result.truncated:
            self.control_output = {"operation": phase, **result.diagnostics(secret_values)}
        return _parse_worker_result(result, phase=phase, secret_values=secret_values)

    def recovery_actions(self, run_id: str, ref: RunRef | None = None) -> list[dict[str, Any]]:
        prefix = [
            "rrctl",
            "--profiles",
            str(self.profile_store.path),
            "--state-root",
            str(self.index.root),
        ]
        actions = [
            {"operation": name, "argv": [*prefix, name, run_id]} for name in ("inspect", "wait")
        ]
        if ref is not None:
            actions.append(
                {
                    "operation": "resume",
                    "argv": [
                        *prefix,
                        "resume",
                        "--profile",
                        ref.profile,
                        "--control-path",
                        ref.control_root,
                    ],
                }
            )
        return actions

    def _unknown(self, exc: RRCError, ref: RunRef, *, operation: str) -> None:
        exc.outcome = "unknown"
        exc.retryable = False
        exc.next_actions = self.recovery_actions(ref.run_id, ref)
        exc.details.update(
            {
                "run_id": ref.run_id,
                "operation": operation,
                "remote_state_unknown": True,
                "automatic_retry_allowed": False,
            }
        )
        if operation in {"launch", "stage"}:
            exc.details["remote_workload_preserved"] = True
        exc.message = (
            f"remote {operation} outcome is unknown; do not repeat it; inspect the same RunID"
        )
        path = self.index.root / ref.run_id / "operation-latest.json"
        with suppress(OSError):
            atomic_write_json(
                path,
                {
                    "run_id": ref.run_id,
                    "operation": operation,
                    "at": utc_now(),
                    "status": "unknown",
                    "error": exc.to_dict(),
                },
            )

    def _mutating_request(
        self, ref: RunRef, transport: Transport, argv: list[str], *, operation: str
    ) -> dict[str, Any]:
        try:
            result = transport.run(argv)
            value = self._parse_result(
                result, phase=operation, secret_values=transport.secret_values
            )
            if getattr(transport, "last_control_output", None):
                self.control_output = transport.last_control_output
            return value
        except (KeyboardInterrupt, RRCError) as error:
            exc = (
                error
                if isinstance(error, RRCError)
                else RRCError("transport_interrupted", "control request interrupted", "transport")
            )
            if exc.code in {
                "transport_timeout",
                "transport_connection",
                "transport_interrupted",
                "worker_output",
                "control_output_truncated",
            }:
                self._unknown(exc, ref, operation=operation)
            if exc is error:
                raise
            raise exc from error

    def ready(self, spec: RunSpec, *, offline: bool = False) -> dict[str, Any]:
        result = validate_run_spec(spec, profile_store=self.profile_store, load_profile=True)
        output = result.to_dict()
        if result.ready and not offline and result.profile:
            transport = transport_for(result.profile)
            output["remote_preflight"] = transport.preflight(
                python=spec.remote.python,
                environment=spec.environment,
                stage_root=spec.remote.stage_root,
            )
            if transport.last_control_output:
                output["control_output"] = transport.last_control_output
        elif offline:
            output["remote_preflight"] = "skipped"
        return output

    def _require_ready(self, spec: RunSpec) -> tuple[ReadinessResult, Transport]:
        result = validate_run_spec(spec, profile_store=self.profile_store, load_profile=True)
        if not result.ready or result.profile is None:
            raise RRCError(
                "readiness_failed",
                "RunSpec did not pass readiness",
                "readiness",
                details=result.to_dict(),
            )
        transport = transport_for(result.profile)
        transport.preflight(
            python=spec.remote.python,
            environment=spec.environment,
            stage_root=spec.remote.stage_root,
        )
        return result, transport

    def _probe_remote_reference(
        self, spec: RunSpec, transport: Transport
    ) -> tuple[str | None, str | None]:
        """Probe remote machine for an existing repository with common history for delta staging."""
        if getattr(spec.source, "bundle_sha256", None):
            return None, None
        local_repo_name = Path(spec.source.repo_root).name
        clean_name = local_repo_name.replace("-worktrees", "").split("-")[0]
        probe_code = (
            "import sys, subprocess\n"
            "from pathlib import Path\n"
            f"spec_repo = Path({spec.remote.repo_root!r})\n"
            f"local_name = {local_repo_name!r}\n"
            f"clean_name = {clean_name!r}\n"
            "candidates = []\n"
            "home = Path.home()\n"
            "for base_dir in (spec_repo.parent.parent, home / 'cdfss-dinov3-workspace', home, home / 'workspace', spec_repo.parent.parent.parent):\n"
            "    if base_dir.is_dir():\n"
            "        for n in (local_name, clean_name, 'cdfss-dinov3', 'fss-diff-calp'):\n"
            "            target = base_dir / n\n"
            "            if (target / '.git').is_dir() and str(target) not in candidates:\n"
            "                candidates.append(str(target))\n"
            "for runs_dir in (spec_repo.parent, spec_repo.parent.parent):\n"
            "    if runs_dir.is_dir():\n"
            "        for d in sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):\n"
            "            repo = d / 'repo'\n"
            "            if (repo / '.git').is_dir() and str(repo) not in candidates:\n"
            "                candidates.append(str(repo))\n"
            "                if len(candidates) >= 15:\n"
            "                    break\n"
            "    if candidates:\n"
            "        break\n"
            "for c in candidates:\n"
            "    try:\n"
            "        out = subprocess.check_output([\n"
            "            'git', '-C', c, 'for-each-ref',\n"
            "            '--sort=-committerdate', '--format=%(objectname)',\n"
            "            'refs/remotes/origin/', 'refs/heads/'\n"
            "        ], text=True).strip()\n"
            "        commits = [item.strip() for item in out.splitlines() if item.strip()][:30]\n"
            "        head = subprocess.check_output(['git', '-C', c, 'rev-parse', 'HEAD'], text=True).strip()\n"
            "        if head and head not in commits:\n"
            "            commits.insert(0, head)\n"
            "        if commits:\n"
            "            print(f'{c}\\t{\" \".join(commits)}')\n"
            "            sys.exit(0)\n"
            "    except Exception:\n"
            "        pass\n"
            "sys.exit(1)\n"
        )
        try:
            result = transport.run(
                [spec.remote.python, "-c", probe_code],
                timeout_seconds=15,
            )
            if result.returncode == 0 and result.stdout:
                line = result.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1]
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    remote_repo, commits_str = parts[0].strip(), parts[1].strip()
                    remote_commits = commits_str.split()
                    repo = Path(spec.source.repo_root)
                    best_base = None
                    min_distance = 999999
                    for c in remote_commits:
                        try:
                            mb_res = subprocess.run(
                                ["git", "-C", str(repo), "merge-base", c, spec.source.commit],
                                capture_output=True,
                                text=True,
                            )
                            if mb_res.returncode == 0:
                                mb = mb_res.stdout.strip()
                                if mb:
                                    dist_res = subprocess.run(
                                        ["git", "-C", str(repo), "rev-list", "--count", f"{mb}..{spec.source.commit}"],
                                        capture_output=True,
                                        text=True,
                                    )
                                    dist = int(dist_res.stdout.strip()) if dist_res.returncode == 0 else 999999
                                    if dist < min_distance:
                                        min_distance = dist
                                        best_base = mb
                        except Exception:
                            pass
                    if best_base:
                        return remote_repo, best_base
        except Exception:
            pass
        return None, None

    def _create_bundle(
        self, spec: RunSpec, destination: Path, *, base_commit: str | None = None
    ) -> str:
        repo = Path(spec.source.repo_root)
        temporary_ref = f"refs/heads/rrctl-{sha256_json(spec.run_id)[:24]}"
        bundle_args = [temporary_ref]
        if base_commit:
            bundle_args.append(f"^{base_commit}")
        created = False
        try:
            subprocess.run(
                ["git", "-C", str(repo), "update-ref", temporary_ref, spec.source.commit],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            if base_commit and base_commit != spec.source.commit:
                args = [temporary_ref, f"^{base_commit}"]
                try:
                    subprocess.run(
                        [
                            "git", "-c", "pack.threads=0", "-C", str(repo),
                            "bundle", "create", str(destination), *args,
                        ],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    created = True
                except subprocess.CalledProcessError:
                    created = False
            if not created:
                # 严禁直接打包整库历史（含 340MB 历史死重 npz）；退避采用深度为 1 的轻量浅层打包（~13MB）
                try:
                    with tempfile.TemporaryDirectory(prefix="rrctl-shallow-") as tmpdir:
                        shallow_dir = Path(tmpdir) / "shallow"
                        subprocess.run(
                            ["git", "clone", "--depth", "1", f"file://{repo.resolve()}", str(shallow_dir)],
                            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        subprocess.run(
                            ["git", "-C", str(shallow_dir), "bundle", "create", str(destination), "HEAD"],
                            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        created = True
                except Exception:
                    created = False
            if not created:
                subprocess.run(
                    [
                        "git", "-c", "pack.threads=0", "-C", str(repo),
                        "bundle", "create", str(destination), temporary_ref,
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
            subprocess.run(
                ["git", "-C", str(repo), "bundle", "verify", str(destination)], check=True, capture_output=True
            )
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "update-ref", "-d", temporary_ref],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        heads = subprocess.run(
            ["git", "-C", str(repo), "bundle", "list-heads", str(destination)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if not any(line.split()[0] == spec.source.commit for line in heads if line.split()):
            raise RRCError(
                "bundle_commit", "source bundle does not contain reviewed commit", "staging"
            )
        actual_sha256 = sha256_file(destination)
        expected_transport_sha = spec.source.transport_bundle_sha256
        if expected_transport_sha and actual_sha256 != expected_transport_sha:
            raise RRCError(
                "bundle_sha_mismatch",
                "generated source bundle does not match the frozen RunSpec SHA",
                "staging",
                details={
                    "expected": expected_transport_sha,
                    "actual": actual_sha256,
                },
            )
        return actual_sha256

    def _stage(self, spec: RunSpec, readiness: ReadinessResult, transport: Transport) -> RunRef:
        ref_repo, base_commit = self._probe_remote_reference(spec, transport)
        with tempfile.TemporaryDirectory(prefix="rrctl-stage-") as temporary_name:
            temporary = Path(temporary_name)
            bundle = temporary / "source.bundle"
            worker = temporary / "rrctl-worker.pyz"
            spec_path = temporary / "run_spec.json"
            manifest_path = temporary / "stage_manifest.json"
            bundle_sha = self._create_bundle(spec, bundle, base_commit=base_commit)
            source_sha = source_content_sha256(Path(spec.source.repo_root), spec.source)
            if readiness.source_content_sha256 != source_sha:
                raise RRCError(
                    "source_content_changed",
                    "source content changed after readiness validation",
                    "staging",
                    details={
                        "readiness": readiness.source_content_sha256,
                        "staging": source_sha,
                    },
                )
            package_source = Path(__file__).resolve().parent.parent
            worker_sha = build_worker_zipapp(package_source, worker)
            atomic_write_json(spec_path, spec.to_dict())
            anchor_files: list[tuple[Path, str, str]] = []
            for anchor in spec.anchors:
                target = temporary / "anchors" / anchor.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(anchor.local_path).expanduser(), target)
                anchor_files.append(
                    (target, f"{spec.remote.stage_root}/anchors/{anchor.name}", anchor.sha256)
                )
            manifest = {
                "schema_version": "rrctl.stage.v1",
                "run_id": spec.run_id,
                "run_spec_sha256": spec.digest,
                "run_spec_file_sha256": sha256_file(spec_path),
                "source_content_sha256": source_sha,
                "transport_bundle_sha256": bundle_sha,
                "bundle_sha256": bundle_sha,
                "worker_sha256": worker_sha,
                "anchors": {item.name: item.sha256 for item in spec.anchors},
            }
            if ref_repo:
                manifest["reference_repo"] = ref_repo
            atomic_write_json(manifest_path, manifest)
            transport.mkdir_exclusive(spec.remote.stage_root)
            uploads = [
                (bundle, f"{spec.remote.stage_root}/source.bundle", bundle_sha),
                (worker, f"{spec.remote.stage_root}/rrctl-worker.pyz", worker_sha),
                (spec_path, f"{spec.remote.stage_root}/run_spec.json", sha256_file(spec_path)),
                (
                    manifest_path,
                    f"{spec.remote.stage_root}/stage_manifest.json",
                    sha256_file(manifest_path),
                ),
                *anchor_files,
            ]
            if anchor_files:
                mkdir = transport.run(["mkdir", "-m", "700", f"{spec.remote.stage_root}/anchors"])
                if mkdir.returncode != 0:
                    raise RRCError(
                        "anchor_stage", "failed to create anchor staging directory", "staging"
                    )
            for local_path, remote_path, expected_sha in uploads:
                transport.upload(local_path, remote_path, expected_sha)
        return RunRef(
            run_id=spec.run_id,
            profile=spec.remote.profile,
            stage_root=spec.remote.stage_root,
            control_root=spec.remote.control_root,
            worker_path=f"{spec.remote.stage_root}/rrctl-worker.pyz",
            remote_python=spec.remote.python,
            run_spec_sha256=readiness.run_spec_sha256,
            source_content_sha256=source_sha,
            transport_bundle_sha256=bundle_sha,
        )

    def launch(self, spec: RunSpec, *, max_wait_seconds: float = 0) -> dict[str, Any]:
        if not math.isfinite(max_wait_seconds) or max_wait_seconds < 0:
            raise RRCError(
                "launch_budget", "max-wait must be nonnegative finite seconds", "observer"
            )
        if (self.index.root / spec.run_id / "run_ref.json").exists():
            ref = self.index.load_ref(spec.run_id)
            raise RRCError(
                "run_already_indexed",
                "RunID is already registered; inspect or resume it instead of launching again",
                "recovery",
                details={"run_id": spec.run_id},
                retryable=False,
                next_actions=self.recovery_actions(spec.run_id, ref),
            )
        try:
            readiness, transport = self._require_ready(spec)
        except RRCError as exc:
            exc.outcome = "failed"
            exc.retryable = False
            exc.details.update({"run_id": spec.run_id, "launch_dispatched": False})
            exc.next_actions = [
                {
                    "operation": "doctor",
                    "argv": [
                        "rrctl",
                        "--profiles",
                        str(self.profile_store.path),
                        "doctor",
                        "--profile",
                        spec.remote.profile,
                    ],
                }
            ]
            raise
        try:
            ref = self._stage(spec, readiness, transport)
        except RRCError as exc:
            if exc.code in {
                "transport_timeout",
                "transport_connection",
                "control_output_truncated",
                "upload_failed",
            }:
                # 尝试安全清理未完成的远端半成品 stage 目录，避免残留引发后续 inspect / collision 死锁
                with suppress(Exception):
                    transport.run(["rm", "-rf", spec.remote.stage_root])
                exc.outcome = "failed"
                exc.retryable = True
                exc.details["launch_dispatched"] = False
                exc.details["stage_transfer_failed"] = True
                exc.message = f"remote stage transfer failed ({exc.code}); stage artifacts were not fully deployed and directory was cleaned"
            raise
        # 启动失败也保留恢复定位，允许通过同一控制面拉取诊断。
        self.index.save(ref, spec)
        launched = self._mutating_request(
            ref,
            transport,
            [
                ref.remote_python,
                ref.worker_path,
                "launch",
                "--stage",
                ref.stage_root,
            ],
            operation="launch",
        )
        self.index.save(ref, spec, launched)
        try:
            if monitoring_protocol({"monitoring": launched.get("monitoring")}):
                budget = (
                    min(spec.health.first_step.timeout_seconds, max_wait_seconds)
                    if max_wait_seconds
                    else spec.health.first_step.timeout_seconds
                )
                observed = self._observe_remote(
                    spec.run_id,
                    deadline=time.monotonic() + budget,
                    interval=spec.health.first_step.poll_interval_seconds,
                    until="first_step",
                )
                state = observed.get("status", {}).get("state")
                if state in {"failed", "aborted"}:
                    raise RRCError(
                        "first_step_terminal", f"run entered {state}", "health", details=observed
                    )
                if observed["observation"] == "timeout":
                    raise RRCError(
                        "first_step_observer_timeout",
                        "first-step observation expired; workload preserved",
                        "observer",
                        details={"last": observed, "remote_workload_preserved": True},
                    )
                if observed["observation"] == "attention":
                    raise RRCError(
                        "first_step_attention",
                        "first-step monitoring needs attention",
                        "observer",
                        details=observed,
                    )
                return {
                    "launch": launched,
                    "first_step": observed.get("first_step", "completed_fast"),
                    "inspect": observed,
                }
            budget = (
                min(spec.health.first_step.timeout_seconds, max_wait_seconds)
                if max_wait_seconds
                else spec.health.first_step.timeout_seconds
            )
            deadline = time.monotonic() + budget
            last: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                inspected = self.inspect(spec.run_id)
                state = inspected["status"]["state"]
                if state == "workload_complete":
                    terminal = self._finalize_completed_workload(spec.run_id)
                    return {
                        "launch": launched,
                        "first_step": "completed_fast",
                        "inspect": terminal,
                    }
                if state == "completed":
                    return {
                        "launch": launched,
                        "first_step": "completed_fast",
                        "inspect": inspected,
                    }
                if state in {"failed", "aborted"}:
                    raise RRCError(
                        "first_step_terminal",
                        f"run entered terminal state before first-step pass: {state}",
                        "health",
                        details=inspected,
                    )
                last = self.health(spec.run_id, phase="first_step")
                if self._health_status(last) != "unhealthy" and last.get("gate_passed") is True:
                    return {"launch": launched, "first_step": last}
                time.sleep(spec.health.first_step.poll_interval_seconds)
            raise RRCError(
                "first_step_observer_timeout",
                "first-step observation ended without an authoritative pass; "
                "remote workload was preserved",
                "observer",
                details={
                    "last": last,
                    "run_ref": ref.to_dict(),
                    "remote_workload_preserved": True,
                },
            )
        except BaseException as exc:
            self._raise_observer_error(spec.run_id, exc)

    def _runtime(self, run_id: str) -> tuple[RunRef, RunSpec, Transport]:
        ref = self.index.load_ref(run_id)
        spec = self.index.load_spec(run_id)
        profile = self.profile_store.load(ref.profile)
        return ref, spec, transport_for(profile)

    def inspect(self, run_id: str, *, timeout_seconds: float = 180) -> dict[str, Any]:
        ref, spec, transport = self._runtime(run_id)
        result = transport.run(
            [ref.remote_python, ref.worker_path, "inspect", "--control", ref.control_root],
            timeout_seconds=timeout_seconds,
        )
        value = self._parse_result(result, phase="inspect", secret_values=transport.secret_values)
        self.index.save(ref, spec, value.get("status"))
        return value

    def health(self, run_id: str, *, phase: str) -> dict[str, Any]:
        ref, spec, transport = self._runtime(run_id)
        if spec.session.backend != "process":
            raise RRCError(
                "legacy_backend_read_only", "use inspect/pull for frozen legacy runs", "observer"
            )
        result = transport.run(
            [
                ref.remote_python,
                ref.worker_path,
                "health",
                "--control",
                ref.control_root,
                "--phase",
                phase,
            ]
        )
        return self._parse_result(result, phase="health", secret_values=transport.secret_values)

    @staticmethod
    def _health_status(value: dict[str, Any]) -> str:
        status = value.get("status")
        if status in {"healthy", "degraded", "unhealthy"}:
            return status
        return "healthy" if value.get("healthy") else "unhealthy"

    @staticmethod
    def _raise_observer_error(run_id: str, exc: BaseException) -> None:
        if isinstance(exc, RRCError):
            if exc.code == "transport_timeout":
                exc.details.update({"run_id": run_id, "remote_workload_preserved": True})
            raise exc
        raise RRCError(
            "observer_detached",
            "local observer detached; remote workload ownership was not changed",
            "observer",
            details={"run_id": run_id, "remote_workload_preserved": True},
        ) from exc

    def _worker_lifecycle_command(
        self, run_id: str, argv: list[str], *, phase: str
    ) -> dict[str, Any]:
        ref, _, transport = self._runtime(run_id)
        return self._mutating_request(
            ref,
            transport,
            [ref.remote_python, ref.worker_path, *argv, "--control", ref.control_root],
            operation=phase,
        )

    def _fail_completed_workload(self, run_id: str, reason: str) -> dict[str, Any]:
        ref, _, transport = self._runtime(run_id)
        inspected: dict[str, Any] | None = None
        with suppress(RRCError):
            inspected = self.inspect(run_id)
        state = inspected.get("status", {}).get("state") if isinstance(inspected, dict) else None
        if state in {"failed", "aborted", "completed"}:
            return inspected or {"state": state}
        if state != "workload_complete":
            raise RRCError(
                "completion_state_unknown",
                "completion failure cannot mutate an active or unobserved run",
                "observer",
                details={"remote_workload_preserved": True, "run_id": run_id},
            )
        command = [
            ref.remote_python,
            ref.worker_path,
            "fail-completed",
            "--control",
            ref.control_root,
            "--reason",
            reason,
        ]
        return self._mutating_request(ref, transport, command, operation="fail-completed")

    def _finalize_completed_workload(self, run_id: str) -> dict[str, Any]:
        _, spec, _ = self._runtime(run_id)
        deadline = time.monotonic() + spec.health.completion.timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.health(run_id, phase="completion")
            status = self._health_status(last)
            if status != "unhealthy" and last.get("complete"):
                self._worker_lifecycle_command(run_id, ["complete"], phase="complete")
                return self.inspect(run_id)
            if status == "unhealthy":
                self._fail_completed_workload(run_id, "authoritative_completion_unhealthy")
                raise RRCError(
                    "completion_health",
                    "remote authoritative completion health failed",
                    "health",
                    details=last,
                )
            time.sleep(spec.health.completion.poll_interval_seconds)
        raise RRCError(
            "completion_observer_timeout",
            "completion observation ended without an authoritative result; "
            "remote state was preserved",
            "observer",
            details={"last": last, "remote_state_preserved": True},
        )

    @staticmethod
    def _timeout(run_id: str, last: dict[str, Any] | None) -> dict[str, Any]:
        return {
            **(last or {"run_id": run_id}),
            "observation": "timeout",
            "remote_workload_preserved": True,
            "resume_argv": ["rrctl", "wait", run_id],
        }

    def _observe_remote(
        self,
        run_id: str,
        *,
        deadline: float | None,
        interval: float,
        until: str = "event",
        after_event: str | None = None,
    ) -> dict[str, Any]:
        ref, spec, transport = self._runtime(run_id)
        last = None
        failures = 0
        requests = 0
        while True:
            remaining = deadline - time.monotonic() if deadline is not None else 180.0
            if remaining <= 0:
                value = self._timeout(run_id, last)
                value["observer_requests"] = requests
                return value
            transport_budget = min(180.0, remaining)
            margin = min(10.0, transport_budget / 4)
            remote_budget = min(OBSERVE_MAX_SECONDS, interval, transport_budget - margin)
            argv = [
                ref.remote_python,
                ref.worker_path,
                "observe",
                "--control",
                ref.control_root,
                "--timeout-seconds",
                str(remote_budget),
                "--until",
                until,
            ]
            if after_event is not None:
                argv += ["--after-event", after_event]
            try:
                requests += 1
                result = transport.run(argv, timeout_seconds=transport_budget)
                if result.returncode == 255:
                    raise RRCError(
                        "transport_connection", "observation connection failed", "observer"
                    )
                last = self._parse_result(
                    result, phase="observe", secret_values=transport.secret_values
                )
            except RRCError as exc:
                if exc.code not in {"transport_timeout", "transport_connection"}:
                    raise
                failures += 1
                if deadline is not None and time.monotonic() >= deadline:
                    value = self._timeout(run_id, last)
                    value["remote_state_unknown"] = True
                    return value
                if failures >= 10:
                    raise RRCError(
                        "observation_unavailable",
                        "bounded observation retries exhausted",
                        "observer",
                        details={"run_id": run_id, "remote_workload_preserved": True},
                    ) from exc
                delay = min(2 ** (failures - 1), 10)
                if deadline is not None:
                    delay = min(delay, max(0, deadline - time.monotonic()))
                time.sleep(delay)
                continue
            failures = 0
            if monitoring_protocol(last.get("binding", {})) is None:
                raise RRCError(
                    "monitor_protocol", "observe response lost its capability", "observer"
                )
            if (
                last.get("run_id") != run_id
                or last["binding"].get("run_spec_sha256") != spec.digest
                or not isinstance(last.get("status"), dict)
            ):
                raise RRCError(
                    "observe_identity", "observation belongs to a different run", "observer"
                )
            if last.get("observation") not in {
                "no_event",
                "terminal",
                "attention",
                "unavailable",
                "first_step",
            }:
                raise RRCError("observe_response", "invalid observation response", "observer")
            if last.get("status", {}).get("state") in {"failed", "aborted"}:
                last["observation"] = "terminal"
            if (
                last["observation"] == "terminal"
                and last["status"].get("state") not in {"completed", "failed", "aborted"}
            ) or (last["observation"] == "first_step" and until != "first_step"):
                raise RRCError(
                    "observe_response", "observation is not an authoritative result", "observer"
                )
            if last["observation"] == "unavailable":
                raise RRCError(
                    "monitor_unavailable",
                    "monitor snapshot is stale, lost or unavailable",
                    "observer",
                    details={"snapshot": last, "remote_workload_preserved": True},
                )
            if last["observation"] != "no_event":
                self.index.save(ref, spec, last.get("status"))
                last["observer_requests"] = requests
                return last
            # 无事件响应只在当前客户端内续等，不向 CLI 或模型输出。

    def wait(
        self,
        run_id: str,
        *,
        poll_seconds: float | None = None,
        max_wait_seconds: float = 0,
        after_event: str | None = None,
    ) -> dict[str, Any]:
        interval = 600 if poll_seconds is None else poll_seconds
        if (
            not math.isfinite(interval)
            or interval <= 0
            or not math.isfinite(max_wait_seconds)
            or max_wait_seconds < 0
        ):
            raise RRCError(
                "wait_budget",
                "poll must be positive and max-wait nonnegative finite seconds",
                "observer",
            )
        deadline = time.monotonic() + max_wait_seconds if max_wait_seconds else None
        try:
            try:
                initial = self.inspect(
                    run_id, timeout_seconds=min(180, max_wait_seconds) if max_wait_seconds else 180
                )
            except RRCError as exc:
                if (
                    exc.code == "transport_timeout"
                    and deadline is not None
                    and time.monotonic() >= deadline
                ):
                    return self._timeout(run_id, None)
                raise
            if monitoring_protocol(initial.get("binding", {})):
                return self._observe_remote(
                    run_id,
                    deadline=deadline,
                    interval=interval,
                    after_event=after_event,
                )
            if after_event is not None:
                raise RRCError(
                    "event_cursor_unsupported",
                    "legacy worker has no monitor event cursor",
                    "observer",
                )
            remaining = max(0, deadline - time.monotonic()) if deadline is not None else 0
            if deadline is not None and remaining <= 0:
                return self._timeout(run_id, initial)
            value = self._wait_legacy(run_id, poll_seconds=interval, max_wait_seconds=remaining)
            return {**value, "monitoring_mode": "client_compatibility"}
        except BaseException as exc:
            self._raise_observer_error(run_id, exc)

    def _wait_legacy(
        self,
        run_id: str,
        *,
        poll_seconds: float | None = None,
        max_wait_seconds: float = 0,
    ) -> dict[str, Any]:
        _, spec, _ = self._runtime(run_id)
        interval = 600 if poll_seconds is None else poll_seconds
        if (
            not math.isfinite(interval)
            or interval <= 0
            or not math.isfinite(max_wait_seconds)
            or max_wait_seconds < 0
        ):
            raise RRCError(
                "wait_budget",
                "poll must be positive and max-wait must be nonnegative finite seconds",
                "observer",
            )
        deadline = time.monotonic() + max_wait_seconds if max_wait_seconds else None
        try:
            while True:
                value = self.inspect(run_id)
                state = value["status"]["state"]
                if spec.session.backend != "process":
                    if state in {"completed", "failed", "aborted"}:
                        return value
                    raise RRCError(
                        "legacy_backend_read_only",
                        "legacy run preserved; inspect its frozen control state",
                        "observer",
                        details={"run_id": run_id, "remote_workload_preserved": True},
                    )
                if state == "workload_complete":
                    return self._finalize_completed_workload(run_id)
                if state == "completed":
                    completion = self.health(run_id, phase="completion")
                    if self._health_status(completion) == "unhealthy" or not completion.get(
                        "complete"
                    ):
                        raise RRCError(
                            "completion_recheck",
                            "terminal completion health recheck failed",
                            "health",
                            details=completion,
                        )
                    return value
                if state in {"failed", "aborted"}:
                    return value
                if deadline is not None and time.monotonic() >= deadline:
                    return {
                        **value,
                        "observation": "timeout",
                        "remote_workload_preserved": True,
                        "resume_argv": ["rrctl", "wait", run_id],
                    }
                phase = "first_step" if state == "launched" else "periodic"
                periodic = self.health(run_id, phase=phase)
                if self._health_status(periodic) == "unhealthy":
                    rechecked = self.inspect(run_id)
                    rechecked_state = rechecked["status"]["state"]
                    if rechecked_state == "workload_complete":
                        return self._finalize_completed_workload(run_id)
                    if rechecked_state in {"failed", "aborted"}:
                        return rechecked
                    raise RRCError(
                        "periodic_health",
                        "periodic health check needs attention; remote workload was preserved",
                        "observer",
                        details={
                            "health": periodic,
                            "recheck": rechecked,
                            "remote_workload_preserved": True,
                            "explicit_abort_required": True,
                        },
                    )
                delay = (
                    min(interval, max(0, deadline - time.monotonic()))
                    if deadline is not None
                    else interval
                )
                time.sleep(delay)
        except BaseException as exc:
            self._raise_observer_error(run_id, exc)

    def recover_evidence(
        self, run_id: str, *, paths: list[str], destination: Path,
        allow_nonzero_exit: bool = False,
    ) -> dict[str, Any]:
        """Create and pull an identity-bound recovery snapshot for a finished workload.

        This deliberately does not mutate the original completion manifest, RunSpec, binding,
        summary, or remote lifecycle state. Nonzero exits require explicit opt-in;
        recovered evidence never claims a passed completion contract.
        """
        ref, spec, transport = self._runtime(run_id)
        if not paths:
            raise RRCError("recovery_paths", "at least one recovery path is required", "artifact")
        normalized = []
        for value in paths:
            try:
                normalized.append(confined_relative_path(value, field="recovery.path").as_posix())
            except Exception as exc:
                raise RRCError("recovery_path", str(exc), "artifact") from exc
        binding = json.loads(transport.download(f"{ref.control_root}/binding.json"))
        if binding.get("run_id") != run_id or binding.get("run_spec_sha256") != spec.digest:
            raise RRCError("recovery_identity", "remote binding does not match RunSpec", "artifact")
        observed = self.inspect(run_id)
        status = observed.get("status", {})
        state = status.get("state")
        if state not in {"workload_complete", "completed", "failed"}:
            raise RRCError("recovery_state", f"workload is not finished: {state}", "artifact")
        exit_evidence = json.loads(transport.download(f"{ref.control_root}/workload-exit.json"))
        exit_code = exit_evidence.get("exit_code")
        if (exit_evidence.get("run_id") != run_id
                or exit_evidence.get("run_spec_sha256") != spec.digest
                or type(exit_code) is not int
                or status.get("detail", {}).get("exit_code") not in (None, exit_code)):
            raise RRCError("recovery_exit", "workload exit evidence is invalid", "artifact")
        if exit_code != 0 and not allow_nonzero_exit:
            raise RRCError("recovery_exit", "nonzero exit requires --allow-nonzero-exit", "artifact")
        result = transport.run(
            [
                spec.remote.python,
                "-c",
                _RECOVERY_SCRIPT,
                ref.control_root,
                spec.remote.output_root,
                json.dumps(normalized, separators=(",", ":")),
                run_id,
                spec.digest,
                str(exit_code),
                "1" if allow_nonzero_exit else "0",
            ],
            timeout_seconds=300,
        )
        if result.returncode != 0:
            raise RRCError(
                "recovery_remote",
                result.stderr.decode("utf-8", errors="replace")[-2000:],
                "artifact",
            )
        try:
            created = json.loads(result.stdout.decode("utf-8"))
            recovery_root = str(created["root"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RRCError("recovery_response", "invalid recovery helper response", "artifact") from exc
        manifest_bytes = transport.download(f"{recovery_root}/artifact_manifest.json")
        with tempfile.NamedTemporaryFile(prefix="rrctl-recovery-manifest-", suffix=".json") as handle:
            handle.write(manifest_bytes)
            handle.flush()
            manifest = load_artifact_manifest(Path(handle.name), expected_run_id=run_id)
        if (manifest.get("run_spec_sha256") != spec.digest
                or type(manifest.get("exit_code")) is not int
                or manifest["exit_code"] != exit_code
                or manifest.get("terminal_state") != state):
            raise RRCError("recovery_manifest_identity", "recovery manifest identity/exit mismatch", "artifact")
        destination = destination.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise RRCError("recovery_collision", f"destination is not empty: {destination}", "artifact")
        for item in manifest["entries"]:
            rel = confined_relative_path(item["path"], field="recovery.manifest.path")
            data = transport.download(f"{recovery_root}/{rel.as_posix()}")
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise RRCError("recovery_hash", f"recovery hash mismatch: {rel}", "artifact")
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (destination / "artifact_manifest.json").write_bytes(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        )
        return {
            "run_id": run_id,
            "destination": str(destination),
            "entries": len(manifest["entries"]),
            "recovery_id": manifest.get("recovery_id"),
            "exit_code": exit_code,
            "terminal_state": state,
            "completion_contract_passed": False,
            "remote_state_preserved": True,
            "original_completion_manifest_unchanged": True,
        }

    def pull(self, run_id: str, *, diagnostic: bool = False) -> dict[str, Any]:
        ref, spec, transport = self._runtime(run_id)
        binding_bytes = transport.download(f"{ref.control_root}/binding.json")
        binding = json.loads(binding_bytes)
        if (
            not isinstance(binding, dict)
            or binding.get("run_id") != run_id
            or binding.get("run_spec_sha256") != spec.digest
            or binding.get("control_root") != ref.control_root
        ):
            raise RRCError(
                "pull_identity",
                "remote binding does not match the locally registered RunSpec; pull refused",
                "artifact",
                details={"run_id": run_id},
            )
        destination = Path(spec.local_pull_root).expanduser() / run_id
        with _pull_lock(destination):
            return self._pull_artifacts(run_id, ref, spec, transport, destination, diagnostic, binding_bytes)

    def _pull_artifacts(
        self, run_id: str, ref: RunRef, spec: RunSpec, transport: Transport,
        destination: Path, diagnostic: bool, binding_bytes: bytes,
    ) -> dict[str, Any]:
        source_root = spec.remote.output_root
        manifest_root = ref.control_root
        if diagnostic:
            snapshot = self._worker_lifecycle_command(run_id, ["diagnostics"], phase="diagnostics")
            snapshot_id = snapshot.get("snapshot_id")
            if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-f]{32}", snapshot_id):
                raise RRCError(
                    "diagnostic_snapshot_invalid", "invalid snapshot identifier", "artifact"
                )
            source_root = f"{ref.control_root}/diagnostics/{snapshot_id}"
            manifest_root = source_root
            destination = destination / "diagnostics" / snapshot_id
        manifest_bytes = transport.download(f"{manifest_root}/artifact_manifest.json")
        with tempfile.TemporaryDirectory(prefix="rrctl-manifest-") as temporary_name:
            manifest_path = Path(temporary_name) / "artifact_manifest.json"
            manifest_path.write_bytes(manifest_bytes)
            manifest = load_artifact_manifest(manifest_path, expected_run_id=run_id)
        if manifest.get("run_spec_sha256", spec.digest) != spec.digest:
            raise RRCError("pull_identity", "artifact manifest RunSpec mismatch", "artifact")
        if any(entry["path"] == "artifact_manifest.json" or (
            not diagnostic and Path(entry["path"]).parts[0] == ".rrctl"
        ) for entry in manifest["entries"]):
            raise RRCError("pull_manifest_path", "artifact_manifest.json and .rrctl are reserved metadata paths", "artifact")
        progress = _PullProgress(run_id, manifest, download_timeout_seconds())
        progress.emit("start")
        diagnostic_only = (
            not diagnostic
            and destination.is_dir()
            and not destination.is_symlink()
            and {path.name for path in destination.iterdir()} == {"diagnostics"}
            and (destination / "diagnostics").is_dir()
            and not (destination / "diagnostics").is_symlink()
        )
        if (destination.exists() or destination.is_symlink()) and not diagnostic_only:
            # 允许预先存在或异常残留的空目录，自动清理以便后续顺利落盘
            if destination.is_dir() and not destination.is_symlink() and not any(destination.iterdir()):
                destination.rmdir()
            else:
                existing_manifest = destination / "artifact_manifest.json"
                if (
                    not destination.is_symlink()
                    and existing_manifest.is_file()
                    and not existing_manifest.is_symlink()
                ):
                    existing = load_artifact_manifest(existing_manifest, expected_run_id=run_id)
                    if sha256_json(existing) == sha256_json(manifest) and all(
                        _verified_pull_file(destination, item)
                        for item in manifest["entries"]
                    ):
                        if not diagnostic:
                            self._pull_completion(spec, ref, transport, destination, manifest, binding_bytes)
                        progress.files = progress.resumed_files = progress.total_files
                        progress.bytes = progress.resumed_bytes = progress.total_bytes
                        progress.emit("end")
                        return {
                            "run_id": run_id,
                            "destination": str(destination),
                            "entries": len(manifest["entries"]),
                            "diagnostic": diagnostic,
                            "reused": True,
                            "resumed_files": progress.resumed_files,
                            "resumed_bytes": progress.resumed_bytes,
                            "download_timeout_seconds": progress.timeout,
                            "layout": "diagnostic_snapshot" if diagnostic else "artifacts",
                        }
                if os.environ.get("RRCTL_PULL_AUTO_RECOVER") == "1":
                    conflict_backup = destination.parent / f"{destination.name}.conflict.{int(time.time())}"
                    try:
                        destination.rename(conflict_backup)
                        sys.stderr.write(
                            f"[rrctl] notice: previous incomplete destination archived to {conflict_backup.name}\n"
                        )
                        sys.stderr.flush()
                    except OSError as exc:
                        raise RRCError(
                            "pull_collision",
                            f"local artifact destination already exists and could not be archived: {destination}; "
                            f"manual intervention required: {exc}",
                            "artifact",
                        ) from exc
                else:
                    raise RRCError(
                        "pull_collision",
                        f"local artifact destination already exists: {destination}; "
                        f"if its contents are complete and verified, keep it (pull is idempotent only "
                        f"for a manifest-identical destination); otherwise move it aside or remove it "
                        f"before retrying: mv {destination} {destination}.conflict",
                        "artifact",
                    )
        destination.parent.mkdir(parents=True, exist_ok=True)
        identity = sha256_json({"manifest": manifest, "run_spec_sha256": spec.digest,
                                "source_root": source_root})
        temporary = destination.parent / f".{destination.name}.pull-{identity}"
        staged_manifest = _pull_path(temporary, "artifact_manifest.json")
        if temporary.exists():
            if (not staged_manifest.is_file()
                    or sha256_json(load_artifact_manifest(staged_manifest, expected_run_id=run_id))
                    != sha256_json(manifest)):
                raise RRCError("pull_staging_invalid", "staging identity does not match manifest",
                               "artifact")
        else:
            temporary.mkdir(mode=0o700)
            atomic_write_json(staged_manifest, manifest)
        # Unknown/random staging belongs to prior attempts: never sweep siblings.
        try:
            # 优先尝试单流批量拉取所有未就绪的条目，将 N 次 SSH 握手骤降为 1 次流式传输
            missing_entries = [e for e in manifest["entries"] if not _verified_pull_file(temporary, e)]
            if missing_entries and hasattr(transport, "download_tar_stream"):
                paths = [e["path"] for e in missing_entries]
                sys.stderr.write(f"[rrctl] pulling {len(paths)} artifacts via single tar stream...\n")
                sys.stderr.flush()
                if transport.download_tar_stream(source_root, paths, temporary, timeout_seconds=progress.timeout):
                    sys.stderr.write("[rrctl] tar stream pull delivered, verifying integrity...\n")
                    sys.stderr.flush()

            for entry in manifest["entries"]:
                relative = Path(entry["path"])
                local_path = _pull_path(temporary, entry["path"])
                if _verified_pull_file(temporary, entry):
                    progress.files += 1
                    progress.bytes += entry["size"]
                    progress.resumed_files += 1
                    progress.resumed_bytes += entry["size"]
                    progress.emit()
                    continue
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.unlink(missing_ok=True)  # only this manifest's unverified file
                try:
                    remote_path = f"{source_root}/{relative.as_posix()}"
                    download = getattr(transport, "download_to_file", None)
                    if download is None:  # legacy duck-typed FakeTransport
                        data = transport.download(remote_path)
                        if len(data) > entry["size"]:
                            raise RRCError("pull_integrity", "artifact exceeds declared size", "artifact")
                        local_path.write_bytes(data)
                    else:
                        download(remote_path, local_path, timeout_seconds=progress.timeout,
                                 expected_size=entry["size"],
                                 progress=lambda count: progress.emit(current_bytes=count))
                    if not _verified_pull_file(temporary, entry):
                        raise RRCError(
                            "pull_integrity",
                            f"downloaded artifact failed size/SHA verification: {relative}", "artifact",
                        )
                except BaseException:
                    local_path.unlink(missing_ok=True)
                    raise
                progress.files += 1
                progress.bytes += entry["size"]
                progress.emit()
            # Recheck every file, including cached files, immediately before publication.
            if not all(_verified_pull_file(temporary, item) for item in manifest["entries"]):
                raise RRCError("pull_integrity", "staged artifacts changed before publication", "artifact")
            if not diagnostic:
                self._pull_completion(spec, ref, transport, temporary, manifest, binding_bytes)
            if diagnostic_only:
                if {path.name for path in destination.iterdir()} != {"diagnostics"} or (
                    temporary / "diagnostics"
                ).exists():
                    raise RRCError("pull_collision", "destination changed during pull", "artifact")
                os.replace(destination / "diagnostics", temporary / "diagnostics")
                try:
                    destination.rmdir()
                    os.replace(temporary, destination)
                except BaseException:
                    destination.mkdir(exist_ok=True)
                    os.replace(temporary / "diagnostics", destination / "diagnostics")
                    raise
            else:
                os.replace(temporary, destination)
        except BaseException:
            progress.emit("error")
            raise
        progress.emit("end")
        return {
            "run_id": run_id,
            "destination": str(destination),
            "entries": len(manifest["entries"]),
            "diagnostic": diagnostic,
            "reused": False,
            "resumed_files": progress.resumed_files,
            "resumed_bytes": progress.resumed_bytes,
            "download_timeout_seconds": progress.timeout,
            "layout": "diagnostic_snapshot" if diagnostic else "artifacts",
        }

    def _pull_completion(
        self, spec: RunSpec, ref: RunRef, transport: Transport, destination: Path,
        manifest: dict[str, Any], binding_bytes: bytes,
    ) -> None:
        """Keep actual sealed control evidence separate from workload artifacts.

        Legacy runs may lack a receipt: absence is unknown, never synthesized success.
        Connection errors still fail the pull so a retry can recover the evidence.
        """
        try:
            receipt_bytes = transport.download(f"{ref.control_root}/completion.json")
        except RRCError as exc:
            if exc.code == "download_failed":
                return
            raise
        status_bytes = transport.download(f"{ref.control_root}/status.json")
        from .finalization import read_completion
        with tempfile.TemporaryDirectory(prefix="rrctl-completion-") as name:
            evidence = Path(name)
            payload = {"completion.json": receipt_bytes, "status.json": status_bytes,
                       "binding.json": binding_bytes}
            for filename, data in payload.items():
                (evidence / filename).write_bytes(data)
            atomic_write_json(evidence / "artifact_manifest.json", manifest)
            receipt = read_completion(spec, evidence, json.loads(binding_bytes))
            status = json.loads(status_bytes)
            if (receipt.get("run_id") != spec.run_id or status.get("run_id") != spec.run_id
                    or status.get("state") != "completed"):
                raise RRCError("completion_receipt_invalid", "completion status identity is invalid",
                               "artifact")
            metadata = _pull_path(destination, ".rrctl")
            if metadata.exists():
                if not metadata.is_dir() or any(
                    not _pull_path(metadata, filename).is_file()
                    or _pull_path(metadata, filename).read_bytes() != data
                    for filename, data in payload.items()
                ):
                    raise RRCError("pull_collision", "local completion metadata differs", "artifact")
                return
            # Rename a complete directory; no reader sees a half-written receipt set.
            with tempfile.TemporaryDirectory(prefix=".rrctl-metadata-", dir=destination) as staging:
                stage = Path(staging) / "evidence"
                stage.mkdir(mode=0o700)
                for filename, data in payload.items():
                    (stage / filename).write_bytes(data)
                os.replace(stage, metadata)

    def resume(self, *, profile_name: str, control_root: str) -> dict[str, Any]:
        profile = self.profile_store.load(profile_name)
        transport = transport_for(profile)
        spec_bytes = transport.download(f"{control_root}/run_spec.json")
        binding_bytes = transport.download(f"{control_root}/binding.json")
        spec_value = json.loads(spec_bytes)
        binding = json.loads(binding_bytes)
        spec = RunSpec.from_dict(spec_value)
        if not isinstance(binding, dict) or binding.get("schema_version") != "rrctl.binding.v1":
            raise RRCError("resume_binding_invalid", "remote binding is invalid", "recovery")
        expected_binding = {
            "run_id": spec.run_id,
            "project": spec.project,
            "commit": spec.source.commit,
            "run_spec_sha256": spec.digest,
            "stage_root": spec.remote.stage_root,
            "repo_root": spec.remote.repo_root,
            "control_root": control_root,
            "output_root": spec.remote.output_root,
            "session": spec.session.name,
        }
        mismatches = {
            key: {"expected": expected, "actual": binding.get(key)}
            for key, expected in expected_binding.items()
            if binding.get(key) != expected
        }
        if profile_name != spec.remote.profile:
            mismatches["profile"] = {
                "expected": spec.remote.profile,
                "actual": profile_name,
            }
        if mismatches:
            raise RRCError(
                "resume_binding_mismatch",
                "remote binding does not match the authoritative RunSpec/control path",
                "recovery",
                details=mismatches,
            )
        worker_sha = binding.get("worker_sha256")
        if (
            not isinstance(worker_sha, str)
            or len(worker_sha) != 64
            or any(character not in "0123456789abcdef" for character in worker_sha)
        ):
            raise RRCError("resume_worker_sha", "bound worker SHA is invalid", "recovery")
        ref = RunRef(
            run_id=spec.run_id,
            profile=profile_name,
            stage_root=binding["stage_root"],
            control_root=control_root,
            worker_path=f"{binding['stage_root']}/rrctl-worker.pyz",
            remote_python=spec.remote.python,
            run_spec_sha256=spec.digest,
            source_content_sha256=str(binding.get("source_content_sha256", "")),
            transport_bundle_sha256=str(
                binding.get("transport_bundle_sha256", binding.get("bundle_sha256", ""))
            ),
        )
        hash_result = transport.run(["sha256sum", ref.worker_path])
        actual_worker_sha = hash_result.stdout.decode("utf-8", errors="replace").split(maxsplit=1)
        if (
            hash_result.returncode != 0
            or not actual_worker_sha
            or actual_worker_sha[0] != worker_sha
        ):
            raise RRCError(
                "resume_worker_sha_mismatch",
                "remote worker does not match the bound SHA",
                "recovery",
                details={
                    "stderr": redact(
                        hash_result.stderr.decode("utf-8", errors="replace")[-4000:],
                        transport.secret_values,
                    )
                },
            )
        result = transport.run(
            [ref.remote_python, ref.worker_path, "inspect", "--control", ref.control_root]
        )
        inspected = self._parse_result(
            result, phase="resume", secret_values=transport.secret_values
        )
        self.index.save(ref, spec, inspected["status"])
        return {"run_ref": ref.to_dict(), **inspected}

    def abort(self, run_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise RRCError("abort_confirmation", "abort requires explicit --yes", "ownership")
        ref, spec, transport = self._runtime(run_id)
        if spec.session.backend != "process":
            raise RRCError(
                "legacy_backend_read_only",
                "legacy runs can be inspected and pulled; "
                "process ownership is required for new lifecycle mutations",
                "ownership",
            )
        return self._mutating_request(
            ref,
            transport,
            [ref.remote_python, ref.worker_path, "abort", "--control", ref.control_root],
            operation="abort",
        )
