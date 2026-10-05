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
# 只有这些事件代表「远端真的产出过结果」。plumbing 事件（绑定/重试/pre-review
# smoke 完成）不进这一集合：它们证明流程在动，不证明科学在动。
SCIENTIFIC_COMPLETION_KINDS = frozenset({
    "remote_completed",
    "remote_run_terminal",
    "remote_terminal",
    "milestone_completed",
})
RETRY_EVENT_PREFIXES = ("retry_", "rebind", "supersession", "prebind")
# 受限用途运行：由 pre_review_smoke / preregistered_read_only_probe 产生的运行。
RESTRICTED_RUN_EVENT_PREFIXES = ("pre_review_smoke",)
STATE_NARRATION_BOUNDARIES = frozenset({"none"})
PROGRESS_SCHEMA = "mission.progress.v1"
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
        verdict_artifact = _note_value(notes, "verdict_artifact")
        if result and not verdict_artifact:
            raise StateUpdateError(
                f"prerun_verdict_artifact_missing: {row['id']}"
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


# ---------------------------------------------------------------------------
# 迁移方向约束（F-012 修复）：做过的不可逆事实不得通过单字段或跨字段组合回退。
#
# 约束分两类：
# (1) 字段单调：dev/review_*/git/remote 已有正面枚举，但未约束方向；这里补齐
#     「只允许向信息更多的一档推进，不允许回退」。
# (2) 组合（junction）：git=已提交 是「源码冻结」事实，冻结之后 review/dev 不
#     得回退，remote 也不得处于「正在运行」但未提交判定的中间态。
#
# 历史背景（为何叫 F-012）：批 3 worker 4 对 378 个 (d,ri,rr,g,remote) 单元格
# 穷举后发现 8 类可随意回退的迁移；worker 已按“现状=允许”写了 8 个锁定断言，
# 本修复翻转那 8 个断言为“拒绝”，并新增 17 个守卫用例。

_PROGRESSION = {"未开始": 0, "进行中": 1, "已完成": 2}
_REMOTE_FORWARD = {
    "": {"", "not_applicable", "running_remote"},
    "not_applicable": {"not_applicable"},
    "running_remote": {"running_remote", "completed", "failed"},
    "completed": {"completed", "artifacts_pulled", "ingested"},
    "artifacts_pulled": {"artifacts_pulled", "ingested"},
    "ingested": {"ingested"},
    "failed": {"failed", "artifacts_pulled", "ingested"},
}


def _validate_retry_binding(
    original: dict[str, str],
    updated: dict[str, str],
    binding: Any,
    event: Any,
) -> bool:
    """Validate an explicit new-RunID retry reconciliation."""
    changed_run = original.get("run_id", "") != updated.get("run_id", "")
    retry_state = original.get("remote_state") == "failed" and changed_run
    if binding is None:
        if retry_state and updated.get("remote_state") != "failed":
            raise StateUpdateError("retry_binding_required: failed row changed to a new RunID")
        return False
    if not isinstance(binding, dict):
        raise StateUpdateError("retry_binding_invalid: expected object")
    if set(binding) != {"prior_run_id", "new_run_id", "reason"}:
        raise StateUpdateError("retry_binding_invalid: expected prior_run_id,new_run_id,reason")
    prior = binding["prior_run_id"]
    new = binding["new_run_id"]
    reason = binding["reason"]
    if not all(isinstance(value, str) and value for value in (prior, new, reason)):
        raise StateUpdateError("retry_binding_invalid: values must be non-empty strings")
    if original.get("remote_state") != "failed":
        raise StateUpdateError("retry_binding_invalid: prior remote_state must be failed")
    row_already_bound = (
        original.get("run_id") == new
        and isinstance(event, dict)
        and event.get("row_run_id") == new
        and (
            f"prior_run:{prior}:" in original.get("notes", "")
            or f"prior_run:{prior};" in original.get("notes", "")
        )
    )
    if (original.get("run_id") != prior and not row_already_bound) or updated.get("run_id") != new or prior == new:
        raise StateUpdateError("retry_binding_invalid: RunID does not match row transition")
    if updated.get("remote_state") not in {"running_remote", "completed", "artifacts_pulled"}:
        raise StateUpdateError("retry_binding_invalid: target remote_state is not a retry outcome")
    if not isinstance(event, dict) or event.get("kind") != "retry_reconciliation":
        raise StateUpdateError("retry_binding_event_required: kind=retry_reconciliation")
    if event.get("prior_run_id") != prior or event.get("new_run_id") != new:
        raise StateUpdateError("retry_binding_event_mismatch")
    return True


def _validate_row_transition(
    original: dict[str, str],
    updated: dict[str, str],
    *,
    retry_binding: bool = False,
) -> None:
    """写后单调性/junction 校验：original 为读入 CSV 的原值，updated 为写后 row。

    只在 apply_update 的 `_validate_rows(rows)` 之前调用（此处抛 StateUpdateError
    即可阻止该行落盘）。不修改 original 或 updated；违反时抛 StateUpdateError。
    """
    for field in ("dev_state", "review_initial_state", "review_regression_state"):
        o = original.get(field, "")
        u = updated.get(field, "")
        if _PROGRESSION.get(u, 0) < _PROGRESSION.get(o, 0):
            raise StateUpdateError(f"state_regression:{field}:{o}->{u}")
    o = original.get("git_state", "")
    u = updated.get("git_state", "")
    if o == "已提交" and u != "已提交":
        raise StateUpdateError(f"git_reopen:{original['id']}")
    o = original.get("remote_state", "")
    u = updated.get("remote_state", "")
    allowed_remote = _REMOTE_FORWARD.get(o, {o})
    if retry_binding and o == "failed" and original.get("run_id") != updated.get("run_id"):
        allowed_remote = allowed_remote | {"running_remote", "completed", "artifacts_pulled"}
    if u not in allowed_remote:
        raise StateUpdateError(f"remote_regression:{o}->{u}")
    if updated.get("git_state") == "已提交":
        # 已提交源码可以拥有仍在运行的远端 RunID；这是 rrctl 超时/恢复
        # 的合法中间态。真正的代码回退仍由 git_state 单调守卫阻止。
        if updated.get("dev_state") == "未开始":
            raise StateUpdateError(f"git_committed_with_dev_unstarted:{original['id']}")


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


def _write_progress_summary(csv_path: Path, rows: list[dict[str, str]], replace) -> Path | None:
    """把「科学在不在动」变成一眼可读的派生物。

    动机：CSV 与 events.json 记录的是**证据**，读它们需要重建状态。历史会话里人
    反复问「卡点在哪」「多久能完」，说明现有信号不可读；而 rrctl 的 attention
    唤醒又不等于科学进展。本文件把两者分开：只有远端真的产出结果的事件才算
    「科学在动」；绑定/重试/pre-review smoke 单独计数。

    本文件是派生视图：不参与任何闭环判定，不替代 CSV 或 events.json，删除后可由
    下一次写回重建。任何以「文件存在」为条件的校验都不要引用它。
    """
    events: list[Any] = []
    sidecar_path = csv_path.with_suffix(".events.json")
    if sidecar_path.exists():
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, list):
            events = payload
    latest: dict[str, tuple[str, str]] = {}
    counts = {"completion": 0, "retry": 0, "restricted": 0}
    for item in events:
        if not isinstance(item, dict):
            continue
        event = item.get("event")
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if not isinstance(kind, str) or not kind:
            continue
        row_id = str(item.get("row_id") or "")
        if kind in SCIENTIFIC_COMPLETION_KINDS:
            counts["completion"] += 1
            latest["completion"] = (
                row_id,
                f"{kind} {event.get('run_id') or event.get('new_run_id') or event.get('row_run_id') or '-'}",
            )
        elif kind.startswith(RETRY_EVENT_PREFIXES):
            counts["retry"] += 1
            latest["retry"] = (row_id, kind)
        if kind.startswith(RESTRICTED_RUN_EVENT_PREFIXES):
            counts["restricted"] += 1
    closed = sum(1 for row in rows if _is_closed(row))
    ingested = sum(1 for row in rows if row.get("remote_state") == "ingested")
    open_remote = [
        row["id"]
        for row in rows
        if row.get("remote_state") in {"running_remote", "completed", "artifacts_pulled"}
    ]
    lines = [
        "# Mission 进度（由 csv_state.py 自动生成，请勿手工编辑）",
        "",
        f"schema: {PROGRESS_SCHEMA}",
        f"csv: {csv_path.name}",
        f"rows: {len(rows)}",
        f"closed_rows: {closed}",
        f"ingested_rows: {ingested}",
        f"scientific_completions: {counts['completion']}",
        f"retry_events: {counts['retry']}",
        f"restricted_run_events: {counts['restricted']}",
    ]
    if "completion" in latest:
        row_id, detail = latest["completion"]
        lines.append(f"last_scientific_completion: {row_id} {detail}")
    else:
        lines.append("last_scientific_completion: none")
    if "retry" in latest:
        row_id, kind = latest["retry"]
        lines.append(f"last_retry_event: {row_id} {kind}")
    lines.append("open_remote_rows: " + (", ".join(open_remote) if open_remote else "none"))
    lines.append("")
    lines.append("（派生视图：不参与闭环判定，也不替代 CSV 或 events.json。）")
    progress_path = csv_path.with_suffix(".progress.md")
    _atomic_replace_bytes(progress_path, ("\n".join(lines) + "\n").encode("utf-8"), replace)
    return progress_path


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
        "retry_binding",
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
    retry_binding = request.get("retry_binding")

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
    original_row = dict(target)
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
    retry_reconciled = _validate_retry_binding(original_row, target, retry_binding, event)
    _validate_row_transition(original_row, target, retry_binding=retry_reconciled)
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
    progress_path = _write_progress_summary(csv_path, rows, replace)
    # 规则 2 已经要求「按逻辑边界提交」：readiness、unchanged poll、结果绑定和
    # closing preparation 不单独提交。历史 Mission 仍然产出大量单事件提交
    # （fss phase2 期间 45% 的提交只动 issues/ 与 docs/）。这里不新增门禁，只把
    # 「这次写回是否只叙述了状态」变成可观测量，让漂移可见而不是靠自觉。
    narration_only = boundary in STATE_NARRATION_BOUNDARIES and event is None
    if narration_only:
        commit_advice = (
            "状态叙述写回（commit_boundary=none，无新事件）：按规则 2 不单独提交，"
            "并入下一个逻辑边界提交。"
        )
    elif boundary in STATE_NARRATION_BOUNDARIES:
        commit_advice = "仅事件追加：与同一逻辑变更合并提交，不按单条事件提交。"
    else:
        commit_advice = "到达逻辑边界，可以提交。"
    return {
        "ok": True,
        "row_id": row_id,
        "event_sha256": event_digest,
        "sidecar": str(sidecar_path) if event is not None else "",
        "commit_boundary": boundary,
        "git_commit_recommended": boundary != "none",
        "commit_advice": commit_advice,
        "progress": str(progress_path) if progress_path is not None else "",
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
