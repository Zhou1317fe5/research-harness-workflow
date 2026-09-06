#!/usr/bin/env python3
"""Shared terminal-state checks for Mission closing and recovery."""

from __future__ import annotations

import csv
from pathlib import Path


EXPECTED_FIELDS = [
    "id", "priority", "phase", "area", "title", "description",
    "acceptance_criteria", "test_mcp", "required_skills", "required_mcp",
    "review_initial_requirements", "review_regression_requirements", "dev_state",
    "review_initial_state", "review_regression_state", "git_state", "owner", "refs",
    "notes", "spec_id", "exp_id", "run_id", "remote_state", "artifact_path",
    "branch", "commit_hash", "next_action", "updated_at",
]
CLOSED_STATES = {
    "dev_state": "已完成",
    "review_initial_state": "已完成",
    "review_regression_state": "已完成",
    "git_state": "已提交",
}
REMOTE_STATES = {
    "",
    "not_applicable",
    "running_remote",
    "completed",
    "artifacts_pulled",
    "ingested",
    "failed",
}
TERMINAL_REMOTE_STATES = {"not_applicable", "completed", "ingested"}
REVIEW_REQUIRED_TAGS = {
    "review_agent_mode",
    "review_independence",
    "review_result",
    "scientific_outcome",
    "claim_coverage_status",
    "review_json",
    "handoff",
}
PATH_TAGS = {"review_json", "handoff"}


def parse_note_tags(notes: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in notes.split(";"):
        key, separator, value = item.strip().partition(":")
        if separator and key:
            result[key.strip()] = value.strip()
    return result


def row_terminal_errors(row: dict[str, str]) -> list[str]:
    row_id = row.get("id", "") or "<missing-id>"
    errors: list[str] = []
    for field, expected in CLOSED_STATES.items():
        if row.get(field) != expected:
            errors.append(f"row_not_closed:{row_id}.{field}={row.get(field, '')!r}")
    remote_state = row.get("remote_state", "")
    if remote_state not in TERMINAL_REMOTE_STATES:
        errors.append(f"remote_state_not_terminal:{row_id}.remote_state={remote_state!r}")
    return errors


def _resolve_artifact(value: str, csv_path: Path, workdir: Path) -> Path | None:
    path = Path(value).expanduser()
    candidates = [path] if path.is_absolute() else [csv_path.parent / path, workdir / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(workdir)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def csv_completion_errors(csv_path: Path, *, workdir: Path) -> list[str]:
    """Return reasons a CSV is not a fully delivered Mission terminal state."""
    errors: list[str] = []
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != EXPECTED_FIELDS:
                return ["csv_schema_invalid:header_mismatch"]
            rows = list(reader)
    except (OSError, csv.Error, UnicodeError) as exc:
        return [f"csv_read_failed:{exc}"]
    if not rows:
        return ["csv_empty:no_rows"]
    ids = [row.get("id", "") for row in rows]
    if any(not row_id for row_id in ids) or len(ids) != len(set(ids)):
        errors.append("row_id_invalid:missing_or_duplicate")
    for row in rows:
        errors.extend(row_terminal_errors(row))

    reviews = [row for row in rows if row.get("id", "").startswith("REVIEW-")]
    if not reviews:
        errors.append("review_row_missing:REVIEW-*")
        return sorted(set(errors))
    tags = parse_note_tags(reviews[-1].get("notes", ""))
    for tag in sorted(REVIEW_REQUIRED_TAGS):
        value = tags.get(tag, "")
        if not value:
            errors.append(f"review_tag_missing:{tag}")
        elif value == "pending":
            errors.append(f"review_tag_pending:{tag}")
    for tag in sorted(PATH_TAGS):
        value = tags.get(tag)
        if value and _resolve_artifact(value, csv_path, workdir) is None:
            errors.append(f"review_artifact_missing:{tag}={value}")
    if tags.get("claims") and not tags.get("claim_ledger"):
        errors.append("claim_ledger_missing:claims_present")
    if tags.get("claim_ledger") and _resolve_artifact(
        tags["claim_ledger"], csv_path, workdir
    ) is None:
        errors.append(f"claim_ledger_unreadable:{tags['claim_ledger']}")
    return sorted(set(errors))
