#!/usr/bin/env python3
"""Schema-aware atomic updates for Mission CSV state."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / ".agents"))
from harness.common.locking import file_lock

from mission_completion import (
    EXPECTED_FIELDS, REMOTE_STATES, parse_note_tags, read_mission_csv, upsert_note_tags,
)


SCHEMA = "mission.csv-state-update.v1"
FIELDS = EXPECTED_FIELDS
DEV_STATES = {"未开始", "进行中", "已完成"}
REVIEW_STATES = {"未开始", "进行中", "已完成"}
GIT_STATES = {"未提交", "已提交"}
COMMIT_BOUNDARIES = {
    "none",
    "implementation",
    "review",
    "launch",
    "terminal",
    "final_review",
}
NOTE_ITEM_LIMIT = 512
NOTE_APPEND_LIMIT = 1024


class StateUpdateError(ValueError):
    """A request or candidate CSV failed deterministic validation."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_csv(path: Path) -> tuple[list[dict[str, str]], bool]:
    try:
        _, rows, has_bom = read_mission_csv(path, allow_compat=True, validate_notes=False)
    except (ValueError, csv.Error) as error:
        raise StateUpdateError(str(error)) from error
    return rows, has_bom


def _validate_rows(rows: list[dict[str, str]]) -> None:
    ids = [row["id"] for row in rows]
    if any(not row_id for row_id in ids):
        raise StateUpdateError("row_id_invalid: id must be non-empty")
    duplicates = sorted({row_id for row_id in ids if ids.count(row_id) > 1})
    if duplicates:
        raise StateUpdateError("duplicate_row_id: " + ",".join(duplicates))
    enum_fields = {
        "dev_state": DEV_STATES,
        "review_initial_state": REVIEW_STATES,
        "review_regression_state": REVIEW_STATES,
        "git_state": GIT_STATES,
        "remote_state": REMOTE_STATES,
    }
    for row in rows:
        for field, allowed in enum_fields.items():
            if field == "remote_state" and field not in row:
                continue
            if row[field] not in allowed:
                raise StateUpdateError(
                    f"enum_invalid: {row['id']}.{field}={row[field]!r}"
                )


def _note_value(notes: str, key: str) -> str | None:
    return parse_note_tags(notes).get(key)


def _is_closed(row: dict[str, str]) -> bool:
    return (
        row["dev_state"] == "已完成"
        and row["review_initial_state"] == "已完成"
        and row["review_regression_state"] == "已完成"
        and row["git_state"] == "已提交"
    )


def _is_prerun(row: dict[str, str]) -> bool:
    return row["id"].startswith("PRERUN-REVIEW-") or (
        _note_value(row["notes"], "review_kind") == "pre_run_implementation"
    )


