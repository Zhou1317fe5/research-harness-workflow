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


SCHEMA_VERSION = "post-run.result-analysis.v1"
ANALYSIS_AGENT_MODE = "scientific-reviewer-subagent"
EXPECTED_REQUESTED_MODEL = "openai-codex/gpt-5.6-sol"
MODEL_EVIDENCE = {"session-metadata", "event-stream", "parent-runtime"}
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
_EXPLICIT_REF_PREFIXES = ("command:", "manual:", "session:")
_SESSION_REF_RE = re.compile(
    r"^session:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}#tool:\S+$"
)
_RUNTIME_MODEL_RE = re.compile(r"^openai-codex/gpt-5\.6-sol(?::max)?$")
_EVENT_REF_RE = re.compile(r"^event:\S+$")
_RUNTIME_REF_RE = re.compile(r"^runtime:\S+$")


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
    try:
        candidate = Path(value.strip().strip("\"'")).expanduser()
        candidates = [candidate] if candidate.is_absolute() else [base / candidate, workdir / candidate]
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


def _validate_model_metadata(data: dict[str, Any], errors: list[str]) -> None:
    if data.get("analysis_agent_mode") != ANALYSIS_AGENT_MODE:
        _error(errors, "analysis_agent_mode_invalid", str(data.get("analysis_agent_mode")))
    if data.get("analysis_independence") is not True:
        _error(errors, "analysis_independence_invalid", str(data.get("analysis_independence")))
    if data.get("requested_model") != EXPECTED_REQUESTED_MODEL:
        _error(errors, "analysis_requested_model_invalid", str(data.get("requested_model")))
    observed = data.get("observed_model")
    if not isinstance(observed, str) or not observed.strip() or observed in {"unknown", "pending", "not_applicable"}:
        _error(errors, "analysis_observed_model_invalid", str(observed))
    elif observed != EXPECTED_REQUESTED_MODEL:
        _error(errors, "analysis_observed_model_not_expected", observed)
    evidence = data.get("model_evidence")
    if evidence not in MODEL_EVIDENCE:
        _error(errors, "analysis_model_evidence_invalid", str(evidence))
    ref = data.get("model_evidence_ref")
    if not isinstance(ref, str) or not ref.strip() or ref.strip() in {"unknown", "pending"}:
        _error(errors, "analysis_model_evidence_ref_invalid", str(ref))
    elif evidence == "session-metadata" and not _SESSION_REF_RE.fullmatch(ref.strip()):
        _error(errors, "analysis_model_evidence_ref_invalid", str(ref))
    elif evidence == "event-stream" and not _EVENT_REF_RE.fullmatch(ref.strip()):
        _error(errors, "analysis_model_evidence_ref_invalid", str(ref))
    elif evidence == "parent-runtime" and not _RUNTIME_REF_RE.fullmatch(ref.strip()):
        _error(errors, "analysis_model_evidence_ref_invalid", str(ref))
    if evidence != "session-metadata":
        _error(errors, "analysis_model_evidence_unverifiable", str(evidence))


def _assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"text", "output_text"}:
            continue
        value = item.get("text") or item.get("output_text")
        if isinstance(value, str) and value:
            parts.append(value)
    return "\n".join(parts)


