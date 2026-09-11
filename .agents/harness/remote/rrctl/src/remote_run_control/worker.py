"""Remote zipapp worker lifecycle commands."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import monitor
from .artifacts import build_diagnostic_snapshot
from .cleanup import cleanup_output
from .environment import activation_argv, make_environment
from .errors import RRCError
from .finalization import complete_existing, finalize_exit, read_completion
from .health import evaluate_health
from .jsonutil import atomic_write_json, load_json, sha256_file, sha256_json, utc_now
from .models import RunSpec
from .processes import boot_id, bound_group, owned_processes
from .processes import process_identity as _process_identity
from .resources import acquire as acquire_resources
from .resources import release as release_resources
from .source_identity import source_content_sha256
from .state import (
    TERMINAL_STATES,
    control_lock,
    mark_launched,
    operation_lock,
    read_status,
    recover_status,
    request_stop,
    transition,
    transition_if_open,
)


def _load_spec(path: Path) -> RunSpec:
    return RunSpec.from_path(path)


def _binding_path(control_root: Path) -> Path:
    return control_root / "binding.json"


def _update_binding(control_root: Path, values: dict[str, Any]) -> dict[str, Any]:
    path = _binding_path(control_root)
    with control_lock(control_root):
        current = load_json(path) if path.is_file() else {}
        current.update(values)
        atomic_write_json(path, current)
    return current


def _capture_workload_identity(pid: int, expected_argv: tuple[str, ...]) -> dict[str, Any] | None:
    deadline = time.monotonic() + 2
    latest: dict[str, Any] | None = None
    stable_hash: str | None = None
    stable_samples = 0
    while time.monotonic() < deadline:
        try:
            current = _process_identity(pid)
        except RRCError:
            return latest
        latest = current
        argv = current["argv"]
        if argv == list(expected_argv):
            return current
        is_activation_shell = len(argv) >= 2 and Path(argv[0]).name == "bash" and "-c" in argv[1:5]
        if current["cmdline_sha256"] == stable_hash and not is_activation_shell:
            stable_samples += 1
            if stable_samples >= 3:
                return current
        else:
            stable_hash = current["cmdline_sha256"]
            stable_samples = 1
        time.sleep(0.05)
    return latest


def _fail(control_root: Path, spec: RunSpec, exc: BaseException, phase: str) -> None:
    try:
        state = read_status(control_root).get("state")
        if state not in TERMINAL_STATES:
            detail = exc.to_dict() if isinstance(exc, RRCError) else {"message": str(exc)}
            transition_if_open(
                control_root,
                run_id=spec.run_id,
                next_state="failed",
                reason=f"{phase}_failed",
                detail=detail,
            )
    except Exception:
        pass


def _copy_anchor(staged: Path, destination: Path, expected_sha: str) -> None:
    if destination.exists() or destination.is_symlink():
        raise RRCError(
            "anchor_collision",
            f"remote anchor already exists: {destination}",
            "worker",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.rrctl-tmp")
    shutil.copyfile(staged, temporary)
    temporary.chmod(0o600)
    actual = sha256_file(temporary)
    if actual != expected_sha:
        temporary.unlink(missing_ok=True)
        raise RRCError(
            "anchor_sha_mismatch",
            f"staged anchor SHA mismatch for {destination}: {actual}",
            "worker",
        )
    os.replace(temporary, destination)


def launch(stage_root: Path) -> dict[str, Any]:
    spec_path = stage_root / "run_spec.json"
    manifest_path = stage_root / "stage_manifest.json"
    spec = _load_spec(spec_path)
    if spec.session.backend != "process" or spec.resources is None:
        raise RRCError(
            "backend_removed",
            "new runs require the process backend and resources declaration",
            "worker",
        )
    manifest = load_json(manifest_path)
    control_root = Path(spec.remote.control_root)
    if control_root.exists():
        raise RRCError(
            "control_collision", f"control directory already exists: {control_root}", "worker"
        )
    control_root.mkdir(parents=True, mode=0o700)
    transition(control_root, run_id=spec.run_id, next_state="prepared", reason="worker_started")
    executor_started = False
    try:
        if sha256_file(spec_path) != manifest.get("run_spec_file_sha256"):
            raise RRCError("run_spec_sha_mismatch", "staged RunSpec file SHA mismatch", "worker")
        if spec.digest != manifest.get("run_spec_sha256"):
            raise RRCError(
                "run_spec_digest_mismatch", "normalized RunSpec digest mismatch", "worker"
            )
        worker_path = stage_root / "rrctl-worker.pyz"
        bundle_path = stage_root / "source.bundle"
        if sha256_file(worker_path) != manifest.get("worker_sha256"):
            raise RRCError("worker_sha_mismatch", "worker zipapp SHA mismatch", "worker")
        expected_transport_sha = manifest.get(
            "transport_bundle_sha256", manifest.get("bundle_sha256")
        )
        if sha256_file(bundle_path) != expected_transport_sha:
            raise RRCError("bundle_sha_mismatch", "source bundle SHA mismatch", "worker")
        repo_root = Path(spec.remote.repo_root)
        output_root = Path(spec.remote.output_root)
        if repo_root.exists() or output_root.exists():
            raise RRCError(
                "runtime_collision",
                "remote repository or output root already exists",
                "worker",
                details={"repo_root": str(repo_root), "output_root": str(output_root)},
            )
        transition(
            control_root,
            run_id=spec.run_id,
            next_state="staged",
            reason="stage_integrity_verified",
        )
        subprocess.run(["git", "clone", str(bundle_path), str(repo_root)], check=True)
        subprocess.run(
            ["git", "-C", str(repo_root), "checkout", "-B", spec.source.branch, spec.source.commit],
            check=True,
        )
        actual_commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if actual_commit != spec.source.commit:
            raise RRCError("checkout_commit", "remote checkout commit mismatch", "worker")
        actual_source_sha = source_content_sha256(repo_root, spec.source)
        if actual_source_sha != manifest.get("source_content_sha256"):
            raise RRCError(
                "source_content_sha_mismatch",
                "remote checkout does not match staged source-content identity",
                "worker",
                details={
                    "expected": manifest.get("source_content_sha256"),
                    "actual": actual_source_sha,
                },
            )
        for anchor in spec.anchors:
            staged = stage_root / "anchors" / anchor.name
            _copy_anchor(staged, Path(anchor.remote_path), anchor.sha256)

        atomic_write_json(control_root / "run_spec.json", spec.to_dict())
        binding = {
            "schema_version": "rrctl.binding.v1",
            "run_id": spec.run_id,
            "project": spec.project,
            "branch": spec.source.branch,
            "commit": spec.source.commit,
            "run_spec_sha256": spec.digest,
            "worker_sha256": manifest["worker_sha256"],
            "source_content_sha256": actual_source_sha,
            "transport_bundle_sha256": expected_transport_sha,
            "bundle_sha256": expected_transport_sha,
            "stage_root": str(stage_root),
            "repo_root": str(repo_root),
            "control_root": str(control_root),
            "output_root": str(output_root),
            "session": spec.session.name,
            "backend": "process",
            "workload_argv_sha256": sha256_json(list(spec.workload.argv)),
            "created_at": utc_now(),
            "monitoring": monitor.declaration(spec),
        }
        atomic_write_json(_binding_path(control_root), binding)
        atomic_write_json(
            control_root / "recovery.json",
            {
                "schema_version": "rrctl.recovery.v1",
                "run_id": spec.run_id,
                "control_root": str(control_root),
                "status": str(control_root / "status.json"),
                "console": str(control_root / "console.log"),
                "health": str(control_root / "health.jsonl"),
                "artifact_manifest": str(control_root / "artifact_manifest.json"),
            },
        )
        settings = asdict(spec.environment)
        if spec.environment.preflight_argv:
            with (control_root / "preflight.log").open("wb") as stream:
                check = subprocess.run(
                    activation_argv(
                        spec.environment.preflight_argv,
                        overrides={**spec.environment.variables, "CUDA_VISIBLE_DEVICES": ""},
                    ),
                    cwd=repo_root / spec.workload.cwd,
                    env=make_environment(settings),
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                    check=False,
                )
            atomic_write_json(control_root / "preflight.json", {"exit_code": check.returncode})
            if check.returncode:
                raise RRCError(
                    "workload_preflight",
                    "staged entrypoint preflight failed; inspect preflight.log",
                    "worker",
                )
        worker_command = [
            sys.executable,
            str(worker_path),
            "execute",
            "--control",
            str(control_root),
        ]
        environment = make_environment(
            settings,
            {
                "RRCTL_RUN_ID": spec.run_id,
                "RRCTL_CONTROL_ROOT": str(control_root),
            },
        )
        with (control_root / "worker.log").open("ab", buffering=0) as worker_log:
            subprocess.Popen(
                worker_command,
                stdin=subprocess.DEVNULL,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                cwd=control_root,
                env=environment,
                start_new_session=True,
                close_fds=True,
            )
        executor_started = True
        mark_launched(control_root, run_id=spec.run_id)
        return {
            "run_id": spec.run_id,
            "state": "launched",
            "control_root": str(control_root),
            "session": spec.session.name,
            "monitoring": binding["monitoring"],
        }
    except BaseException as exc:
        if not executor_started:
            _fail(control_root, spec, exc, "launch")
        raise


def _conda_command(spec: RunSpec, extra: dict[str, str]) -> list[str]:
    return activation_argv(spec.workload.argv, overrides={**spec.environment.variables, **extra})


def execute(control_root: Path) -> int:
    # 先取得整个执行期的唯一拥有权；重复 execute 不得重启 workload 或改坏原状态。
    with operation_lock(control_root, "monitor"):
        binding = load_json(_binding_path(control_root))
        if binding.get("executor_pid") or (control_root / "monitor.json").exists():
            raise RRCError(
                "execute_already_started",
                "bound worker was already started; inspect its state",
                "monitor",
            )
        return _execute_owned(control_root)


def _execute_owned(control_root: Path) -> int:
    spec = _load_spec(control_root / "run_spec.json")
    output_root = Path(spec.remote.output_root)
    console_path = control_root / "console.log"
    lease: dict[str, Any] = {}
    process: subprocess.Popen | None = None
    watcher: monitor.Monitor | None = None
    try:
        executor_identity = _process_identity(os.getpid())
        if (
            spec.session.backend != "process"
            or executor_identity["session_id"] != os.getpid()
            or executor_identity["process_group_id"] != os.getpid()
        ):
            raise RRCError(
                "executor_session_invalid",
                "worker must own an independent process session",
                "ownership",
            )
        _update_binding(
            control_root,
            {
                "boot_id": boot_id(),
                "executor_pid": os.getpid(),
                "executor_started_at": utc_now(),
                "executor_process_group_id": executor_identity["process_group_id"],
                "executor_session_id": executor_identity["session_id"],
                "executor_start_ticks": executor_identity["start_ticks"],
                "executor_cmdline_sha256": executor_identity["cmdline_sha256"],
            },
        )
        mark_launched(control_root, run_id=spec.run_id)
        watcher = monitor.Monitor(spec, control_root)
        lease = acquire_resources(spec, control_root)
        _update_binding(control_root, {"resources": lease})
        output_root.parent.mkdir(parents=True, exist_ok=True)
        extra = {
            "RRCTL_RUN_ID": spec.run_id,
            "RRCTL_CONTROL_ROOT": str(control_root),
            "RRCTL_OUTPUT_ROOT": str(output_root),
            "CUDA_VISIBLE_DEVICES": ",".join(lease["gpu_ids"]),
        }
        environment = make_environment(asdict(spec.environment), extra)
        with console_path.open("ab", buffering=0) as console:
            process = subprocess.Popen(
                _conda_command(spec, extra),
                stdout=console,
                stderr=subprocess.STDOUT,
                env=environment,
                cwd=Path(spec.remote.repo_root) / spec.workload.cwd,
                start_new_session=False,
            )
            workload_identity = _capture_workload_identity(process.pid, spec.workload.argv)
            identity_values = (
                {
                    "workload_start_ticks": workload_identity["start_ticks"],
                    "workload_cmdline_sha256": workload_identity["cmdline_sha256"],
                }
                if workload_identity
                else {}
            )
            _update_binding(
                control_root,
                {
                    "workload_pid": process.pid,
                    "workload_started_at": utc_now(),
                    "workload_process_group_id": executor_identity["process_group_id"],
                    **identity_values,
                },
            )
            return_code = watcher.supervise(process)
        watcher.data["monitor_status"] = "finalizing"
        watcher.heartbeat()
        status = finalize_exit(
            spec,
            control_root,
            return_code,
            check=lambda phase: watcher.check(phase, process_required=False),
            heartbeat=watcher.heartbeat,
        )
        watcher.finish(status)
        return return_code if return_code else (0 if status["state"] == "completed" else 2)
    except BaseException as exc:
        if process is None:
            _fail(control_root, spec, exc, "execute")
        # 检查器或监控器故障不能冒充 workload 的退出，也不能自动停止活进程。
        if watcher is not None:
            with suppress(Exception):
                watcher.finish(read_status(control_root), error_code="monitor_failed")
        raise
    finally:
        if lease:
            release_resources(lease, spec, control_root)


def health(control_root: Path, phase: str) -> dict[str, Any]:
    if monitor.protocol(load_json(_binding_path(control_root))):
        return monitor.read_health(control_root, phase)
    spec = _load_spec(control_root / "run_spec.json")
    result = evaluate_health(
        spec,
        control_root,
        phase=phase,
        process_required=phase != "completion",
        transition_lifecycle=True,
    )
    return {**result.to_dict(), "monitoring_mode": "client_compatibility"}


def inspect(control_root: Path) -> dict[str, Any]:
    binding = load_json(_binding_path(control_root))
    if monitor.protocol(binding):
        return monitor.read_observation(control_root)
    status, recovered = recover_status(control_root)
    return {
        "run_id": status["run_id"],
        "status": status,
        "status_recovered": recovered,
        "binding": binding,
        "control_root": str(control_root),
        "monitoring_mode": "client_compatibility",
    }


def diagnostics(control_root: Path) -> dict[str, Any]:
    spec = _load_spec(control_root / "run_spec.json")
    paths = {item.path for item in spec.artifacts}
    for phase in (spec.health.first_step, spec.health.periodic):
        if phase.progress_path:
            paths.add(phase.progress_path)
    contract = spec.metadata.get("adapter_contract", {})
    if isinstance(contract, dict) and isinstance(contract.get("summary_path"), str):
        paths.add(contract["summary_path"])
    return build_diagnostic_snapshot(
        run_id=spec.run_id,
        control_root=control_root,
        output_root=Path(spec.remote.output_root),
        output_paths=sorted(paths),
    )


def complete(control_root: Path) -> dict[str, Any]:
    spec = _load_spec(control_root / "run_spec.json")
    binding = load_json(_binding_path(control_root))
    if monitor.protocol(binding):
        if read_status(control_root)["state"] == "completed":
            read_completion(spec, control_root, binding)
            return read_status(control_root)
        raise RRCError("monitor_owned", "completion belongs to the bound worker", "monitor")
    return complete_existing(spec, control_root)


def fail_completed_workload(control_root: Path, reason: str) -> dict[str, Any]:
    spec = _load_spec(control_root / "run_spec.json")
    if monitor.protocol(load_json(_binding_path(control_root))):
        raise RRCError("monitor_owned", "completion belongs to the bound worker", "monitor")
    if read_status(control_root).get("state") != "workload_complete":
        raise RRCError(
            "fail_state",
            "terminal health failure requires workload_complete",
            "health",
        )
    transition_if_open(
        control_root,
        run_id=spec.run_id,
        next_state="failed",
        reason=reason,
        detail={"cleanup": "workload_already_exited"},
    )
    return read_status(control_root)


def abort(
    control_root: Path,
    *,
    terminal_state: str = "aborted",
    reason: str = "ownership_verified_abort",
) -> dict[str, Any]:
    spec = _load_spec(control_root / "run_spec.json")
    status = read_status(control_root)
    if status.get("state") in TERMINAL_STATES:
        raise RRCError("abort_terminal", "cannot abort a terminal run", "ownership")
    if terminal_state not in {"failed", "aborted"}:
        raise RRCError(
            "abort_terminal_state",
            "abort terminal state must be failed or aborted",
            "ownership",
        )
    if spec.session.backend != "process":
        raise RRCError(
            "legacy_backend_read_only",
            "legacy runs remain readable; new process ownership is required to stop a run",
            "ownership",
        )
    binding = load_json(_binding_path(control_root))
    if binding.get("workload_argv_sha256") != sha256_json(list(spec.workload.argv)):
        raise RRCError(
            "abort_command_mismatch", "bound workload argv does not match RunSpec", "ownership"
        )
    try:
        owned_pgid, members = bound_group(binding, spec.run_id, control_root)
    except RRCError as exc:
        if exc.code != "abort_process_missing":
            raise
        request_stop(control_root, run_id=spec.run_id, reason=reason, state=terminal_state)
        # 已确认进程消失时，显式停止请求仍可关闭运行；不伪造工作负载退出码。
        cleanup_output(spec, terminal_state=terminal_state)
        release_resources(binding.get("resources", {}), spec, control_root)
        return transition_if_open(
            control_root,
            run_id=spec.run_id,
            next_state=terminal_state,
            reason="owned_process_already_gone",
            detail={"exit_code": None, "cleanup": "no_live_process"},
        )
    request_stop(control_root, run_id=spec.run_id, reason=reason, state=terminal_state)
    with suppress(ProcessLookupError):
        os.killpg(owned_pgid, signal.SIGTERM)
    term_deadline = time.monotonic() + 10
    while owned_processes(spec.run_id, control_root) and time.monotonic() < term_deadline:
        time.sleep(0.1)
    remaining = owned_processes(spec.run_id, control_root)
    if remaining:
        owned_pgid, _ = bound_group(binding, spec.run_id, control_root)
        with suppress(ProcessLookupError):
            os.killpg(owned_pgid, signal.SIGKILL)
        kill_deadline = time.monotonic() + 5
        while owned_processes(spec.run_id, control_root) and time.monotonic() < kill_deadline:
            time.sleep(0.1)
    remaining = [item["pid"] for item in owned_processes(spec.run_id, control_root)]
    if not remaining:
        cleanup_output(spec, terminal_state=terminal_state)
        release_resources(binding.get("resources", {}), spec, control_root)
    transition_if_open(
        control_root,
        run_id=spec.run_id,
        next_state=terminal_state,
        reason=reason,
        detail={
            "pids": [item["pid"] for item in members],
            "pgid": owned_pgid,
            "backend": "process",
            "cleanup": "reaped" if not remaining else "incomplete",
            "remaining_owned_pids": remaining,
        },
    )
    if remaining:
        raise RRCError(
            "abort_reap_failed",
            "task-owned processes remain after TERM/KILL",
            "ownership",
            details={"remaining_owned_pids": remaining},
        )
    return read_status(control_root)


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="rrctl-worker")
    commands = root.add_subparsers(dest="command", required=True)
    launch_parser = commands.add_parser("launch")
    launch_parser.add_argument("--stage", type=Path, required=True)
    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--control", type=Path, required=True)
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("--control", type=Path, required=True)
    observe_parser = commands.add_parser("observe")
    observe_parser.add_argument("--control", type=Path, required=True)
    observe_parser.add_argument("--timeout-seconds", type=float, required=True)
    observe_parser.add_argument("--after-event")
    observe_parser.add_argument("--until", choices=("event", "first_step"), default="event")
    diagnostic_parser = commands.add_parser("diagnostics")
    diagnostic_parser.add_argument("--control", type=Path, required=True)
    health_parser = commands.add_parser("health")
    health_parser.add_argument("--control", type=Path, required=True)
    health_parser.add_argument(
        "--phase", choices=("first_step", "periodic", "completion"), required=True
    )
    abort_parser = commands.add_parser("abort")
    abort_parser.add_argument("--control", type=Path, required=True)
    abort_parser.add_argument("--terminal-state", choices=("failed", "aborted"), default="aborted")
    abort_parser.add_argument("--reason", default="ownership_verified_abort")
    complete_parser = commands.add_parser("complete")
    complete_parser.add_argument("--control", type=Path, required=True)
    fail_parser = commands.add_parser("fail-completed")
    fail_parser.add_argument("--control", type=Path, required=True)
    fail_parser.add_argument("--reason", required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "launch":
            _print(launch(args.stage))
        elif args.command == "execute":
            return execute(args.control)
        elif args.command == "inspect":
            _print(inspect(args.control))
        elif args.command == "observe":
            _print(
                monitor.observe(
                    args.control,
                    timeout_seconds=args.timeout_seconds,
                    after_event=args.after_event,
                    until=args.until,
                )
            )
        elif args.command == "diagnostics":
            _print(diagnostics(args.control))
        elif args.command == "health":
            _print(health(args.control, args.phase))
        elif args.command == "abort":
            _print(
                abort(
                    args.control,
                    terminal_state=args.terminal_state,
                    reason=args.reason,
                )
            )
        elif args.command == "complete":
            _print(complete(args.control))
        elif args.command == "fail-completed":
            _print(fail_completed_workload(args.control, args.reason))
        return 0
    except RRCError as exc:
        _print({"ok": False, "error": exc.to_dict()})
        return 2
    except Exception as exc:
        _print(
            {
                "ok": False,
                "error": {
                    "code": "worker_unhandled",
                    "message": str(exc),
                    "phase": "worker",
                },
            }
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