def _validate_single_prerun(
    rows: list[dict[str, str]], target_id: str | None = None
) -> None:
    prerun_rows = [row for row in rows if _is_prerun(row)]
    if target_id is not None:
        target = next((row for row in prerun_rows if row["id"] == target_id), None)
        if target is None:
            return
        target_mode = _note_value(target["notes"], "review_mode")
        if _is_closed(target) and target_mode not in {
            "scientific_review",
            "targeted_review",
        }:
            return

    active = [
        row
        for row in prerun_rows
        if not _is_closed(row) or row["id"] == target_id
    ]
    gates: dict[str, list[str]] = {}
    for row in active:
        notes = row["notes"]
        if _note_value(notes, "root_budget_enforced") == "true" or _note_value(
            notes, "formal_attempt"
        ):
            raise StateUpdateError(
                f"legacy_prerun_protocol_not_actionable: {row['id']} must use one scientific review"
            )
        mode = _note_value(notes, "review_mode")
        if mode not in {"scientific_review", "targeted_review"}:
            raise StateUpdateError(
                f"prerun_review_mode_invalid: {row['id']}={mode!r}"
            )
        gated_run = _note_value(notes, "gated_run")
        if not gated_run:
            raise StateUpdateError(
                f"prerun_gated_run_missing: {row['id']} requires gated_run"
            )
        result = _note_value(notes, "review_result")
        if result and result not in {
            "scientifically_correct",
            "scientifically_incorrect",
            "not_evaluable",
            "targeted_correct",
            "targeted_incorrect",
        }:
            raise StateUpdateError(
                f"prerun_review_result_invalid: {row['id']}={result!r}"
            )
        if mode == "scientific_review" and result and result not in {
            "scientifically_correct",
            "scientifically_incorrect",
            "not_evaluable",
        }:
            raise StateUpdateError(
                f"prerun_review_mode_result_mismatch: {row['id']}"
            )
        if mode == "targeted_review" and result and result not in {
            "targeted_correct",
            "targeted_incorrect",
            "not_evaluable",
        }:
            raise StateUpdateError(
                f"prerun_review_mode_result_mismatch: {row['id']}"
            )
        if _note_value(notes, "pre_run_result") == "pass":
            direct_pass = result in {
                "scientifically_correct",
                "targeted_correct",
            }
            closure = _note_value(notes, "blocker_closure_evidence")
            repaired_pass = result in {
                "scientifically_incorrect",
                "targeted_incorrect",
            } and bool(closure)
            if not (direct_pass or repaired_pass):
                raise StateUpdateError(
                    f"prerun_pass_without_correctness_evidence: {row['id']}"
                )
        gates.setdefault(gated_run, []).append(row["id"])

    duplicates = {gate: ids for gate, ids in gates.items() if len(ids) > 1}
    if duplicates:
        gate, ids = sorted(duplicates.items())[0]
        raise StateUpdateError(
            f"multiple_prerun_reviews_for_gate: {gate} rows={','.join(sorted(ids))}"
        )


def _validate_claims(csv_path: Path, rows: list[dict[str, str]]) -> None:
    from validate_claim_ledger import resolve_path, validate_ledger

    root = Path(_git(["rev-parse", "--show-toplevel"], csv_path.parent) or csv_path.parent)
    ledgers: dict[Path, set[str]] = {}
    try:
        for row in rows:
            tags = parse_note_tags(row["notes"])
            claims = {x.strip() for x in tags.get("claims", "").split(",") if x.strip()}
            value = tags.get("claim_ledger")
            if claims and not value:
                raise StateUpdateError(f"claim_ledger_missing:{row['id']}")
            if value:
                path = resolve_path(value, csv_path.parent, root)
                ledgers.setdefault(path, set()).update(claims)
        for path, claims in ledgers.items():
            errors = validate_ledger(path, csv_path, claims, rows=rows, workdir=root)
            if errors:
                raise StateUpdateError("; ".join(errors))
    except ValueError as exc:
        raise StateUpdateError(str(exc)) from exc


def _encode_csv(rows: list[dict[str, str]], has_bom: bool) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    encoded = output.getvalue().encode("utf-8")
    return (b"\xef\xbb\xbf" + encoded) if has_bom else encoded


def _atomic_replace_bytes(
    path: Path,
    content: bytes,
    replace: Callable[[str | bytes, str | bytes], None],
) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _git(args: list[str], cwd: Path) -> str | None:
    """执行 git 并返回单行输出；非仓库或 git 不可用时返回 None。"""
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def _assert_write_context(csv_path: Path, row: dict[str, str]) -> None:
    """写回 CSV 前确认当前确实在该 mission 的仓库与分支上。

    「CSV 是唯一执行状态源」规定的是状态写在哪里，没规定写之前先确认自己在哪。
    历史上出现过两次 false completion：工作发生在平行目录（主仓 CSV 十行全是
    未开始），以及修复提交落到无关分支（该 commit 至今孤悬）。两次都是未读
    `git branch --show-current` 就断言了分支状态，靠自觉纠正无效。

    git 不可用或不在仓库内时跳过——那说明前提本就不成立，不是本函数能判的。
    """
    csv_repo = _git(["rev-parse", "--show-toplevel"], csv_path.parent.resolve())
    if csv_repo is None:
        return
    here_repo = _git(["rev-parse", "--show-toplevel"], Path.cwd())
    if here_repo is not None and Path(here_repo) != Path(csv_repo):
        raise StateUpdateError(
            "write_context_repo_mismatch: "
            f"cwd 属于 {here_repo}，但 CSV 属于 {csv_repo}；"
            "先切到该 mission 的仓库再写状态"
        )
    declared = (row.get("branch") or "").strip()
    if not declared:
        return
    # 用 branch --show-current 而非 rev-parse --abbrev-ref HEAD：
    # 后者在尚无 commit 的仓库上失败，会让断言被静默跳过。
    current = _git(["branch", "--show-current"], Path(csv_repo))
    if current is None:
        return
    if current != declared:
        raise StateUpdateError(
            "write_context_branch_mismatch: "
            f"当前分支 {current}，但该行 branch 字段为 {declared}；"
            "先 checkout 目标分支再写状态"
        )


