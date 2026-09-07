#!/usr/bin/env python3
"""Deterministic ExecutionPlan validation and stage materialization."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


EXECUTION_PLAN_SCHEMA = "prerun.execution-plan.v2"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VALUE_TYPES = {"string", "integer", "number", "boolean", "object", "array"}
PLAN_FIELDS = {
    "schema_version",
    "plan_id",
    "source_intent",
    "reviewed_commit",
    "stage_graph",
    "runspec_templates",
    "adapter_contracts",
    "scientific_gates",
    "late_bound_fields",
    "rrctl_binding",
    "critical_surface_rules",
}
LATE_BOUND_FIELDS = {
    "path",
    "source_stage",
    "manifest_selector",
    "value_type",
    "required",
    "identity",
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _error(errors: list[str], code: str, path: str, detail: str) -> None:
    errors.append(f"{code}: {path}: {detail}")


def _mapping(value: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _error(errors, "type_invalid", path, "expected object")
        return {}
    return value


def _text(value: Any, path: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(errors, "type_invalid", path, "expected non-empty text")
        return ""
    return value.strip()


def _identifier(value: Any, path: str, errors: list[str]) -> str:
    result = _text(value, path, errors)
    if result and not IDENTIFIER_RE.fullmatch(result):
        _error(errors, "identifier_invalid", path, result)
    return result


def _reject_unknown(
    mapping: dict[str, Any], allowed: set[str], path: str, errors: list[str]
) -> None:
    for field in sorted(set(mapping) - allowed):
        _error(errors, "unknown_field", f"{path}.{field}", "not allowed")


def _pointer_parts(pointer: Any, path: str, errors: list[str]) -> list[str]:
    text = _text(pointer, path, errors)
    if not text:
        return []
    if not text.startswith("/") or text == "/":
        _error(errors, "json_pointer_invalid", path, "must start with / and name a value")
        return []
    parts: list[str] = []
    for raw in text[1:].split("/"):
        if "~" in re.sub(r"~[01]", "", raw):
            _error(errors, "json_pointer_invalid", path, "invalid ~ escape")
            return []
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    return parts


def _pointer_get(value: Any, parts: list[str]) -> tuple[bool, Any]:
    current = value
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < len(current):
                current = current[index]
                continue
        return False, None
    return True, current


def _pointer_set(value: Any, parts: list[str], replacement: Any) -> bool:
    if not parts:
        return False
    current = value
    for part in parts[:-1]:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return False
    final = parts[-1]
    if isinstance(current, dict) and final in current:
        current[final] = replacement
        return True
    if isinstance(current, list) and final.isdigit() and int(final) < len(current):
        current[int(final)] = replacement
        return True
    return False


def _is_placeholder(value: Any) -> bool:
    return value is None or value == {"$late_bound": True}


def _value_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return False


def _dependency_closure(
    stage_graph: dict[str, dict[str, Any]], stage: str
) -> set[str]:
    result: set[str] = set()
    pending = list(stage_graph.get(stage, {}).get("depends_on", []))
    while pending:
        dependency = pending.pop()
        if dependency in result:
            continue
        result.add(dependency)
        pending.extend(stage_graph.get(dependency, {}).get("depends_on", []))
    return result


def _validate_stage_graph(
    raw_graph: Any,
    templates: dict[str, Any],
    scientific_gates: dict[str, Any],
    errors: list[str],
) -> dict[str, dict[str, Any]]:
    graph = _mapping(raw_graph, "plan.stage_graph", errors)
    if not graph:
        _error(errors, "value_invalid", "plan.stage_graph", "must not be empty")
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for stage in sorted(graph):
        _identifier(stage, f"plan.stage_graph.{stage}", errors)
        config = _mapping(graph[stage], f"plan.stage_graph.{stage}", errors)
        _reject_unknown(
            config,
            {"depends_on", "scientific_gate"},
            f"plan.stage_graph.{stage}",
            errors,
        )
        dependencies = config.get("depends_on")
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or not item for item in dependencies
        ):
            _error(
                errors,
                "type_invalid",
                f"plan.stage_graph.{stage}.depends_on",
                "expected string array",
            )
            dependencies = []
        if len(set(dependencies)) != len(dependencies):
            _error(
                errors,
                "duplicate_dependency",
                f"plan.stage_graph.{stage}.depends_on",
                "dependencies must be unique",
            )
        for dependency in dependencies:
            if dependency == stage:
                _error(
                    errors,
                    "self_dependency",
                    f"plan.stage_graph.{stage}.depends_on",
                    stage,
                )
            elif dependency not in graph:
                _error(
                    errors,
                    "unknown_dependency",
                    f"plan.stage_graph.{stage}.depends_on",
                    dependency,
                )
        gate = config.get("scientific_gate")
        if gate is not None and (
            not isinstance(gate, str) or gate not in scientific_gates
        ):
            _error(
                errors,
                "unknown_scientific_gate",
                f"plan.stage_graph.{stage}.scientific_gate",
                str(gate),
            )
        normalized[stage] = {
            "depends_on": list(dependencies),
            **({"scientific_gate": gate} if gate is not None else {}),
        }
    if set(graph) != set(templates):
        missing = sorted(set(graph) - set(templates))
        extra = sorted(set(templates) - set(graph))
        _error(
            errors,
            "stage_template_mismatch",
            "plan.runspec_templates",
            f"missing={missing} extra={extra}",
        )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(stage: str) -> None:
        if stage in visiting:
            _error(errors, "stage_cycle", "plan.stage_graph", stage)
            return
        if stage in visited:
            return
        visiting.add(stage)
        for dependency in normalized.get(stage, {}).get("depends_on", []):
            visit(dependency)
        visiting.remove(stage)
        visited.add(stage)

    for stage in sorted(normalized):
        visit(stage)
    return normalized


def _split_template_scope(template: Any) -> tuple[Any, Any, Any]:
    """Split one RunSpec template into scientific, execution, and bookkeeping scopes."""

    if not isinstance(template, dict):
        return copy.deepcopy(template), {}, {}
    scientific = copy.deepcopy(template)
    execution: dict[str, Any] = {}
    bookkeeping: dict[str, Any] = {}
    for field in ("run_id", "remote", "session", "health", "local_pull_root"):
        if field in scientific:
            execution[field] = scientific.pop(field)
    source = scientific.get("source")
    if isinstance(source, dict):
        source_execution = {}
        for field in ("repo_root", "branch", "bundle_sha256", "transport_bundle_sha256"):
            if field in source:
                source_execution[field] = source.pop(field)
        if source_execution:
            execution["source"] = source_execution
    metadata = scientific.get("metadata")
    if isinstance(metadata, dict) and "gate_provenance" in metadata:
        bookkeeping["gate_provenance"] = metadata.pop("gate_provenance")
        if not metadata:
            scientific.pop("metadata", None)
    return scientific, execution, bookkeeping


def _review_scope_payloads(plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    scientific_templates: dict[str, Any] = {}
    execution_templates: dict[str, Any] = {}
    bookkeeping_templates: dict[str, Any] = {}
    templates = plan.get("runspec_templates")
    if isinstance(templates, dict):
        for stage, template in sorted(templates.items()):
            scientific, execution, bookkeeping = _split_template_scope(template)
            scientific_templates[stage] = scientific
            if execution:
                execution_templates[stage] = execution
            if bookkeeping:
                bookkeeping_templates[stage] = bookkeeping
    else:
        scientific_templates = copy.deepcopy(templates)
    scientific = {
        "schema_version": plan.get("schema_version"),
        "source_intent": plan.get("source_intent"),
        "reviewed_commit": plan.get("reviewed_commit"),
        "stage_graph": plan.get("stage_graph"),
        "runspec_templates": scientific_templates,
        "adapter_contracts": plan.get("adapter_contracts"),
        "scientific_gates": plan.get("scientific_gates"),
        "late_bound_fields": plan.get("late_bound_fields"),
        "critical_surface_rules": plan.get("critical_surface_rules", []),
    }
    execution = {
        "rrctl_binding": plan.get("rrctl_binding"),
        "runspec_templates": execution_templates,
    }
    bookkeeping = {
        "plan_id": plan.get("plan_id"),
        "gate_provenance": bookkeeping_templates,
    }
    return scientific, execution, bookkeeping


def _semantic_payload(plan: dict[str, Any]) -> dict[str, Any]:
    """Return the scientific snapshot payload."""

    scientific, _, _ = _review_scope_payloads(plan)
    return scientific


def _changed_paths(previous: Any, candidate: Any, prefix: str = "") -> list[str]:
    if isinstance(previous, dict) and isinstance(candidate, dict):
        paths: list[str] = []
        for key in sorted(set(previous) | set(candidate)):
            child = f"{prefix}/{key}"
            if key not in previous or key not in candidate:
                paths.append(child)
            else:
                paths.extend(_changed_paths(previous[key], candidate[key], child))
        return paths
    if isinstance(previous, list) and isinstance(candidate, list):
        paths = []
        for index in range(max(len(previous), len(candidate))):
            child = f"{prefix}/{index}"
            if index >= len(previous) or index >= len(candidate):
                paths.append(child)
            else:
                paths.extend(_changed_paths(previous[index], candidate[index], child))
        return paths
    return [] if previous == candidate else [prefix or "/"]


def validate_execution_plan(value: Any) -> dict[str, Any]:
    errors: list[str] = []
    plan = _mapping(value, "plan", errors)
    plan_digest = digest_json(value)
    scientific_payload, execution_payload, bookkeeping_payload = _review_scope_payloads(plan)
    scientific_snapshot_sha256 = digest_json(scientific_payload)
    execution_binding_sha256 = digest_json(execution_payload)
    bookkeeping_sha256 = digest_json(bookkeeping_payload)
    semantic_fingerprint = scientific_snapshot_sha256
    rrctl_binding_sha256 = digest_json(plan.get("rrctl_binding"))
    plan_without_binding = copy.deepcopy(plan)
    plan_without_binding.pop("rrctl_binding", None)
    review_snapshot_sha256 = digest_json(plan_without_binding)
    if not plan:
        if not errors:
            _error(errors, "value_invalid", "plan", "must not be empty")
        return {
            "valid": False,
            "errors": sorted(set(errors)),
            "execution_plan_sha256": plan_digest,
            "semantic_fingerprint": semantic_fingerprint,
            "scientific_snapshot_sha256": scientific_snapshot_sha256,
            "execution_binding_sha256": execution_binding_sha256,
            "bookkeeping_sha256": bookkeeping_sha256,
            "rrctl_binding_sha256": rrctl_binding_sha256,
            "review_snapshot_sha256": review_snapshot_sha256,
        }
    _reject_unknown(plan, PLAN_FIELDS, "plan", errors)
    required = PLAN_FIELDS - {"critical_surface_rules"}
    for field in sorted(required - set(plan)):
        _error(errors, "missing_field", f"plan.{field}", "required")
    if plan.get("schema_version") != EXECUTION_PLAN_SCHEMA:
        _error(
            errors,
            "schema_version_invalid",
            "plan.schema_version",
            f"expected {EXECUTION_PLAN_SCHEMA}",
        )
    _identifier(plan.get("plan_id"), "plan.plan_id", errors)
    _mapping(plan.get("source_intent"), "plan.source_intent", errors)
    commit = plan.get("reviewed_commit")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        _error(
            errors,
            "commit_invalid",
            "plan.reviewed_commit",
            "expected 40 lowercase hexadecimal characters",
        )
    templates = _mapping(
        plan.get("runspec_templates"), "plan.runspec_templates", errors
    )
    for stage, template in sorted(templates.items()):
        _identifier(stage, f"plan.runspec_templates.{stage}", errors)
        _mapping(template, f"plan.runspec_templates.{stage}", errors)
    adapters = _mapping(
        plan.get("adapter_contracts"), "plan.adapter_contracts", errors
    )
    gates = _mapping(plan.get("scientific_gates"), "plan.scientific_gates", errors)
    graph = _validate_stage_graph(plan.get("stage_graph"), templates, gates, errors)
    for name, contract in sorted(adapters.items()):
        _identifier(name, f"plan.adapter_contracts.{name}", errors)
        _mapping(contract, f"plan.adapter_contracts.{name}", errors)
    for name, gate in sorted(gates.items()):
        _identifier(name, f"plan.scientific_gates.{name}", errors)
        _mapping(gate, f"plan.scientific_gates.{name}", errors)
    binding = _mapping(plan.get("rrctl_binding"), "plan.rrctl_binding", errors)
    _reject_unknown(
        binding,
        {"release", "conformance_digest"},
        "plan.rrctl_binding",
        errors,
    )
    _text(binding.get("release"), "plan.rrctl_binding.release", errors)
    rules = plan.get("critical_surface_rules", [])
    if not isinstance(rules, list) or any(
        not isinstance(item, str) or not item for item in rules
    ):
        _error(
            errors,
            "type_invalid",
            "plan.critical_surface_rules",
            "expected string array",
        )
    late_fields = plan.get("late_bound_fields")
    if not isinstance(late_fields, list):
        _error(errors, "type_invalid", "plan.late_bound_fields", "expected array")
        late_fields = []
    seen_paths: set[str] = set()
    for index, raw in enumerate(late_fields):
        path = f"plan.late_bound_fields[{index}]"
        field = _mapping(raw, path, errors)
        _reject_unknown(field, LATE_BOUND_FIELDS, path, errors)
        for required_field in sorted(LATE_BOUND_FIELDS - set(field)):
            _error(errors, "missing_field", f"{path}.{required_field}", "required")
        target_parts = _pointer_parts(field.get("path"), f"{path}.path", errors)
        source = _identifier(field.get("source_stage"), f"{path}.source_stage", errors)
        selector_parts = _pointer_parts(
            field.get("manifest_selector"), f"{path}.manifest_selector", errors
        )
        value_type = field.get("value_type")
        if value_type not in VALUE_TYPES:
            _error(
                errors,
                "value_type_invalid",
                f"{path}.value_type",
                str(value_type),
            )
        if not isinstance(field.get("required"), bool):
            _error(
                errors,
                "type_invalid",
                f"{path}.required",
                "expected boolean",
            )
        identity = _mapping(field.get("identity"), f"{path}.identity", errors)
        for selector in sorted(identity):
            _pointer_parts(selector, f"{path}.identity.{selector}", errors)
        raw_path = field.get("path")
        if isinstance(raw_path, str):
            if raw_path in seen_paths:
                _error(errors, "duplicate_late_bound_path", f"{path}.path", raw_path)
            seen_paths.add(raw_path)
        target_stage = ""
        if len(target_parts) < 3 or target_parts[0] != "runspec_templates":
            _error(
                errors,
                "late_bound_target_invalid",
                f"{path}.path",
                "must point inside /runspec_templates/<stage>/...",
            )
        else:
            target_stage = target_parts[1]
            if target_stage not in graph:
                _error(
                    errors,
                    "unknown_target_stage",
                    f"{path}.path",
                    target_stage,
                )
        if source and source not in graph:
            _error(errors, "unknown_source_stage", f"{path}.source_stage", source)
        elif source and target_stage and source not in _dependency_closure(
            graph, target_stage
        ):
            _error(
                errors,
                "late_bound_source_not_dependency",
                f"{path}.source_stage",
                f"{source} is not upstream of {target_stage}",
            )
        found, current = _pointer_get(plan, target_parts)
        if not found:
            _error(errors, "late_bound_target_missing", f"{path}.path", str(raw_path))
        elif not _is_placeholder(current):
            _error(
                errors,
                "late_bound_target_not_placeholder",
                f"{path}.path",
                "manual or pre-bound values are forbidden",
            )
        if not selector_parts:
            continue
    return {
        "valid": not errors,
        "errors": sorted(set(errors)),
        "execution_plan_sha256": plan_digest,
        "semantic_fingerprint": semantic_fingerprint,
        "scientific_snapshot_sha256": scientific_snapshot_sha256,
        "execution_binding_sha256": execution_binding_sha256,
        "bookkeeping_sha256": bookkeeping_sha256,
        "rrctl_binding_sha256": rrctl_binding_sha256,
        "review_snapshot_sha256": review_snapshot_sha256,
    }


def materialize_stage(
    plan: Any, stage_id: str, manifests: dict[str, Any]
) -> dict[str, Any]:
    validation = validate_execution_plan(plan)
    errors = list(validation["errors"])
    if not validation["valid"]:
        return {
            "ready": False,
            "errors": errors,
            "execution_plan_sha256": validation["execution_plan_sha256"],
            "semantic_fingerprint": validation["semantic_fingerprint"],
        }
    if stage_id not in plan["runspec_templates"]:
        _error(errors, "unknown_stage", "stage_id", stage_id)
        return {
            "ready": False,
            "errors": sorted(set(errors)),
            "execution_plan_sha256": validation["execution_plan_sha256"],
            "semantic_fingerprint": validation["semantic_fingerprint"],
        }
    bound_plan = copy.deepcopy(plan)
    provenance: list[dict[str, Any]] = []
    for index, field in enumerate(plan["late_bound_fields"]):
        target_parts = _pointer_parts(
            field["path"], f"plan.late_bound_fields[{index}].path", errors
        )
        if len(target_parts) < 2 or target_parts[1] != stage_id:
            continue
        source = field["source_stage"]
        manifest = manifests.get(source)
        if manifest is None:
            if field["required"]:
                _error(
                    errors,
                    "required_manifest_missing",
                    f"manifests.{source}",
                    f"required by {field['path']}",
                )
            continue
        if not isinstance(manifest, dict):
            _error(errors, "manifest_type_invalid", f"manifests.{source}", "object")
            continue
        if manifest.get("stage_id") != source or manifest.get("status") != "succeeded":
            _error(
                errors,
                "manifest_identity_invalid",
                f"manifests.{source}",
                "stage_id/status must identify a succeeded upstream stage",
            )
            continue
        manifest_digest = manifest.get("manifest_sha256")
        identity_valid = True
        for selector, expected in sorted(field["identity"].items()):
            identity_parts = _pointer_parts(
                selector,
                f"plan.late_bound_fields[{index}].identity.{selector}",
                errors,
            )
            found, actual = _pointer_get(manifest, identity_parts)
            if not found or actual != expected:
                _error(
                    errors,
                    "manifest_identity_mismatch",
                    f"manifests.{source}{selector}",
                    f"expected {expected!r}",
                )
                identity_valid = False
        selector_parts = _pointer_parts(
            field["manifest_selector"],
            f"plan.late_bound_fields[{index}].manifest_selector",
            errors,
        )
        found, replacement = _pointer_get(manifest, selector_parts)
        if not found:
            _error(
                errors,
                "manifest_selector_missing",
                f"manifests.{source}{field['manifest_selector']}",
                "value not found",
            )
            continue
        if not _value_matches(replacement, field["value_type"]):
            _error(
                errors,
                "manifest_value_type_invalid",
                f"manifests.{source}{field['manifest_selector']}",
                f"expected {field['value_type']}",
            )
            continue
        if not identity_valid:
            continue
        if not _pointer_set(bound_plan, target_parts, copy.deepcopy(replacement)):
            _error(errors, "late_bound_target_missing", field["path"], "value not set")
            continue
        provenance.append(
            {
                "path": field["path"],
                "source_stage": source,
                "manifest_selector": field["manifest_selector"],
                "manifest_sha256": manifest_digest,
                "value_sha256": digest_json(replacement),
            }
        )
    if errors:
        return {
            "ready": False,
            "errors": sorted(set(errors)),
            "execution_plan_sha256": validation["execution_plan_sha256"],
            "semantic_fingerprint": validation["semantic_fingerprint"],
        }
    run_spec = bound_plan["runspec_templates"][stage_id]
    return {
        "ready": True,
        "errors": [],
        "execution_plan_sha256": validation["execution_plan_sha256"],
        "semantic_fingerprint": validation["semantic_fingerprint"],
        "template_sha256": digest_json(plan["runspec_templates"][stage_id]),
        "run_spec_sha256": digest_json(run_spec),
        "run_spec": run_spec,
        "provenance": provenance,
    }
