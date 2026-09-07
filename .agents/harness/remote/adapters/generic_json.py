#!/usr/bin/env python3
"""Validate generic JSON progress/summary contracts for a rrctl run."""

from __future__ import annotations

# 直接运行脚本和通过 Python 包导入时使用同一实现。
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from harness.remote.adapters.generic_json import main
    raise SystemExit(main())

import sys
from pathlib import Path
from typing import Any

from harness.remote.adapters.common import (
    AdapterContractError, confined_path, dotted_value, emit_error, emit_success,
    load_structured_object, read_context, relative_path, require_finite_fields,
    require_identity_fields, require_mapping, require_string_list, require_text,
)

ADAPTER_CONTRACT_FIELDS = {
    "progress_path",
    "progress_format",
    "summary_path",
    "summary_format",
    "progress_count_field",
    "first_step_min_count",
    "completion_min_count",
    "completion_exact_count",
    "progress_finite_fields",
    "summary_finite_fields",
    "summary_required_fields",
    "artifacts",
    "progress_identity_fields",
    "summary_identity_fields",
}


def _optional_nonnegative_int(contract: dict[str, Any], field: str) -> int | None:
    value = contract.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdapterContractError(f"adapter_contract.{field}_type_invalid")
    return value


def _validate_contract(contract: dict[str, Any]) -> None:
    unknown = sorted(set(contract) - ADAPTER_CONTRACT_FIELDS)
    if unknown:
        raise AdapterContractError(
            f"adapter_contract.unknown_fields: {','.join(unknown)}"
        )
    relative_path(contract.get("progress_path"), "adapter_contract.progress_path")
    relative_path(contract.get("summary_path"), "adapter_contract.summary_path")
    for field in ("progress_format", "summary_format"):
        value = contract.get(field, "json")
        if value not in {"json", "jsonl_last"}:
            raise AdapterContractError(f"adapter_contract.{field}_invalid")
    count_field = contract.get("progress_count_field")
    if count_field is not None:
        require_text(count_field, "adapter_contract.progress_count_field")
    first_min = _optional_nonnegative_int(contract, "first_step_min_count")
    completion_min = _optional_nonnegative_int(contract, "completion_min_count")
    completion_exact = _optional_nonnegative_int(contract, "completion_exact_count")
    if completion_min is not None and completion_exact is not None:
        raise AdapterContractError("adapter_contract_completion_count_ambiguous")
    if any(
        value is not None for value in (first_min, completion_min, completion_exact)
    ):
        if count_field is None:
            raise AdapterContractError("adapter_contract.progress_count_field_required")
    for field in (
        "progress_finite_fields",
        "summary_finite_fields",
        "summary_required_fields",
        "artifacts",
    ):
        require_string_list(contract.get(field, []), f"adapter_contract.{field}")
    for field in ("progress_identity_fields", "summary_identity_fields"):
        require_mapping(contract.get(field, {}), f"adapter_contract.{field}")


def _count(progress: dict[str, Any], contract: dict[str, Any]) -> int | None:
    field = contract.get("progress_count_field")
    if field is None:
        return None
    value = dotted_value(progress, field, "progress")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdapterContractError(f"progress_count_invalid: {field}")
    return value


def evaluate(context: dict[str, Any]) -> dict[str, Any]:
    metadata = require_mapping(context["metadata"], "metadata")
    contract = require_mapping(
        metadata.get("adapter_contract"), "metadata.adapter_contract"
    )
    _validate_contract(contract)
    output_root = Path(require_text(context.get("output_root"), "output_root"))
    if not output_root.is_absolute():
        raise AdapterContractError("output_root_not_absolute")
    phase = context["phase"]

    progress_path = confined_path(
        output_root,
        contract["progress_path"],
        "adapter_contract.progress_path",
    )
    progress = load_structured_object(
        progress_path,
        "progress",
        contract.get("progress_format", "json"),
    )
    require_finite_fields(
        progress,
        contract.get("progress_finite_fields", []),
        "progress",
    )
    require_identity_fields(
        progress,
        contract.get("progress_identity_fields", {}),
        "progress",
    )
    count = _count(progress, contract)
    if phase == "first_step":
        minimum = contract.get("first_step_min_count", 1 if count is not None else None)
        if minimum is not None and count is not None and count < minimum:
            raise AdapterContractError(
                f"first_step_count_incomplete: expected>={minimum} actual={count}"
            )

    observations: dict[str, Any] = {
        "phase": phase,
        "progress_path": contract["progress_path"],
    }
    progress_report: dict[str, Any] = {}
    if count is not None:
        observations["progress_count"] = count
        progress_report["count"] = count
        progress_report["count_field"] = contract["progress_count_field"]
    if phase != "completion":
        return {
            "healthy": True,
            "complete": False,
            "progress": progress_report,
            "observations": observations,
            "artifacts": [],
        }

    summary_path = confined_path(
        output_root,
        contract["summary_path"],
        "adapter_contract.summary_path",
    )
    summary = load_structured_object(
        summary_path,
        "summary",
        contract.get("summary_format", "json"),
    )
    for field in contract.get("summary_required_fields", []):
        dotted_value(summary, field, "summary")
    require_finite_fields(
        summary,
        contract.get("summary_finite_fields", []),
        "summary",
    )
    require_identity_fields(
        summary,
        contract.get("summary_identity_fields", {}),
        "summary",
    )
    minimum = contract.get("completion_min_count")
    exact = contract.get("completion_exact_count")
    if minimum is not None and count is not None and count < minimum:
        raise AdapterContractError(
            f"completion_count_incomplete: expected>={minimum} actual={count}"
        )
    if exact is not None and count != exact:
        raise AdapterContractError(
            f"completion_count_mismatch: expected={exact} actual={count}"
        )

    artifacts: list[str] = []
    for index, value in enumerate(contract.get("artifacts", [])):
        relative = relative_path(
            value, f"adapter_contract.artifacts[{index}]"
        ).as_posix()
        confined_path(output_root, relative, f"adapter_contract.artifacts[{index}]")
        artifacts.append(relative)
    observations.update(
        {
            "summary_path": contract["summary_path"],
            "summary_fields_checked": sorted(
                contract.get("summary_required_fields", [])
            ),
        }
    )
    return {
        "healthy": True,
        "complete": True,
        "progress": progress_report,
        "observations": observations,
        "artifacts": artifacts,
    }


def main() -> int:
    try:
        result = evaluate(read_context())
        emit_success(**result)
        return 0
    except (AdapterContractError, OSError, ValueError) as exc:
        return emit_error(exc)
