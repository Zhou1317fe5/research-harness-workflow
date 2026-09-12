#!/usr/bin/env python3
"""Shared terminal-state checks for Mission closing and recovery."""

from __future__ import annotations

import csv
import io
import json
import re
import sys
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
TERMINAL_REMOTE_STATES = {"not_applicable", "ingested"}
# 只注册参与状态、路由与证据选择的单值键；event/evidence 等历史仍可重复。
SINGLETON_NOTE_KEYS = frozenset({
    "claims", "claim_ledger", "claim_coverage", "claim_coverage_status",
    "outcome_contract", "deferred_ledger", "deferred_findings", "deferred_coverage",
    "source_doc", "execution_scope", "evidence_level", "production_path",
    "review_kind", "review_mode", "review_result", "review_json",
    "review_agent_mode", "review_independence", "review_requested_model",
    "review_observed_model", "review_model_evidence", "scientific_outcome",
    "handoff", "handoff_contract", "handoff_humanized", "gated_run",
    "pre_run_result", "pre_run_code_commit", "blocker_closure_evidence",
    "readiness_result", "command_owner", "legacy_reason",
    "legacy_migration_deadline", "legacy_migration_issue", "legacy_responsible_component",
    "artifact_evidence", "artifact_policy", "formal_attempt", "root_budget_enforced",
    "commit_hash", "git_repo",
})
REVIEW_REQUIRED_TAGS = {
    "review_agent_mode",
    "review_independence",
    "review_result",
    "scientific_outcome",
    "claim_coverage_status",
    "review_json",
    "handoff",
    "handoff_contract",
}
PATH_TAGS = {"review_json", "handoff"}


