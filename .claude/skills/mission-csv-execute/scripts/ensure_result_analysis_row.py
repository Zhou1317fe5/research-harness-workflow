#!/usr/bin/env python3
"""Insert the post-run result-analysis row into an active canonical Mission CSV."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import sys
import tempfile
from pathlib import Path

import importlib.util as _ilu

from csv_state import file_lock
from mission_completion import read_mission_csv, parse_note_tags, upsert_note_tags


ANALYSIS_ROW_ID = "RESULT-ANALYSIS-01"


def _review_model() -> str:
    """Load the canonical review model (searched upward for .agents)."""
    for ancestor in Path(__file__).resolve().parents:
        module_path = ancestor / ".agents" / "harness" / "review_model.py"
        if module_path.is_file():
            spec = _ilu.spec_from_file_location("review_model", module_path)
            if spec is None or spec.loader is None:
                raise RuntimeError("cannot load review_model constants")
            module = _ilu.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.accepted_model_for_host("pi")
    raise RuntimeError("cannot locate .agents/harness/review_model.py")
CLOSED = {"已完成"}


def _all_rows_closed(rows: list[dict[str, str]]) -> bool:
    return bool(rows) and all(
        row.get(field) in CLOSED
        for row in rows
        for field in ("dev_state", "review_initial_state", "review_regression_state", "git_state")
    )


def _copy_tag(rows: list[dict[str, str]], key: str) -> str:
    for row in rows:
        value = parse_note_tags(row.get("notes", "")).get(key, "")
        if value:
            return value
    return ""


def _make_row(fieldnames: list[str], rows: list[dict[str, str]]) -> dict[str, str]:
    row = dict.fromkeys(fieldnames, "")
    first = next((item for item in rows if item.get("id", "") != ANALYSIS_ROW_ID), {})
    outcome = _copy_tag(rows, "outcome_contract")
    deferred = _copy_tag(rows, "deferred_ledger")
    notes = [
        "analysis_kind:post_run",
        "result_analysis:reviews/result-analysis.json",
        "analysis_agent_mode:pending",
        "analysis_independence:pending",
        f"analysis_requested_model:{_review_model()}",
        "analysis_observed_model:pending",
        "analysis_model_evidence:pending",
        "analysis_model_evidence_ref:pending",
    ]
    if outcome:
        notes.append(f"outcome_contract:{outcome}")
    if deferred:
        notes.append(f"deferred_ledger:{deferred}")
    row.update(
        {
            "id": ANALYSIS_ROW_ID,
            "priority": "P0",
            "phase": "analysis",
            "area": "research",
            "title": "Analyze ingested experiment results with an independent strong model",
            "description": "After all formal remote results are ingested, obtain an independent scientific-reviewer analysis and bind every ExpID/RunID to the canonical analysis artifact.",
            "acceptance_criteria": "Every ingested ExpID/RunID is covered by reviews/result-analysis.json; each analysis.md has Change/Result/Finding/Next, evidence refs, SHA-256, per-entry reviewer evidence and output SHA-256, and a verifiable scientific outcome; strong reviewer identity is recorded from parent session/tool evidence (Pi sub-agent channel) or a codex-exec verdict with event-stream model evidence; no Executor self-review fallback.",
            "test_mcp": "contract",
            "required_skills": "post-run-result-analysis",
            "required_mcp": "",
            "review_initial_requirements": "Verify the independent strong-reviewer input (Pi sub-agent or codex-exec session) is raw evidence rather than the Executor's conclusion.",
            "review_regression_requirements": "Verify analysis index coverage, channel-appropriate model evidence, analysis hashes, evidence refs and fixed four-section analysis before REVIEW-*.",
            "dev_state": "未开始",
            "review_initial_state": "未开始",
            "review_regression_state": "未开始",
            "git_state": "未提交",
            "owner": first.get("owner", "codex"),
            "refs": ".codex/skills/post-run-result-analysis/SKILL.md:1; reviews/result-analysis.json",
            "notes": "; ".join(notes),
            "spec_id": first.get("spec_id", ""),
            "branch": first.get("branch", ""),
            "next_action": ANALYSIS_ROW_ID,
            "updated_at": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        }
    )
    if "remote_state" in fieldnames:
        row["remote_state"] = "not_applicable"
    return row


def _ensure_locked(path: Path) -> bool:
    fieldnames, rows, has_bom = read_mission_csv(path, allow_compat=True)
    if "remote_state" not in fieldnames:
        return False
    if any(row.get("id", "").startswith("RESULT-ANALYSIS-") for row in rows):
        return False
    if not any(row.get("exp_id", "").strip() for row in rows):
        return False
    if _all_rows_closed(rows):
        return False

    analysis = _make_row(fieldnames, rows)
    review_index = next(
        (index for index, row in enumerate(rows) if row.get("id", "").startswith("REVIEW-")),
        len(rows),
    )
    rows.insert(review_index, analysis)
    for row in rows:
        if row.get("id", "").startswith("REVIEW-"):
            row["notes"] = upsert_note_tags(
                row.get("notes", ""),
                {"result_analysis": "reviews/result-analysis.json"},
            )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    content = output.getvalue().encode("utf-8-sig" if has_bom else "utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.chmod(temporary, path.stat().st_mode & 0o777)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def ensure_result_analysis_row(path: Path) -> bool:
    path = path.expanduser().resolve()
    with file_lock(path.with_name("." + path.name + ".lock")):
        return _ensure_locked(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    try:
        changed = ensure_result_analysis_row(args.csv_path)
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        print(f"ensure_result_analysis_row: {exc}", file=sys.stderr)
        return 2
    print("appended RESULT-ANALYSIS-01" if changed else "analysis row already present or not applicable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
