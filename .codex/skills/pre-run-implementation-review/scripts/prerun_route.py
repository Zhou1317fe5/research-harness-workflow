#!/usr/bin/env python3
"""Risk-route a committed change before PRERUN reviewer selection."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA = "prerun.change-route.v1"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
NON_RUNTIME_CLASSES = {
    "documentation",
    "review_log",
    "handoff",
    "csv_state",
    "evidence_index",
    "test_only",
    "formatting",
}
MICRO_CLASSES = {
    "environment_binding",
    "branch_binding",
    "timeout_binding",
    "shell_syntax",
    "run_identity",
}
# Scaffolding watches or moves the computation; it never computes a result.
# A real smoke run exercises these paths end to end, which is stronger evidence
# than a static reviewer reading concurrency code, so they never invoke the
# scientific reviewer.
SCAFFOLDING_CLASSES = {
    "artifact_transport",
    "blocker_fix",
    "cleanup_ownership",
    "process_supervision",
    "run_monitoring",
    "scheduler_plumbing",
}
TARGETED_CLASSES = {
    "credential",
    "destructive_lifecycle",
}
FULL_CLASSES = {
    "model",
    "attention",
    "loss",
    "data",
    "metric",
    "checkpoint",
    "scientific_args",
    "computation_sink",
    "dependency",
    "entrypoint",
    "public_interface",
    "result_attribution",
    "unknown",
}
KNOWN_CLASSES = (
    NON_RUNTIME_CLASSES
    | MICRO_CLASSES
    | SCAFFOLDING_CLASSES
    | TARGETED_CLASSES
    | FULL_CLASSES
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _error(errors: list[str], code: str, path: str, detail: str) -> None:
    errors.append(f"{code}: {path}: {detail}")


def _text(value: Any, path: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(errors, "type_invalid", path, "expected non-empty text")
        return ""
    return value.strip()


def _validate_probe(raw: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _error(errors, "type_invalid", path, "expected object")
        return {}
    allowed = {"command", "exit_code", "observation", "reaches_production"}
    for field in sorted(set(raw) - allowed):
        _error(errors, "unknown_field", f"{path}.{field}", "not allowed")
    for field in sorted(allowed - set(raw)):
        _error(errors, "missing_field", f"{path}.{field}", "required")
    command = _text(raw.get("command"), f"{path}.command", errors)
    observation = _text(raw.get("observation"), f"{path}.observation", errors)
    exit_code = raw.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        _error(errors, "type_invalid", f"{path}.exit_code", "expected integer")
        exit_code = -1
    reaches = raw.get("reaches_production")
    if not isinstance(reaches, bool):
        _error(
            errors,
            "type_invalid",
            f"{path}.reaches_production",
            "expected boolean",
        )
        reaches = False
    return {
        "command": command,
        "exit_code": exit_code,
        "observation": observation,
        "reaches_production": reaches,
    }


def classify_change_route(value: Any) -> dict[str, Any]:
    """Return a stable, fail-closed route for a change manifest."""
    errors: list[str] = []
    if not isinstance(value, dict):
        value = {}
        _error(errors, "type_invalid", "request", "expected object")
    allowed = {"schema_version", "reviewed_commit", "candidate_commit", "changes"}
    for field in sorted(set(value) - allowed):
        _error(errors, "unknown_field", field, "not allowed")
    if value.get("schema_version") != SCHEMA:
        _error(errors, "schema_invalid", "schema_version", f"expected {SCHEMA}")
    reviewed_commit = value.get("reviewed_commit")
    candidate_commit = value.get("candidate_commit")
    for field, commit in (
        ("reviewed_commit", reviewed_commit),
        ("candidate_commit", candidate_commit),
    ):
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            _error(errors, "commit_invalid", field, "expected 40 lowercase hex")
    raw_changes = value.get("changes")
    if not isinstance(raw_changes, list):
        _error(errors, "type_invalid", "changes", "expected array")
        raw_changes = []

    changes: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, raw in enumerate(raw_changes):
        path = f"changes[{index}]"
        if not isinstance(raw, dict):
            _error(errors, "type_invalid", path, "expected object")
            continue
        allowed_change = {
            "path",
            "change_class",
            "production_reachable",
            "dependency_closure_changed",
            "blocker_ids",
            "probes",
        }
        for field in sorted(set(raw) - allowed_change):
            _error(errors, "unknown_field", f"{path}.{field}", "not allowed")
        for field in sorted(allowed_change - set(raw)):
            _error(errors, "missing_field", f"{path}.{field}", "required")
        changed_path = _text(raw.get("path"), f"{path}.path", errors)
        if changed_path in seen_paths:
            _error(errors, "duplicate_path", f"{path}.path", changed_path)
        seen_paths.add(changed_path)
        change_class = _text(
            raw.get("change_class"), f"{path}.change_class", errors
        )
        if change_class not in KNOWN_CLASSES:
            _error(
                errors,
                "change_class_unknown",
                f"{path}.change_class",
                change_class,
            )
            change_class = "unknown"
        production_reachable = raw.get("production_reachable")
        if not isinstance(production_reachable, bool):
            _error(
                errors,
                "type_invalid",
                f"{path}.production_reachable",
                "expected boolean",
            )
            production_reachable = True
        closure_changed = raw.get("dependency_closure_changed")
        if not isinstance(closure_changed, bool):
            _error(
                errors,
                "type_invalid",
                f"{path}.dependency_closure_changed",
                "expected boolean",
            )
            closure_changed = True
        blocker_ids = raw.get("blocker_ids")
        if not isinstance(blocker_ids, list) or any(
            not isinstance(item, str) or not item for item in blocker_ids
        ):
            _error(errors, "type_invalid", f"{path}.blocker_ids", "string array")
            blocker_ids = []
        probes_raw = raw.get("probes")
        if not isinstance(probes_raw, list):
            _error(errors, "type_invalid", f"{path}.probes", "expected array")
            probes_raw = []
        probes = [
            _validate_probe(item, f"{path}.probes[{probe_index}]", errors)
            for probe_index, item in enumerate(probes_raw)
        ]
        changes.append(
            {
                "path": changed_path,
                "change_class": change_class,
                "production_reachable": production_reachable,
                "dependency_closure_changed": closure_changed,
                "blocker_ids": sorted(set(blocker_ids)),
                "probes": probes,
            }
        )

    classes = {item["change_class"] for item in changes}
    production = [item for item in changes if item["production_reachable"]]
    micro_evidence_ok = bool(production) and all(
        item["probes"]
        and all(
            probe.get("exit_code") == 0 and probe.get("reaches_production") is True
            for probe in item["probes"]
        )
        for item in production
    )
    closure_changed = any(
        item["dependency_closure_changed"] for item in production
    )

    if errors:
        route = "full_review"
        reason_codes = ["manifest_invalid"]
    elif not changes or (
        classes <= NON_RUNTIME_CLASSES and not production
    ):
        route = "no_prerun"
        reason_codes = ["non_runtime_only"]
    elif classes & FULL_CLASSES or closure_changed:
        route = "full_review"
        reason_codes = [
            "full_surface_changed" if classes & FULL_CLASSES else "dependency_closure_changed"
        ]
    elif classes & TARGETED_CLASSES:
        route = "targeted_review"
        reason_codes = ["targeted_risk_or_blocker"]
    elif classes & SCAFFOLDING_CLASSES:
        if micro_evidence_ok:
            route = "smoke_validation"
            reason_codes = ["scaffolding_verified_by_production_run"]
        else:
            route = "full_review"
            reason_codes = ["scaffolding_smoke_evidence_incomplete"]
    elif classes <= (MICRO_CLASSES | NON_RUNTIME_CLASSES) and micro_evidence_ok:
        route = "micro_validation"
        reason_codes = ["bounded_runtime_binding_with_production_probes"]
    else:
        route = "full_review"
        reason_codes = ["micro_evidence_incomplete"]

    normalized = {
        "schema_version": SCHEMA,
        "reviewed_commit": reviewed_commit,
        "candidate_commit": candidate_commit,
        "changes": changes,
    }
    review_mode = {
        "no_prerun": "none",
        "micro_validation": "none",
        "smoke_validation": "none",
        "targeted_review": "targeted_review",
        "full_review": "scientific_review",
    }[route]
    return {
        "schema_version": SCHEMA,
        "valid": not errors,
        "errors": sorted(set(errors)),
        "route": route,
        "reason_codes": reason_codes,
        "review_mode": review_mode,
        "requires_prerun": route in {"targeted_review", "full_review"},
        "requires_pre_review_smoke": route == "full_review",
        "requires_smoke": route == "smoke_validation",
        "reviewer_delta": 0 if review_mode == "none" else 1,
        "validation_passed": route == "no_prerun" or (
            route in {"micro_validation", "smoke_validation"} and micro_evidence_ok
        ),
        "reviewed_commit": reviewed_commit,
        "candidate_commit": candidate_commit,
        "route_sha256": hashlib.sha256(_canonical(normalized)).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(args.request.read_text(encoding="utf-8"))
        result = classify_change_route(payload)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        result = {
            "schema_version": SCHEMA,
            "valid": False,
            "errors": [f"request_unreadable: request: {error}"],
            "route": "full_review",
            "reason_codes": ["request_unreadable"],
            "review_mode": "scientific_review",
            "requires_prerun": True,
            "requires_pre_review_smoke": True,
            "requires_smoke": False,
            "reviewer_delta": 1,
            "validation_passed": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    sys.exit(main())
