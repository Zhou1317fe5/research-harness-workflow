#!/usr/bin/env python3
"""Run independent post-run scientific result analysis via reviewer_job.

The single supported channel is ``result-analysis-reviewer-job``: a fresh,
read-only Reviewer Job session whose model comes from the per-host entry in
``.agents/harness/config/review_contract.toml``. Both Pi and Codex users go
through the same code path by choosing ``--backend {pi,codex}``.

- a fresh session per invocation with the read-only tool whitelist enforced
  by ``reviewer_job.py``;
- the reviewer receives only the task prompt built from the mission CSV and
  the raw artifact locations, never the main conversation or its conclusions;
- the verdict artifact binds packet SHA-256, task SHA-256, raw response
  SHA-256 and the normalized reviewer output SHA-256; the
  ``post-run-result-analysis`` validator recomputes the output digest from
  disk.

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
`job:reviews/result-analysis-<ExpID>/verdict.json#verdict` (CSV-relative),
with `analysis_agent_mode:result-analysis-reviewer-job` and
`analysis_model_evidence:job-verdict`. A failed invocation leaves no verdict;
rerun the same command after the service recovers. Quota and launcher failures
are review-service failures, not a completed analysis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "post-run.result-analysis.v1"
PACKET_SCHEMA = "post-run.result-analysis.v1"
VERDICT_SCHEMA = "post-run.result-analysis-verdict.v1"

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


def canonical_json(value: Any) -> bytes:
    """Match reviewer_job.canonical_json so packet hashes agree."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


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


def _write_if_changed(path: Path, content: bytes) -> None:
    try:
        if path.read_bytes() == content:
            return
    except FileNotFoundError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


def _reviewer_job_script(workdir: Path) -> Path:
    candidate = workdir / ".agents" / "harness" / "reviewer_job.py"
    if not candidate.is_file():
        raise RuntimeError(f"reviewer_job_missing:{candidate}")
    return candidate


def build_reviewer_packet(args: argparse.Namespace, workdir: Path) -> dict[str, Any]:
    return {
        "schema_version": PACKET_SCHEMA,
        "exp_id": args.exp_id,
        "run_ids": sorted(set(args.run_ids)),
        "csv_path": str(args.csv.expanduser().resolve()),
        "repo_root": str(workdir),
    }


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
    packet_path = job_dir / "packet.json"
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
    packet = build_reviewer_packet(args, workdir)
    task_bytes = task_text.encode("utf-8")
    packet_bytes = canonical_json(packet)

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
            and verdict.get("packet_sha256") == sha256_bytes(packet_bytes)
        ):
            print(json.dumps({"status": "completed", "verdict": str(verdict_path)}, ensure_ascii=False))
            return 0
        raise ValueError("existing_verdict_belongs_to_different_inputs")

    _write_if_changed(task_path, task_bytes)
    _write_if_changed(packet_path, packet_bytes + b"\n")

    executable = _reviewer_job_script(workdir)
    cmd = [
        sys.executable,
        str(executable),
        "--backend",
        args.backend,
        "--packet",
        str(packet_path),
        "--task",
        str(task_path),
        "--job-dir",
        str(job_dir),
        "--review-kind",
        "result-analysis",
        "--cwd",
        str(workdir),
    ]
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=args.attempt_timeout_seconds + 60,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"reviewer_job timed out; "
            "review_service_failure:timeout; rerun the same command after the review service recovers\n"
        )
        return 124

    stderr = (proc.stderr or "").strip()
    if stderr:
        sys.stderr.write(stderr + "\n")
    if proc.returncode != 0:
        sys.stderr.write(
            f"reviewer_job exited {proc.returncode}; "
            "review_service_failure:exit; rerun the same command after the review service recovers\n"
        )
        return proc.returncode

    stdout = (proc.stdout or "").strip()
    if not stdout:
        sys.stderr.write("reviewer_job produced no final status\n")
        return 3
    try:
        status = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError:
        sys.stderr.write("reviewer_job final status was not valid JSON\n")
        sys.stderr.write(stdout + "\n")
        return 3
    if status.get("status") != "completed":
        sys.stderr.write(f"reviewer_job did not complete: {status}\n")
        return 3

    verdict_rel = status.get("verdict")
    if not isinstance(verdict_rel, str) or not verdict_rel.strip():
        sys.stderr.write("reviewer_job missing verdict path\n")
        return 3
    print(json.dumps({"status": "completed", "verdict": verdict_rel}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="Canonical mission CSV.")
    parser.add_argument("--exp-id", required=True, help="Experiment id to analyze.")
    parser.add_argument("--run-ids", required=True, nargs="+", help="Every ingested RunID of the experiment.")
    parser.add_argument(
        "--backend",
        choices=("pi", "codex"),
        default="pi",
        help="Reviewer transport backend; Pi harnesses use 'pi', Codex harnesses use 'codex'.",
    )
    parser.add_argument(
        "--attempt-timeout-seconds",
        type=float,
        default=1800,
        help="Per-attempt reviewer timeout (matches reviewer_job default).",
    )
    parser.add_argument("--workdir", default=".", type=Path, help="Repository root.")
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        sys.stderr.write(f"run_result_analysis: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
