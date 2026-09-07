#!/usr/bin/env python3
"""Append REVIEW-01 to a valid compatibility CSV when it has no review row."""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import tempfile
from pathlib import Path

from mission_completion import read_mission_csv


COMPAT_FIELDNAMES = [
    "id", "priority", "phase", "area", "title", "description",
    "acceptance_criteria", "test_mcp", "required_skills", "required_mcp",
    "review_initial_requirements", "review_regression_requirements", "dev_state",
    "review_initial_state", "review_regression_state", "git_state", "owner",
    "refs", "notes",
]
PROJECT_EXTRA_FIELDS = ["spec_id", "exp_id", "run_id", "remote_state", "artifact_path", "branch", "commit_hash", "next_action", "updated_at"]
PROJECT_FIELDNAMES = COMPAT_FIELDNAMES + PROJECT_EXTRA_FIELDS


def _scope(rows: list[dict[str, str]]) -> str:
    parts: list[str] = []
    for row in rows:
        parts.append(
            " | ".join(
                f"{key}={row.get(key, '').strip()}"
                for key in ("id", "description", "acceptance_criteria", "refs", "notes")
                if row.get(key, "").strip()
            )
        )
    return "; ".join(part for part in parts if part)


def ensure_review_row(path: Path) -> bool:
    path = path.expanduser().resolve()
    fieldnames, rows, has_bom = read_mission_csv(path, allow_compat=True)
    if any(row.get("id", "").startswith("REVIEW-") for row in rows):
        return False

    phases = [int(row["phase"]) for row in rows if row.get("phase", "").isdigit()]
    review = dict.fromkeys(fieldnames, "")
    review.update(
        {
            "id": "REVIEW-01",
            "priority": "P0",
            "phase": str(max(phases, default=0) + 1),
            "area": "review",
            "title": "Review compatibility CSV against delivered work",
            "description": "Review every ordinary row's declared scope, acceptance data, delivered diff, and validation evidence.",
            "acceptance_criteria": "WHEN all ordinary rows are closed THEN run mechanical readiness and choose evidence-close unless unresolved L3/L4 risk, evidence conflict, or a suspected current-scope gap requires the independent capability ladder; WHEN current-scope gaps exist THEN append follow-up rows and another REVIEW row; WHEN no current-scope gaps remain THEN record Mission result separately from scientific outcome and close.",
            "test_mcp": "manual",
            "review_initial_requirements": "Verify all ordinary rows are closed before review.",
            "review_regression_requirements": "Review source CSV scope: " + _scope(rows),
            "dev_state": "未开始",
            "review_initial_state": "未开始",
            "review_regression_state": "未开始",
            "git_state": "未提交",
            "refs": str(path),
            "notes": (
                f"review_kind:vision; source_csv:{path}; "
                "review_agent_mode:pending; review_independence:pending; "
                "review_requested_model:pending; review_observed_model:pending; "
                "review_model_evidence:pending; claim_coverage:unknown; "
                "claim_coverage_status:pending; scientific_outcome:pending"
            ),
        }
    )
    if "remote_state" in fieldnames:
        review["remote_state"] = "not_applicable"
    rows.append(review)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    try:
        changed = ensure_review_row(args.csv_path)
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        print(f"ensure_review_row: {exc}", file=sys.stderr)
        return 2
    print("appended REVIEW-01" if changed else "review row already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
