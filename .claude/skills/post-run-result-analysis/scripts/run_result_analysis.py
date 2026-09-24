#!/usr/bin/env python3
"""Run the codex-exec channel for post-run scientific result analysis.

Pi harnesses dispatch the registered `scientific-reviewer` sub-agent and bind
parent session metadata; that channel does not exist outside Pi. This runner
drives the same independent analysis through a fresh, ephemeral, read-only
`codex exec` session so Codex/CLI harnesses produce verifiable evidence of
the same strength as the Pi parent-session channel:

- a fresh ephemeral session per invocation, sandboxed read-only;
- the reviewer receives only the task prompt built from the mission CSV and
  the raw artifact locations, never the main conversation or its conclusions;
- the observed model is taken from the trusted CLI JSON event stream
  (`thread.started`/`session_meta` style events), never from reviewer text;
- the verdict artifact binds task SHA-256, the raw event stream and the
  normalized reviewer output SHA-256; the `post-run-result-analysis`
  validator recomputes the output digest from disk.

Usage (run from the repository root):

```bash
python3 .agents/skills/post-run-result-analysis/scripts/run_result_analysis.py \
  --csv issues/<stem>/<stem>.csv \
  --exp-id <ExpID> \
  --run-ids <RunID-1> <RunID-2> \
  --workdir .
```

The verdict lands at `issues/<stem>/reviews/result-analysis-<ExpID>/verdict.json`.
Reference it from the analysis index as
`exec:reviews/result-analysis-<ExpID>/verdict.json#verdict` (CSV-relative),
with `analysis_agent_mode:codex-exec-independent` and
`analysis_model_evidence:event-stream`. A failed invocation leaves no verdict;
rerun the same command after the service recovers. Quota and launcher failures
are review-service failures, not a completed analysis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "post-run.result-analysis.v1"
VERDICT_SCHEMA = "post-run.result-analysis-verdict.v1"


def _load_review_model():
    """Load the canonical review-model constants (searched upward for .agents)."""
    import importlib.util

    for ancestor in Path(__file__).resolve().parents:
        module_path = ancestor / ".agents" / "harness" / "review_model.py"
        if module_path.is_file():
            spec = importlib.util.spec_from_file_location("review_model", module_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load review_model: {module_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("cannot locate .agents/harness/review_model.py")


review_model = _load_review_model()

REQUESTED_MODEL = review_model.REVIEW_MODEL
EXEC_MODEL = review_model.EXEC_MODEL
# Bounded wait matches reviewer_job's attempt timeout so a stuck exec session
# fails closed instead of blocking the analysis row indefinitely.
EXEC_TIMEOUT_SECONDS = 1800
_QUOTA_ERROR_MARKERS = (
    "insufficient_quota", "quota exceeded", "rate_limit", "rate limit",
    "429", "usage limit", "credits", "billing", "too many requests",
)
_REVIEW_OUTPUT_KEYS = {
    "exp_id", "run_ids", "analysis_markdown", "scientific_outcome",
    "limitations", "validation_gaps",
}

TASK_TEMPLATE = """You are the independent scientific reviewer for post-run experiment \
result analysis. Work read-only: do not modify files, do not run training or \
evaluation, do not delegate.

Independence requirements:
- Read the raw evidence yourself: the RunSpec, manifest, record, summary and \
raw artifacts for every target RunID, plus the approved spec / outcome \
contract, the mission CSV, the claim/evidence ledger, and the corresponding \
baseline, fold, shot and ablation references.
- Do NOT trust or reuse the main agent's summaries, conclusions or handoff \
prose as evidence. Raw metrics and directly readable files take priority.
- Compare against the pre-registered gates and baselines before judging.

result_analysis_targets: {targets}

Return exactly one JSON object as your final answer, with no code fences and \
no prose around it, containing only these fields:

{{
  "exp_id": "{exp_id}",
  "run_ids": {run_ids},
  "analysis_markdown": "## Change\\n...\\n\\n## Result\\n...\\n\\n## Finding\\n...\\n\\n## Next\\n...\\n",
  "scientific_outcome": "inconclusive",
  "limitations": [],
  "validation_gaps": []
}}