def apply_update(
    csv_path: Path,
    request: Any,
    *,
    replace: Callable[[str | bytes, str | bytes], None] = os.replace,
) -> dict[str, Any]:
    csv_path = csv_path.expanduser().resolve()
    with file_lock(csv_path.with_name("." + csv_path.name + ".lock")):
        return _apply_update_locked(csv_path, request, replace=replace)


def _apply_update_locked(csv_path, request, *, replace):
    if not isinstance(request, dict):
        raise StateUpdateError("request_invalid: expected object")
    allowed = {
        "schema_version",
        "row_id",
        "set",
        "append_notes",
        "set_note_tags",
        "event",
        "commit_boundary",
        "expected_sha256",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        raise StateUpdateError("request_unknown_fields: " + ",".join(unknown))
    if request.get("schema_version") != SCHEMA:
        raise StateUpdateError(f"schema_version_invalid: expected {SCHEMA}")
    row_id = request.get("row_id")
    if not isinstance(row_id, str) or not row_id:
        raise StateUpdateError("row_id_invalid: expected non-empty string")
    updates = request.get("set", {})
    if not isinstance(updates, dict):
        raise StateUpdateError("set_invalid: expected object")
    invalid_fields = sorted(set(updates) - (set(FIELDS) - {"id"}))
    if invalid_fields:
        raise StateUpdateError("set_unknown_or_immutable: " + ",".join(invalid_fields))
    if any(not isinstance(value, str) for value in updates.values()):
        raise StateUpdateError("set_value_invalid: all values must be strings")
    append_notes = request.get("append_notes", [])
    if not isinstance(append_notes, list) or any(
        not isinstance(value, str) or not value for value in append_notes
    ):
        raise StateUpdateError("append_notes_invalid: expected non-empty string array")
    if any(len(value) > NOTE_ITEM_LIMIT for value in append_notes):
        raise StateUpdateError(
            f"append_notes_item_too_large: maximum {NOTE_ITEM_LIMIT} characters; use event"
        )
    if sum(len(value) for value in append_notes) > NOTE_APPEND_LIMIT:
        raise StateUpdateError(
            f"append_notes_too_large: maximum {NOTE_APPEND_LIMIT} characters; use event"
        )
    boundary = request.get("commit_boundary", "none")
    if boundary not in COMMIT_BOUNDARIES:
        raise StateUpdateError(
            "commit_boundary_invalid: expected " + ",".join(sorted(COMMIT_BOUNDARIES))
        )
    event = request.get("event")
    if event is not None and not isinstance(event, dict):
        raise StateUpdateError("event_invalid: expected object or null")

    original_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    expected_hash = request.get("expected_sha256")
    if expected_hash is not None and (not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or expected_hash != original_hash):
        raise StateUpdateError("csv_version_conflict: CSV 已更新，请基于新版本重试")
    rows, has_bom = _read_csv(csv_path)
    missing_fields = sorted(set(updates) - set(rows[0]))
    if missing_fields:
        raise StateUpdateError("set_field_not_in_csv: " + ",".join(missing_fields))
    _validate_rows(rows)
    matches = [row for row in rows if row["id"] == row_id]
    if len(matches) != 1:
        raise StateUpdateError(
            f"row_lookup_invalid: {row_id} matched {len(matches)} rows"
        )
    target = matches[0]
    _assert_write_context(csv_path, target)
    original_notes = target["notes"]
    replacement_notes = updates.get("notes")
    if (
        replacement_notes is not None
        and len(replacement_notes) - len(original_notes) > NOTE_APPEND_LIMIT
    ):
        raise StateUpdateError(
            f"notes_growth_too_large: maximum growth {NOTE_APPEND_LIMIT}; use event"
        )
    target.update(updates)
    try:
        target["notes"] = upsert_note_tags(target["notes"], request.get("set_note_tags", {}))
    except ValueError as exc:
        raise StateUpdateError(str(exc)) from exc
    if len(target["notes"]) - len(original_notes) > NOTE_APPEND_LIMIT:
        raise StateUpdateError("notes_growth_too_large: use event for long evidence")

    event_digest = ""
    sidecar_path = csv_path.with_suffix(".events.json")
    if event is not None:
        event_record = {
            "schema_version": SCHEMA,
            "row_id": row_id,
            "event": event,
        }
        event_digest = hashlib.sha256(_canonical_json(event_record)).hexdigest()
        append_notes = [
            *append_notes,
            f"event:{sidecar_path.name}#{event_digest}",
        ]
    if append_notes:
        prefix = "; " if target["notes"].strip() else ""
        target["notes"] = target["notes"] + prefix + "; ".join(append_notes)

    _validate_rows(rows)
    try:
        for row in rows:
            parse_note_tags(row["notes"])
    except ValueError as exc:
        raise StateUpdateError(f"{exc}:row={row['id']}") from exc
    _validate_single_prerun(rows, row_id)
    _validate_claims(csv_path, rows)
    from git_isolation import row_git_errors
    git_errors = row_git_errors(csv_path, target)
    if git_errors:
        raise StateUpdateError("; ".join(git_errors))
    if target.get("remote_state") == "ingested":
        from mission_completion import ingest_completion_errors
        root = Path(_git(["rev-parse", "--show-toplevel"], csv_path.parent) or csv_path.parent)
        ingest_errors = ingest_completion_errors(csv_path, [target], workdir=root)
        if ingest_errors:
            raise StateUpdateError("; ".join(ingest_errors))
    csv_bytes = _encode_csv(rows, has_bom)
    if hashlib.sha256(csv_path.read_bytes()).hexdigest() != original_hash:
        raise StateUpdateError("csv_version_conflict: CSV 被其他写者修改")

    if event is not None:
        events: list[Any] = []
        if sidecar_path.exists():
            try:
                existing = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise StateUpdateError(
                    f"event_sidecar_invalid: {sidecar_path}: {error}"
                ) from error
            if not isinstance(existing, list):
                raise StateUpdateError("event_sidecar_invalid: expected JSON array")
            events = existing
        if not any(
            isinstance(item, dict) and item.get("event_sha256") == event_digest
            for item in events
        ):
            events.append(
                {
                    "event_sha256": event_digest,
                    "row_id": row_id,
                    "event": event,
                }
            )
        sidecar_bytes = json.dumps(
            events, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8") + b"\n"
        _atomic_replace_bytes(sidecar_path, sidecar_bytes, replace)

    _atomic_replace_bytes(csv_path, csv_bytes, replace)
    return {
        "ok": True,
        "row_id": row_id,
        "event_sha256": event_digest,
        "sidecar": str(sidecar_path) if event is not None else "",
        "commit_boundary": boundary,
        "git_commit_recommended": boundary != "none",
        # Authoritative post-write state. The caller must not re-read the CSV to
        # confirm a successful write; doing so was the single largest source of
        # redundant bookkeeping calls.
        "row": dict(target),
        "rows_total": len(rows),
        "columns": len(rows[0]),
        "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument(
        "request",
        help="JSON request path, or '-' to read the request from stdin",
    )
    args = parser.parse_args()
    try:
        request_text = (
            sys.stdin.read()
            if args.request == "-"
            else Path(args.request).read_text(encoding="utf-8")
        )
        request = json.loads(request_text)
        result = apply_update(args.csv_path, request)
    except (OSError, UnicodeError, json.JSONDecodeError, StateUpdateError) as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
