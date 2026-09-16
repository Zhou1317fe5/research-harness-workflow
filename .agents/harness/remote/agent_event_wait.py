#!/usr/bin/env python3
"""Wait for one rrctl terminal/attention event and wake a Codex thread once."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "rrctl.agent-event-wait.v1"
THREAD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def process_start(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except (OSError, UnicodeError):
        return None
    return fields[21] if len(fields) > 21 else None


def running(state: dict[str, Any]) -> bool:
    pid = state.get("pid")
    start = state.get("process_start")
    return isinstance(pid, int) and isinstance(start, str) and process_start(pid) == start


def state_path(runspec: Path, run_id: str, thread: str) -> Path:
    key = hashlib.sha256(f"{runspec}\0{thread}".encode()).hexdigest()[:20]
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return root / "rrctl/agent-waits" / f"{run_id}-{key}.json"


def start(args: argparse.Namespace) -> int:
    runspec = args.runspec.resolve()
    spec = load_json(runspec)
    run_id = spec.get("run_id")
    if spec.get("schema_version") != "rrctl.run.v1" or not isinstance(run_id, str):
        raise ValueError("expected a valid rrctl.run.v1 RunSpec")
    thread = args.thread or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    if not thread or not THREAD_RE.fullmatch(thread):
        raise ValueError("a valid --thread or CODEX_THREAD_ID/CODEX_SESSION_ID is required")
    codex = shutil.which("codex")
    if not codex:
        raise ValueError("codex executable is unavailable")
    capability = subprocess.run(
        [codex, "queue", "--help"], capture_output=True, text=True,
        check=False, timeout=10,
    )
    if capability.returncode:
        raise ValueError("this Codex installation does not support thread queue wake-up")
    path = state_path(runspec, run_id, thread)
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.is_file():
            previous = load_json(path)
            if previous.get("status") == "waiting" and running(previous):
                print(json.dumps({
                    "status": "waiting", "reused": True,
                    "run_id": run_id, "state_path": str(path),
                }, ensure_ascii=False))
                return 0
        log_path = path.with_suffix(".log")
        command = [
            sys.executable, str(Path(__file__).resolve()), "_run",
            str(runspec), "--thread", thread, "--state", str(path),
            "--poll-seconds", str(args.poll_seconds),
        ]
        if args.resume:
            command.append("--resume")
        if args.profiles:
            command += ["--profiles", str(args.profiles.resolve())]
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        state = {
            "schema_version": SCHEMA,
            "status": "waiting",
            "run_id": run_id,
            "runspec": str(runspec),
            "thread": thread,
            "pid": process.pid,
            "process_start": process_start(process.pid),
            "resume": args.resume,
            "log_path": str(log_path),
            "created_at": now(),
        }
        atomic_json(path, state)
    print(json.dumps({
        "status": "waiting", "reused": False, "run_id": run_id,
        "state_path": str(path),
        "message": "No model polling is required; this thread will be queued once on a terminal or attention event.",
    }, ensure_ascii=False))
    return 0


def run_child(args: argparse.Namespace) -> int:
    state_path_value = args.state.resolve()
    for _ in range(50):
        if state_path_value.is_file():
            break
        time.sleep(0.1)
    state = load_json(state_path_value)
    script = Path(__file__).resolve().with_name("remote_run.py")
    command = [
        sys.executable, str(script), str(args.runspec.resolve()), "--execute",
        "--poll-seconds", str(args.poll_seconds), "--max-wait-seconds", "0",
    ]
    if args.resume:
        command.append("--resume")
    if args.profiles:
        command += ["--profiles", str(args.profiles.resolve())]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    event_path = state_path_value.with_suffix(".event.json")
    event = {
        "schema_version": "rrctl.agent-event.v1",
        "run_id": state["run_id"],
        "kind": "terminal" if result.returncode == 0 else "attention",
        "exit_code": result.returncode,
        "output": result.stdout[-12000:],
        "error": result.stderr[-4000:],
        "created_at": now(),
    }
    atomic_json(event_path, event)
    state.update(
        status="terminal" if result.returncode == 0 else "attention",
        exit_code=result.returncode,
        event_path=str(event_path),
        finished_at=now(),
    )
    atomic_json(state_path_value, state)
    codex = shutil.which("codex")
    if not codex:
        state.update(status="notification_failed", notification_error="codex executable unavailable")
        atomic_json(state_path_value, state)
        return 4
    message = (
        f"rrctl {event['kind']} event for RunID {state['run_id']}. "
        f"Inspect {event_path} and continue the existing workflow; do not relaunch the RunID."
    )
    notified = subprocess.run(
        [codex, "queue", "--thread", args.thread, "--message", message],
        capture_output=True, text=True, check=False,
    )
    state["notification_exit_code"] = notified.returncode
    state["notified_at"] = now()
    if notified.returncode:
        state["status"] = "notification_failed"
        state["notification_error"] = notified.stderr[-2000:]
    atomic_json(state_path_value, state)
    return 0 if notified.returncode == 0 else 4


def status(args: argparse.Namespace) -> int:
    value = load_json(args.state.resolve())
    value["process_running"] = running(value)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("runspec", type=Path)
    start_parser.add_argument("--thread")
    start_parser.add_argument("--profiles", type=Path)
    start_parser.add_argument("--poll-seconds", type=float, default=600)
    start_parser.add_argument("--resume", action="store_true")
    child = subparsers.add_parser("_run")
    child.add_argument("runspec", type=Path)
    child.add_argument("--thread", required=True)
    child.add_argument("--state", type=Path, required=True)
    child.add_argument("--profiles", type=Path)
    child.add_argument("--poll-seconds", type=float, default=600)
    child.add_argument("--resume", action="store_true")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("state", type=Path)
    args = parser.parse_args()
    try:
        if hasattr(args, "poll_seconds") and args.poll_seconds <= 0:
            raise ValueError("poll-seconds must be positive")
        return {"start": start, "_run": run_child, "status": status}[args.command](args)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[agent-event-wait] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
