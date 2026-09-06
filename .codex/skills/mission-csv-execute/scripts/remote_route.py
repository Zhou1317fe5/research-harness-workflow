#!/usr/bin/env python3
"""Deterministic route decision for Mission remote execution rows."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "mission.remote-route.v1"
OWNERS = {None, "rrctl", "legacy"}
LIFECYCLES = {"closed", "running_remote", "not_started", "failed_retry"}
CHECK_STATES = {"not_checked", "passed", "failed"}
CONTROL_ROLES = {"stage", "launch", "health", "cleanup"}
EXECUTION_PURPOSES = {
    "official",
    "pre_review_smoke",
    "preregistered_read_only_probe",
}


def _load_prerun_route():
    path = (
        Path(__file__).resolve().parents[2]
        / "pre-run-implementation-review"
        / "scripts"
        / "prerun_route.py"
    )
    spec = importlib.util.spec_from_file_location("remote_prerun_route", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load prerun route: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTE = _load_prerun_route()


def _error(errors: list[str], code: str, path: str, detail: str) -> None:
    errors.append(f"{code}: {path}: {detail}")


def _non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_legacy_exception(value: Any, errors: list[str]) -> bool:
    if not isinstance(value, dict):
        _error(
            errors,
            "legacy_exception_incomplete",
            "legacy_exception",
            "expected object",
        )
        return False

    allowed = {
        "reason",
        "migration_deadline",
        "migration_issue",
        "responsible_component",
    }
    for field in sorted(set(value) - allowed):
        _error(errors, "unknown_field", f"legacy_exception.{field}", "not allowed")

    complete = True
    if not _non_empty_text(value.get("reason")):
        _error(
            errors,
            "legacy_exception_incomplete",
            "legacy_exception.reason",
            "required",
        )
        complete = False
    if not (
        _non_empty_text(value.get("migration_deadline"))
        or _non_empty_text(value.get("migration_issue"))
    ):
        _error(
            errors,
            "legacy_exception_incomplete",
            "legacy_exception.migration",
            "migration_deadline or migration_issue required",
        )
        complete = False
    if not _non_empty_text(value.get("responsible_component")):
        _error(
            errors,
            "legacy_exception_incomplete",
            "legacy_exception.responsible_component",
            "required",
        )
        complete = False
    return complete


def _validate_rrctl(value: Any, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _error(errors, "type_invalid", "rrctl", "expected object")
        return {
            "available": False,
            "readiness": "not_checked",
            "launch": "not_checked",
        }

    allowed = {"available", "readiness", "launch"}
    for field in sorted(set(value) - allowed):
        _error(errors, "unknown_field", f"rrctl.{field}", "not allowed")

    available = value.get("available")
    if not isinstance(available, bool):
        _error(errors, "type_invalid", "rrctl.available", "expected boolean")
        available = False

    readiness = value.get("readiness")
    if readiness not in CHECK_STATES:
        _error(
            errors,
            "value_invalid",
            "rrctl.readiness",
            "expected not_checked, passed, or failed",
        )
        readiness = "not_checked"

    launch = value.get("launch")
    if launch not in CHECK_STATES:
        _error(
            errors,
            "value_invalid",
            "rrctl.launch",
            "expected not_checked, passed, or failed",
        )
        launch = "not_checked"

    if launch == "passed" and readiness != "passed":
        _error(
            errors,
            "state_invalid",
            "rrctl.launch",
            "launch passed requires readiness passed",
        )

    return {
        "available": available,
        "readiness": readiness,
        "launch": launch,
    }


def _validate_pre_review_smoke(
    value: Any, errors: list[str]
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        _error(errors, "type_invalid", "pre_review_smoke", "expected object")
        return None

    allowed = {
        "candidate_commit",
        "max_steps",
        "gpu_count",
        "isolated_output",
        "official_metrics_disabled",
        "artifact_ingest_disabled",
        "user_authorized",
        "production_command_bound",
        "fail_on_output_collision",
        "checkpoint_cleanup_required",
    }
    for field in sorted(set(value) - allowed):
        _error(errors, "unknown_field", f"pre_review_smoke.{field}", "not allowed")
    for field in sorted(allowed - set(value)):
        _error(errors, "missing_field", f"pre_review_smoke.{field}", "required")

    commit = value.get("candidate_commit")
    if not isinstance(commit, str) or not ROUTE.COMMIT_RE.fullmatch(commit):
        _error(
            errors,
            "commit_invalid",
            "pre_review_smoke.candidate_commit",
            "expected 40 lowercase hex",
        )
        commit = ""
    max_steps = value.get("max_steps")
    if (
        not isinstance(max_steps, int)
        or isinstance(max_steps, bool)
        or not 1 <= max_steps <= 100
    ):
        _error(
            errors,
            "value_invalid",
            "pre_review_smoke.max_steps",
            "expected integer from 1 through 100",
        )
        max_steps = 0
    gpu_count = value.get("gpu_count")
    if (
        not isinstance(gpu_count, int)
        or isinstance(gpu_count, bool)
        or gpu_count < 1
    ):
        _error(
            errors,
            "value_invalid",
            "pre_review_smoke.gpu_count",
            "expected positive integer",
        )
        gpu_count = 0
    flags: dict[str, bool] = {}
    for field in (
        "isolated_output",
        "official_metrics_disabled",
        "artifact_ingest_disabled",
        "user_authorized",
        "production_command_bound",
        "fail_on_output_collision",
        "checkpoint_cleanup_required",
    ):
        flag = value.get(field)
        if not isinstance(flag, bool):
            _error(
                errors,
                "type_invalid",
                f"pre_review_smoke.{field}",
                "expected boolean",
            )
            flag = False
        elif flag is not True:
            _error(
                errors,
                "smoke_boundary_invalid",
                f"pre_review_smoke.{field}",
                "must be true",
            )
        flags[field] = flag
    return {
        "candidate_commit": commit,
        "max_steps": max_steps,
        "gpu_count": gpu_count,
        **flags,
    }


def _validate_preregistered_read_only_probe(
    value: Any, errors: list[str]
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        _error(
            errors,
            "type_invalid",
            "preregistered_read_only_probe",
            "expected object",
        )
        return None

    allowed = {
        "candidate_commit",
        "base_validation_only",
        "parameter_updates_disabled",
        "official_access_disabled",
        "artifact_ingest_disabled",
        "user_authorized",
        "production_command_bound",
        "fail_on_output_collision",
        "preregistered_before_review",
    }
    for field in sorted(set(value) - allowed):
        _error(
            errors,
            "unknown_field",
            f"preregistered_read_only_probe.{field}",
            "not allowed",
        )
    for field in sorted(allowed - set(value)):
        _error(
            errors,
            "missing_field",
            f"preregistered_read_only_probe.{field}",
            "required",
        )

    commit = value.get("candidate_commit")
    if not isinstance(commit, str) or not ROUTE.COMMIT_RE.fullmatch(commit):
        _error(
            errors,
            "commit_invalid",
            "preregistered_read_only_probe.candidate_commit",
            "expected 40 lowercase hex",
        )
        commit = ""
    flags: dict[str, bool] = {}
    for field in sorted(allowed - {"candidate_commit"}):
        flag = value.get(field)
        if not isinstance(flag, bool):
            _error(
                errors,
                "type_invalid",
                f"preregistered_read_only_probe.{field}",
                "expected boolean",
            )
            flag = False
        elif flag is not True:
            _error(
                errors,
                "probe_boundary_invalid",
                f"preregistered_read_only_probe.{field}",
                "must be true",
            )
        flags[field] = flag
    return {"candidate_commit": commit, **flags}


def decide_remote_route(payload: Any) -> dict[str, Any]:
    """Validate a route request and return a stable fail-closed decision."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        payload = {}
        _error(errors, "type_invalid", "request", "expected object")

    allowed = {
        "schema_version",
        "execution_kind",
        "lifecycle",
        "has_running_evidence",
        "command_owner",
        "legacy_exception",
        "rrctl",
        "code_changed",
        "change_manifest",
        "formal_review",
        "custom_control_scripts",
        "execution_purpose",
        "pre_review_smoke",
        "preregistered_read_only_probe",
    }
    for field in sorted(set(payload) - allowed):
        _error(errors, "unknown_field", field, "not allowed")

    if payload.get("schema_version") != SCHEMA_VERSION:
        _error(
            errors,
            "schema_invalid",
            "schema_version",
            f"expected {SCHEMA_VERSION}",
        )

    execution_kind = payload.get("execution_kind")
    if execution_kind not in {"local", "remote"}:
        _error(
            errors,
            "value_invalid",
            "execution_kind",
            "expected local or remote",
        )

    lifecycle = payload.get("lifecycle")
    if lifecycle not in LIFECYCLES:
        _error(
            errors,
            "value_invalid",
            "lifecycle",
            "expected closed, running_remote, not_started, or failed_retry",
        )

    has_running_evidence = payload.get("has_running_evidence")
    if not isinstance(has_running_evidence, bool):
        _error(
            errors,
            "type_invalid",
            "has_running_evidence",
            "expected boolean",
        )
        has_running_evidence = False

    owner = payload.get("command_owner")
    if owner not in OWNERS:
        _error(
            errors,
            "value_invalid",
            "command_owner",
            "expected null, rrctl, or legacy",
        )

    rrctl = _validate_rrctl(payload.get("rrctl"), errors)

    execution_purpose = payload.get("execution_purpose", "official")
    if execution_purpose not in EXECUTION_PURPOSES:
        _error(
            errors,
            "value_invalid",
            "execution_purpose",
            "expected official, pre_review_smoke, or preregistered_read_only_probe",
        )
        execution_purpose = "official"
    pre_review_smoke = _validate_pre_review_smoke(
        payload.get("pre_review_smoke"), errors
    )
    read_only_probe = _validate_preregistered_read_only_probe(
        payload.get("preregistered_read_only_probe"), errors
    )
    if execution_purpose == "pre_review_smoke" and pre_review_smoke is None:
        _error(
            errors,
            "pre_review_smoke_missing",
            "pre_review_smoke",
            "required for pre_review_smoke execution",
        )
    if execution_purpose != "pre_review_smoke" and pre_review_smoke is not None:
        _error(
            errors,
            "pre_review_smoke_unexpected",
            "pre_review_smoke",
            "only allowed for pre_review_smoke execution",
        )
    if (
        execution_purpose == "preregistered_read_only_probe"
        and read_only_probe is None
    ):
        _error(
            errors,
            "preregistered_read_only_probe_missing",
            "preregistered_read_only_probe",
            "required for preregistered_read_only_probe execution",
        )
    if (
        execution_purpose != "preregistered_read_only_probe"
        and read_only_probe is not None
    ):
        _error(
            errors,
            "preregistered_read_only_probe_unexpected",
            "preregistered_read_only_probe",
            "only allowed for preregistered_read_only_probe execution",
        )
    if execution_purpose == "pre_review_smoke" and execution_kind != "remote":
        _error(
            errors,
            "pre_review_smoke_remote_required",
            "execution_kind",
            "pre_review_smoke requires remote execution",
        )

    code_changed = payload.get("code_changed", False)
    if not isinstance(code_changed, bool):
        _error(errors, "type_invalid", "code_changed", "expected boolean")
        code_changed = True
    formal_review = payload.get("formal_review")
    normalized_review: dict[str, Any] | None = None
    if formal_review is not None:
        if not isinstance(formal_review, dict):
            _error(errors, "type_invalid", "formal_review", "expected object")
        else:
            allowed_review = {
                "passed",
                "candidate_commit",
                "review_mode",
                "review_result",
                "reviewer_id",
                "closure_evidence_paths",
            }
            for field in sorted(set(formal_review) - allowed_review):
                _error(errors, "unknown_field", f"formal_review.{field}", "not allowed")
            passed = formal_review.get("passed")
            candidate = formal_review.get("candidate_commit")
            review_mode = formal_review.get("review_mode")
            review_result = formal_review.get("review_result")
            reviewer_id = formal_review.get("reviewer_id")
            closure_paths = formal_review.get("closure_evidence_paths", [])
            if not isinstance(passed, bool):
                _error(errors, "type_invalid", "formal_review.passed", "expected boolean")
                passed = False
            if not isinstance(candidate, str) or not ROUTE.COMMIT_RE.fullmatch(candidate):
                _error(
                    errors,
                    "commit_invalid",
                    "formal_review.candidate_commit",
                    "expected 40 lowercase hex",
                )
                candidate = ""
            if review_mode not in {"scientific_review", "targeted_review"}:
                _error(
                    errors,
                    "value_invalid",
                    "formal_review.review_mode",
                    "expected scientific_review or targeted_review",
                )
                review_mode = "scientific_review"
            if review_result not in {
                "scientifically_correct",
                "scientifically_incorrect",
                "not_evaluable",
                "targeted_correct",
                "targeted_incorrect",
            }:
                _error(
                    errors,
                    "value_invalid",
                    "formal_review.review_result",
                    "expected a single scientific or targeted review result",
                )
                review_result = "not_evaluable"
            if review_mode == "scientific_review" and review_result not in {
                "scientifically_correct",
                "scientifically_incorrect",
                "not_evaluable",
            }:
                _error(
                    errors,
                    "formal_review_mode_result_mismatch",
                    "formal_review.review_result",
                    "scientific_review requires a scientific result",
                )
            if review_mode == "targeted_review" and review_result not in {
                "targeted_correct",
                "targeted_incorrect",
                "not_evaluable",
            }:
                _error(
                    errors,
                    "formal_review_mode_result_mismatch",
                    "formal_review.review_result",
                    "targeted_review requires a targeted result",
                )
            if not _non_empty_text(reviewer_id):
                _error(
                    errors,
                    "type_invalid",
                    "formal_review.reviewer_id",
                    "required",
                )
                reviewer_id = ""
            if not isinstance(closure_paths, list) or any(
                not _non_empty_text(item) for item in closure_paths
            ):
                _error(
                    errors,
                    "type_invalid",
                    "formal_review.closure_evidence_paths",
                    "expected string array",
                )
                closure_paths = []
            direct_pass = review_result in {
                "scientifically_correct",
                "targeted_correct",
            }
            repaired_pass = review_result in {
                "scientifically_incorrect",
                "targeted_incorrect",
            } and bool(closure_paths)
            if passed and not (direct_pass or repaired_pass):
                _error(
                    errors,
                    "formal_review_pass_invalid",
                    "formal_review.passed",
                    "pass requires a correct verdict or explicit blocker-closure evidence",
                )
            normalized_review = {
                "passed": passed,
                "candidate_commit": candidate,
                "review_mode": review_mode,
                "review_result": review_result,
                "reviewer_id": reviewer_id,
                "closure_evidence_paths": closure_paths,
            }
    custom_control_scripts = payload.get("custom_control_scripts", [])
    if not isinstance(custom_control_scripts, list) or any(
        not isinstance(item, dict) for item in custom_control_scripts
    ):
        _error(
            errors,
            "type_invalid",
            "custom_control_scripts",
            "expected object array",
        )
        custom_control_scripts = []
    normalized_scripts: list[dict[str, str]] = []
    for index, item in enumerate(custom_control_scripts):
        allowed_script = {"path", "role"}
        for field in sorted(set(item) - allowed_script):
            _error(
                errors,
                "unknown_field",
                f"custom_control_scripts[{index}].{field}",
                "not allowed",
            )
        path = item.get("path")
        role = item.get("role")
        if not _non_empty_text(path):
            _error(
                errors,
                "type_invalid",
                f"custom_control_scripts[{index}].path",
                "required",
            )
            path = ""
        if role not in CONTROL_ROLES | {"adapter", "project_semantics"}:
            _error(
                errors,
                "value_invalid",
                f"custom_control_scripts[{index}].role",
                "expected stage, launch, health, cleanup, adapter, or project_semantics",
            )
            role = ""
        normalized_scripts.append({"path": path, "role": role})

    change_result = None
    if payload.get("change_manifest") is not None:
        change_result = ROUTE.classify_change_route(payload["change_manifest"])

    base = {
        "schema_version": SCHEMA_VERSION,
        "actionable": lifecycle in {"not_started", "failed_retry"},
        "fallback_allowed": False,
        "change_route": change_result,
        "formal_review": normalized_review,
        "execution_purpose": execution_purpose,
        "pre_review_smoke": pre_review_smoke,
        "preregistered_read_only_probe": read_only_probe,
    }
    if errors:
        return {
            **base,
            "decision": "blocked",
            "route": None,
            "reason_codes": ["request_invalid"],
            "errors": sorted(errors),
        }

    if execution_purpose == "pre_review_smoke":
        if not code_changed or change_result is None:
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["pre_review_smoke_change_route_missing"],
                "errors": [
                    "pre_review_smoke_change_route_missing: change_manifest: changed code and risk route are required"
                ],
            }
        if change_result.get("valid") is not True:
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["change_route_invalid"],
                "errors": change_result.get("errors", []),
            }
        if change_result.get("route") != "full_review":
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["pre_review_smoke_route_invalid"],
                "errors": [
                    "pre_review_smoke_route_invalid: change_manifest: full_review required"
                ],
            }
        if (
            not pre_review_smoke
            or pre_review_smoke["candidate_commit"]
            != change_result.get("candidate_commit")
        ):
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["pre_review_smoke_commit_mismatch"],
                "errors": [
                    "pre_review_smoke_commit_mismatch: pre_review_smoke.candidate_commit: must bind change manifest candidate commit"
                ],
            }
        if owner == "legacy":
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["pre_review_smoke_requires_rrctl"],
                "errors": [
                    "pre_review_smoke_requires_rrctl: command_owner: legacy is forbidden"
                ],
            }

    if execution_purpose == "preregistered_read_only_probe":
        if not code_changed or change_result is None:
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["read_only_probe_change_route_missing"],
                "errors": [
                    "read_only_probe_change_route_missing: change_manifest: changed code and risk route are required"
                ],
            }
        if change_result.get("valid") is not True:
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["change_route_invalid"],
                "errors": change_result.get("errors", []),
            }
        if change_result.get("route") != "full_review":
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["read_only_probe_route_invalid"],
                "errors": [
                    "read_only_probe_route_invalid: change_manifest: full_review required"
                ],
            }
        if (
            not read_only_probe
            or read_only_probe["candidate_commit"]
            != change_result.get("candidate_commit")
        ):
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["read_only_probe_commit_mismatch"],
                "errors": [
                    "read_only_probe_commit_mismatch: preregistered_read_only_probe.candidate_commit: must bind change manifest candidate commit"
                ],
            }
        if owner == "legacy":
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["read_only_probe_requires_rrctl"],
                "errors": [
                    "read_only_probe_requires_rrctl: command_owner: legacy is forbidden"
                ],
            }

    if execution_kind == "local":
        return {
            **base,
            "actionable": False,
            "decision": "not_applicable",
            "route": None,
            "reason_codes": ["local_execution"],
            "errors": [],
        }

    if lifecycle == "closed":
        return {
            **base,
            "actionable": False,
            "decision": "read_only",
            "route": owner,
            "reason_codes": ["closed_history_preserved"],
            "errors": [],
        }

    if lifecycle == "running_remote":
        if not has_running_evidence:
            return {
                **base,
                "actionable": False,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["running_evidence_missing"],
                "errors": [
                    "running_evidence_missing: has_running_evidence: "
                    "running_remote requires real control or process evidence"
                ],
            }
        route = (
            "rrctl"
            if execution_purpose
            in {"pre_review_smoke", "preregistered_read_only_probe"}
            or owner == "rrctl"
            else "legacy"
        )
        if route == "rrctl" and not rrctl["available"]:
            return {
                **base,
                "actionable": False,
                "decision": "blocked",
                "route": "rrctl",
                "reason_codes": ["rrctl_unavailable"],
                "errors": ["rrctl_unavailable: rrctl.available: false"],
            }
        return {
            **base,
            "actionable": False,
            "decision": "resume",
            "route": route,
            "reason_codes": [
                "running_rrctl_resume"
                if route == "rrctl"
                else "running_legacy_preserved"
            ],
            "errors": [],
        }

    if code_changed:
        if change_result is None:
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["change_route_missing"],
                "errors": [
                    "change_route_missing: change_manifest: changed code requires risk route"
                ],
            }
        if change_result.get("valid") is not True:
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": ["change_route_invalid"],
                "errors": change_result.get("errors", []),
            }
        if (
            execution_purpose == "official"
            and change_result.get("route") in {"targeted_review", "full_review"}
        ):
            candidate = change_result.get("candidate_commit")
            if not (
                normalized_review
                and normalized_review["passed"] is True
                and normalized_review["candidate_commit"] == candidate
            ):
                return {
                    **base,
                    "decision": "blocked",
                    "route": None,
                    "reason_codes": ["formal_review_required"],
                    "errors": [
                        "formal_review_required: formal_review: passing review must bind candidate commit"
                    ],
                }
        if change_result.get("route") in {
            "micro_validation",
            "smoke_validation",
        } and not change_result.get("validation_passed"):
            failed_route = change_result["route"]
            return {
                **base,
                "decision": "blocked",
                "route": None,
                "reason_codes": [f"{failed_route}_failed"],
                "errors": [
                    f"{failed_route}_failed: change_manifest: production probes required"
                ],
            }

    if lifecycle == "failed_retry":
        route = "rrctl"
        reason_codes = ["failed_retry_migrates_to_rrctl"]
    elif owner == "legacy":
        legacy_errors: list[str] = []
        complete = _validate_legacy_exception(
            payload.get("legacy_exception"), legacy_errors
        )
        if not complete or legacy_errors:
            return {
                **base,
                "decision": "blocked",
                "route": "legacy",
                "reason_codes": ["legacy_exception_incomplete"],
                "errors": sorted(legacy_errors),
            }
        return {
            **base,
            "decision": "proceed",
            "route": "legacy",
            "reason_codes": ["legacy_exception_accepted"],
            "errors": [],
        }
    else:
        route = "rrctl"
        reason_codes = (
            ["pre_review_smoke_authorized"]
            if execution_purpose == "pre_review_smoke"
            else (
                ["preregistered_read_only_probe_authorized"]
                if execution_purpose == "preregistered_read_only_probe"
                else [
                    "default_owner_rrctl"
                    if owner is None
                    else "explicit_owner_rrctl"
                ]
            )
        )

    forbidden_controls = sorted(
        item["path"]
        for item in normalized_scripts
        if item["role"] in CONTROL_ROLES
    )
    if route == "rrctl" and forbidden_controls:
        return {
            **base,
            "decision": "blocked",
            "route": route,
            "reason_codes": [*reason_codes, "rrctl_control_plane_bypass"],
            "errors": [
                "rrctl_control_plane_bypass: custom_control_scripts: "
                + ",".join(forbidden_controls)
            ],
        }

    if not rrctl["available"]:
        return {
            **base,
            "decision": "blocked",
            "route": route,
            "reason_codes": [*reason_codes, "rrctl_unavailable"],
            "errors": ["rrctl_unavailable: rrctl.available: false"],
        }
    if rrctl["readiness"] == "failed":
        return {
            **base,
            "decision": "blocked",
            "route": route,
            "reason_codes": [*reason_codes, "rrctl_readiness_failed"],
            "errors": ["rrctl_readiness_failed: rrctl.readiness: failed"],
        }
    if rrctl["launch"] == "failed":
        return {
            **base,
            "decision": "blocked",
            "route": route,
            "reason_codes": [*reason_codes, "rrctl_launch_failed"],
            "errors": ["rrctl_launch_failed: rrctl.launch: failed"],
        }
    return {
        **base,
        "decision": "proceed",
        "route": route,
        "reason_codes": reason_codes,
        "errors": [],
    }


def _load_json(path: str) -> Any:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="Route request JSON path, or - for stdin")
    args = parser.parse_args()
    try:
        result = decide_remote_route(_load_json(args.request))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        result = {
            "schema_version": SCHEMA_VERSION,
            "actionable": False,
            "fallback_allowed": False,
            "decision": "blocked",
            "route": None,
            "reason_codes": ["request_unreadable"],
            "errors": [f"request_unreadable: request: {error}"],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["decision"] != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
