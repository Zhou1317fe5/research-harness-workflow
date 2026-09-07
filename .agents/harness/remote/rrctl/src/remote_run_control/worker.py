"""Remote zipapp worker lifecycle commands."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .artifacts import build_artifact_manifest, build_diagnostic_snapshot
from .cleanup import cleanup_output
from .errors import RRCError
from .health import evaluate_health
from .jsonutil import atomic_write_json, load_json, sha256_file, sha256_json, utc_now
from .models import RunSpec
from .source_identity import source_content_sha256
from .state import TERMINAL_STATES, read_status, recover_status, transition


def _load_spec(path: Path) -> RunSpec:
    return RunSpec.from_path(path)


def _binding_path(control_root: Path) -> Path:
    return control_root / "binding.json"


def _update_binding(control_root: Path, values: dict[str, Any]) -> dict[str, Any]:
    path = _binding_path(control_root)
    current = load_json(path) if path.is_file() else {}
    current.update(values)
    atomic_write_json(path, current)
    return current


def _process_identity(pid: int) -> dict[str, Any]:
    try:
        stat_text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        command_bytes = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError) as exc:
        raise RRCError(
            "process_identity_unavailable", "process identity is unavailable", "ownership"
        ) from exc
    _, separator, fields_text = stat_text.rpartition(")")
    fields = fields_text.split()
    if not separator or len(fields) < 20:
        raise RRCError("process_identity_invalid", "process stat identity is invalid", "ownership")
    argv = [item.decode("utf-8", errors="replace") for item in command_bytes.split(b"\0") if item]
    if not argv:
        raise RRCError("process_identity_invalid", "process command line is empty", "ownership")
    return {
        "pid": pid,
        "parent_pid": int(fields[1]),
        "process_group_id": int(fields[2]),
        "start_ticks": int(fields[19]),
        "cmdline_sha256": sha256_json(argv),
        "argv": argv,
    }


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
        is_activation_shell = len(argv) >= 2 and Path(argv[0]).name == "bash" and argv[1] == "-c"
        if current["cmdline_sha256"] == stable_hash and not is_activation_shell:
            stable_samples += 1
            if stable_samples >= 3:
                return current
        else:
            stable_hash = current["cmdline_sha256"]
            stable_samples = 1
        time.sleep(0.05)
    return latest


def _has_run_marker(pid: int, run_id: str) -> bool:
    try:
        environment = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return False
    return f"RRCTL_RUN_ID={run_id}".encode() in environment


def _is_descendant(pid: int, ancestor_pid: int) -> bool:
    current = pid
    for _ in range(64):
        if current == ancestor_pid:
            return True
        if current <= 1:
            return False
        try:
            current = int(_process_identity(current)["parent_pid"])
        except RRCError:
            return False
    return False


def _owned_marker_pids(run_id: str) -> list[int]:
    return sorted(
        int(entry.name)
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit() and _has_run_marker(int(entry.name), run_id)
    )


def _fail(control_root: Path, spec: RunSpec, exc: BaseException, phase: str) -> None:
    try:
        state = read_status(control_root).get("state")
        if state not in TERMINAL_STATES:
            detail = exc.to_dict() if isinstance(exc, RRCError) else {"message": str(exc)}
            transition(
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
    manifest = load_json(manifest_path)
    control_root = Path(spec.remote.control_root)
    if control_root.exists():
        raise RRCError(
            "control_collision", f"control directory already exists: {control_root}", "worker"
        )
    control_root.mkdir(parents=True, mode=0o700)
    transition(control_root, run_id=spec.run_id, next_state="prepared", reason="worker_started")
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
        if (
            subprocess.run(
                ["tmux", "has-session", "-t", spec.session.name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            raise RRCError(
                "session_collision",
                f"tmux session already exists: {spec.session.name}",
                "worker",
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
            "workload_argv_sha256": sha256_json(list(spec.workload.argv)),
            "created_at": utc_now(),
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
        worker_command = [
            "env",
            f"RRCTL_RUN_ID={spec.run_id}",
            spec.remote.python,
            str(worker_path),
            "execute",
            "--control",
            str(control_root),
        ]
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                spec.session.name,
                shlex.join(worker_command),
            ],
            check=True,
        )
        transition(
            control_root,
            run_id=spec.run_id,
            next_state="launched",
            reason="tmux_worker_started",
        )
        return {
            "run_id": spec.run_id,
            "state": "launched",
            "control_root": str(control_root),
            "session": spec.session.name,
        }
    except BaseException as exc:
        _fail(control_root, spec, exc, "launch")
        raise


def _conda_command(spec: RunSpec) -> list[str]:
    shell = (
        'source "$RRCTL_CONDA_SH" && conda activate "$RRCTL_CONDA_ENV" '
        '&& cd "$RRCTL_WORKDIR" && exec "$@"'
    )
    return ["bash", "-c", shell, "rrctl-workload", *spec.workload.argv]


def execute(control_root: Path) -> int:
    spec = _load_spec(control_root / "run_spec.json")
    executor_identity = _process_identity(os.getpid())
    _update_binding(
        control_root,
        {
            "executor_pid": os.getpid(),
            "executor_started_at": utc_now(),
            "executor_process_group_id": executor_identity["process_group_id"],
            "executor_start_ticks": executor_identity["start_ticks"],
            "executor_cmdline_sha256": executor_identity["cmdline_sha256"],
        },
    )
    launch_deadline = time.monotonic() + 10
    while read_status(control_root).get("state") == "staged":
        if time.monotonic() >= launch_deadline:
            raise RRCError(
                "launch_state_timeout",
                "executor did not observe the launched state within 10 seconds",
                "worker",
            )
        time.sleep(0.05)
    output_root = Path(spec.remote.output_root)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    console_path = control_root / "console.log"
    environment = os.environ.copy()
    environment.update(
        {
            "RRCTL_RUN_ID": spec.run_id,
            "RRCTL_CONTROL_ROOT": str(control_root),
            "RRCTL_CONDA_SH": spec.environment.conda_sh,
            "RRCTL_CONDA_ENV": spec.environment.name,
            "RRCTL_WORKDIR": str(Path(spec.remote.repo_root) / spec.workload.cwd),
            "RRCTL_OUTPUT_ROOT": str(output_root),
            "PYTHONUNBUFFERED": "1",
        }
    )
    try:
        with console_path.open("ab", buffering=0) as console:
            process = subprocess.Popen(
                _conda_command(spec),
                stdout=console,
                stderr=subprocess.STDOUT,
                env=environment,
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
                    "workload_process_group_id": os.getpgid(process.pid),
                    **identity_values,
                },
            )
            return_code = process.wait()
        cleanup_output(
            spec,
            terminal_state="workload_exit_zero" if return_code == 0 else "failed",
        )
        current = read_status(control_root).get("state")
        if current in TERMINAL_STATES:
            return return_code
        if return_code != 0:
            transition(
                control_root,
                run_id=spec.run_id,
                next_state="failed",
                reason="workload_exit_nonzero",
                detail={"exit_code": return_code},
            )
            return return_code
        if current == "launched":
            first = evaluate_health(
                spec,
                control_root,
                phase="first_step",
                process_required=False,
                transition_lifecycle=True,
            )
            if not first.healthy:
                transition(
                    control_root,
                    run_id=spec.run_id,
                    next_state="failed",
                    reason="first_step_health_failed_after_fast_exit",
                    detail={"health": first.to_dict()},
                )
                return 1
        transition(
            control_root,
            run_id=spec.run_id,
            next_state="workload_complete",
            reason="workload_exit_zero_awaiting_external_completion",
            detail={"exit_code": 0},
        )
        return 0
    except BaseException as exc:
        _fail(control_root, spec, exc, "execute")
        raise


def health(control_root: Path, phase: str) -> dict[str, Any]:
    spec = _load_spec(control_root / "run_spec.json")
    result = evaluate_health(
        spec,
        control_root,
        phase=phase,
        process_required=phase != "completion",
        transition_lifecycle=True,
    )
    return result.to_dict()


def inspect(control_root: Path) -> dict[str, Any]:
    status, recovered = recover_status(control_root)
    binding = load_json(_binding_path(control_root))
    return {
        "run_id": status["run_id"],
        "status": status,
        "status_recovered": recovered,
        "binding": binding,
        "control_root": str(control_root),
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
    if read_status(control_root).get("state") != "workload_complete":
        raise RRCError(
            "complete_state",
            "completion is only valid after workload_complete",
            "health",
        )
    completion = evaluate_health(
        spec,
        control_root,
        phase="completion",
        process_required=False,
        transition_lifecycle=False,
    )
    if not completion.healthy or not completion.complete:
        raise RRCError(
            "completion_health_failed",
            "completion health contract did not pass",
            "health",
            details={"health": completion.to_dict()},
        )
    manifest = build_artifact_manifest(
        run_id=spec.run_id,
        output_root=Path(spec.remote.output_root),
        declared=spec.artifacts,
        adapter_paths=completion.adapter.artifacts if completion.adapter else (),
        destination=control_root / "artifact_manifest.json",
    )
    transition(
        control_root,
        run_id=spec.run_id,
        next_state="completed",
        reason="external_completion_and_artifact_contract_passed",
        detail={"exit_code": 0, "artifact_count": len(manifest["entries"])},
    )
    return read_status(control_root)


def fail_completed_workload(control_root: Path, reason: str) -> dict[str, Any]:
    spec = _load_spec(control_root / "run_spec.json")
    if read_status(control_root).get("state") != "workload_complete":
        raise RRCError(
            "fail_state",
            "terminal health failure requires workload_complete",
            "health",
        )
    transition(
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
    if status.get("state") == "workload_complete":
        cleanup_output(spec, terminal_state=terminal_state)
        transition(
            control_root,
            run_id=spec.run_id,
            next_state=terminal_state,
            reason=reason,
            detail={"cleanup": "workload_already_exited"},
        )
        return read_status(control_root)
    binding = load_json(_binding_path(control_root))
    owned_pid = binding.get("workload_pid") or binding.get("executor_pid")
    if not isinstance(owned_pid, int):
        raise RRCError("abort_pid_missing", "owned process PID is unavailable", "ownership")
    if not _has_run_marker(owned_pid, spec.run_id):
        raise RRCError(
            "abort_owner_mismatch", "PID does not carry the expected run marker", "ownership"
        )
    if binding.get("workload_argv_sha256") != sha256_json(list(spec.workload.argv)):
        raise RRCError(
            "abort_command_mismatch", "bound workload argv does not match RunSpec", "ownership"
        )
    workload_identity = _process_identity(owned_pid)
    if (
        workload_identity["start_ticks"] != binding.get("workload_start_ticks")
        or workload_identity["cmdline_sha256"] != binding.get("workload_cmdline_sha256")
        or workload_identity["process_group_id"] != binding.get("workload_process_group_id")
    ):
        raise RRCError(
            "abort_identity_mismatch",
            "workload PID identity or command fingerprint changed",
            "ownership",
        )
    executor_pid = binding.get("executor_pid")
    if not isinstance(executor_pid, int) or not _has_run_marker(executor_pid, spec.run_id):
        raise RRCError(
            "abort_executor_mismatch", "executor ownership cannot be verified", "ownership"
        )
    executor_identity = _process_identity(executor_pid)
    if (
        executor_identity["start_ticks"] != binding.get("executor_start_ticks")
        or executor_identity["cmdline_sha256"] != binding.get("executor_cmdline_sha256")
        or executor_identity["process_group_id"] != binding.get("executor_process_group_id")
        or not _is_descendant(owned_pid, executor_pid)
    ):
        raise RRCError(
            "abort_executor_mismatch", "workload is not owned by the bound executor", "ownership"
        )
    panes = subprocess.run(
        ["tmux", "list-panes", "-t", spec.session.name, "-F", "#{pane_pid}"],
        check=False,
        capture_output=True,
        text=True,
    )
    pane_pids = {int(line) for line in panes.stdout.splitlines() if line.strip().isdigit()}
    if panes.returncode != 0 or not any(
        _is_descendant(executor_pid, pane_pid) for pane_pid in pane_pids
    ):
        raise RRCError("abort_session_missing", "bound tmux session does not exist", "ownership")
    owned_pgid = workload_identity["process_group_id"]
    if owned_pgid <= 1 or owned_pgid == os.getpgrp():
        raise RRCError(
            "abort_process_group_invalid",
            "owned process group is unsafe to signal",
            "ownership",
        )
    subprocess.run(["tmux", "kill-session", "-t", spec.session.name], check=True)
    with suppress(ProcessLookupError):
        os.killpg(owned_pgid, signal.SIGTERM)
    term_deadline = time.monotonic() + 10
    while _owned_marker_pids(spec.run_id) and time.monotonic() < term_deadline:
        time.sleep(0.1)
    remaining = _owned_marker_pids(spec.run_id)
    if remaining:
        with suppress(ProcessLookupError):
            os.killpg(owned_pgid, signal.SIGKILL)
        kill_deadline = time.monotonic() + 5
        while _owned_marker_pids(spec.run_id) and time.monotonic() < kill_deadline:
            time.sleep(0.1)
    remaining = _owned_marker_pids(spec.run_id)
    cleanup_output(spec, terminal_state=terminal_state)
    transition(
        control_root,
        run_id=spec.run_id,
        next_state=terminal_state,
        reason=reason,
        detail={
            "pid": owned_pid,
            "pgid": owned_pgid,
            "session": spec.session.name,
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
