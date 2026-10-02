#!/usr/bin/env python3
"""Fail-closed validation for the post-run scientific result-analysis index."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


def _load_review_model():
    """Load the canonical review-model constants (searched upward for .agents)."""
    import importlib.util

    for ancestor in Path(__file__).resolve().parents:
        module_path = ancestor / ".agents" / "harness" / "review_model.py"
        if module_path.is_file():
            spec = importlib.util.spec_from_file_location("review_model", module_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load review_model: {module_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("cannot locate .agents/harness/review_model.py")


review_model = _load_review_model()

# requested/observed models are validated per-channel via
# _accepted_for_mode plus the reviewer_job verdict (the only source of truth
# for a job's requested model).


SCHEMA_VERSION = "post-run.result-analysis.v1"
ANALYSIS_AGENT_MODE = "result-analysis-reviewer-job"
ANALYSIS_AGENT_MODES = {
    "result-analysis-reviewer-job",
}
MODEL_EVIDENCE = {"job-verdict"}
# The reviewer-job channel persists the reviewer job verdict under the mission's
# own reviews directory; the validator recomputes its digest from disk.
VERDICT_SCHEMA = "post-run.result-analysis-verdict.v1"
SCIENTIFIC_OUTCOMES = {
    "hypothesis_supported",
    "hypothesis_not_supported",
    "gate_failed",
    "inconclusive",
    "not_applicable",
}
ANALYSIS_HEADINGS = ("Change", "Result", "Finding", "Next")
_HEADING_RE = re.compile(r"^#{1,6}\s+(Change|Result|Finding|Next)\s*$", re.MULTILINE)
CANONICAL_FIELDS = [
    "id", "priority", "phase", "area", "title", "description",
    "acceptance_criteria", "test_mcp", "required_skills", "required_mcp",
    "review_initial_requirements", "review_regression_requirements", "dev_state",
    "review_initial_state", "review_regression_state", "git_state", "owner", "refs",
    "notes", "spec_id", "exp_id", "run_id", "remote_state", "artifact_path",
    "branch", "commit_hash", "next_action", "updated_at",
]
COMPAT_FIELDS = CANONICAL_FIELDS[:19]
_EXPLICIT_REF_PREFIXES = ("command:", "manual:", "job:")
_JOB_REF_RE = re.compile(r"^job:[^#\s]+#verdict$")
_REVIEW_OUTPUT_KEYS = {
    "exp_id", "run_ids", "analysis_markdown", "scientific_outcome",
    "limitations", "validation_gaps",
}


def _error(errors: list[str], code: str, detail: str) -> None:
    errors.append(f"{code}:{detail}")


def _load_csv(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames not in (CANONICAL_FIELDS, COMPAT_FIELDS):
            raise ValueError("csv_schema_invalid:header_mismatch")
        rows = list(reader)
    ids: set[str] = set()
    for index, row in enumerate(rows, start=2):
        if None in row or any(row.get(field) is None for field in reader.fieldnames):
            raise ValueError(f"csv_schema_invalid:row_width:{index}")
        row_id = row.get("id", "")
        if not row_id.strip() or row_id in ids:
            raise ValueError(f"row_id_invalid:missing_or_duplicate:{index}")
        ids.add(row_id)
    return list(reader.fieldnames), rows


def _resolve(value: str, *, base: Path, workdir: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value.strip().strip("\"'")).expanduser()
    candidates = [candidate] if candidate.is_absolute() else [base / candidate, workdir / candidate]
    try:
        existing = {item.resolve() for item in candidates if item.exists()}
        if len(existing) > 1:
            raise ValueError(f"reference_ambiguous:{value}")
        resolved = next(iter(existing), candidates[0].resolve())
        if not resolved.is_relative_to(workdir.resolve()):
            raise ValueError(f"reference_outside_workspace:{value}")
        return resolved
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"reference_unresolvable:{value}:{exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_analysis_document(path: Path, errors: list[str], label: str) -> None:
    if not path.is_file():
        _error(errors, "analysis_file_missing", label)
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _error(errors, "analysis_file_unreadable", f"{label}:{exc}")
        return
    matches = list(_HEADING_RE.finditer(text))
    headings = [match.group(1) for match in matches]
    if headings != list(ANALYSIS_HEADINGS):
        _error(errors, "analysis_headings_invalid", f"{label}:{headings!r}")
        return
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if not text[match.end():end].strip():
            _error(errors, "analysis_section_empty", f"{label}:{match.group(1)}")


def _accepted_for_mode(value: str, mode: Any, *, host: str | None = None) -> bool:
    """Whether ``value`` is an accepted runtime identity for ``mode``'s host.

    ``host`` is the channel-specific backend (pi/codex). When omitted the
    check accepts any approved identity, letting host-precise binding happen
    per-entry via the reviewer_job verdict.
    """
    if not isinstance(mode, str) or mode not in ANALYSIS_AGENT_MODES:
        return review_model.is_accepted_model_identity(value)
    return review_model.is_accepted_model_identity(value, host)


def _validate_model_metadata(data: dict[str, Any], errors: list[str]) -> None:
    if data.get("analysis_agent_mode") not in ANALYSIS_AGENT_MODES:
        _error(errors, "analysis_agent_mode_invalid", str(data.get("analysis_agent_mode")))
    if data.get("analysis_independence") is not True:
        _error(errors, "analysis_independence_invalid", str(data.get("analysis_independence")))
    mode = data.get("analysis_agent_mode")
    observed = data.get("observed_model")
    if not isinstance(observed, str) or not observed.strip() or observed in {"unknown", "pending", "not_applicable"}:
        _error(errors, "analysis_observed_model_invalid", str(observed))
        return
    elif not _accepted_for_mode(observed, mode):
        _error(errors, "analysis_observed_model_not_expected", observed)
    evidence = data.get("model_evidence")
    if evidence not in MODEL_EVIDENCE:
        _error(errors, "analysis_model_evidence_invalid", str(evidence))
    ref = data.get("model_evidence_ref")
    if not isinstance(ref, str) or not ref.strip() or ref.strip() in {"unknown", "pending"}:
        _error(errors, "analysis_model_evidence_ref_invalid", str(ref))
    elif not _JOB_REF_RE.fullmatch(ref.strip()):
        _error(errors, "analysis_model_evidence_ref_invalid", str(ref))
    # requested_model must equal the reviewer_job verdict's requested_model;
    # cross-checked in the entry-level evidence validation where the verdict
    # file is loaded. Here we only require it to normalize to the host's
    # approved base identity so a different approved model cannot be silently
    # substituted.
    requested = data.get("requested_model")
    if not isinstance(requested, str) or not requested.strip():
        _error(errors, "analysis_requested_model_invalid", str(requested))
    elif not _accepted_for_mode(requested, mode):
        _error(errors, "analysis_requested_model_not_expected", requested)


def _strict_json_loads(value: str) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate_json_key:{key}")
            result[key] = item
        return result

    def reject_constant(constant: str) -> None:
        raise ValueError(f"invalid_json_constant:{constant}")

    return json.loads(
        value,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )


def _normalized_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _normalized_text(value: str) -> str:
    return _normalized_newlines(value).strip()


def _resolve_job_verdict(
    ref: str,
    *,
    csv_path: Path,
    workdir: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    verdict_value = ref[len("job:"):][: -len("#verdict")].strip()
    if not verdict_value:
        return None, "job_ref_empty"
    try:
        verdict_path = _resolve(verdict_value, base=csv_path.parent, workdir=workdir)
    except ValueError as exc:
        return None, str(exc)
    if not verdict_path.is_file():
        return None, f"job_verdict_missing:{verdict_value}"
    try:
        verdict = _strict_json_loads(verdict_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, f"job_verdict_invalid:{exc}"
    if not isinstance(verdict, dict):
        return None, "job_verdict_invalid:expected_object"
    if verdict.get("schema_version") != VERDICT_SCHEMA:
        return None, f"job_verdict_schema_invalid:{verdict.get('schema_version')}"
    if verdict.get("review_kind") != "result-analysis":
        return None, f"job_verdict_review_kind_invalid:{verdict.get('review_kind')}"
    if verdict.get("status") != "completed":
        return None, f"job_verdict_status_invalid:{verdict.get('status')}"
    for label, sha_field, path_field in (
        ("packet", "packet_sha256", "packet_path"),
        ("task", "task_sha256", "task_path"),
        ("raw_response", "raw_response_sha256", "raw_response_path"),
        ("response", "response_sha256", "response_path"),
    ):
        sha_val = verdict.get(sha_field)
        path_val = verdict.get(path_field)
        if not isinstance(sha_val, str) or not sha_val.strip():
            return None, f"verdict_{label}_sha_missing"
        if not isinstance(path_val, str) or not path_val.strip():
            return None, f"verdict_{label}_path_missing"
        # Verify the recorded artifact hash matches the on-disk file bytes so
        # a swapped packet/task cannot silently pass validation.
        try:
            actual = _sha256(_resolve(path_val, base=workdir, workdir=workdir))
        except (OSError, ValueError) as exc:
            return None, f"verdict_{label}_unreadable:{exc}"
        if actual != sha_val:
            return None, f"verdict_{label}_hash_mismatch"

    return verdict, None


def _validate_reviewer_evidence(
    ref: str,
    output_hash: str,
    analysis_path: Path | None,
    errors: list[str],
    *,
    workdir: Path,
    csv_path: Path,
    exp_id: str,
    run_id: str,
    expected_run_ids: set[str],
    expected_outcome: Any,
    expected_limitations: Any,
    expected_gaps: Any,
    expected_requested_model: Any,
) -> None:
    if not isinstance(ref, str) or not _JOB_REF_RE.fullmatch(ref.strip()):
        _error(errors, "review_evidence_ref_invalid", str(ref))
        return
    if not isinstance(output_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", output_hash):
        _error(errors, "review_output_hash_invalid", str(output_hash))
        return
    verdict, failure = _resolve_job_verdict(ref, csv_path=csv_path, workdir=workdir)
    if failure:
        _error(errors, "review_evidence_unverifiable", f"{ref}:{failure}")
        return
    assert verdict is not None
    backend = verdict.get("backend")
    if backend not in {"pi", "codex"}:
        _error(errors, "review_backend_invalid", f"{ref}:{backend}")
    else:
        runtime_model = verdict.get("observed_model")
        if not isinstance(runtime_model, str) or not review_model.is_accepted_model_identity(runtime_model, backend):
            _error(errors, "review_runtime_model_invalid", f"{ref}:{runtime_model}")
        # requested_model recorded in the verdict is the invocation identity the
        # launcher actually passed; the index-level requested_model must match
        # it exactly, otherwise someone could claim a different reviewer model
        # than the one that actually ran. Host-precise matching is delegated to
        # _accepted_for_mode with the verdict's backend.
        job_requested = verdict.get("requested_model")
        if not isinstance(job_requested, str) or not job_requested.strip():
            _error(errors, "review_requested_model_invalid", f"{ref}:{job_requested}")
        elif job_requested.strip() != expected_requested_model:
            _error(errors, "review_requested_model_mismatch", f"{ref}:{job_requested}")
        elif not _accepted_for_mode(job_requested, "result-analysis-reviewer-job", host=backend):
            _error(errors, "review_requested_model_not_expected", f"{ref}:{job_requested}")
    if verdict.get("exp_id") != exp_id:
        _error(errors, "job_verdict_exp_id_mismatch", f"{ref}:{verdict.get('exp_id')}")
    verdict_runs = verdict.get("run_ids")
    if (
        not isinstance(verdict_runs, list)
        or any(not isinstance(item, str) or not item.strip() for item in verdict_runs)
        or len(verdict_runs) != len(set(verdict_runs))
        or set(verdict_runs) != expected_run_ids
    ):
        _error(errors, "job_verdict_run_ids_mismatch", f"{ref}:{verdict_runs!r}")
    output_text = verdict.get("review_output")
    final_text = output_text if isinstance(output_text, str) else ""
    if not final_text:
        _error(errors, "review_output_missing", ref)
        return
    normalized_final = _normalized_text(final_text)
    actual_hash = hashlib.sha256(normalized_final.encode("utf-8")).hexdigest()
    if actual_hash != output_hash:
        _error(errors, "review_output_hash_mismatch", ref)
    try:
        payload = _strict_json_loads(normalized_final)
    except (json.JSONDecodeError, ValueError) as exc:
        _error(errors, "review_output_json_invalid", f"{ref}:{exc}")
        return
    if not isinstance(payload, dict) or set(payload) != _REVIEW_OUTPUT_KEYS:
        _error(errors, "review_output_schema_invalid", ref)
        return
    if payload.get("exp_id") != exp_id:
        _error(errors, "review_exp_id_mismatch", ref)
    payload_run_ids = payload.get("run_ids")
    if (
        not isinstance(payload_run_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in payload_run_ids)
        or len(payload_run_ids) != len(set(payload_run_ids))
        or set(payload_run_ids) != expected_run_ids
    ):
        _error(errors, "review_run_ids_mismatch", ref)
    if not isinstance(payload.get("analysis_markdown"), str) or not payload["analysis_markdown"].strip():
        _error(errors, "review_analysis_markdown_invalid", ref)
    if payload.get("scientific_outcome") not in SCIENTIFIC_OUTCOMES:
        _error(errors, "review_scientific_outcome_invalid", ref)
    elif payload.get("scientific_outcome") != expected_outcome:
        _error(errors, "review_scientific_outcome_mismatch", ref)
    for field, expected in (
        ("limitations", expected_limitations),
        ("validation_gaps", expected_gaps),
    ):
        value = payload.get(field)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            _error(errors, "review_output_schema_invalid", f"{ref}:{field}")
        elif value != expected:
            _error(errors, f"review_{field}_mismatch", ref)
    if analysis_path is not None:
        try:
            analysis_text = _normalized_newlines(analysis_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            _error(errors, "analysis_file_unreadable", f"{analysis_path}:{exc}")
        else:
            if _normalized_newlines(payload.get("analysis_markdown", "")) != _normalized_newlines(analysis_text):
                _error(errors, "analysis_not_bound_to_reviewer_output", ref)


def validate_index(index_path: Path, csv_path: Path, *, workdir: Path) -> list[str]:
    """Return deterministic validation errors; an empty list means valid."""
    errors: list[str] = []
    root = workdir.resolve()
    index_path = index_path.resolve()
    csv_path = csv_path.resolve()
    for label, path in (("index", index_path), ("csv", csv_path)):
        if not path.is_relative_to(root):
            return [f"analysis_{label}_outside_workdir:{path}"]
    if not index_path.is_file():
        return [f"analysis_index_missing:{index_path}"]
    try:
        data = _strict_json_loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return [f"analysis_index_invalid:{exc}"]
    if not isinstance(data, dict):
        return ["analysis_index_invalid:expected_object"]
    if data.get("schema_version") != SCHEMA_VERSION:
        _error(errors, "analysis_schema_invalid", str(data.get("schema_version")))
    status = data.get("status")
    if status not in {"complete", "not_applicable"}:
        _error(errors, "analysis_status_invalid", str(status))
    try:
        fields, rows = _load_csv(csv_path)
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        return [f"analysis_csv_invalid:{exc}"]
    if "remote_state" not in fields:
        # Explicit external 19-column compatibility CSVs have no remote ingest state.
        return sorted(set(errors))
    expected = {
        (row.get("exp_id", "").strip(), row.get("run_id", "").strip())
        for row in rows
        if row.get("remote_state") == "ingested" and row.get("exp_id", "").strip()
    }
    if any(not run_id for _, run_id in expected):
        _error(errors, "analysis_scope_invalid", "ingested_row_missing_run_id")
    entries = data.get("entries")
    if not isinstance(entries, list):
        _error(errors, "analysis_entries_invalid", "expected_array")
        entries = []

    if status == "complete" and not expected:
        _error(errors, "analysis_complete_without_results", "use_not_applicable")

    if status == "not_applicable":
        if expected:
            _error(errors, "analysis_not_applicable_with_results", str(sorted(expected)))
        if entries:
            _error(errors, "analysis_not_applicable_entries_nonempty", str(len(entries)))
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            _error(errors, "analysis_not_applicable_reason_missing", "reason")
        return sorted(set(errors))

    _validate_model_metadata(data, errors)
    expected_run_ids_by_exp: dict[str, set[str]] = {}
    for exp_id, run_id in expected:
        expected_run_ids_by_exp.setdefault(exp_id, set()).add(run_id)
    actual: set[tuple[str, str]] = set()
    analysis_paths: dict[Path, str] = {}
    review_refs_by_exp: dict[str, str] = {}
    review_exp_by_ref: dict[str, str] = {}
    for index, entry in enumerate(entries):
        label = f"entries[{index}]"
        if not isinstance(entry, dict):
            _error(errors, "analysis_entry_invalid", f"{label}:expected_object")
            continue
        exp_value = entry.get("exp_id")
        run_value = entry.get("run_id")
        exp_id = exp_value.strip() if isinstance(exp_value, str) else ""
        run_id = run_value.strip() if isinstance(run_value, str) else ""
        key = (exp_id, run_id)
        if key in actual:
            _error(errors, "analysis_entry_duplicate", f"{label}:{key}")
        actual.add(key)
        if key not in expected:
            _error(errors, "analysis_entry_unexpected", f"{label}:{key}")
        for field in (
            "exp_id", "run_id", "analysis_path", "analysis_sha256", "scientific_outcome",
            "review_evidence_ref", "review_output_sha256",
        ):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                _error(errors, "analysis_entry_field_missing", f"{label}.{field}")
        outcome = entry.get("scientific_outcome")
        if not isinstance(outcome, str) or outcome not in SCIENTIFIC_OUTCOMES:
            _error(errors, "scientific_outcome_invalid", f"{label}:{outcome}")
        analysis_path: Path | None = None
        analysis_value = entry.get("analysis_path")
        if isinstance(analysis_value, str) and analysis_value.strip():
            try:
                analysis_path = _resolve(analysis_value, base=workdir, workdir=root)
            except ValueError as exc:
                _error(errors, "analysis_path_invalid", f"{label}:{exc}")
                analysis_path = None
            if analysis_path is None or not analysis_path.is_file():
                _error(errors, "analysis_path_missing", f"{label}:{analysis_value}")
            else:
                relative_path = analysis_path.relative_to(root)
                relative = relative_path.as_posix()
                expected_relative = Path("research_workspace") / "experiments" / exp_id / "analysis" / "analysis.md"
                if relative_path != expected_relative:
                    _error(errors, "analysis_path_exp_scope_invalid", f"{label}:{relative}")
                previous = analysis_paths.get(analysis_path)
                if previous is not None and previous != exp_id:
                    _error(errors, "analysis_path_reused_across_exp", f"{label}:{previous}")
                else:
                    analysis_paths[analysis_path] = exp_id
                _valid_analysis_document(analysis_path, errors, label)
                expected_hash = entry.get("analysis_sha256")
                if isinstance(expected_hash, str) and expected_hash.strip():
                    try:
                        actual_hash = _sha256(analysis_path)
                    except OSError as exc:
                        _error(errors, "analysis_hash_failed", f"{label}:{exc}")
                    else:
                        if actual_hash != expected_hash:
                            _error(errors, "analysis_hash_mismatch", label)
        review_ref = entry.get("review_evidence_ref")
        if isinstance(review_ref, str):
            previous_ref = review_refs_by_exp.get(exp_id)
            if previous_ref is not None and previous_ref != review_ref:
                _error(errors, "review_evidence_changed_within_exp", f"{label}:{previous_ref}")
            else:
                review_refs_by_exp[exp_id] = review_ref
            previous_exp = review_exp_by_ref.get(review_ref)
            if previous_exp is not None and previous_exp != exp_id:
                _error(errors, "review_evidence_reused_across_exp", f"{label}:{previous_exp}")
            else:
                review_exp_by_ref[review_ref] = exp_id
        _validate_reviewer_evidence(
            review_ref,
            entry.get("review_output_sha256"),
            analysis_path,
            errors,
            workdir=workdir,
            csv_path=csv_path,
            exp_id=exp_id,
            run_id=run_id,
            expected_run_ids=expected_run_ids_by_exp.get(exp_id, set()),
            expected_outcome=entry.get("scientific_outcome"),
            expected_limitations=entry.get("limitations"),
            expected_gaps=entry.get("validation_gaps"),
            expected_requested_model=data.get("requested_model"),
        )
        evidence_refs = entry.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs or any(not isinstance(ref, str) or not ref.strip() for ref in evidence_refs):
            _error(errors, "analysis_evidence_refs_invalid", label)
        else:
            for ref_index, ref in enumerate(evidence_refs):
                explicit_prefix = next((prefix for prefix in _EXPLICIT_REF_PREFIXES if ref.startswith(prefix)), None)
                if explicit_prefix is not None:
                    payload = ref[len(explicit_prefix):].strip()
                    if not payload or any(char in payload for char in "\0\n\r"):
                        _error(errors, "analysis_evidence_ref_invalid", f"{label}[{ref_index}]:{ref}")
                    elif explicit_prefix == "job:" and not _JOB_REF_RE.fullmatch(ref):
                        _error(errors, "analysis_evidence_ref_invalid", f"{label}[{ref_index}]:{ref}")
                    continue
                try:
                    resolved = _resolve(ref, base=csv_path.parent, workdir=root)
                except ValueError as exc:
                    _error(errors, "analysis_evidence_ref_invalid", f"{label}[{ref_index}]:{exc}")
                    continue
                if resolved is None or not resolved.exists():
                    _error(errors, "analysis_evidence_ref_missing", f"{label}[{ref_index}]:{ref}")
                    continue
                relative = resolved.relative_to(root).parts
                if relative[:1] == ("remote_artifacts",):
                    if len(relative) < 3 or relative[1] != exp_id or relative[2] != run_id:
                        _error(errors, "analysis_evidence_scope_invalid", f"{label}[{ref_index}]:{ref}")
                elif relative[:2] == ("research_workspace", "experiments"):
                    if len(relative) < 3 or relative[2] != exp_id:
                        _error(errors, "analysis_evidence_scope_invalid", f"{label}[{ref_index}]:{ref}")
                elif relative in {
                    ("research_workspace", "EXPERIMENTS.csv"),
                    ("research_workspace", "STATE.md"),
                    ("research_workspace", "CONCLUSIONS.md"),
                }:
                    continue
                elif relative[:1] not in {("issues",), ("docs",)}:
                    _error(errors, "analysis_evidence_scope_invalid", f"{label}[{ref_index}]:{ref}")
        for field in ("limitations", "validation_gaps"):
            value = entry.get(field)
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                _error(errors, "analysis_list_invalid", f"{label}.{field}")

    if actual != expected:
        _error(errors, "analysis_scope_mismatch", f"expected={sorted(expected)!r};actual={sorted(actual)!r}")
    entry_refs = {
        entry.get("review_evidence_ref")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("review_evidence_ref"), str)
    }
    if data.get("model_evidence_ref") not in entry_refs:
        _error(errors, "analysis_model_evidence_ref_not_bound", str(data.get("model_evidence_ref")))
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--workdir", default=".", type=Path)
    args = parser.parse_args()
    try:
        errors = validate_index(args.index, args.csv, workdir=args.workdir)
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError) as exc:
        errors = [f"analysis_validation_failed:{exc}"]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    try:
        repo_root = args.workdir.resolve()
        agents_root = repo_root / ".agents"
        if str(agents_root) not in sys.path:
            sys.path.insert(0, str(agents_root))
        from harness.records.experiment_records import apply_result_analysis_outcomes
        apply_result_analysis_outcomes(
            args.index,
            repo_root=repo_root,
            allowed_outcomes=SCIENTIFIC_OUTCOMES,
            csv_path=args.csv,
            stderr=sys.stderr,
        )
    except Exception as exc:  # 派生侧失败不改变验证结论
        print(f"result_analysis_sync: warn (unexpected): {exc}", file=sys.stderr)
    print("post-run result analysis: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
