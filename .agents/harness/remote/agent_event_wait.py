#!/usr/bin/env python3
"""Wait for one rrctl terminal/attention event and wake a Codex thread once."""

from __future__ import annotations

# 直接运行脚本与通过包导入使用同一实现。
if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from harness.remote.agent_event_wait import main as _main

    raise SystemExit(_main())

import argparse
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.common.primitives import atomic_json, now  # noqa: E402


SCHEMA = "rrctl.agent-event-wait.v1"
THREAD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
PROGRESS_PREFIX = b"[rrctl-progress] "
CODEX_PROGRESS_INTERVAL_SECONDS = 1800


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
    path = state_path(runspec, run_id, thread)
    # 已投递的 attention 事件仍在等待执行者处理时，不启动第二个 relay：重复唤醒
    # 只会让执行者对同一 RunID 做重复诊断。复用判断不查远端状态（保持 start 快速）。
    if path.is_file():
        try:
            previous = load_json(path)
        except (OSError, ValueError):
            previous = {}
        if previous.get("status") == "attention":
            print(json.dumps({
                "status": "attention_pending", "reused": True,
                "run_id": run_id, "state_path": str(path),
                "message": "An attention event for this RunID was already delivered; inspect it before starting another relay.",
            }, ensure_ascii=False))
            return 0
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
            if previous.get("status") == "attention":
                print(json.dumps({
                    "status": "attention_pending", "reused": True,
                    "run_id": run_id, "state_path": str(path),
                }, ensure_ascii=False))
                return 0
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
        "message": "No model polling is required. Terminal/attention events queue this thread immediately; changed progress may queue at most once per 30 minutes (Codex queue has no non-waking notification mode).",
    }, ensure_ascii=False))
    return 0


def queue_message(codex: str, thread: str, message: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [codex, "queue", "--thread", thread, "--message", message],
        capture_output=True, text=True, check=False, timeout=10,
    )


def observe(command: list[str], state: dict[str, Any], args: argparse.Namespace) -> tuple[int, str, str]:
    """Drain both pipes continuously; retain bounded tails and relay real progress changes."""
    tails = {"stdout": b"", "stderr": b""}
    line = b""
    dropping = False
    last_signature = None
    last_notification = float("-inf")
    codex = shutil.which("codex")

    def progress(frame: bytes) -> None:
        nonlocal last_signature, last_notification
        if not frame.startswith(PROGRESS_PREFIX):
            return
        try:
            event = json.loads(frame[len(PROGRESS_PREFIX):])
        except (ValueError, UnicodeError):
            return
        if not isinstance(event, dict) or event.get("schema_version") != "rrctl.progress.v1" or event.get("run_id") != state["run_id"] or event.get("stage") != "wait":
            return
        message = event.get("message")
        if not isinstance(message, str) or not message.strip():
            return
        # Elapsed time / ETA changes alone must not wake Codex. Unlike Pi, queue has no
        # display-only flag (checked via `codex queue --help`); don't invent one.
        snapshot = event.get("progress", event)
        if not isinstance(snapshot, dict) or not any(snapshot.get(key) is not None for key in (
            "state", "current_step", "total_steps",
        )):
            return  # A failed heartbeat query is not a real progress change.
        signature = json.dumps({key: snapshot.get(key) for key in (
            "state", "current_step", "step_field", "total_steps", "metrics",
        )}, sort_keys=True, ensure_ascii=False)
        current = time.monotonic()
        if signature == last_signature or current - last_notification < CODEX_PROGRESS_INTERVAL_SECONDS:
            return
        if not codex:
            state["progress_notification_error"] = "codex executable unavailable"
            return
        last_notification = current
        try:
            result = queue_message(codex, args.thread, (
                f"rrctl progress for RunID {state['run_id']}: {message}\n"
                "仅为低频进度通知（Codex queue 会唤醒会话）；无需轮询、重启或重新 launch，继续等待终态事件。"
            ))
            if result.returncode:
                state["progress_notification_error"] = result.stderr[-2000:]
            else:
                last_signature = signature
                state["progress_notified_at"] = now()
        except (OSError, subprocess.SubprocessError) as exc:
            state["progress_notification_error"] = str(exc)
        atomic_json(args.state.resolve(), state)

    log_path = Path(state.get("log_path", args.state.with_suffix(".log")))
    with log_path.open("ab") as log, subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as process, selectors.DefaultSelector() as selector:
        assert process.stdout is not None and process.stderr is not None  # PIPE above guarantees both.
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            for key, _ in selector.select():
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    if key.data == "stderr" and line and not dropping:
                        progress(line)
                    continue
                log.write(chunk)
                log.flush()
                name = key.data
                tails[name] = (tails[name] + chunk)[-(12000 if name == "stdout" else 4000):]
                if name != "stderr":
                    continue
                parts = chunk.split(b"\n")
                for index, part in enumerate(parts):
                    if not dropping:
                        if len(line) + len(part) > 65536:
                            line, dropping = b"", True
                        else:
                            line += part
                    if index < len(parts) - 1:
                        if not dropping:
                            progress(line)
                        line, dropping = b"", False
        return process.wait(), tails["stdout"].decode("utf-8", errors="replace"), tails["stderr"].decode("utf-8", errors="replace")


def run_child(args: argparse.Namespace) -> int:
    state_path_value = args.state.resolve()
    for _ in range(50):
        if state_path_value.is_file():
            break
        time.sleep(0.1)
    state = load_json(state_path_value)
    script = Path(__file__).resolve().with_name("remote_run.py")
    command = [
        sys.executable, str(script), str(args.runspec.resolve()), "--execute", "--observe-only",
        "--poll-seconds", str(args.poll_seconds), "--max-wait-seconds", "0",
    ]
    if args.resume:
        command.append("--resume")
    if args.profiles:
        command += ["--profiles", str(args.profiles.resolve())]
    try:
        exit_code, output, error = observe(command, state, args)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        exit_code, output, error = 2, "", str(exc)
    event_path = state_path_value.with_suffix(".event.json")
    event = {
        "schema_version": "rrctl.agent-event.v1",
        "run_id": state["run_id"],
        "kind": "terminal" if exit_code == 0 else "attention",
        "exit_code": exit_code,
        "output": output,
        "error": error,
        "created_at": now(),
    }
    atomic_json(event_path, event)
    state.update(
        status="terminal" if exit_code == 0 else "attention",
        exit_code=exit_code,
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
        f"Inspect {event_path}. "
        + ("Compute is terminal; continue pull/ingest now (artifacts have not been pulled). " if exit_code == 0 else "Diagnose the existing run and pull diagnostic artifacts as needed. ")
        + "Do not relaunch the RunID."
    )
    try:
        notified = queue_message(codex, args.thread, message)
    except (OSError, subprocess.SubprocessError) as exc:
        state.update(status="notification_failed", notification_error=str(exc))
        atomic_json(state_path_value, state)
        return 4
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