def _normalized_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _tool_call_arguments(item: dict[str, Any]) -> dict[str, Any] | None:
    arguments = item.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _session_tool_result(
    ref: str, *, workdir: Path
) -> tuple[dict[str, Any] | None, str | None]:
    match = _SESSION_REF_RE.fullmatch(ref.strip())
    if not match:
        return None, "invalid_session_tool_reference"
    session_id, tool_id = ref[len("session:"):].split("#tool:", 1)
    session_root = Path.home() / ".pi" / "agent" / "sessions"
    candidates = sorted(session_root.glob(f"**/*_{session_id}.jsonl"))
    if len(candidates) != 1:
        return None, f"session_file_count:{len(candidates)}"
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    try:
        with candidates[0].open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = record.get("message") if isinstance(record, dict) else None
                if not isinstance(message, dict):
                    continue
                if message.get("role") == "assistant":
                    content = message.get("content")
                    if isinstance(content, list):
                        for item in content:
                            if (
                                isinstance(item, dict)
                                and item.get("type") == "toolCall"
                                and item.get("name") == "subagent"
                                and item.get("id") == tool_id
                            ):
                                tool_calls.append(item)
                if (
                    message.get("role") == "toolResult"
                    and message.get("toolName") == "subagent"
                    and message.get("toolCallId") == tool_id
                ):
                    tool_results.append(message)
    except (OSError, UnicodeError) as exc:
        return None, f"session_read_failed:{exc}"
    if len(tool_calls) != 1 or len(tool_results) != 1:
        return None, f"tool_call_result_pair:{len(tool_calls)}:{len(tool_results)}"
    arguments = _tool_call_arguments(tool_calls[0])
    if arguments is None:
        return None, "subagent_arguments_invalid"
    if arguments.get("agent") != "scientific-reviewer":
        return None, f"subagent_agent_invalid:{arguments.get('agent')}"
    if arguments.get("agentScope") not in {"project", "both"}:
        return None, f"subagent_scope_invalid:{arguments.get('agentScope')}"
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        return None, "subagent_task_missing"
    cwd = arguments.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None, "subagent_cwd_missing"
    try:
        if Path(cwd).expanduser().resolve() != workdir.resolve():
            return None, f"subagent_cwd_invalid:{cwd}"
    except (OSError, RuntimeError) as exc:
        return None, f"subagent_cwd_unresolvable:{exc}"
    details = tool_results[0].get("details")
    if not isinstance(details, dict):
        return None, "subagent_details_missing"
    results = details.get("results")
    if not isinstance(results, list):
        return None, "subagent_results_missing"
    matching = [
        item for item in results
        if isinstance(item, dict) and item.get("agent") == "scientific-reviewer"
    ]
    if len(matching) != 1:
        return None, f"scientific_reviewer_result_count:{len(matching)}"
    return matching[0], None


def _validate_reviewer_evidence(
    ref: str,
    output_hash: str,
    analysis_path: Path | None,
    errors: list[str],
    *,
    workdir: Path,
) -> None:
    if not isinstance(ref, str) or not _SESSION_REF_RE.fullmatch(ref.strip()):
        _error(errors, "review_evidence_ref_invalid", str(ref))
        return
    if not isinstance(output_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", output_hash):
        _error(errors, "review_output_hash_invalid", str(output_hash))
        return
    result, failure = _session_tool_result(ref, workdir=workdir)
    if failure:
        _error(errors, "review_evidence_unverifiable", f"{ref}:{failure}")
        return
    assert result is not None
    if result.get("exitCode") != 0:
        _error(errors, "review_subagent_failed", f"{ref}:{result.get('exitCode')}")
    runtime_model = result.get("model")
    if not isinstance(runtime_model, str) or not _RUNTIME_MODEL_RE.fullmatch(runtime_model):
        _error(errors, "review_runtime_model_invalid", f"{ref}:{runtime_model}")
    messages = result.get("messages")
    final_text = ""
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "assistant":
                candidate = _assistant_text(message)
                if candidate:
                    final_text = candidate
    if not final_text:
        _error(errors, "review_output_missing", ref)
        return
    actual_hash = hashlib.sha256(_normalized_text(final_text).encode("utf-8")).hexdigest()
    if actual_hash != output_hash:
        _error(errors, "review_output_hash_mismatch", ref)
    if analysis_path is not None:
        try:
            analysis_text = _normalized_text(analysis_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            _error(errors, "analysis_file_unreadable", f"{analysis_path}:{exc}")
        else:
            if analysis_text not in _normalized_text(final_text):
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
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
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
    actual: set[tuple[str, str]] = set()
    analysis_paths: dict[Path, str] = {}
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
                if previous is not None:
                    _error(errors, "analysis_path_reused", f"{label}:{previous}")
                else:
                    analysis_paths[analysis_path] = label
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
        _validate_reviewer_evidence(
            entry.get("review_evidence_ref"),
            entry.get("review_output_sha256"),
            analysis_path,
            errors,
            workdir=workdir,
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
                    elif explicit_prefix == "session:" and not _SESSION_REF_RE.fullmatch(ref):
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
    print("post-run result analysis: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
