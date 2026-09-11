"""rrctl command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .controller import Controller
from .errors import RRCError
from .jsonutil import atomic_write_json, sha256_file, sha256_json
from .models import RunSpec
from .monitor import PROTOCOL
from .output_limits import CONTROL_OUTPUT_LIMIT
from .security import redact, redact_data

CLI_SCHEMA = "rrctl.cli.v1"
OPERATIONS = {"ready", "launch", "inspect", "health", "wait", "pull", "resume", "abort", "doctor"}


def _emit(value: dict[str, Any], *, machine: bool) -> None:
    if machine:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _compact_wait(value: dict[str, Any], controller: Controller) -> dict[str, Any]:
    if value.get("monitoring_mode") != "worker":
        return value
    path = controller.index.root / value["run_id"] / "last_observation.json"
    try:
        atomic_write_json(path, value)
    except OSError:
        return value
    compact = {key: item for key, item in value.items() if key not in {"binding", "first_step"}}
    status = dict(compact.get("status", {}))
    status["detail"] = {
        key: item
        for key, item in status.get("detail", {}).items()
        if key
        in {
            "exit_code",
            "failure_kind",
            "artifact_count",
            "completion_sha256",
        }
    }
    compact["status"] = status
    compact["details_path"] = str(path)
    return compact


def build_parser(*, machine: bool = False) -> argparse.ArgumentParser:
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            if machine:
                raise RRCError(
                    "cli_arguments",
                    "invalid command arguments; use the operation help",
                    "cli",
                    details={"usage": self.format_usage().strip()},
                    retryable=False,
                )
            super().error(message)

    parser = Parser(
        prog="rrctl",
        description="Daemonless fail-closed control plane for remote workloads",
    )
    parser.add_argument("--json", action="store_true", help="emit compact machine-readable JSON")
    parser.add_argument("--profiles", type=Path, help="credential profile JSON path")
    parser.add_argument("--state-root", type=Path, help="local run index root")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser(
        "doctor", help="show installation or diagnose a profile without launching a run"
    )
    doctor.add_argument("--profile", help="check connection and environment for this profile")
    doctor.add_argument(
        "--env-file", type=Path, help="check file permissions only; never read or source its values"
    )
    doctor.add_argument("--offline", action="store_true", help="only perform local profile checks")

    ready = commands.add_parser("ready", help="validate RunSpec and remote prerequisites")
    ready.add_argument("run_spec", type=Path)
    ready.add_argument("--offline", action="store_true", help="skip remote preflight")

    launch = commands.add_parser("launch", help="stage, launch, and pass first-step health")
    launch.add_argument("run_spec", type=Path)
    launch.add_argument(
        "--max-wait-seconds",
        type=float,
        default=900,
        help="maximum first-step observation budget; 0 uses the RunSpec gate budget",
    )

    inspect = commands.add_parser("inspect", help="inspect authoritative remote state")
    inspect.add_argument("run_id")

    health = commands.add_parser(
        "health", help="read cached health; legacy workers use compatibility probes"
    )
    health.add_argument("run_id")
    health.add_argument("--phase", choices=("first_step", "periodic", "completion"), required=True)

    wait = commands.add_parser(
        "wait", help="observe silently until terminal state or required attention"
    )
    wait.add_argument("run_id")
    wait.add_argument(
        "--poll-seconds",
        type=float,
        default=600,
        help="client observation window; does not change worker health sampling",
    )
    wait.add_argument(
        "--after-event", help="explicit run-bound event cursor; active alerts replay when omitted"
    )
    wait.add_argument(
        "--full-output", action="store_true", help="include full binding and verdict details"
    )
    wait.add_argument(
        "--max-wait-seconds",
        type=float,
        default=900,
        help="observer budget; 0 waits indefinitely; timeout preserves the remote run",
    )

    pull = commands.add_parser("pull", help="pull manifest-declared artifacts")
    pull.add_argument("run_id")
    pull.add_argument(
        "--diagnostic",
        action="store_true",
        help="snapshot bounded logs and state, including failed or running workloads",
    )

    resume = commands.add_parser("resume", help="rebuild local index from remote control state")
    resume.add_argument("--profile", required=True)
    resume.add_argument("--control-path", required=True)

    abort = commands.add_parser("abort", help="ownership-check and stop this run only")
    abort.add_argument("run_id")
    abort.add_argument("--yes", action="store_true", help="confirm abort")
    return parser


def installation_info() -> dict[str, Any]:
    package = Path(__file__).resolve().parent
    return {
        "ok": True,
        "version": __version__,
        "implementation": str(package),
        "implementation_sha256": sha256_json(
            {path.name: sha256_file(path) for path in sorted(package.glob("*.py"))}
        ),
        "backends": ["process"],
        "capabilities": [
            "environment-preflight",
            "gpu-leases",
            "observer-deadline",
            "idempotent-pull",
            "worker-monitoring",
            "remote-finalization",
            "cached-health",
            "bounded-observe",
            "monitor-events",
            "unknown-operation-outcome",
            "bounded-control-output",
            "cli-result-envelope",
            "connection-doctor",
        ],
        "monitor_protocols": [PROTOCOL],
        "cli_schema_version": CLI_SCHEMA,
        "control_output_limit_bytes": CONTROL_OUTPUT_LIMIT,
        "wait_default_seconds": 900,
        "wait_indefinite_value": 0,
        "server_health_checked": False,
        "wait_exit_codes": {
            "completed": 0,
            "failed_or_aborted": 1,
            "attention_or_error": 2,
            "observer_timeout": 124,
        },
    }


def _envelope(
    operation: str,
    status: str,
    *,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    run_id: str | None = None,
    ok: bool = True,
    control_output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # ready/doctor 的旧顶层字段仍是现有 wrapper 的发现接口。
    value = dict(result or {}) if operation in {"ready", "doctor"} else {}
    value.update(
        {
            "schema_version": CLI_SCHEMA,
            "operation": operation,
            "status": status,
            "run_id": run_id,
            "ok": ok,
            "result": result or {},
            "error": error or {},
        }
    )
    if control_output and not error:
        value["control_output"] = control_output
    return value


def _operation_hint(argv: list[str]) -> str:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"--profiles", "--state-root"}:
            index += 2
            continue
        if not token.startswith("-"):
            return token if token in OPERATIONS else "unknown"
        index += 1
    return "unknown"


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    machine = "--json" in tokens
    operation = _operation_hint(tokens)
    run_id = None
    controller = None
    try:
        args = build_parser(machine=machine).parse_args(tokens)
        operation = args.command
        run_id = getattr(args, "run_id", None)
        controller = Controller(profiles_path=args.profiles, state_root=args.state_root)
        code = 0
        status = "succeeded"
        ok = True
        if operation == "doctor":
            if not args.profile and (args.env_file or args.offline):
                raise RRCError("doctor_profile_required", "doctor flags require --profile", "cli")
            value = installation_info()
            if args.profile:
                value.update(
                    controller.doctor(args.profile, env_file=args.env_file, offline=args.offline)
                )
                if not value["ok"]:
                    code, status, ok = 2, "failed", False
        elif operation in {"ready", "launch"}:
            spec = RunSpec.from_path(args.run_spec)
            run_id = spec.run_id
            if operation == "ready":
                value = controller.ready(spec, offline=args.offline)
                if not value.get("ready"):
                    code, status, ok = 1, "failed", False
            else:
                value = controller.launch(spec, max_wait_seconds=args.max_wait_seconds)
        elif operation == "inspect":
            value = controller.inspect(args.run_id)
            if value.get("observation") in {"attention", "unavailable"}:
                status = "attention"
        elif operation == "health":
            value = controller.health(args.run_id, phase=args.phase)
            if value.get("status") in {"unhealthy", "unavailable", "starting", "stale"}:
                status = "attention"
        elif operation == "wait":
            value = controller.wait(
                args.run_id,
                poll_seconds=args.poll_seconds,
                max_wait_seconds=args.max_wait_seconds,
                after_event=args.after_event,
            )
            run_status = value.get("status")
            state = run_status.get("state") if isinstance(run_status, dict) else None
            if value.get("observation") == "timeout":
                code, status = 124, "attention"
            elif state in {"failed", "aborted"}:
                code, status, ok = 1, "failed", False
            elif value.get("observation") == "attention":
                code, status, ok = 2, "attention", False
            elif state != "completed":
                raise RRCError(
                    "wait_result_incomplete",
                    "wait did not produce an authoritative result",
                    "observer",
                )
            if not args.full_output:
                value = _compact_wait(value, controller)
        elif operation == "pull":
            value = controller.pull(args.run_id, diagnostic=args.diagnostic)
        elif operation == "resume":
            value = controller.resume(profile_name=args.profile, control_root=args.control_path)
        elif operation == "abort":
            value = controller.abort(args.run_id, confirmed=args.yes)
        else:
            raise AssertionError(operation)
        run_id = run_id or value.get("run_id")
        error = None
        if operation in {"doctor", "ready"} and status == "failed":
            error = {
                "code": operation + "_checks_failed",
                "message": "see the failed checks in result",
                "phase": operation,
                "outcome": "failed",
                "retryable": False,
                "next_actions": [{"argv": ["rrctl", operation, "--help"]}],
            }
        _emit(
            _envelope(
                operation,
                status,
                result=value,
                error=error,
                run_id=run_id,
                ok=ok,
                control_output=getattr(controller, "control_output", None),
            ),
            machine=machine,
        )
        return code
    except RRCError as exc:
        run_id = run_id or exc.details.get("run_id")
        status = exc.outcome or ("attention" if exc.phase in {"observer", "health"} else "failed")
        if exc.code in {"first_step_terminal", "completion_health", "launch_budget", "wait_budget"}:
            status = "failed"
        error = exc.to_dict()
        error.setdefault("outcome", status)
        error.setdefault("retryable", False)
        if not error.get("next_actions"):
            error["next_actions"] = (
                controller.recovery_actions(run_id)
                if run_id and hasattr(controller, "recovery_actions")
                else [
                    {"argv": ["rrctl", *([operation] if operation in OPERATIONS else []), "--help"]}
                ]
            )
        _emit(
            _envelope(operation, status, error=redact_data(error), run_id=run_id, ok=False),
            machine=machine,
        )
        return 2
    except Exception as exc:
        _emit(
            _envelope(
                operation,
                "failed",
                run_id=run_id,
                ok=False,
                error={
                    "code": "unhandled",
                    "message": redact(str(exc)),
                    "phase": "cli",
                    "outcome": "failed",
                    "retryable": False,
                    "next_actions": [{"argv": ["rrctl", "--help"]}],
                },
            ),
            machine=machine,
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
