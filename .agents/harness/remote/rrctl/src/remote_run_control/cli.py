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
from .models import RunSpec
from .security import redact


def _emit(value: dict[str, Any], *, machine: bool) -> None:
    if machine:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rrctl",
        description="Daemonless fail-closed control plane for remote workloads",
    )
    parser.add_argument("--json", action="store_true", help="emit compact machine-readable JSON")
    parser.add_argument("--profiles", type=Path, help="credential profile JSON path")
    parser.add_argument("--state-root", type=Path, help="local run index root")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "doctor", help="show the installed implementation and supported capabilities"
    )

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

    health = commands.add_parser("health", help="run a health phase")
    health.add_argument("run_id")
    health.add_argument("--phase", choices=("first_step", "periodic", "completion"), required=True)

    wait = commands.add_parser("wait", help="poll until terminal state or health failure")
    wait.add_argument("run_id")
    wait.add_argument("--poll-seconds", type=float, default=600)
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        _emit(
            {
                "ok": True,
                "version": __version__,
                "implementation": str(Path(__file__).resolve().parent),
                "backends": ["process"],
                "capabilities": [
                    "environment-preflight",
                    "gpu-leases",
                    "observer-deadline",
                    "idempotent-pull",
                ],
                "wait_exit_codes": {
                    "completed": 0,
                    "failed_or_aborted": 1,
                    "attention_or_error": 2,
                    "observer_timeout": 124,
                },
            },
            machine=args.json,
        )
        return 0
    controller = Controller(profiles_path=args.profiles, state_root=args.state_root)
    try:
        if args.command == "ready":
            value = controller.ready(RunSpec.from_path(args.run_spec), offline=args.offline)
            _emit(value, machine=args.json)
            return 0 if value.get("ready") else 1
        if args.command == "launch":
            value = controller.launch(
                RunSpec.from_path(args.run_spec), max_wait_seconds=args.max_wait_seconds
            )
        elif args.command == "inspect":
            value = controller.inspect(args.run_id)
        elif args.command == "health":
            value = controller.health(args.run_id, phase=args.phase)
        elif args.command == "wait":
            value = controller.wait(
                args.run_id, poll_seconds=args.poll_seconds, max_wait_seconds=args.max_wait_seconds
            )
        elif args.command == "pull":
            value = controller.pull(args.run_id, diagnostic=args.diagnostic)
        elif args.command == "resume":
            value = controller.resume(profile_name=args.profile, control_root=args.control_path)
        elif args.command == "abort":
            value = controller.abort(args.run_id, confirmed=args.yes)
        else:
            raise AssertionError(args.command)
        run_status = value.get("status") if isinstance(value, dict) else None
        state = run_status.get("state") if isinstance(run_status, dict) else None
        failed = args.command == "wait" and state in {"failed", "aborted"}
        _emit({"ok": not failed, "result": value}, machine=args.json)
        if args.command == "wait" and value.get("observation") == "timeout":
            return 124
        if failed:
            return 1
        return 0
    except RRCError as exc:
        _emit({"ok": False, "error": exc.to_dict()}, machine=args.json)
        return 2
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "unhandled",
                    "message": redact(str(exc)),
                    "phase": "cli",
                },
            },
            machine=args.json,
        )
        return 3


if __name__ == "__main__":
    sys.exit(main())
