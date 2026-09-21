"""Fail-closed completion checks for post-run result analysis."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


ANALYSIS_ROW_ID = "RESULT-ANALYSIS-01"
ANALYSIS_AGENT_MODE = "scientific-reviewer-subagent"
REQUESTED_MODEL = "openai-codex/gpt-5.6-sol"
ANALYSIS_SKILL = "post-run-result-analysis"


def _load_validator():
    """Load the validator beside the active skill mirror, not from sys.path."""
    validator_path = (
        Path(__file__).resolve().parents[2]
        / "post-run-result-analysis"
        / "scripts"
        / "validate_result_analysis.py"
    )
    spec = importlib.util.spec_from_file_location(
        "post_run_result_analysis_validator", validator_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load result-analysis validator: {validator_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metadata_errors(
    index: dict[str, Any], tags: dict[str, str], errors: list[str]
) -> None:
    expected = {
        "analysis_agent_mode": ("analysis_agent_mode", ANALYSIS_AGENT_MODE),
        "analysis_independence": ("analysis_independence", "true"),
        "analysis_requested_model": ("requested_model", REQUESTED_MODEL),
        "analysis_observed_model": ("observed_model", None),
        "analysis_model_evidence": ("model_evidence", None),
        "analysis_model_evidence_ref": ("model_evidence_ref", None),
    }
    for row_tag, (index_key, expected_value) in expected.items():
        index_value = index.get(index_key)
        if isinstance(index_value, bool):
            index_value = str(index_value).lower()
        elif index_value is not None:
            index_value = str(index_value)
        if expected_value is not None and index_value != expected_value:
            errors.append(f"result_analysis_index_metadata_invalid:{index_key}")
        if not isinstance(index_value, str) or not index_value.strip():
            errors.append(f"result_analysis_index_metadata_missing:{index_key}")
        if tags.get(row_tag) != index_value:
            errors.append(f"result_analysis_tag_mismatch:{row_tag}")


def result_analysis_completion_errors(
    csv_path: Path,
    rows: list[dict[str, str]],
    *,
    workdir: Path,
) -> list[str]:
    """Return completion errors for canonical post-run result analysis.

    Explicit 19-column compatibility CSVs have no remote lifecycle and retain their
    historical completion behavior.  Canonical CSVs require an analysis artifact
    only after at least one formal result has reached ``remote_state=ingested``.
    """
    if not any("remote_state" in row for row in rows):
        return []

    ingested = {
        (row.get("exp_id", "").strip(), row.get("run_id", "").strip())
        for row in rows
        if row.get("remote_state") == "ingested"
        and row.get("exp_id", "").strip()
    }
    if not ingested:
        return []

    # Keep imports local: mission_completion calls this module during completion,
    # while final_ready imports it directly.
    from mission_completion import parse_note_tags, resolve_reference_path

    errors: list[str] = []
    analysis_like_rows = [
        row for row in rows if row.get("id", "").startswith("RESULT-ANALYSIS-")
    ]
    if any(row.get("id") != ANALYSIS_ROW_ID for row in analysis_like_rows):
        errors.append("result_analysis_row_extra:only_RESULT-ANALYSIS-01_allowed")
    analysis_rows = [row for row in analysis_like_rows if row.get("id") == ANALYSIS_ROW_ID]
    if not analysis_rows:
        errors.append(f"result_analysis_row_missing:{ANALYSIS_ROW_ID}")
        analysis_row: dict[str, str] | None = None
    elif len(analysis_rows) != 1:
        errors.append(f"result_analysis_row_duplicate:{ANALYSIS_ROW_ID}")
        analysis_row = analysis_rows[-1]
    else:
        analysis_row = analysis_rows[0]

    analysis_position = next(
        (index for index, row in enumerate(rows) if row.get("id") == ANALYSIS_ROW_ID),
        None,
    )
    first_review_position = next(
        (index for index, row in enumerate(rows) if row.get("id", "").startswith("REVIEW-")),
        None,
    )
    if (
        analysis_position is not None
        and first_review_position is not None
        and analysis_position >= first_review_position
    ):
        errors.append("result_analysis_row_order_invalid:must_precede_REVIEW-*")

    if analysis_row is not None:
        if analysis_row.get("phase") != "analysis":
            errors.append("result_analysis_row_phase_invalid:expected_analysis")
        skills = {
            item.strip()
            for item in analysis_row.get("required_skills", "").replace(";", ",").split(",")
            if item.strip()
        }
        if ANALYSIS_SKILL not in skills:
            errors.append(f"result_analysis_row_skill_missing:{ANALYSIS_SKILL}")
        if analysis_row.get("remote_state") != "not_applicable":
            errors.append("result_analysis_row_remote_state_invalid:not_applicable")

    analysis_tags: dict[str, str] = {}
    index_value = ""
    index_path: Path | None = None
    if analysis_row is not None:
        try:
            analysis_tags = parse_note_tags(analysis_row.get("notes", ""))
        except ValueError as exc:
            errors.append(f"result_analysis_row_notes_invalid:{exc}")
        if analysis_tags.get("analysis_kind") != "post_run":
            errors.append("result_analysis_row_kind_invalid:post_run")
        index_value = analysis_tags.get("result_analysis", "").strip()
        if not index_value:
            errors.append("result_analysis_index_tag_missing")
        else:
            try:
                index_path = resolve_reference_path(index_value, csv_path.parent, workdir)
            except (OSError, ValueError) as exc:
                errors.append(f"result_analysis_index_unreadable:{exc}")
            else:
                try:
                    expected_index = (csv_path.parent / "reviews" / "result-analysis.json").resolve()
                    if index_path != expected_index:
                        errors.append(f"result_analysis_index_path_invalid:{index_path}")
                    if not index_path.is_file():
                        errors.append(f"result_analysis_index_unreadable:{index_value}")
                except (OSError, RuntimeError) as exc:
                    errors.append(f"result_analysis_index_unreadable:{exc}")

    index: dict[str, Any] | None = None
    if index_path is not None and index_path.is_file():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"result_analysis_index_invalid:{exc}")
        else:
            if not isinstance(loaded, dict):
                errors.append("result_analysis_index_invalid:expected_object")
            else:
                index = loaded

    if index_path is not None:
        try:
            validator = _load_validator()
            errors.extend(validator.validate_index(index_path, csv_path, workdir=workdir))
        except (OSError, ImportError, RuntimeError, AttributeError, TypeError, ValueError) as exc:
            errors.append(f"result_analysis_validation_failed:{exc}")

    if index is not None and analysis_row is not None:
        _metadata_errors(index, analysis_tags, errors)

    review_rows = [row for row in rows if row.get("id", "").startswith("REVIEW-")]
    if not review_rows:
        errors.append("result_analysis_review_consumer_missing:REVIEW-*")
    else:
        for review_row in review_rows:
            try:
                review_tags = parse_note_tags(review_row.get("notes", ""))
            except ValueError as exc:
                errors.append(f"result_analysis_review_notes_invalid:{exc}")
            else:
                if review_tags.get("result_analysis", "").strip() != index_value:
                    errors.append(
                        f"result_analysis_review_consumer_mismatch:{review_row.get('id', '<missing-id>')}"
                    )

    return sorted(set(errors))
