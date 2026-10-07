#!/usr/bin/env python3
"""Run one persistent, resumable independent reviewer job.

Supports three review kinds:
- ``prerun``: pre-run implementation review (existing packet schema).
- ``result-analysis``: post-run scientific result analysis; output contract
  is the post-run result-analysis payload (four-section markdown embedded in
  JSON) and the verdict schema is ``post-run.result-analysis-verdict.v1``.
- ``closing``: closing vision review; model is supplied by the invoking
  session rather than the review contract (``--model-source invoker``).
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import importlib.util
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


def _load_review_model():
    """Per-host review model registry shared with the gates."""
    path = Path(__file__).resolve().parent / "review_model.py"
    spec = importlib.util.spec_from_file_location("review_model", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load review model registry: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


review_model = _load_review_model()


VERDICT_SCHEMA_PRERUN = "prerun.scientific-verdict.v1"
VERDICT_SCHEMA_RESULT_ANALYSIS = "post-run.result-analysis-verdict.v1"
VERDICT_SCHEMA_CLOSING = "closing.vision-verdict.v1"
VERDICT_SCHEMA_BY_KIND = {
    "prerun": VERDICT_SCHEMA_PRERUN,
    "result-analysis": VERDICT_SCHEMA_RESULT_ANALYSIS,
    "closing": VERDICT_SCHEMA_CLOSING,
}
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

RESULT_ANALYSIS_KEYS = {
    "exp_id",
    "run_ids",
    "analysis_markdown",
    "scientific_outcome",
    "limitations",
    "validation_gaps",
}
RESULT_ANALYSIS_OUTCOMES = {
    "hypothesis_supported",
    "hypothesis_not_supported",
    "gate_failed",
    "inconclusive",
    "not_applicable",
}
CLOSING_RESULTS = {"pass", "issues_found", "not_evaluable"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_plus(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()


# Quota wait cap: at most three cumulative hours before surfacing to the caller.
MAX_QUOTA_WAIT_SECONDS = 3 * 3600

# Failure classes that terminate the review immediately (no transport retry).
_CONFIG_ERROR_MARKERS = (
    "ambiguous across providers",
    "not authenticated",
    "unauthorized",
    "invalid api key",
    "usage error",
)

# Quota/cap markers. `429` alone is transient rate limit unless the text
# explicitly signals usage/cap/monthly exhaustion.
_QUOTA_CAP_MARKERS = (
    "usage limit", "quota", "cap_error", "inference_cap", "monthly", "weekly",
    "credits", "billing", "insufficient_quota",
)


def _extract_service_error(stdout_text: str) -> str:
    """Extract ONLY the structured service error from codex/pi stdout.
    Checks:
    1. JSON lines with errorMessage field (structured codex errors).
    2. Lines that ONLY contain known service error patterns (stderr-like
       content that leaked into stdout).
    Never scans full task content or assistant text.
    """
    # Try structured JSON error extraction first.
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            msg = event.get("errorMessage") or event.get("error") or ""
            if isinstance(msg, str) and msg.strip():
                return msg
    # If no structured error, check for known service-error-only lines.
    for line in stdout_text.splitlines():
        line = line.strip()
        if any(m in line.lower() for m in (
            "ambiguous across providers", "not authenticated", "unauthorized",
            "invalid api key", "usage error", "usage limit", "quota",
            "cap_error", "inference_cap", "rate limit", "too many requests",
            "error 429", "try again",
        )):
            return line
    return ""


def classify_attempt_failure(
    stdout_error_text: str,
    stderr_delta: str,
    timed_out: bool,
) -> tuple[str, float | None, str]:
    """Classify one failed transport attempt.

    Uses ONLY:
    - stdout_error_text: structured service error extracted by
      _extract_service_error() from this attempt's stdout events.
    - stderr_delta: stderr increment from this attempt only.
    - timed_out flag.
    Never scans full stdout (avoids confusing task content with service errors).
    """
    text = f"{stdout_error_text}\n{stderr_delta}".lower()
    if timed_out:
        return "transient", None, "attempt timed out"
    if any(m in text for m in _CONFIG_ERROR_MARKERS):
        return "config_error", None, stderr_delta.strip() or stdout_error_text.strip() or "permanent configuration / authentication failure"
    retry = _parse_retry_after_seconds(text)
    if retry is not None:
        if any(m in text for m in _QUOTA_CAP_MARKERS):
            return "quota_exhausted", retry, stderr_delta.strip() or stdout_error_text.strip() or "quota/rate limit with reported recovery"
        return "transient", retry, stderr_delta.strip() or stdout_error_text.strip() or "HTTP 429 / rate limit"
    if any(m in text for m in _QUOTA_CAP_MARKERS):
        return "quota_exhausted", None, stderr_delta.strip() or stdout_error_text.strip() or "quota/rate limit without recovery time"
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "transient", None, stderr_delta.strip() or stdout_error_text.strip() or "HTTP 429 / rate limit"
    return "unknown", None, stderr_delta.strip() or stdout_error_text.strip() or "unclassified failure"


_RETRY_AFTER_PATTERNS = (
    (re.compile(r"try again in ~?(\d+)\s*min", re.IGNORECASE), 60),
    (re.compile(r"try again in ~?(\d+)\s*h(?!d)", re.IGNORECASE), 3600),
    (re.compile(r"resets? in ~?(\d+)d\s*(\d+)h", re.IGNORECASE), None),
    (re.compile(r"retry-after:\s*(\d+)", re.IGNORECASE), 1),
)


def _parse_retry_after_seconds(text: str) -> float | None:
    for pattern, multiplier in _RETRY_AFTER_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if multiplier is None:
            days, hours = int(match.group(1)), int(match.group(2))
            return float(days * 86400 + hours * 3600)
        value = int(match.group(1))
        return float(value * multiplier)
    return None


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


def validate_packet(packet_path: Path, review_kind: str) -> tuple[dict[str, Any], str]:
    packet = load_object(packet_path, "review packet")
    if review_kind == "prerun":
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

    if review_kind == "result-analysis":
        if packet.get("schema_version") != "post-run.result-analysis.v1":
            raise ValueError("review packet schema is not post-run.result-analysis.v1")
        exp_id = packet.get("exp_id")
        if not isinstance(exp_id, str) or not exp_id.strip():
            raise ValueError("review packet has an invalid exp_id")
        run_ids = packet.get("run_ids")
        if (
            not isinstance(run_ids, list)
            or not run_ids
            or any(not isinstance(item, str) or not item.strip() for item in run_ids)
            or len(run_ids) != len(set(run_ids))
        ):
            raise ValueError("review packet has invalid run_ids")
        repo = Path(str(packet.get("repo_root", ""))).expanduser().resolve()
        if not repo.is_dir():
            raise ValueError("review packet repo_root is not a directory")
        return packet, sha256_bytes(canonical_json(packet))

    if review_kind == "closing":
        if packet.get("schema_version") != "closing.vision-review.v1":
            raise ValueError("review packet schema is not closing.vision-review.v1")
        repo = Path(str(packet.get("repo_root", ""))).expanduser().resolve()
        if not repo.is_dir():
            raise ValueError("review packet repo_root is not a directory")
        return packet, sha256_bytes(canonical_json(packet))

    raise ValueError(f"unknown review kind: {review_kind}")


def output_schema(review_kind: str, mode: str | None = None) -> dict[str, Any]:
    if review_kind == "prerun":
        assert mode is not None
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
    if review_kind == "result-analysis":
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": sorted(RESULT_ANALYSIS_KEYS),
            "properties": {
                "exp_id": {"type": "string", "minLength": 1},
                "run_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                "analysis_markdown": {"type": "string", "minLength": 1},
                "scientific_outcome": {"type": "string", "enum": sorted(RESULT_ANALYSIS_OUTCOMES)},
                "limitations": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "validation_gaps": {"type": "array", "items": {"type": "string", "minLength": 1}},
            },
        }
    if review_kind == "closing":
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["reviewer_id", "result", "report_markdown", "gaps"],
            "properties": {
                "reviewer_id": {"type": "string", "minLength": 1},
                "result": {"type": "string", "enum": sorted(CLOSING_RESULTS)},
                "report_markdown": {"type": "string", "minLength": 1},
                "gaps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_ref", "evidence_ref", "why_it_matters", "suggested_followup_issue"],
                        "properties": {
                            "source_ref": {"type": "string", "minLength": 1},
                            "evidence_ref": {"type": "string", "minLength": 1},
                            "why_it_matters": {"type": "string", "minLength": 1},
                            "suggested_followup_issue": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        }
    raise ValueError(f"unknown review kind: {review_kind}")


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


def _validate_result_analysis_payload(value: dict[str, Any], packet: dict[str, Any]) -> str | None:
    if set(value) != RESULT_ANALYSIS_KEYS:
        return "result-analysis payload keys do not match the contract"
    if value.get("exp_id") != packet.get("exp_id"):
        return "result-analysis exp_id does not match the packet"
    run_ids = value.get("run_ids")
    packet_runs = packet.get("run_ids") or []
    if (
        not isinstance(run_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in run_ids)
        or len(run_ids) != len(set(run_ids))
        or set(run_ids) != set(packet_runs)
    ):
        return "result-analysis run_ids do not match the packet"
    if not isinstance(value.get("analysis_markdown"), str) or not value["analysis_markdown"].strip():
        return "analysis_markdown is missing"
    if value.get("scientific_outcome") not in RESULT_ANALYSIS_OUTCOMES:
        return "scientific_outcome is invalid"
    for field in ("limitations", "validation_gaps"):
        items = value.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            return f"{field} is invalid"
    return None


def _validate_closing_payload(value: dict[str, Any]) -> str | None:
    if value.get("result") not in CLOSING_RESULTS:
        return "closing result is invalid"
    if not isinstance(value.get("reviewer_id"), str) or not value["reviewer_id"].strip():
        return "reviewer_id is missing"
    if not isinstance(value.get("report_markdown"), str) or not value["report_markdown"].strip():
        return "report_markdown is missing"
    gaps = value.get("gaps")
    if not isinstance(gaps, list):
        return "gaps is invalid"
    for gap in gaps:
        if not isinstance(gap, dict):
            return "gap entry is not an object"
        for key in ("source_ref", "evidence_ref", "why_it_matters", "suggested_followup_issue"):
            if not isinstance(gap.get(key), str) or not gap[key].strip():
                return f"gap entry missing {key}"
    return None


def validate_response(
    value: dict[str, Any] | None,
    review_kind: str,
    mode: str | None,
    packet: dict[str, Any],
) -> str | None:
    if value is None:
        return "reviewer did not return a JSON object"
    if review_kind == "prerun":
        assert mode is not None
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
    if review_kind == "result-analysis":
        return _validate_result_analysis_payload(value, packet)
    if review_kind == "closing":
        return _validate_closing_payload(value)
    return f"unknown review kind: {review_kind}"


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
        "--tools", "read,grep,find,ls", "--no-skills",
        "--no-context-files", "--system-prompt",
        "You are an independent, read-only scientific implementation reviewer. Follow the supplied task exactly and return only the required JSON object.",
    ]
    # 审查会话与交互会话共用同一套 Pi 模型注册表（含扩展注册的 provider），所以
    # `/model` 里能选的模型都能直接作为审查模型。审查侧的约束只保留只读工具白名单、
    # --no-skills 与 --no-context-files，不做扩展发现禁用。
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


def observed_model_from_pi_session(session_path: Path) -> str | None:
    """Extract the effective model from the Pi session event stream.

    Only runtime-written top-level events are trusted; the reviewer's own message
    text is never parsed for identity. Pi records conversation content as
    ``{"type":"message",...}`` and establishes the session model as
    ``{"type":"model_change","provider":"<provider>","modelId":"<model>"}``
    (including the launch ``--model``). The Pi backend runs with ``--mode text``,
    so its captured stdout holds only the reviewer's answer; the session file is
    the only place this event exists.
    """
    observed: str | None = None
    try:
        lines = session_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "model_change":
            continue
        model_id = event.get("modelId")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        provider = event.get("provider")
        # The provider prefix is only missing sometimes. Real samples cover both
        # shapes: ``provider=<p>/modelId=<bare-name>`` needs the prefix, while
        # ``provider=<p>/modelId=<prefixed/name>`` already carries one and its
        # provider casing does not match that prefix.
        if isinstance(provider, str) and provider.strip() and "/" not in model_id:
            model_id = f"{provider.strip()}/{model_id}"
        observed = model_id
    return observed


def observed_model_from_codex_session(session_path: Path) -> str | None:
    """Extract the effective model from the Codex rollout session file.

    Codex CLI (0.155.x) writes rollout files under CODEX_HOME/sessions (default
    ``~/.codex/sessions``) named ``rollout-<ts>-<thread-id>.jsonl``; the runtime
    ``turn_context`` payload records the effective ``model``. Only this
    runtime-written rollout event is trusted -- the reviewer's own message text
    is never parsed for identity. Mirrors the Pi backend, which reads
    ``model_change`` from its session file.
    """
    try:
        lines = session_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    observed: str | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn_context":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        value = payload.get("model")
        if isinstance(value, str) and value.strip():
            observed = value.strip()
    return observed


def _codex_sessions_root() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home) / "sessions"
    return Path.home() / ".codex" / "sessions"


def find_codex_rollout(thread_id: str) -> Path | None:
    """Locate the rollout file for one thread under the Codex sessions root."""
    if not thread_id or not thread_id.strip():
        return None
    pattern = f"rollout-*-{glob.escape(thread_id.strip())}.jsonl"
    try:
        matches = sorted(_codex_sessions_root().rglob(pattern))
    except OSError:
        return None
    return matches[-1] if matches else None


def observed_model_for_backend(
    backend: str, event_path: Path, session_path: Path, thread_id: str | None = None
) -> tuple[str | None, str | None]:
    """Trusted runtime model identity from a backend's own transport artifacts.

    Returns ``(model, evidence_channel)``. The channel is ``event-stream`` when the
    identity came from the live transport event stream and ``session-metadata``
    when it came from the backend's own session/rollout file; downstream gates
    distinguish the two.
    """
    if backend == "pi":
        return observed_model_from_pi_session(session_path), "session-metadata"
    observed = observed_model_from_events(event_path)
    if observed:
        return observed, "event-stream"
    rollout = find_codex_rollout(thread_id) if thread_id else None
    if rollout is not None:
        model = observed_model_from_codex_session(rollout)
        if model:
            return model, "session-metadata"
    return None, None


def run_process(
    argv: list[str],
    prompt: str,
    event_path: Path,
    stderr_path: Path,
    timeout_seconds: float,
    on_session,
    cwd: Path,
) -> tuple[int, str, bool, str]:
    lines: list[str] = []
    timed_out = False
    # Capture the stderr increment owned by this attempt (the log is append-only
    # across attempts, so only the tail written in this call is meaningful).
    stderr_start = stderr_path.stat().st_size if stderr_path.is_file() else 0
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
        try:
            process.stdin.write(prompt)
        except BrokenPipeError:
            # Child closed stdin before we finished writing (e.g. immediate
            # exit on config/quota). Continue reading stdout/stderr for
            # error classification; wait/reap below.
            pass
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
                if not isinstance(event, dict):
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
        stderr_end = stderr_path.stat().st_size if stderr_path.is_file() else 0
        stderr_delta = ""
        if stderr_end > stderr_start and stderr_path.is_file():
            with stderr_path.open("rb") as stream:
                stream.seek(stderr_start)
                stderr_delta = stream.read(stderr_end - stderr_start).decode(
                    "utf-8", errors="replace"
                )
        return process.wait(), "".join(lines), timed_out, stderr_delta


def reviewer_prompt(task: str, review_kind: str, mode: str | None = None) -> str:
    if review_kind == "prerun":
        assert mode is not None
        allowed = ", ".join(sorted(RESULTS[mode]))
        return (
            task.rstrip()
            + "\n\nTransport output contract: return only one JSON object with keys "
            "reviewer_id, review_mode, result, decision, report_markdown. "
            f"review_mode must be {mode}; result must be one of {allowed}. "
            "Use allow_run only for a correct result; every other result uses do_not_run.\n"
        )
    if review_kind == "result-analysis":
        keys = ", ".join(sorted(RESULT_ANALYSIS_KEYS))
        outcomes = ", ".join(sorted(RESULT_ANALYSIS_OUTCOMES))
        return (
            task.rstrip()
            + "\n\nTransport output contract: return only one JSON object with keys "
            f"{keys}. scientific_outcome must be one of {outcomes}. "
            "analysis_markdown must contain the four sections Change, Result, Finding, Next, in order. "
            "Do not wrap the JSON in code fences or prose.\n"
        )
    if review_kind == "closing":
        allowed = ", ".join(sorted(CLOSING_RESULTS))
        return (
            task.rstrip()
            + "\n\nTransport output contract: return only one JSON object with keys "
            "reviewer_id, result, report_markdown, gaps. "
            f"result must be one of {allowed}. "
            "Each gap must have source_ref, evidence_ref, why_it_matters, suggested_followup_issue. "
            "Do not wrap the JSON in code fences or prose.\n"
        )
    raise ValueError(f"unknown review kind: {review_kind}")


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
    review_kind = getattr(args, "review_kind", None) or "prerun"
    model_source = getattr(args, "model_source", None) or "contract"
    packet, packet_sha = validate_packet(packet_path, review_kind)
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
    mode = packet.get("review_mode") if review_kind == "prerun" else None
    candidate_commit = packet.get("pre_run_code_commit") if review_kind == "prerun" else None
    # The approved reviewer identity comes from review_model.py; a launcher may
    # omit --model and inherit it instead of restating the value.
    if model_source == "invoker":
        if not (args.model or "").strip():
            raise ValueError("--model is required when --model-source=invoker")
        model = args.model.strip()
    else:
        # model_source == contract: ignore any --model and use the contract value
        model = review_model.review_job_model(args.backend)
    job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = job_dir / "job.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("reviewer job is already running") from exc

        state_path = job_dir / "job.json"
        verdict_path = job_dir / "verdict.json"
        verdict_schema = VERDICT_SCHEMA_BY_KIND[review_kind]
        if verdict_path.is_file():
            verdict = load_object(verdict_path, "verdict artifact")
            if (
                verdict.get("schema_version") == verdict_schema
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
                "review_kind": review_kind,
                "packet_path": str(packet_path),
                "packet_sha256": packet_sha,
                "task_path": str(task_path),
                "task_sha256": task_sha,
                "candidate_commit": candidate_commit,
                "review_mode": mode,
                "replacement": 0,
                "resumes_used": 0,
                "session_id": None,
                "invocations": 0,
                "quota_deadline": None,
                "status": "pending",
                "created_at": now(),
            }
            atomic_json(state_path, state)

        schema_path = job_dir / "response-schema.json"
        atomic_json(schema_path, output_schema(review_kind, mode))
        initial_prompt = reviewer_prompt(task_text, review_kind, mode)
        prompt_path = job_dir / "review-task.txt"
        prompt_path.write_text(initial_prompt, encoding="utf-8")
        os.chmod(prompt_path, 0o600)
        last_error = "review service did not return a verdict"
        attempts_path = job_dir / "attempts.log"
        max_total_invocations = (1 + args.max_resumes) * (1 + args.max_replacements)
        while state["replacement"] <= args.max_replacements:
            # P0-1: hard cap on total transport invocations, independent of
            # session state machine. Persists across runner restarts.
            if int(state.get("invocations", 0)) >= max_total_invocations:
                last_error = (
                    f"invocation budget exhausted ({max_total_invocations}); "
                    "service presumed unavailable"
                )
                break

            # Quota wait: if a prior attempt was rate-limited and the caller
            # re-invokes before retry_at, decline to launch the transport.
            quota = state.get("quota_waiting")
            if isinstance(quota, dict):
                retry_at_str = quota.get("retry_at", "")
                try:
                    from datetime import datetime as _dt
                    retry_at = _dt.fromisoformat(retry_at_str.replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) < retry_at:
                        remaining = (retry_at - datetime.now(timezone.utc)).total_seconds()
                        print(json.dumps({
                            "status": "quota_waiting",
                            "model": quota.get("model", model),
                            "retry_at": retry_at_str,
                            "remaining_seconds": int(remaining),
                        }, ensure_ascii=False))
                        return 4
                    # Retry time arrived: clear the wait flag and retry once.
                    state.pop("quota_waiting", None)
                    atomic_json(state_path, state)
                except (ValueError, TypeError, AttributeError):
                    state.pop("quota_waiting", None)
                    atomic_json(state_path, state)

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
                schema_path, prompt_path, model,
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

            returncode, stdout, timed_out, stderr_delta = run_process(
                argv, prompt, event_path, stderr_path,
                args.attempt_timeout_seconds, record_session, cwd,
            )
            pi_session = job_dir / f"pi-session-{execution}.jsonl"
            if args.backend == "pi" and state.get("session_id") is None and pi_session.is_file():
                record_session(str(job_dir / f"pi-session-{execution}.jsonl"))
            observed, evidence_channel = observed_model_for_backend(
                args.backend, event_path, pi_session, state.get("session_id")
            )
            # P0-3: only persist non-empty response files. If the transport
            # produced an empty file (common on quota/config/ambiguity failures),
            # remove it — it carries no evidence.
            if not raw_path.is_file():
                if stdout.strip():
                    raw_path.write_text(stdout, encoding="utf-8")
                    os.chmod(raw_path, 0o600)
                else:
                    raw_path = None
            if raw_path is not None:
                response_text = raw_path.read_text(encoding="utf-8")
                if not response_text.strip() and raw_path.is_file():
                    raw_path.unlink(missing_ok=True)
                    raw_path = None
                    response_text = stdout if stdout.strip() else ""
            else:
                response_text = stdout if stdout.strip() else ""
            response = parse_json_object(response_text)
            last_error = validate_response(response, review_kind, mode, packet) or ""
            if not last_error and response is not None:
                raw_sha = sha256_bytes(response_text.encode("utf-8"))
                # raw_path must not be None here: response_text is non-empty
                # and parsed successfully, so a path was persisted.
                if raw_path is None:
                    raise ValueError(
                        "internal error: valid verdict but no persisted raw response file"
                    )
                verdict = {
                    "schema_version": verdict_schema,
                    "status": "completed",
                    "backend": args.backend,
                    "review_kind": review_kind,
                    "reviewer_session_id": state.get("session_id"),
                    "requested_model": model,
                    "observed_model": observed or "unknown",
                    "model_evidence": evidence_channel if observed else "unknown",
                    "model_source": model_source,
                    "packet_path": str(packet_path),
                    "packet_sha256": packet_sha,
                    "task_path": str(task_path),
                    "task_sha256": task_sha,
                    "raw_response_path": str(raw_path),
                    "raw_response_sha256": raw_sha,
                    # 保留 response 别名：兼容在 raw_response_* 成为规范字段之前产出的
                    # 校验器与已归档 verdict。
                    "response_path": str(raw_path),
                    "response_sha256": raw_sha,
                    "replacement_count": state["replacement"],
                    "resume_count": state["resumes_used"],
                    "transport_exit_code": returncode,
                    "completed_at": now(),
                }
                if review_kind == "prerun":
                    assert mode is not None
                    assert candidate_commit is not None
                    verdict.update(
                        {
                            "reviewer_id": response["reviewer_id"].strip(),
                            "review_mode": mode,
                            "result": response["result"],
                            "decision": response["decision"],
                            "candidate_commit": candidate_commit,
                            "report_markdown": response["report_markdown"],
                        }
                    )
                elif review_kind == "result-analysis":
                    verdict.update(
                        {
                            "exp_id": packet["exp_id"],
                            "run_ids": list(packet["run_ids"]),
                            "review_output": json.dumps(response, ensure_ascii=False, sort_keys=True),
                            "review_output_sha256": sha256_bytes(
                                json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")
                            ),
                        }
                    )
                elif review_kind == "closing":
                    verdict.update(
                        {
                            "reviewer_id": response["reviewer_id"].strip(),
                            "result": response["result"],
                            "report_markdown": response["report_markdown"],
                            "gaps": response["gaps"],
                        }
                    )
                atomic_json(verdict_path, verdict)
                state.update(status="completed", verdict_path=str(verdict_path), updated_at=now())
                atomic_json(state_path, state)
                print(json.dumps({"status": "completed", "verdict": str(verdict_path)}, ensure_ascii=False))
                return 0

            last_error = (
                "review attempt timed out" if timed_out else
                last_error or f"review process exited {returncode}"
            )
            category, retry_after, detail = classify_attempt_failure(
                _extract_service_error(stdout), stderr_delta, timed_out,
            )
            state.update(status="service_failed", last_error=last_error, updated_at=now())
            atomic_json(state_path, state)

            if category == "config_error":
                # Write attempts.log before break (config error must be logged for audit).
                attempts_path = job_dir / "attempts.log"
                with attempts_path.open("a", encoding="utf-8") as attempts:
                    attempts.write(json.dumps({
                        "at": now(),
                        "execution": execution,
                        "sequence": sequence,
                        "exit_code": returncode,
                        "timed_out": timed_out,
                        "category": category,
                        "detail": detail,
                        "stdout_bytes": len(stdout.encode("utf-8")),
                        "stderr_bytes": len(stderr_delta.encode("utf-8")),
                    }, ensure_ascii=False) + "\n")
                break

            if category == "quota_exhausted":
                # Write attempts.log before branch decisions (quota errors must be audit-logged).
                with attempts_path.open("a", encoding="utf-8") as attempts:
                    attempts.write(json.dumps({
                        "at": now(),
                        "execution": execution,
                        "sequence": sequence,
                        "exit_code": returncode,
                        "timed_out": timed_out,
                        "category": category,
                        "detail": detail,
                        "stdout_bytes": len(stdout.encode("utf-8")),
                        "stderr_bytes": len(stderr_delta.encode("utf-8")),
                    }, ensure_ascii=False) + "\n")
                if retry_after is None:
                    last_error = (
                        "quota limit hit but recovery time is unknown; "
                        "manual intervention required before retrying"
                    )
                    break
                # Fixed UTC deadline from first hit; later hits cannot extend it.
                existing_deadline = state.get("quota_deadline")
                if not existing_deadline:
                    state["quota_deadline"] = _utc_plus(MAX_QUOTA_WAIT_SECONDS)
                    atomic_json(state_path, state)
                    existing_deadline = state["quota_deadline"]
                retry_at_ts = time.time() + retry_after
                from datetime import datetime as _dt
                deadline_dt = _dt.fromisoformat(existing_deadline)
                if retry_at_ts > deadline_dt.timestamp():
                    last_error = (
                        f"quota recovery at {_utc_plus(retry_after)} exceeds "
                        f"deadline {existing_deadline} ({retry_after:.0f}s > {MAX_QUOTA_WAIT_SECONDS}s window); "
                        f"manual intervention required"
                    )
                    break
                state.update(
                    status="quota_waiting",
                    quota_waiting={
                        "retry_at": _utc_plus(retry_after),
                        "model": model,
                        "raw_message": detail,
                        "deadline": existing_deadline,
                    },
                    updated_at=now(),
                )
                atomic_json(state_path, state)
                # Record before exiting to quota_waiting.
                with attempts_path.open("a", encoding="utf-8") as attempts:
                    attempts.write(json.dumps({
                        "at": now(),
                        "execution": execution,
                        "sequence": sequence,
                        "exit_code": returncode,
                        "timed_out": timed_out,
                        "category": category,
                        "detail": detail,
                        "stdout_bytes": len(stdout.encode("utf-8")),
                        "stderr_bytes": len(stderr_delta.encode("utf-8")),
                    }, ensure_ascii=False) + "\n")
                print(json.dumps({
                    "status": "quota_waiting",
                    "model": model,
                    "retry_at": state["quota_waiting"]["retry_at"],
                    "deadline": existing_deadline,
                    "detail": detail,
                }, ensure_ascii=False))
                return 4

            if not state.get("session_id"):
                state["replacement"] += 1
                state["resumes_used"] = 0
                state["session_id"] = None
                atomic_json(state_path, state)

            # P0-3: record the failed attempt BEFORE any branch exit so no
            # category (config_error / transient / unknown) is missed.
            with attempts_path.open("a", encoding="utf-8") as attempts:
                attempts.write(json.dumps({
                    "at": now(),
                    "execution": execution,
                    "sequence": sequence,
                    "exit_code": returncode,
                    "timed_out": timed_out,
                    "category": category,
                    "detail": detail,
                    "stdout_bytes": len(stdout.encode("utf-8")),
                    "stderr_bytes": len(stderr_delta.encode("utf-8")),
                }, ensure_ascii=False) + "\n")
            time.sleep(5)

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
    parser.add_argument(
        "--review-kind",
        choices=("prerun", "result-analysis", "closing"),
        default="prerun",
    )
    parser.add_argument(
        "--model-source",
        choices=("contract", "invoker"),
        default="contract",
        help="contract: model comes from review_contract.toml; invoker: must pass --model",
    )
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