`analysis_markdown` must contain exactly the four sections Change, Result, \
Finding, Next, in that order. `scientific_outcome` must be one of \
hypothesis_supported, hypothesis_not_supported, gate_failed, inconclusive or \
not_applicable. `limitations` and `validation_gaps` are arrays of non-empty \
strings.
"""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def load_csv_targets(csv_path: Path) -> dict[str, set[str]]:
    import csv

    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames is None or "remote_state" not in reader.fieldnames:
            raise ValueError("csv_schema_invalid:remote_state_column_missing")
        targets: dict[str, set[str]] = {}
        for row in reader:
            if row.get("remote_state") != "ingested":
                continue
            exp_id = (row.get("exp_id") or "").strip()
            run_id = (row.get("run_id") or "").strip()
            if exp_id and run_id:
                targets.setdefault(exp_id, set()).add(run_id)
    return targets


def write_atomic(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resolve_codex_executable(which=shutil.which) -> str | None:
    """Resolve the platform launcher, including codex.CMD on Windows."""
    return which("codex")


def build_exec_command(executable: str, workdir: str, model: str) -> list[str]:
    return [
        executable,
        "exec",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "-m",
        model,
        "-C",
        workdir,
        "--sandbox",
        "read-only",
        "-",
    ]


def trusted_event_model(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    payload_type = payload.get("type")
    trusted_types = {
        "thread.started",
        "session_meta",
        "session_metadata",
        "turn.started",
        "response.started",
    }
    if event_type not in trusted_types and payload_type not in trusted_types:
        return None
    for container in (event, payload):
        for key in ("model", "model_id"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def parse_json_events(stdout: str) -> tuple[str | None, str | None]:
    final_message = None
    observed_model = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        observed_model = trusted_event_model(event) or observed_model
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if item.get("type") == "agent_message":
            final_message = item.get("text")
        if item.get("type") == "message" and item.get("role") == "assistant":
            parts = []
            for content in item.get("content") or []:
                if isinstance(content, dict):
                    text = content.get("text") or content.get("output_text")
                    if text:
                        parts.append(text)
            if parts:
                final_message = "\n".join(parts)
        if event.get("type") == "agent_message":
            final_message = event.get("message") or event.get("text") or payload.get("message")
        if event.get("type") == "event_msg" and payload.get("type") == "agent_message":
            final_message = payload.get("message")
        if event.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
            parts = []
            for content in payload.get("content") or []:
                if isinstance(content, dict):
                    text = content.get("text") or content.get("output_text")
                    if text:
                        parts.append(text)
            if parts:
                final_message = "\n".join(parts)
    return final_message, observed_model


def run(args: argparse.Namespace) -> int:
    workdir = args.workdir.expanduser().resolve()
    csv_path = args.csv.expanduser().resolve()
    if not csv_path.is_relative_to(workdir):
        raise ValueError("csv_outside_workdir")
    targets = load_csv_targets(csv_path)
    expected_runs = targets.get(args.exp_id)
    if not expected_runs:
        raise ValueError(f"no_ingested_runs_for_exp:{args.exp_id}")
    run_ids = sorted(set(args.run_ids))
    if not run_ids or set(run_ids) != expected_runs:
        raise ValueError(
            "run_ids_mismatch:"
            f"csv_ingested={sorted(expected_runs)!r};requested={run_ids!r}"
        )

    job_dir = csv_path.parent / "reviews" / f"result-analysis-{args.exp_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    task_path = job_dir / "task.md"
    events_path = job_dir / "events.jsonl"
    verdict_path = job_dir / "verdict.json"

    targets_json = json.dumps(
        {"exp_id": args.exp_id, "run_ids": run_ids},
        ensure_ascii=False, sort_keys=True,
    )
    task_text = TASK_TEMPLATE.format(
        targets=targets_json,
        exp_id=args.exp_id,
        run_ids=json.dumps(run_ids, ensure_ascii=False),
    )
    task_bytes = task_text.encode("utf-8")

    if verdict_path.is_file():
        try:
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"existing_verdict_unreadable:{exc}") from exc
        if (
            isinstance(verdict, dict)
            and verdict.get("schema_version") == VERDICT_SCHEMA
            and verdict.get("exp_id") == args.exp_id
            and verdict.get("run_ids") == run_ids
            and verdict.get("task_sha256") == sha256_bytes(task_bytes)
        ):
            print(json.dumps({"status": "completed", "verdict": str(verdict_path)}, ensure_ascii=False))
            return 0
        raise ValueError("existing_verdict_belongs_to_different_inputs")

    write_atomic(task_path, task_text)

    executable = resolve_codex_executable()
    if not executable:
        sys.stderr.write("codex executable not found; result-analysis review service unavailable\n")
        return 127
    cmd = build_exec_command(executable, str(workdir), EXEC_MODEL)
    try:
        proc = subprocess.run(
            cmd,
            input=task_text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=EXEC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"codex exec timed out after {EXEC_TIMEOUT_SECONDS}s; "
            "review_service_failure:timeout; rerun the same command after the review service recovers\n"
        )
        return 124
    write_atomic(events_path, proc.stdout or "")
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.stderr.write(proc.stdout)
        joined = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
        kind = (
            "quota_error"
            if any(marker in joined for marker in _QUOTA_ERROR_MARKERS)
            else "transport_error"
        )
        sys.stderr.write(
            f"codex exec failed (review_service_failure:{kind}); "
            "rerun the same command after the review service recovers\n"
        )
        return proc.returncode or 2

    final_message, observed_model = parse_json_events(proc.stdout)
    if not final_message:
        sys.stderr.write("codex exec produced no final agent message\n")
        return 2
    try:
        payload = json.loads(final_message.strip())
    except json.JSONDecodeError:
        sys.stderr.write("final agent message was not valid JSON\n")
        sys.stderr.write(final_message + "\n")
        return 3
    if not isinstance(payload, dict) or set(payload) != _REVIEW_OUTPUT_KEYS:
        sys.stderr.write("final agent message did not match the result-analysis schema\n")
        return 3

    normalized_output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    verdict = {
        "schema_version": VERDICT_SCHEMA,
        "status": "completed",
        "backend": "codex-exec",
        "exp_id": args.exp_id,
        "run_ids": run_ids,
        "requested_model": REQUESTED_MODEL,
        "observed_model": observed_model or "unknown",
        "task_path": str(task_path),
        "task_sha256": sha256_bytes(task_bytes),
        "events_path": str(events_path),
        "events_sha256": sha256_bytes((proc.stdout or "").encode("utf-8")),
        "review_output": normalized_output,
        "review_output_sha256": sha256_bytes(normalized_output.encode("utf-8")),
        "transport": "codex-exec-ephemeral",
    }
    write_atomic(verdict_path, json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": "completed", "verdict": str(verdict_path)}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="Canonical mission CSV.")
    parser.add_argument("--exp-id", required=True, help="Experiment id to analyze.")
    parser.add_argument("--run-ids", required=True, nargs="+", help="Every ingested RunID of the experiment.")
    parser.add_argument("--workdir", default=".", type=Path, help="Repository root.")
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        sys.stderr.write(f"run_result_analysis: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
