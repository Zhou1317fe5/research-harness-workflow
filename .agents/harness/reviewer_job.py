#!/usr/bin/env python3
"""Run one persistent, resumable independent pre-run reviewer job."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERDICT_SCHEMA = "prerun.scientific-verdict.v1"
JOB_SCHEMA = "prerun.reviewer-job.v1"
RESULTS = {
    "scientific_review": {
        "scientifically_correct",
        "scientifically_incorrect",
        "not_evaluable",
    },
    "targeted_review": {
        "targeted_correct",
        "targeted_incorrect",
        "not_evaluable",
    },
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def validate_packet(packet_path: Path) -> tuple[dict[str, Any], str]:
    packet = load_object(packet_path, "review packet")
    if packet.get("schema_version") != "prerun.scientific-review.v1":
        raise ValueError("review packet schema is not prerun.scientific-review.v1")
    mode = packet.get("review_mode")
    if mode not in RESULTS:
        raise ValueError("review packet has an invalid review_mode")
    commit = packet.get("pre_run_code_commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise ValueError("review packet has an invalid pre_run_code_commit")
    repo = Path(str(packet.get("repo_root", ""))).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError("review packet repo_root is not a directory")
    scripts = Path(__file__).resolve().parents[1] / "skills/pre-run-implementation-review/scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from prerun_ready import validate_packet as validate_ready_packet
    readiness = validate_ready_packet(packet)
    if not readiness.get("ready"):
        raise ValueError("review packet is not ready: " + "; ".join(readiness.get("errors", [])))
    return packet, sha256_bytes(canonical_json(packet))


def output_schema(mode: str) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["reviewer_id", "review_mode", "result", "decision", "report_markdown"],
        "properties": {
            "reviewer_id": {"type": "string", "minLength": 1},
            # 严格 provider 要求每个属性带显式 type；enum/const 与 type 并存不改变取值范围。
            "review_mode": {"type": "string", "const": mode},
            "result": {"type": "string", "enum": sorted(RESULTS[mode])},
            "decision": {"type": "string", "enum": ["allow_run", "do_not_run"]},
            "report_markdown": {"type": "string", "minLength": 1},
        },
    }


def parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "result" in value:
            found = value
    return found


def validate_response(value: dict[str, Any] | None, mode: str) -> str | None:
    if value is None:
        return "reviewer did not return a JSON object"
    result = value.get("result")
    if value.get("review_mode") != mode or result not in RESULTS[mode]:
        return "reviewer result does not match the requested review mode"
    decision = value.get("decision")
    expected = "allow_run" if result in {"scientifically_correct", "targeted_correct"} else "do_not_run"
    if decision != expected:
        return f"reviewer decision must be {expected} for result {result}"
    if not isinstance(value.get("reviewer_id"), str) or not value["reviewer_id"].strip():
        return "reviewer_id is missing"
    if not isinstance(value.get("report_markdown"), str) or not value["report_markdown"].strip():
        return "report_markdown is missing"
    return None


def command_for(
    backend: str,
    cwd: Path,
    job_dir: Path,
    execution: int,
    session_id: str | None,
    output_path: Path,
    schema_path: Path,
    prompt_path: Path,
    model: str | None,
) -> tuple[list[str], str]:
    if backend == "codex":
        executable = shutil.which("codex")
        if not executable:
            raise ValueError("codex executable is unavailable")
        if session_id:
            argv = [
                executable, "exec", "resume", "--json", "--output-schema",
                str(schema_path), "--output-last-message", str(output_path),
            ]
            if model:
                argv += ["--model", model]
            argv += [session_id, "-"]
            return argv, "Continue the same review after the transport interruption. Return the required JSON verdict."
        argv = [
            executable, "exec", "--json", "--sandbox", "read-only", "--cd",
            str(cwd), "--output-schema", str(schema_path),
            "--output-last-message", str(output_path),
        ]
        if model:
            argv += ["--model", model]
        argv.append("-")
        return argv, ""

    executable = shutil.which("pi")
    if not executable:
        raise ValueError("pi executable is unavailable")
    session_path = job_dir / f"pi-session-{execution}.jsonl"
    base = [
        executable, "--mode", "text", "--print", "--session", str(session_path),
        "--tools", "read,grep,find,ls", "--no-extensions", "--no-skills",
        "--no-context-files", "--system-prompt",
        "You are an independent, read-only scientific implementation reviewer. Follow the supplied task exactly and return only the required JSON object.",
    ]
    if model:
        base += ["--model", model]
    if session_id:
        base.append("Continue the same review after the transport interruption. Return the required JSON verdict.")
        return base, ""
    base.append(f"@{prompt_path}")
    return base, ""


def observed_model_from_events(event_path: Path) -> str | None:
    """Extract the model identity from trusted transport events only."""
    trusted_types = {
        "thread.started",
        "session_meta",
        "session_metadata",
        "turn.started",
        "response.started",
        # codex CLI (0.155.x) records the effective model in these events;
        # confirmed from local session logs.
        "thread_settings_applied",
        "turn_context",
    }
    observed: str | None = None
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type not in trusted_types and payload.get("type") not in trusted_types:
            continue
        containers = [event, payload]
        settings = payload.get("thread_settings") if isinstance(payload, dict) else None
        if isinstance(settings, dict):
            containers.append(settings)
        for container in containers:
            if not isinstance(container, dict):
                continue
            for key in ("model", "model_id"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    observed = value.strip()
    return observed


def run_process(
    argv: list[str],
    prompt: str,
    event_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    on_session,
    cwd: Path,
) -> tuple[int, str, bool]:
    lines: list[str] = []
    timed_out = False
    with event_path.open("a", encoding="utf-8") as events, stderr_path.open(
        "a", encoding="utf-8"
    ) as errors:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            bufsize=1,
            cwd=cwd,
        )
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(prompt)
        process.stdin.close()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        started = time.monotonic()
        while True:
            if time.monotonic() - started >= timeout_seconds:
                timed_out = True
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                break
            ready = selector.select(timeout=1)
            for key, _ in ready:
                line = key.fileobj.readline()
                if not line:
                    continue
                events.write(line)
                events.flush()
                lines.append(line)
                if len(lines) > 1000:
                    del lines[:500]
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                    on_session(event["thread_id"])
            if process.poll() is not None:
                remainder = process.stdout.read()
                if remainder:
                    events.write(remainder)
                    lines.append(remainder)
                break
        selector.close()
        return process.wait(), "".join(lines), timed_out


def reviewer_prompt(task: str, mode: str) -> str:
    allowed = ", ".join(sorted(RESULTS[mode]))
    return (
        task.rstrip()
        + "\n\nTransport output contract: return only one JSON object with keys "
        "reviewer_id, review_mode, result, decision, report_markdown. "
        f"review_mode must be {mode}; result must be one of {allowed}. "
        "Use allow_run only for a correct result; every other result uses do_not_run.\n"
    )


def _reviewer_job_children_alive() -> bool:
    """Return True if a live descendant looks like a reviewer transport child."""
    me = str(os.getpid())
    # Match the real transport argv: `codex exec ...` or `pi --mode text|json ...`.
    # (command_for launches pi with `--mode text --print --session`; an earlier
    # `--mode json` pattern never matched live pi children and would misjudge a
    # live review as dead.)
    pattern = re.compile(r"codex\s+exec|\bpi\b.*--mode\s+(?:text|json)\b")
    try:
        with subprocess.Popen(
            ["ps", "-eo", "ppid=,args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as proc:
            out, _ = proc.communicate(timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot inspect: assume alive, never auto-fail a live review
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] == me and pattern.search(parts[1]):
            return True
    return False


def execute(args: argparse.Namespace) -> int:
    packet_path = args.packet.resolve()
    task_path = args.task.resolve()
    job_dir = args.job_dir.resolve()
    packet, packet_sha = validate_packet(packet_path)
    cwd = Path(packet["repo_root"]).resolve()
    if args.cwd and args.cwd.resolve() != cwd:
        raise ValueError("--cwd must equal packet.repo_root")
    for label, path in (
        ("packet", packet_path),
        ("task", task_path),
        ("job-dir", job_dir),
    ):
        if not path.is_relative_to(cwd):
            raise ValueError(f"--{label} must stay inside packet.repo_root")
    try:
        task_bytes = task_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"review task unreadable: {exc}") from exc
    task_text = task_bytes.decode("utf-8")
    task_sha = sha256_bytes(task_bytes)
    mode = packet["review_mode"]
    job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = job_dir / "job.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("reviewer job is already running") from exc

        state_path = job_dir / "job.json"
        verdict_path = job_dir / "verdict.json"
        if verdict_path.is_file():
            verdict = load_object(verdict_path, "verdict artifact")
            if (
                verdict.get("schema_version") == VERDICT_SCHEMA
                and verdict.get("packet_sha256") == packet_sha
                and verdict.get("task_sha256") == task_sha
            ):
                print(json.dumps({"status": "completed", "verdict": str(verdict_path)}, ensure_ascii=False))
                return 0
            raise ValueError("existing verdict artifact belongs to different review inputs")

        if state_path.is_file():
            state = load_object(state_path, "reviewer job state")
            identity = (state.get("backend"), state.get("packet_sha256"), state.get("task_sha256"))
            if identity != (args.backend, packet_sha, task_sha):
                raise ValueError("existing reviewer job belongs to different inputs")
            if state.get("status") == "running" and not _reviewer_job_children_alive():
                # A prior runner was killed externally (e.g. pkill); its recorded
                # session stays resumable, but the attempt itself is dead.
                state.update(
                    status="service_failed",
                    last_error="runner killed externally; resuming recorded session",
                    updated_at=now(),
                )
                atomic_json(state_path, state)
        else:
            state = {
                "schema_version": JOB_SCHEMA,
                "backend": args.backend,
                "packet_path": str(packet_path),
                "packet_sha256": packet_sha,
                "task_path": str(task_path),
                "task_sha256": task_sha,
                "candidate_commit": packet["pre_run_code_commit"],
                "review_mode": mode,
                "replacement": 0,
                "resumes_used": 0,
                "session_id": None,
                "status": "pending",
                "created_at": now(),
            }
            atomic_json(state_path, state)

        schema_path = job_dir / "response-schema.json"
        atomic_json(schema_path, output_schema(mode))
        initial_prompt = reviewer_prompt(task_text, mode)
        prompt_path = job_dir / "review-task.txt"
        prompt_path.write_text(initial_prompt, encoding="utf-8")
        os.chmod(prompt_path, 0o600)
        last_error = "review service did not return a verdict"
        while state["replacement"] <= args.max_replacements:
            session_id = state.get("session_id")
            is_resume = bool(session_id)
            if is_resume and state["resumes_used"] >= args.max_resumes:
                state.update(
                    replacement=state["replacement"] + 1,
                    resumes_used=0,
                    session_id=None,
                    status="replacement_pending",
                    updated_at=now(),
                )
                atomic_json(state_path, state)
                continue

            execution = int(state["replacement"])
            sequence = int(state.get("invocations", 0)) + 1
            raw_path = job_dir / f"response-{execution}-{sequence}.json"
            event_path = job_dir / f"events-{execution}.jsonl"
            stderr_path = job_dir / f"stderr-{execution}.log"
            argv, continuation = command_for(
                args.backend, cwd, job_dir, execution, session_id, raw_path,
                schema_path, prompt_path, args.model,
            )
            prompt = continuation or (initial_prompt if args.backend == "codex" else "")
            state.update(status="running", invocations=sequence, updated_at=now())
            if is_resume:
                state["resumes_used"] += 1
            atomic_json(state_path, state)

            def record_session(value: str) -> None:
                state["session_id"] = value
                state["updated_at"] = now()
                atomic_json(state_path, state)

            returncode, stdout, timed_out = run_process(
                argv, prompt, event_path, stderr_path,
                args.attempt_timeout_seconds, record_session, cwd,
            )
            pi_session = job_dir / f"pi-session-{execution}.jsonl"
            if args.backend == "pi" and state.get("session_id") is None and pi_session.is_file():
                record_session(str(job_dir / f"pi-session-{execution}.jsonl"))
            if not raw_path.is_file():
                raw_path.write_text(stdout, encoding="utf-8")
                os.chmod(raw_path, 0o600)
            response_text = raw_path.read_text(encoding="utf-8")
            response = parse_json_object(response_text)
            last_error = validate_response(response, mode) or ""
            if not last_error and response is not None:
                raw_sha = sha256_bytes(response_text.encode("utf-8"))
                verdict = {
                    "schema_version": VERDICT_SCHEMA,
                    "status": "completed",
                    "backend": args.backend,
                    "reviewer_session_id": state.get("session_id"),
                    "reviewer_id": response["reviewer_id"].strip(),
                    "requested_model": args.model,
                    "observed_model": observed_model_from_events(event_path) or "unknown",
                    "model_evidence": "event-stream" if observed_model_from_events(event_path) else "unknown",
                    "review_mode": mode,
                    "result": response["result"],
                    "decision": response["decision"],
                    "candidate_commit": packet["pre_run_code_commit"],
                    "packet_path": str(packet_path),
                    "packet_sha256": packet_sha,
                    "task_path": str(task_path),
                    "task_sha256": task_sha,
                    "raw_response_path": str(raw_path),
                    "raw_response_sha256": raw_sha,
                    "report_markdown": response["report_markdown"],
                    "replacement_count": state["replacement"],
                    "resume_count": state["resumes_used"],
                    "transport_exit_code": returncode,
                    "completed_at": now(),
                }
                atomic_json(verdict_path, verdict)
                state.update(status="completed", verdict_path=str(verdict_path), updated_at=now())
                atomic_json(state_path, state)
                print(json.dumps({"status": "completed", "verdict": str(verdict_path)}, ensure_ascii=False))
                return 0

            last_error = (
                "review attempt timed out" if timed_out else
                last_error or f"review process exited {returncode}"
            )
            state.update(status="service_failed", last_error=last_error, updated_at=now())
            atomic_json(state_path, state)
            if not state.get("session_id"):
                state["resumes_used"] = args.max_resumes

        state.update(status="capability_gap", last_error=last_error, updated_at=now())
        atomic_json(state_path, state)
        print(json.dumps({
            "status": "capability_gap", "error": last_error,
            "job": str(state_path), "verdict": None,
        }, ensure_ascii=False))
        return 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("codex", "pi"), required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--max-resumes", type=int, default=3)
    parser.add_argument("--max-replacements", type=int, default=1)
    parser.add_argument("--attempt-timeout-seconds", type=float, default=1800)
    args = parser.parse_args()
    try:
        if args.max_resumes < 0 or args.max_replacements < 0 or args.attempt_timeout_seconds <= 0:
            raise ValueError("retry counts must be nonnegative and timeout must be positive")
        return execute(args)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"[reviewer-job] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
