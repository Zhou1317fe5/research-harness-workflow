#!/usr/bin/env python3
"""Validate the lean packet for one pre-run scientific review."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA = "prerun.scientific-review.v1"
SMOKE_SCHEMA = "prerun.pre-review-smoke.v1"
REVIEW_MODES = {"scientific_review", "targeted_review"}
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_PATTERNS = (
    re.compile(
        r"(?i)(?:password|passwd|pwd|api[_-]?key|access[_-]?token|secret[_-]?key)"
        r"\s*[=:]\s*['\"]?[^\s'\"]+"
    ),
    re.compile(
        r"(?i)--(?:password|passwd|api-key|access-token|token|secret)"
        r"(?:=|\s+)['\"]?[^\s'\"]+"
    ),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
)
EXPERIMENT_FIELDS = (
    "benchmark",
    "dataset",
    "checkpoint",
    "seed",
    "metric_policy",
    "output_path",
)
PACKET_FIELDS = {
    "schema_version",
    "review_mode",
    "repo_root",
    "pre_run_code_commit",
    "review_diff_base_commit",
    "approved_basis",
    "implementation_intent",
    "exact_command",
    "output_collision_policy",
    "local_validation",
    "pre_review_smoke",
    "critical_values",
    "experiment",
}


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def add_error(errors: list[str], code: str, detail: str) -> None:
    errors.append(f"{code}: {detail}")


def add_warning(warnings: list[str], code: str, detail: str) -> None:
    warnings.append(f"{code}: {detail}")


def require_text(
    mapping: dict[str, Any], key: str, prefix: str, errors: list[str]
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        add_error(errors, "missing_or_invalid_field", f"{prefix}.{key} must be non-empty text")
        return ""
    return value.strip()


def git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def committed_diff_paths(
    repo_root: Path, base_commit: str, reviewed_commit: str, errors: list[str]
) -> list[str]:
    result = git(
        repo_root,
        "diff",
        "--name-only",
        f"{base_commit}..{reviewed_commit}",
        "--",
    )
    if result.returncode != 0:
        add_error(
            errors,
            "review_diff_failed",
            result.stderr.strip() or "unable to inspect reviewed implementation diff",
        )
        return []
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def _finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_baseline_equivalence(raw: dict[str, Any], errors: list[str]) -> None:
    """等价性由同一固定评估设置下的有限数值决定，不能只自报 passed。"""
    required = raw.get("baseline_equivalence_required", False)
    prefix = "pre_review_smoke.baseline_equivalence_probe"
    if not isinstance(required, bool):
        add_error(errors, "baseline_equivalence_invalid", "baseline_equivalence_required must be boolean")
    probe = raw.get("baseline_equivalence_probe")
    if probe is None and required is False:
        return
    if not isinstance(probe, dict):
        add_error(errors, "baseline_equivalence_missing", f"{prefix} must be an object")
        return
    for field in (
        "reference_id", "reference_weights_path", "candidate_weights_path", "metric_name"
    ):
        require_text(probe, field, prefix, errors)

    setting = probe.get("evaluation_setting")
    if not isinstance(setting, dict):
        add_error(errors, "baseline_equivalence_invalid", f"{prefix}.evaluation_setting must be an object")
    else:
        for field in ("dataset", "post_processing"):
            require_text(setting, field, f"{prefix}.evaluation_setting", errors)
        if not isinstance(setting.get("parameters"), dict):
            add_error(errors, "baseline_equivalence_invalid", f"{prefix}.evaluation_setting.parameters must be an object")
        seed = setting.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            add_error(errors, "baseline_equivalence_invalid", f"{prefix}.evaluation_setting.seed must be an integer")
        count = setting.get("sample_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            add_error(errors, "baseline_equivalence_invalid", f"{prefix}.evaluation_setting.sample_count must be positive")
    evidence = probe.get("evidence_paths")
    if not isinstance(evidence, list) or not evidence or any(
        not isinstance(item, str) or not item.strip() for item in evidence
    ):
        add_error(errors, "baseline_equivalence_evidence_missing", f"{prefix}.evidence_paths must name the measured comparison")

    numeric_fields = ("reference_metric", "candidate_metric", "absolute_difference", "tolerance")
    invalid = [field for field in numeric_fields if not _finite_number(probe.get(field))]
    for field in invalid:
        add_error(errors, "baseline_equivalence_nonfinite", f"{prefix}.{field} must be a finite number")
    if invalid:
        return
    difference = abs(probe["candidate_metric"] - probe["reference_metric"])
    reported = probe["absolute_difference"]
    tolerance = probe["tolerance"]
    if reported < 0 or tolerance < 0:
        add_error(errors, "baseline_equivalence_invalid", f"{prefix} difference and tolerance must be nonnegative")
    if not math.isclose(reported, difference, rel_tol=1e-9, abs_tol=1e-12):
        add_error(errors, "baseline_equivalence_difference_mismatch", f"{prefix}.absolute_difference must equal abs(candidate_metric-reference_metric)")
    if difference > tolerance:
        add_error(errors, "baseline_equivalence_outside_tolerance", f"{prefix} measured difference {difference} exceeds tolerance {tolerance}")


def validate_smoke(
    packet: dict[str, Any], commit: str, errors: list[str], warnings: list[str]
) -> None:
    raw = packet.get("pre_review_smoke")
    if not isinstance(raw, dict):
        add_error(errors, "pre_review_smoke_missing", "pre_review_smoke must be an object")
        return
    if raw.get("schema_version") != SMOKE_SCHEMA:
        add_error(
            errors,
            "pre_review_smoke_schema_invalid",
            f"pre_review_smoke.schema_version must be {SMOKE_SCHEMA}",
        )

    computation_kind = raw.get("computation_kind", "training")
    if not isinstance(computation_kind, str) or computation_kind not in {"training", "inference"}:
        add_error(
            errors, "pre_review_smoke_computation_kind_invalid",
            "pre_review_smoke.computation_kind must be training or inference",
        )

    disposition = raw.get("disposition")
    if disposition == "not_applicable":
        require_text(raw, "reason", "pre_review_smoke", errors)
        require_text(raw, "alternative_validation", "pre_review_smoke", errors)
        add_warning(
            warnings,
            "pre_review_smoke_not_applicable",
            "no GPU production path; reviewer must inspect the named alternative validation",
        )
        return
    if disposition != "passed":
        add_error(
            errors,
            "pre_review_smoke_disposition_invalid",
            "pre_review_smoke.disposition must be passed or not_applicable",
        )
        return

    candidate = require_text(raw, "candidate_commit", "pre_review_smoke", errors)
    if candidate and candidate != commit:
        add_error(
            errors,
            "pre_review_smoke_commit_mismatch",
            "pre_review_smoke.candidate_commit must equal pre_run_code_commit",
        )
    if candidate and not COMMIT_RE.fullmatch(candidate):
        add_error(
            errors,
            "pre_review_smoke_commit_invalid",
            "pre_review_smoke.candidate_commit must be 40 lowercase hex",
        )
    require_text(raw, "run_id", "pre_review_smoke", errors)
    command = require_text(raw, "exact_command", "pre_review_smoke", errors)
    if any(pattern.search(command) for pattern in SECRET_PATTERNS):
        add_error(
            errors,
            "secret_in_smoke_command",
            "pre_review_smoke.exact_command appears to contain a credential",
        )
    if raw.get("exit_code") != 0:
        add_error(
            errors,
            "pre_review_smoke_failed",
            f"pre_review_smoke.exit_code must be 0, got {raw.get('exit_code')}",
        )
    budget = raw.get("step_budget")
    if (
        not isinstance(budget, int)
        or isinstance(budget, bool)
        or not 1 <= budget <= 100
    ):
        add_error(
            errors,
            "pre_review_smoke_step_budget_invalid",
            "pre_review_smoke.step_budget must be an integer from 1 through 100",
        )
        budget = 0
    completed = raw.get("completed_steps")
    if (
        not isinstance(completed, int)
        or isinstance(completed, bool)
        or completed < 1
        or completed > budget
    ):
        add_error(
            errors,
            "pre_review_smoke_steps_invalid",
            "pre_review_smoke.completed_steps must be within the step budget",
        )
    if computation_kind == "inference":
        if raw.get("finite_outputs") is not True:
            add_error(errors, "pre_review_smoke_boundary_invalid", "pre_review_smoke.finite_outputs must be true for inference")
        if raw.get("finite_loss") is not None:
            add_error(errors, "pre_review_smoke_numerical_contract_invalid", "pre_review_smoke.finite_loss must be omitted or null for inference")
    elif raw.get("finite_loss") is not True:
        add_error(errors, "pre_review_smoke_boundary_invalid", "pre_review_smoke.finite_loss must be true for training")
    for field in (
        "production_entrypoint_reached",
        "isolated_output",
        "official_metrics_disabled",
        "artifact_ingest_disabled",
        "checkpoint_cleanup_completed",
    ):
        if raw.get(field) is not True:
            add_error(
                errors,
                "pre_review_smoke_boundary_invalid",
                f"pre_review_smoke.{field} must be true",
            )
    evidence_paths = raw.get("evidence_paths")
    if (
        not isinstance(evidence_paths, list)
        or not evidence_paths
        or any(not isinstance(item, str) or not item.strip() for item in evidence_paths)
    ):
        add_error(
            errors,
            "pre_review_smoke_evidence_missing",
            "pre_review_smoke.evidence_paths must be a non-empty string list",
        )
    if raw.get("checkpoint_paths_remaining") != []:
        add_error(
            errors,
            "pre_review_smoke_cleanup_incomplete",
            "pre_review_smoke.checkpoint_paths_remaining must be an empty array",
        )
    retained = raw.get("retained_evidence_paths")
    if (
        not isinstance(retained, list)
        or any(not isinstance(item, str) or not item.strip() for item in retained)
        or not {"console.log", "status.json", "smoke_summary.json"}.issubset(
            {Path(item).name for item in retained if isinstance(item, str)}
        )
    ):
        add_error(
            errors,
            "pre_review_smoke_retained_evidence_invalid",
            "pre_review_smoke.retained_evidence_paths must include console.log, status.json, and smoke_summary.json",
        )
    validate_baseline_equivalence(raw, errors)


def validate_local_validation(packet: dict[str, Any], errors: list[str]) -> None:
    validations = packet.get("local_validation")
    if not isinstance(validations, list) or not validations:
        add_error(
            errors,
            "local_validation_unavailable",
            "local_validation must be a non-empty list",
        )
        return
    for index, item in enumerate(validations):
        prefix = f"local_validation[{index}]"
        if not isinstance(item, dict):
            add_error(errors, "local_validation_invalid", f"{prefix} must be an object")
            continue
        require_text(item, "command", prefix, errors)
        require_text(item, "observation", prefix, errors)
        exit_code = item.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            add_error(errors, "local_validation_invalid", f"{prefix}.exit_code must be an integer")
        elif exit_code != 0:
            add_error(errors, "local_validation_failed", f"{prefix}.exit_code={exit_code}")


def validate_critical_values(
    packet: dict[str, Any], review_mode: str, errors: list[str]
) -> None:
    values = packet.get("critical_values")
    if review_mode == "targeted_review" and values is None:
        return
    if not isinstance(values, list) or not values:
        add_error(
            errors,
            "critical_values_missing",
            "critical_values must name at least one source-to-sink path",
        )
        return
    seen: set[str] = set()
    for index, item in enumerate(values):
        prefix = f"critical_values[{index}]"
        if not isinstance(item, dict):
            add_error(errors, "critical_value_invalid", f"{prefix} must be an object")
            continue
        name = require_text(item, "name", prefix, errors)
        require_text(item, "source", prefix, errors)
        require_text(item, "sink", prefix, errors)
        require_text(item, "evidence", prefix, errors)
        if name in seen:
            add_error(errors, "critical_value_duplicate", f"duplicate critical value: {name}")
        seen.add(name)


def validate_experiment(
    packet: dict[str, Any], review_mode: str, errors: list[str]
) -> None:
    experiment = packet.get("experiment")
    if review_mode == "targeted_review" and experiment is None:
        return
    if not isinstance(experiment, dict):
        add_error(errors, "experiment_identity_missing", "experiment must be an object")
        return
    for field in EXPERIMENT_FIELDS:
        value = experiment.get(field)
        if value is None or isinstance(value, str) and not value.strip():
            add_error(
                errors,
                "experiment_identity_incomplete",
                f"experiment.{field} is required",
            )


def validate_packet(packet: Any) -> dict[str, Any]:
    digest = hashlib.sha256(canonical_json(packet)).hexdigest()
    errors: list[str] = []
    warnings: list[str] = []
    diff_paths: list[str] = []

    if not isinstance(packet, dict):
        add_error(errors, "invalid_packet", "top-level JSON value must be an object")
        return {
            "ready": False,
            "packet_complete": False,
            "errors": errors,
            "warnings": warnings,
            "packet_sha256": digest,
            "recommended_review_mode": "scientific_review",
            "implementation_diff_paths": diff_paths,
        }

    if packet.get("schema_version") != SCHEMA:
        add_error(errors, "schema_invalid", f"schema_version must be {SCHEMA}")
    for field in sorted(set(packet) - PACKET_FIELDS):
        add_error(errors, "unknown_field", f"packet.{field} is not allowed")
    review_mode = packet.get("review_mode")
    if review_mode not in REVIEW_MODES:
        add_error(
            errors,
            "review_mode_invalid",
            f"review_mode must be one of {sorted(REVIEW_MODES)}",
        )
        review_mode = "scientific_review"

    repo_root_text = require_text(packet, "repo_root", "packet", errors)
    commit = require_text(packet, "pre_run_code_commit", "packet", errors)
    base_commit = require_text(packet, "review_diff_base_commit", "packet", errors)
    require_text(packet, "implementation_intent", "packet", errors)
    command = require_text(packet, "exact_command", "packet", errors)
    collision = require_text(packet, "output_collision_policy", "packet", errors)
    if collision and collision not in {"fail", "unique_output"}:
        add_error(
            errors,
            "output_collision_policy_invalid",
            "output_collision_policy must be fail or unique_output",
        )

    approved_basis = packet.get("approved_basis")
    if (
        not isinstance(approved_basis, list)
        or not approved_basis
        or any(not isinstance(item, str) or not item.strip() for item in approved_basis)
    ):
        add_error(
            errors,
            "approved_basis_missing",
            "approved_basis must be a non-empty string list",
        )

    for pattern in SECRET_PATTERNS:
        if command and pattern.search(command):
            add_error(errors, "secret_in_command", "exact_command appears to contain a credential")
            break

    validate_local_validation(packet, errors)
    validate_critical_values(packet, review_mode, errors)
    validate_experiment(packet, review_mode, errors)
    if review_mode == "scientific_review":
        validate_smoke(packet, commit, errors, warnings)

    commit_valid = bool(COMMIT_RE.fullmatch(commit))
    base_valid = bool(COMMIT_RE.fullmatch(base_commit))
    if not commit_valid:
        add_error(
            errors,
            "invalid_commit",
            "pre_run_code_commit must be a full 40-character lowercase commit hash",
        )
    if not base_valid:
        add_error(
            errors,
            "invalid_review_diff_base_commit",
            "review_diff_base_commit must be a full 40-character lowercase commit hash",
        )

    repo_root = Path(repo_root_text).expanduser() if repo_root_text else Path(".")
    if not repo_root.is_dir():
        add_error(errors, "invalid_repo_root", f"repo_root is not a directory: {repo_root}")
    elif git(repo_root, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        add_error(errors, "invalid_repo_root", f"repo_root is not a Git work tree: {repo_root}")
    elif commit_valid and base_valid:
        resolved = git(repo_root, "rev-parse", "--verify", f"{commit}^{{commit}}")
        if resolved.returncode != 0 or resolved.stdout.strip() != commit:
            add_error(errors, "unresolvable_commit", f"commit is not resolvable: {commit}")
        base_resolved = git(
            repo_root, "rev-parse", "--verify", f"{base_commit}^{{commit}}"
        )
        if base_resolved.returncode != 0 or base_resolved.stdout.strip() != base_commit:
            add_error(
                errors,
                "unresolvable_review_diff_base_commit",
                f"base commit is not resolvable: {base_commit}",
            )
        elif git(
            repo_root, "merge-base", "--is-ancestor", base_commit, commit
        ).returncode != 0:
            add_error(
                errors,
                "review_diff_base_not_ancestor",
                f"{base_commit} is not an ancestor of {commit}",
            )
        elif resolved.returncode == 0:
            diff_paths = committed_diff_paths(repo_root, base_commit, commit, errors)
            if not diff_paths:
                add_warning(
                    warnings,
                    "implementation_diff_empty",
                    "reviewed commit has no paths different from review_diff_base_commit",
                )

    return {
        "ready": not errors,
        "packet_complete": not errors,
        "review_mode": review_mode,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "packet_sha256": digest,
        "recommended_review_mode": review_mode,
        "implementation_diff_paths": diff_paths,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    args = parser.parse_args()
    try:
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        result = validate_packet(packet)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        result = {
            "ready": False,
            "packet_complete": False,
            "errors": [f"packet_unreadable: {error}"],
            "warnings": [],
            "packet_sha256": "",
            "recommended_review_mode": "scientific_review",
            "implementation_diff_paths": [],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    sys.exit(main())
