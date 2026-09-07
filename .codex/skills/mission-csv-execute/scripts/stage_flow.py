#!/usr/bin/env python3
"""Stateless Mission progression for a reviewed multi-stage ExecutionPlan."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


REQUEST_SCHEMA = "mission.stage-flow.v2"
STATE_SCHEMA = "mission.execution-state.v2"
STAGE_STATES = {"pending", "running", "succeeded", "failed", "skipped"}
GATE_KINDS = {"effect_prediction", "preregistered_stop", "correctness", "safety", "attribution"}


def _load_prerun_core():
    path = (
        Path(__file__).resolve().parents[2]
        / "pre-run-implementation-review"
        / "scripts"
        / "prerun_core.py"
    )
    spec = importlib.util.spec_from_file_location("mission_prerun_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load prerun core: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CORE = _load_prerun_core()


def _load_prerun_route():
    path = (
        Path(__file__).resolve().parents[2]
        / "pre-run-implementation-review"
        / "scripts"
        / "prerun_route.py"
    )
    spec = importlib.util.spec_from_file_location("mission_prerun_route", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load prerun route: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTE = _load_prerun_route()


def _error(errors: list[str], code: str, path: str, detail: str) -> None:
    errors.append(f"{code}: {path}: {detail}")


def _base(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": REQUEST_SCHEMA,
        "execution_plan_sha256": validation["execution_plan_sha256"],
        "semantic_fingerprint": validation["semantic_fingerprint"],
        "scientific_snapshot_sha256": validation["scientific_snapshot_sha256"],
        "execution_binding_sha256": validation["execution_binding_sha256"],
        "bookkeeping_sha256": validation["bookkeeping_sha256"],
        "change_route": validation.get("_change_route"),
        "advisories": validation.get("_advisories", []),
        "requires_scientific_review": False,
        "state_patch": {},
    }


def _result(
    validation: dict[str, Any],
    decision: str,
    *,
    errors: list[str] | None = None,
    reason_codes: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        **_base(validation),
        "decision": decision,
        "reason_codes": reason_codes or [],
        "errors": sorted(set(errors or [])),
        **extra,
    }


def _descendants(graph: dict[str, Any], root: str) -> set[str]:
    result = {root}
    changed = True
    while changed:
        changed = False
        for stage, config in graph.items():
            if stage in result:
                continue
            if any(dependency in result for dependency in config["depends_on"]):
                result.add(stage)
                changed = True
    return result


def _dependency_closure(graph: dict[str, Any], stage_id: str) -> set[str]:
    result: set[str] = set()
    pending = list(graph[stage_id]["depends_on"])
    while pending:
        dependency = pending.pop()
        if dependency in result:
            continue
        result.add(dependency)
        pending.extend(graph[dependency]["depends_on"])
    return result


def _validate_manifest(
    plan: dict[str, Any],
    stage_id: str,
    manifest: Any,
    errors: list[str],
) -> None:
    path = f"manifests.{stage_id}"
    if not isinstance(manifest, dict):
        _error(errors, "manifest_missing", path, "expected rrctl manifest object")
        return
    if manifest.get("stage_id") != stage_id:
        _error(errors, "manifest_identity_invalid", f"{path}.stage_id", stage_id)
    if manifest.get("status") != "succeeded":
        _error(errors, "manifest_status_invalid", f"{path}.status", "succeeded required")
    if manifest.get("plan_id") != plan["plan_id"]:
        _error(
            errors,
            "manifest_identity_invalid",
            f"{path}.plan_id",
            f"expected {plan['plan_id']}",
        )


def _validate_request(
    request: Any,
    validation: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(request, dict):
        _error(errors, "type_invalid", "request", "expected object")
        return {}, errors
    allowed = {
        "schema_version",
        "execution_plan",
        "mission_state",
        "manifests",
        "scientific_gate_results",
        "manual_overrides",
        "retry",
        "change_route",
    }
    for field in sorted(set(request) - allowed):
        _error(errors, "unknown_field", field, "not allowed")
    if request.get("schema_version") != REQUEST_SCHEMA:
        _error(
            errors,
            "schema_invalid",
            "schema_version",
            f"expected {REQUEST_SCHEMA}",
        )

    state = request.get("mission_state")
    if not isinstance(state, dict):
        _error(errors, "type_invalid", "mission_state", "expected object")
        state = {}
    state_allowed = {
        "schema_version",
        "reviewed_execution_plan_sha256",
        "reviewed_review_snapshot_sha256",
        "reviewed_semantic_fingerprint",
        "validated_rrctl_binding_sha256",
        "implementation_reviewed_commit",
        "prerun_passed",
        "stages",
    }
    for field in sorted(set(state) - state_allowed):
        _error(errors, "unknown_field", f"mission_state.{field}", "not allowed")
    if state.get("schema_version") != STATE_SCHEMA:
        _error(
            errors,
            "schema_invalid",
            "mission_state.schema_version",
            f"expected {STATE_SCHEMA}",
        )
    if state.get("prerun_passed") is not True:
        _error(
            errors,
            "prerun_required",
            "mission_state.prerun_passed",
            "must be true",
        )
    reviewed_commit = state.get("implementation_reviewed_commit")
    if not isinstance(reviewed_commit, str) or not CORE.COMMIT_RE.fullmatch(
        reviewed_commit
    ):
        _error(
            errors,
            "implementation_reviewed_commit_invalid",
            "mission_state.implementation_reviewed_commit",
            "expected reviewed 40-character commit",
        )
    plan = request.get("execution_plan")
    if isinstance(plan, dict):
        for gate, config in plan.get("scientific_gates", {}).items():
            kind = config.get("kind", "preregistered_stop")
            if not isinstance(kind, str) or kind not in GATE_KINDS:
                _error(
                    errors, "scientific_gate_kind_invalid",
                    f"execution_plan.scientific_gates.{gate}.kind", str(kind),
                )
    stages = state.get("stages")
    if not isinstance(stages, dict):
        _error(errors, "type_invalid", "mission_state.stages", "expected object")
        stages = {}
    plan_stages = set(plan.get("stage_graph", {})) if isinstance(plan, dict) else set()
    if set(stages) != plan_stages:
        _error(
            errors,
            "stage_state_mismatch",
            "mission_state.stages",
            f"missing={sorted(plan_stages - set(stages))} "
            f"extra={sorted(set(stages) - plan_stages)}",
        )
    for stage, status in sorted(stages.items()):
        if status not in STAGE_STATES:
            _error(
                errors,
                "stage_state_invalid",
                f"mission_state.stages.{stage}",
                str(status),
            )

    if not isinstance(request.get("manifests"), dict):
        _error(errors, "type_invalid", "manifests", "expected object")
    gate_results = request.get("scientific_gate_results")
    if not isinstance(gate_results, dict):
        _error(
            errors,
            "type_invalid",
            "scientific_gate_results",
            "expected object",
        )
    else:
        known_gates = (
            set(plan.get("scientific_gates", {})) if isinstance(plan, dict) else set()
        )
        for gate, result in sorted(gate_results.items()):
            if gate not in known_gates:
                _error(
                    errors,
                    "unknown_scientific_gate",
                    f"scientific_gate_results.{gate}",
                    "not declared by ExecutionPlan",
                )
            if not isinstance(result, bool):
                _error(
                    errors,
                    "type_invalid",
                    f"scientific_gate_results.{gate}",
                    "expected boolean",
                )
    overrides = request.get("manual_overrides")
    if not isinstance(overrides, dict):
        _error(errors, "type_invalid", "manual_overrides", "expected object")

    retry = request.get("retry")
    if retry is not None and not isinstance(retry, dict):
        _error(errors, "type_invalid", "retry", "expected object or null")

    return state, errors


def advance_stage(request: Any) -> dict[str, Any]:
    """Return a deterministic transition proposal without mutating Mission state."""
    plan = request.get("execution_plan", {}) if isinstance(request, dict) else {}
    validation = CORE.validate_execution_plan(plan)
    if not validation["valid"]:
        return _result(
            validation,
            "blocked",
            errors=validation["errors"],
            reason_codes=["execution_plan_invalid"],
        )

    state, request_errors = _validate_request(request, validation)
    validation = {**validation, "_advisories": []}
    if request_errors:
        return _result(
            validation,
            "blocked",
            errors=request_errors,
            reason_codes=["request_invalid"],
        )

    if request["manual_overrides"]:
        return _result(
            validation,
            "blocked",
            reason_codes=["manual_override_unsupported"],
            errors=[
                "manual_override_unsupported: manual_overrides: stage_flow cannot "
                "safely materialize untracked overrides"
            ],
        )

    if state["implementation_reviewed_commit"] != plan["reviewed_commit"]:
        route_manifest = request.get("change_route")
        route_result = (
            ROUTE.classify_change_route(route_manifest)
            if route_manifest is not None
            else None
        )
        if route_result is not None and (
            route_result.get("reviewed_commit")
            != state["implementation_reviewed_commit"]
            or route_result.get("candidate_commit") != plan["reviewed_commit"]
        ):
            return _result(
                validation,
                "blocked",
                reason_codes=["change_route_commit_mismatch"],
                errors=[
                    "change_route_commit_mismatch: change_route: reviewed/candidate "
                    "commits must bind mission state and ExecutionPlan"
                ],
            )
        if (
            route_result is not None
            and route_result.get("valid") is True
            and route_result.get("route")
            in {"no_prerun", "micro_validation", "smoke_validation"}
            and route_result.get("validation_passed") is True
        ):
            validation["_change_route"] = route_result
            validation["_advisories"].append(
                f"change_route:{route_result['route']}"
            )
        else:
            review_mode = (
                route_result.get("review_mode")
                if route_result is not None
                else "scientific_review"
            )
            reason = (
                f"change_route:{route_result.get('route')}"
                if route_result is not None and route_result.get("valid") is True
                else "code_snapshot_changed_unclassified"
            )
            result = _result(
                validation,
                "implementation_review_required",
                reason_codes=[reason],
                review_mode=review_mode,
            )
            result["requires_scientific_review"] = True
            return result

    graph = plan["stage_graph"]
    stages = state["stages"]
    manifests = request["manifests"]
    gate_results = request["scientific_gate_results"]

    retry = request.get("retry")
    if retry is not None:
        allowed = {
            "stage_id",
            "execution_plan_sha256",
            "semantic_fingerprint",
            "template_sha256",
            "run_spec_sha256",
        }
        retry_errors: list[str] = []
        for field in sorted(set(retry) - allowed):
            _error(retry_errors, "unknown_field", f"retry.{field}", "not allowed")
        stage_id = retry.get("stage_id")
        if stage_id not in graph:
            _error(retry_errors, "unknown_stage", "retry.stage_id", str(stage_id))
        elif stages[stage_id] != "failed":
            _error(
                retry_errors,
                "retry_state_invalid",
                f"mission_state.stages.{stage_id}",
                "failed required",
            )
        if retry_errors:
            return _result(
                validation,
                "blocked",
                errors=retry_errors,
                reason_codes=["retry_invalid"],
            )
        for dependency in sorted(_dependency_closure(graph, stage_id)):
            _validate_manifest(plan, dependency, manifests.get(dependency), retry_errors)
        if retry_errors:
            return _result(
                validation,
                "blocked",
                errors=retry_errors,
                reason_codes=["upstream_manifest_invalid"],
                stage_id=stage_id,
            )
        materialized = CORE.materialize_stage(plan, stage_id, manifests)
        if not materialized["ready"]:
            return _result(
                validation,
                "blocked",
                errors=materialized["errors"],
                reason_codes=["stage_materialization_failed"],
                stage_id=stage_id,
            )
        return _result(
            validation,
            "retry",
            reason_codes=["idempotent_retry"],
            stage_id=stage_id,
            template_sha256=materialized["template_sha256"],
            run_spec_sha256=materialized["run_spec_sha256"],
            run_spec=materialized["run_spec"],
            provenance=materialized["provenance"],
        )

    pending = sorted(stage for stage, status in stages.items() if status == "pending")
    if not pending:
        if any(status in {"running", "failed"} for status in stages.values()):
            return _result(
                validation,
                "wait",
                reason_codes=["nonterminal_stage_exists"],
            )
        return _result(validation, "complete", reason_codes=["all_stages_terminal"])

    for stage_id in pending:
        dependencies = graph[stage_id]["depends_on"]
        if any(stages[dependency] == "skipped" for dependency in dependencies):
            skipped = sorted(
                stage
                for stage in _descendants(graph, stage_id)
                if stages[stage] == "pending"
            )
            return _result(
                validation,
                "skipped",
                reason_codes=["upstream_stage_skipped"],
                stage_id=stage_id,
                state_patch={"stages": {stage: "skipped" for stage in skipped}},
            )
        if not all(stages[dependency] == "succeeded" for dependency in dependencies):
            continue

        manifest_errors: list[str] = []
        for dependency in sorted(_dependency_closure(graph, stage_id)):
            _validate_manifest(plan, dependency, manifests.get(dependency), manifest_errors)
        if manifest_errors:
            return _result(
                validation,
                "blocked",
                errors=manifest_errors,
                reason_codes=["upstream_manifest_invalid"],
                stage_id=stage_id,
            )

        scientific_gate = graph[stage_id].get("scientific_gate")
        advisory_gate = (
            scientific_gate is not None
            and plan["scientific_gates"][scientific_gate].get("kind") == "effect_prediction"
        )
        if advisory_gate:
            if gate_results.get(scientific_gate) is not True:
                status = "false" if scientific_gate in gate_results else "pending"
                validation["_advisories"].append(f"scientific_gate_{status}:{scientific_gate}")
        elif scientific_gate is not None:
            if scientific_gate not in gate_results:
                return _result(
                    validation,
                    "wait",
                    reason_codes=["scientific_gate_pending"],
                    stage_id=stage_id,
                    scientific_gate=scientific_gate,
                )
            if gate_results[scientific_gate] is False:
                descendants = _descendants(graph, stage_id)
                conflicting = sorted(
                    stage
                    for stage in descendants
                    if stages[stage] in {"running", "succeeded"}
                )
                if conflicting:
                    return _result(
                        validation,
                        "blocked",
                        errors=[
                            "scientific_gate_state_conflict: "
                            f"mission_state.stages: active descendants={conflicting}"
                        ],
                        reason_codes=["scientific_gate_state_conflict"],
                        stage_id=stage_id,
                    )
                skipped = sorted(
                    stage for stage in descendants if stages[stage] == "pending"
                )
                return _result(
                    validation,
                    "skipped",
                    reason_codes=["scientific_gate_false"],
                    stage_id=stage_id,
                    scientific_gate=scientific_gate,
                    gate_result_source="mission_scientific_gate",
                    state_patch={"stages": {stage: "skipped" for stage in skipped}},
                )

        materialized = CORE.materialize_stage(plan, stage_id, manifests)
        if not materialized["ready"]:
            return _result(
                validation,
                "blocked",
                errors=materialized["errors"],
                reason_codes=["stage_materialization_failed"],
                stage_id=stage_id,
            )
        return _result(
            validation,
            "ready",
            reason_codes=["reviewed_stage_ready"],
            stage_id=stage_id,
            template_sha256=materialized["template_sha256"],
            run_spec_sha256=materialized["run_spec_sha256"],
            run_spec=materialized["run_spec"],
            provenance=materialized["provenance"],
            **(
                {
                    "scientific_gate": scientific_gate,
                    "gate_result_source": "mission_scientific_gate",
                }
                if scientific_gate is not None
                else {}
            ),
        )

    return _result(
        validation,
        "wait",
        reason_codes=["upstream_stage_not_succeeded"],
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="JSON request path, or - for stdin")
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.read()) if args.request == "-" else _read_json(Path(args.request))
        result = advance_stage(request)
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as error:
        result = {
            "schema_version": REQUEST_SCHEMA,
            "decision": "blocked",
            "reason_codes": ["request_unreadable"],
            "errors": [f"request_unreadable: request: {error}"],
            "requires_scientific_review": False,
            "state_patch": {},
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["decision"] not in {
        "blocked",
        "implementation_review_required",
        "conformance_revalidation",
    } else 2


if __name__ == "__main__":
    sys.exit(main())
