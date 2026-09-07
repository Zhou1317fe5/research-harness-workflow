#!/usr/bin/env python3
"""Mechanical readiness and closing-route decision for a Mission review."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from mission_completion import (
    CLOSED_STATES,
    TERMINAL_REMOTE_STATES,
    claim_completion_errors,
    ingest_completion_errors,
    read_mission_csv,
)


SCHEMA_VERSION = "mission.final-ready.v2"
RISK_LEVELS = {"L0", "L1", "L2", "L3", "L4"}
CLOSING_CONTEXT_FIELDS = {
    "risk_level",
    "independent_prerun_covered",
    "scientific_contract_changed_since_prerun",
    "evidence_conflict",
    "current_scope_gap_suspected",
}


def _load_handoff_lint():
    path = Path(__file__).with_name("lint_handoff.py")
    spec = importlib.util.spec_from_file_location("mission_handoff_lint", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load handoff lint: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HANDOFF = _load_handoff_lint()


def _error(errors: list[str], code: str, path: str, detail: str) -> None:
    errors.append(f"{code}: {path}: {detail}")


def _resolve_file(value: Any, workdir: Path, field: str, errors: list[str]) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        _error(errors, "path_missing", field, "expected non-empty path")
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workdir / path
    path = path.resolve()
    try:
        path.relative_to(workdir)
    except ValueError:
        _error(errors, "path_outside_workdir", field, str(path))
        return None
    if not path.is_file():
        _error(errors, "file_missing", field, str(path))
        return None
    return path


def _text_list(value: Any, field: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        _error(errors, "type_invalid", field, "expected non-empty string array")
        return []
    return [item.strip() for item in value]


def _read_csv(path: Path, review_row_id: str, errors: list[str]) -> list[dict[str, str]]:
    try:
        _, rows, _ = read_mission_csv(path, allow_compat=True)
    except (OSError, csv.Error, UnicodeError, ValueError) as exc:
        _error(errors, "csv_read_failed", "csv_path", str(exc))
        return []

    ids: set[str] = set()
    for index, row in enumerate(rows, start=2):
        row_id = row.get("id", "")
        if not row_id or row_id in ids:
            _error(errors, "row_id_invalid", f"csv:{index}", row_id or "missing")
        ids.add(row_id)
        if row_id == review_row_id:
            continue
        for field, expected in CLOSED_STATES.items():
            if row.get(field) != expected:
                _error(
                    errors,
                    "row_not_closed",
                    f"{row_id}.{field}",
                    f"expected {expected}",
                )
        remote_state = row.get("remote_state", "not_applicable")
        if remote_state not in TERMINAL_REMOTE_STATES:
            _error(
                errors,
                "remote_state_not_terminal",
                f"{row_id}.remote_state",
                remote_state or "missing",
            )
    if review_row_id not in ids:
        _error(errors, "review_row_missing", "review_row_id", review_row_id)
    return rows


def _validate_claims(path: Path, errors: list[str]) -> None:
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _error(errors, "claim_ledger_invalid", "claims_path", str(exc))
        return
    claims = ledger.get("claims") if isinstance(ledger, dict) else None
    if not isinstance(claims, list) or not claims:
        _error(errors, "claim_ledger_invalid", "claims_path", "claims array required")
        return
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict) or not claim.get("claim_id"):
            _error(errors, "claim_invalid", f"claims[{index}]", "claim_id required")
            continue
        status = claim.get("status")
        if not isinstance(status, str) or status not in {
            "verified", "not_run_by_preregistered_gate", "out_of_scope"
        }:
            _error(
                errors,
                "claim_not_terminal",
                str(claim.get("claim_id")),
                str(status or "missing"),
            )
        if status == "not_run_by_preregistered_gate" and not claim.get("gate_evidence"):
            _error(
                errors,
                "claim_gate_evidence_missing",
                str(claim.get("claim_id")),
                "gate_evidence required",
            )
        if (
            status == "verified"
            and claim.get("evidence_required") == "real_e2e"
            and not claim.get("evidence_refs")
        ):
            _error(
                errors,
                "claim_real_e2e_evidence_missing",
                str(claim.get("claim_id")),
                "evidence_refs required",
            )


def _closing_route(value: Any, errors: list[str]) -> tuple[str | None, list[str]]:
    reasons: list[str] = []
    if not isinstance(value, dict):
        _error(errors, "closing_context_invalid", "closing_context", "expected object")
        return None, reasons
    for field in sorted(set(value) - CLOSING_CONTEXT_FIELDS):
        _error(errors, "unknown_field", f"closing_context.{field}", "not allowed")
    for field in sorted(CLOSING_CONTEXT_FIELDS - set(value)):
        _error(errors, "missing_field", f"closing_context.{field}", "required")
    risk = value.get("risk_level")
    if risk not in RISK_LEVELS:
        _error(errors, "risk_level_invalid", "closing_context.risk_level", str(risk))
    for field in sorted(CLOSING_CONTEXT_FIELDS - {"risk_level"}):
        if not isinstance(value.get(field), bool):
            _error(errors, "type_invalid", f"closing_context.{field}", "expected boolean")
    if errors:
        return None, reasons
    if value["evidence_conflict"]:
        reasons.append("evidence_conflict")
    if value["current_scope_gap_suspected"]:
        reasons.append("current_scope_gap_suspected")
    if reasons:
        return "independent_review_required", reasons
    if risk in {"L0", "L1", "L2"}:
        return "evidence_close", ["risk_l0_l2"]
    if (
        value["independent_prerun_covered"]
        and not value["scientific_contract_changed_since_prerun"]
    ):
        return "evidence_close", ["equivalent_prerun_coverage"]
    if not value["independent_prerun_covered"]:
        reasons.append("high_risk_without_independent_prerun")
    if value["scientific_contract_changed_since_prerun"]:
        reasons.append("scientific_contract_changed_since_prerun")
    return "independent_review_required", reasons


def check_final_ready(payload: Any, *, workdir: Path | None = None) -> dict[str, Any]:
    """Return a stable fail-closed final-review readiness decision."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        payload = {}
        _error(errors, "type_invalid", "request", "expected object")
    allowed = {
        "schema_version",
        "csv_path",
        "review_row_id",
        "review_path",
        "handoff_path",
        "claims_path",
        "expected_run_ids",
        "required_review_tokens",
        "provenance_paths",
        "protected_paths_clean",
        "closing_context",
    }
    for field in sorted(set(payload) - allowed):
        _error(errors, "unknown_field", field, "not allowed")
    if payload.get("schema_version") != SCHEMA_VERSION:
        _error(errors, "schema_invalid", "schema_version", f"expected {SCHEMA_VERSION}")

    root = (workdir or Path.cwd()).expanduser().resolve()
    review_row_id = payload.get("review_row_id")
    if not isinstance(review_row_id, str) or not review_row_id.startswith("REVIEW-"):
        _error(errors, "review_row_invalid", "review_row_id", "expected REVIEW-* id")
        review_row_id = ""

    csv_path = _resolve_file(payload.get("csv_path"), root, "csv_path", errors)
    review_path = _resolve_file(payload.get("review_path"), root, "review_path", errors)
    handoff_path = _resolve_file(payload.get("handoff_path"), root, "handoff_path", errors)
    claims_path = (
        _resolve_file(payload.get("claims_path"), root, "claims_path", errors)
        if payload.get("claims_path") is not None
        else None
    )
    run_ids = _text_list(payload.get("expected_run_ids", []), "expected_run_ids", errors)
    tokens = _text_list(
        payload.get("required_review_tokens", []), "required_review_tokens", errors
    )
    provenance = _text_list(
        payload.get("provenance_paths", []), "provenance_paths", errors
    )
    route, route_reasons = _closing_route(payload.get("closing_context"), errors)

    if csv_path is not None and review_row_id:
        rows = _read_csv(csv_path, review_row_id, errors)
        errors.extend(ingest_completion_errors(csv_path, rows, workdir=root))
        errors.extend(claim_completion_errors(csv_path, rows, workdir=root))
        actual_run_ids = sorted(
            {row.get("run_id", "").strip() for row in rows if row.get("run_id", "").strip()}
        )
        for run_id in actual_run_ids:
            if run_id not in run_ids:
                _error(
                    errors,
                    "expected_run_missing",
                    "expected_run_ids",
                    run_id,
                )

    if review_path is not None:
        review_text = review_path.read_text(encoding="utf-8")
        summary = review_text.split("Appendix: Execution Log", 1)[0]
        for token in run_ids + tokens:
            if token not in summary:
                _error(errors, "review_summary_stale", "review_path", f"missing {token}")

    if handoff_path is not None:
        for detail in HANDOFF.lint(handoff_path.read_text(encoding="utf-8")):
            _error(errors, "handoff_lint_failed", "handoff_path", detail)

    if claims_path is not None:
        _validate_claims(claims_path, errors)

    for index, value in enumerate(provenance):
        _resolve_file(value, root, f"provenance_paths[{index}]", errors)

    if payload.get("protected_paths_clean") is not True:
        _error(
            errors,
            "protected_paths_dirty",
            "protected_paths_clean",
            "must be true after scoped git-status check",
        )

    errors = sorted(set(errors))
    normalized = {
        "csv_path": str(csv_path) if csv_path else None,
        "review_path": str(review_path) if review_path else None,
        "handoff_path": str(handoff_path) if handoff_path else None,
        "claims_path": str(claims_path) if claims_path else None,
        "review_row_id": review_row_id,
        "expected_run_ids": run_ids,
        "required_review_tokens": tokens,
        "provenance_paths": provenance,
        "closing_context": payload.get("closing_context"),
        "route_reasons": route_reasons,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ready": not errors,
        "decision": route if not errors else "repair_same_review_row",
        "errors": errors,
        **normalized,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="JSON request path, or - for stdin")
    parser.add_argument("--workdir", default=".")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read() if args.request == "-" else Path(args.request).read_text()
        payload = json.loads(raw)
        result = check_final_ready(payload, workdir=Path(args.workdir))
    except (OSError, json.JSONDecodeError) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "ready": False,
            "decision": "repair_same_review_row",
            "errors": [f"request_read_failed: request: {exc}"],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
