#!/usr/bin/env python3
"""Shared terminal-state checks for Mission closing and recovery."""

from __future__ import annotations

import csv
import io
import json
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
    "handoff_contract",
    "handoff_humanized",
}
PATH_TAGS = {"review_json", "handoff"}


def read_mission_csv(
    path: Path, *, allow_compat: bool = False
) -> tuple[list[str], list[dict[str, str]], bool]:
    """校验完整行结构后返回数据；调用方不得先覆盖再发现坏行。"""
    raw = path.read_bytes()
    reader = csv.DictReader(
        io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True
    )
    allowed = [EXPECTED_FIELDS]
    if allow_compat:
        allowed.append(EXPECTED_FIELDS[:19])
    if reader.fieldnames not in allowed:
        raise ValueError("csv_schema_invalid:header_mismatch")
    fields = list(reader.fieldnames)
    rows = list(reader)
    if not rows:
        raise ValueError("csv_empty:no_rows")
    ids: set[str] = set()
    for index, row in enumerate(rows, start=2):
        if None in row or any(row.get(field) is None for field in fields):
            raise ValueError(f"csv_schema_invalid:row_width:{index}")
        row_id = row["id"]
        if not row_id.strip() or row_id in ids:
            raise ValueError(f"row_id_invalid:missing_or_duplicate:{index}")
        ids.add(row_id)
    return fields, rows, raw.startswith(b"\xef\xbb\xbf")


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
    if remote_state == "ingested" and not _ingest_reference(row):
        errors.append(f"ingest_evidence_missing:{row_id}.artifact_path")
    return errors


def _ingest_reference(row: dict[str, str]) -> str:
    return row.get("artifact_path", "").strip() or parse_note_tags(
        row.get("notes", "")
    ).get("artifact_evidence", "")


def _resolve_artifact(
    value: str, csv_path: Path, workdir: Path, *, allow_directory: bool = False
) -> Path | None:
    path = Path(value).expanduser()
    candidates = [path] if path.is_absolute() else [csv_path.parent / path, workdir / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(workdir)
        except ValueError:
            continue
        if resolved.is_file() or (allow_directory and resolved.is_dir()):
            return resolved
    return None


def ingest_completion_errors(
    csv_path: Path, rows: list[dict[str, str]], *, workdir: Path
) -> list[str]:
    errors: list[str] = []
    for row in rows:
        if row.get("remote_state") != "ingested":
            continue
        value = _ingest_reference(row)
        if not value:
            errors.append(f"ingest_evidence_missing:{row['id']}.artifact_path")
        elif _resolve_artifact(value, csv_path, workdir, allow_directory=True) is None:
            errors.append(f"ingest_artifact_missing:{row['id']}:{value}")
    return errors


def claim_completion_errors(
    csv_path: Path, rows: list[dict[str, str]], *, workdir: Path
) -> list[str]:
    from validate_claim_ledger import validate_ledger

    errors: list[str] = []
    ledgers: dict[Path, set[str]] = {}
    for row in rows:
        tags = parse_note_tags(row["notes"])
        claims = {item.strip() for item in tags.get("claims", "").split(",") if item.strip()}
        value = tags.get("claim_ledger")
        if claims and not value:
            errors.append(f"claim_ledger_missing:{row['id']}:claims_present")
        if not value:
            continue
        path = _resolve_artifact(value, csv_path, workdir)
        if path is None:
            errors.append(f"claim_ledger_unreadable:{value}")
            continue
        ledgers.setdefault(path, set()).update(claims)
    for path, claims in ledgers.items():
        errors.extend(validate_ledger(path, csv_path, claims, require_terminal=True))
    return errors


def csv_completion_errors(csv_path: Path, *, workdir: Path) -> list[str]:
    """Return reasons a CSV is not a fully delivered Mission terminal state."""
    errors: list[str] = []
    try:
        _, rows, _ = read_mission_csv(csv_path)
    except (OSError, csv.Error, UnicodeError, ValueError) as exc:
        return [f"csv_read_failed:{exc}"]
    for row in rows:
        errors.extend(row_terminal_errors(row))
    errors.extend(ingest_completion_errors(csv_path, rows, workdir=workdir))
    errors.extend(claim_completion_errors(csv_path, rows, workdir=workdir))

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
    if tags.get("handoff_humanized") != "true":
        errors.append("handoff_not_humanized:latest_review")
    if tags.get("review_result") == "vision_met" and tags.get("handoff_contract") != "passed":
        errors.append("handoff_contract_not_passed:vision_met")

    from check_handoff_contract import check_contract, load_outcome_contract
    from run_vision_review import validate_review_result

    try:
        contract, contract_errors = load_outcome_contract(csv_path)
        errors.extend(contract_errors)
        review_path = _resolve_artifact(tags.get("review_json", ""), csv_path, workdir)
        if review_path is not None:
            review = json.loads(review_path.read_text(encoding="utf-8"))
            if not isinstance(review, dict):
                errors.append("review_json_invalid:expected_object")
            else:
                errors.extend(validate_review_result(review, contract))
                for tag, field in (
                    ("review_agent_mode", "review_agent_mode"),
                    ("review_independence", "review_independence"),
                    ("review_result", "result"),
                    ("scientific_outcome", "scientific_outcome"),
                    ("claim_coverage_status", "claim_coverage_status"),
                ):
                    value = review.get(field)
                    expected = str(value).lower() if isinstance(value, bool) else value
                    if tags.get(tag) != expected:
                        errors.append(f"review_tag_mismatch:{tag}")
        handoff_path = _resolve_artifact(tags.get("handoff", ""), csv_path, workdir)
        if handoff_path is not None:
            errors.extend(check_contract(handoff_path, csv_path))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        errors.append(f"review_artifact_invalid:{exc}")
    return sorted(set(errors))