def read_mission_csv(
    path: Path, *, allow_compat: bool = False, validate_notes: bool = True
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
        if validate_notes:
            try:
                parse_note_tags(row["notes"])
            except ValueError as exc:
                raise ValueError(f"{exc}:row={row_id}") from exc
    return fields, rows, raw.startswith(b"\xef\xbb\xbf")


def parse_note_tags(notes: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in notes.split(";"):
        key, separator, value = item.strip().partition(":")
        if separator and key:
            key, value = key.strip(), value.strip()
            if key in SINGLETON_NOTE_KEYS and key in result and result[key] != value:
                raise ValueError(f"notes_conflict:{key}")
            result[key] = value
    return result


def upsert_note_tags(notes: str, updates: dict[str, str]) -> str:
    """显式替换单值键，保留未涉及的文本及多值事件；不替调用者裁决旧冲突。"""
    if not isinstance(updates, dict) or any(
        key not in SINGLETON_NOTE_KEYS or not isinstance(value, str)
        or not value.strip() or ";" in value or "\n" in value or "\r" in value
        for key, value in updates.items()
    ):
        raise ValueError("set_note_tags_invalid:registered keys and non-empty single values required")
    if not updates:
        parse_note_tags(notes)
        return notes
    parts = [part for part in notes.split(";")
             if part.strip().partition(":")[0].strip() not in updates]
    remaining = ";".join(parts).rstrip()
    suffix = "; ".join(f"{key}:{value.strip()}" for key, value in updates.items())
    result = remaining + ("; " if remaining else "") + suffix
    parse_note_tags(result)
    return result


def row_terminal_errors(row: dict[str, str], *, allow_compat: bool = False) -> list[str]:
    row_id = row.get("id", "") or "<missing-id>"
    errors: list[str] = []
    for field, expected in CLOSED_STATES.items():
        if row.get(field) != expected:
            errors.append(f"row_not_closed:{row_id}.{field}={row.get(field, '')!r}")
    try:
        tags = parse_note_tags(row.get("notes", ""))
    except ValueError as exc:
        return errors + [f"{exc}:row={row_id}"]
    # 兼容入口显式允许缺少远程列；canonical 的缺失值不能获得豁免。
    if allow_compat and "remote_state" not in row:
        return errors
    remote_state = row.get("remote_state", "")
    maintenance_complete = (
        remote_state == "completed" and tags.get("artifact_policy") == "none"
        and not row.get("exp_id", "").strip()
    )
    if remote_state not in TERMINAL_REMOTE_STATES and not maintenance_complete:
        errors.append(f"remote_state_not_terminal:{row_id}.remote_state={remote_state!r}")
    if remote_state == "ingested" and not _ingest_reference(row):
        errors.append(f"ingest_evidence_missing:{row_id}.artifact_path")
    return errors


def _ingest_reference(row: dict[str, str]) -> str:
    return row.get("artifact_path", "").strip() or parse_note_tags(
        row.get("notes", "")
    ).get("artifact_evidence", "")


def resolve_reference_path(value: str, base_dir: Path, workdir: Path) -> Path:
    """只解析显式 workdir 内的引用；相对基准不额外授权目录。"""
    if not value.strip().strip("\"'"):
        raise ValueError("reference_empty")
    root = workdir.resolve()
    path = Path(value.strip().strip("\"'")).expanduser()
    candidates = [path] if path.is_absolute() else [base_dir / path, workdir / path]
    existing = {p.resolve() for p in candidates if p.exists()}
    if any(not p.is_relative_to(root) for p in existing):
        raise ValueError(f"reference_outside_workspace:{value}")
    if len(existing) > 1:
        raise ValueError(f"reference_ambiguous:{value}")
    resolved = next(iter(existing)) if existing else candidates[0].resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"reference_outside_workspace:{value}")
    return resolved


def _resolve_artifact(
    value: str, csv_path: Path, workdir: Path, *, allow_directory: bool = False
) -> Path | None:
    try:
        resolved = resolve_reference_path(value, csv_path.parent, workdir)
    except ValueError:
        return None
    return resolved if resolved.is_file() or (allow_directory and resolved.is_dir()) else None


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
        else:
            errors.extend(_record_completion_errors(csv_path, row, workdir=workdir))
    return errors


def _record_completion_errors(csv_path: Path, row: dict[str, str], *, workdir: Path) -> list[str]:
    """核对当前 run 的既有 RunSpec/manifest/record，不生成新状态或科研结论。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / ".agents"))
    from harness.records.experiment_records import (
        build_record, csv_projection, identifier, load_run_provenance, record_index_row,
    )
    from harness.common.project_config import load_config
    from harness.remote.build_rrctl_runspec import RunSpecBuildError, run_spec_digest

    try:
        exp_id = identifier(row.get("exp_id", ""))
        run_id = identifier(row.get("run_id", ""))
        tags = parse_note_tags(row.get("notes", ""))
        commit = (tags.get("pre_run_code_commit", "") if tags.get("git_repo")
                  else row.get("commit_hash", "")).strip()
        spec_id = row.get("spec_id", "").strip()
        if not spec_id or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError("ingest_source_identity_missing")
        run_root = workdir / "remote_artifacts" / exp_id / run_id
        supplied = _resolve_artifact(_ingest_reference(row), csv_path, workdir, allow_directory=True)
        if supplied is None or (supplied != run_root.parent.resolve() and not supplied.is_relative_to(run_root.resolve())):
            raise ValueError("ingest_artifact_path_does_not_identify_this_run")
        spec, _ = load_run_provenance(csv_path, exp_id, run_id, repo_root=workdir)
        digest = run_spec_digest(spec)
        if spec["source"]["commit"] != commit or spec["metadata"]["spec_id"] != spec_id:
            raise ValueError("ingest_runspec_identity_mismatch")
        config = workdir / ".agents/harness/config/project.toml"
        settings = load_config(config).get("records", {}) if config.is_file() else {}
        expected = build_record(exp_id, csv_projection(workdir).get(exp_id), settings,
                                repo_root=workdir, artifacts=workdir / "remote_artifacts")
        actual = expected["runs"]
        matching = [run for run in actual if run["run_id"] == run_id]
        if not matching or any(run.get("commit") != commit or run.get("run_spec_sha256") != digest for run in matching):
            raise ValueError("ingest_run_evidence_missing_or_mismatched:" + "; ".join(expected["_pending"]))
        record_path = workdir / "research_workspace/experiments" / exp_id / "record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("exp_id") != exp_id or record.get("runs") != actual:
            raise ValueError("ingest_record_stale_or_mismatched")
        source = record.get("source", {})
        for key, value in expected["source"].items():
            if source.get(key) != value:
                raise ValueError(f"ingest_record_source_mismatch:{key}")
        if (any(record.get("metrics", {}).get(key) != expected["metrics"][key]
                for key in ("protocol", "ours_metric")) or record.get("_pending") != expected["_pending"]):
            raise ValueError("ingest_record_projection_mismatch")
        with (workdir / "research_workspace/EXPERIMENTS.csv").open(encoding="utf-8-sig", newline="") as stream:
            index = [item for item in csv.DictReader(stream) if item.get("ExpID") == exp_id]
        if len(index) != 1 or any(index[0].get(key) != str(value) for key, value in record_index_row(record).items()):
            raise ValueError("ingest_index_missing_or_stale")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, RunSpecBuildError) as exc:
        return [f"ingest_record_invalid:{row['id']}:{exc}"]
    return []


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
        errors.extend(validate_ledger(path, csv_path, claims, require_terminal=True, rows=rows, workdir=workdir))
    return errors


def git_completion_errors(
    csv_path: Path, rows: list[dict[str, str]], *, workdir: Path | None = None
) -> list[str]:
    from git_isolation import row_git_errors
    return [error for row in rows for error in row_git_errors(csv_path, row, workdir=workdir)]


def csv_completion_errors(
    csv_path: Path, *, workdir: Path, allow_compat: bool = False
) -> list[str]:
    """Return reasons a CSV is not a fully delivered Mission terminal state."""
    errors: list[str] = []
    try:
        _, rows, _ = read_mission_csv(csv_path, allow_compat=allow_compat)
    except (OSError, csv.Error, UnicodeError, ValueError) as exc:
        return [f"csv_read_failed:{exc}"]
    for row in rows:
        errors.extend(row_terminal_errors(row, allow_compat=allow_compat))
    errors.extend(git_completion_errors(csv_path, rows, workdir=workdir))
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
    if tags.get("review_result") == "vision_met" and tags.get("handoff_contract") != "passed":
        errors.append("handoff_contract_not_passed:vision_met")

    from check_handoff_contract import check_contract, load_outcome_contract
    from run_vision_review import validate_review_result

    try:
        contract, contract_errors = load_outcome_contract(csv_path, workdir=workdir)
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
            errors.extend(check_contract(handoff_path, csv_path, workdir=workdir))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        errors.append(f"review_artifact_invalid:{exc}")
    return sorted(set(errors))
