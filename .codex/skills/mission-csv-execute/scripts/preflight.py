#!/usr/bin/env python3
"""Mission CSV 执行前静态预检：把运行期才首次触发的 schema/状态/分支错误前移。

不修改任何文件，只读校验并逐行打印结果；任何一项失败以退出码 2 结束。

检查项（全部与 `csv_state.py` 的运行期校验同源，直接复用其函数，不复制逻辑）：

1. **请求面**：每个 update 请求的 schema_version、字段白名单、commit_boundary 枚举、
   append_notes 大小、set_note_tags 可解析——在首次写之前暴露
   `commit_boundary_invalid` / `set_note_tags_invalid` / `request_unknown_fields` 等。
2. **分支上下文**：对每个待写行预检 `_assert_write_context` 的分支一致性
   （`write_context_branch_mismatch` 在远程行 launch 前才触发，代价最大）。
3. **PRERUN 行**：review_mode / review_result 枚举、gated_run 存在性——
   `prerun_review_result_invalid` 等。
4. **claim ledger**：`_validate_claims` 全量预检。

用法::

    python3 preflight.py <mission.csv> [request.json ...]

不带 request 时只检查 CSV 本身的可写性（枚举、分支上下文、PRERUN 结构、claim 引用）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import csv_state  # noqa: E402
from mission_completion import parse_note_tags  # noqa: E402


def _check_request(request: dict, label: str, rows: list[dict[str, str]]) -> list[str]:
    """静态重放 csv_state 的请求面校验（不含写入与 git 副作用）。"""
    errors: list[str] = []
    allowed = {
        "schema_version", "row_id", "set", "append_notes", "set_note_tags",
        "event", "retry_binding", "commit_boundary", "expected_sha256",
    }
    unknown = sorted(set(request) - allowed)
    if unknown:
        errors.append(f"{label}: request_unknown_fields: {','.join(unknown)}")
    if request.get("schema_version") != csv_state.SCHEMA:
        errors.append(f"{label}: schema_version_invalid: expected {csv_state.SCHEMA}")
    row_id = request.get("row_id")
    if not isinstance(row_id, str) or not row_id:
        errors.append(f"{label}: row_id_invalid: expected non-empty string")
        return errors
    updates = request.get("set", {})
    if not isinstance(updates, dict):
        errors.append(f"{label}: set_invalid: expected object")
        updates = {}
    invalid_fields = sorted(set(updates) - (set(csv_state.FIELDS) - {"id"}))
    if invalid_fields:
        errors.append(f"{label}: set_unknown_or_immutable: {','.join(invalid_fields)}")
    if any(not isinstance(value, str) for value in updates.values()):
        errors.append(f"{label}: set_value_invalid: all values must be strings")
    append_notes = request.get("append_notes", [])
    if not isinstance(append_notes, list) or any(
        not isinstance(value, str) or not value for value in append_notes
    ):
        errors.append(f"{label}: append_notes_invalid: expected non-empty string array")
    if any(len(value) > csv_state.NOTE_ITEM_LIMIT for value in append_notes):
        errors.append(
            f"{label}: append_notes_item_too_large: maximum {csv_state.NOTE_ITEM_LIMIT} characters"
        )
    if sum(len(value) for value in append_notes) > csv_state.NOTE_APPEND_LIMIT:
        errors.append(
            f"{label}: append_notes_too_large: maximum {csv_state.NOTE_APPEND_LIMIT} characters"
        )
    boundary = request.get("commit_boundary", "none")
    if boundary not in csv_state.COMMIT_BOUNDARIES:
        errors.append(
            f"{label}: commit_boundary_invalid: expected {','.join(sorted(csv_state.COMMIT_BOUNDARIES))}"
        )
    retry_binding = request.get("retry_binding")
    if retry_binding is not None and not isinstance(retry_binding, dict):
        errors.append(f"{label}: retry_binding_invalid: expected object")
    matches = [row for row in rows if row["id"] == row_id]
    if len(matches) != 1:
        errors.append(f"{label}: row_lookup_invalid: {row_id} matched {len(matches)} rows")
        return errors
    target = matches[0]
    set_note_tags = request.get("set_note_tags", {})
    planned = dict(target)
    planned.update({k: v for k, v in updates.items() if isinstance(v, str)})
    if set_note_tags:
        try:
            planned["notes"] = csv_state.upsert_note_tags(target["notes"], set_note_tags)
            parse_note_tags(planned["notes"])
        except ValueError as exc:
            errors.append(f"{label}: set_note_tags_invalid: {exc}")
    # 对计划翻转状态的行，预演 PRERUN 结构校验（枚举与模式/结果一致性同样约束
    # set_note_tags 写入的 review_result，不能只检查 set 的列更新）。
    if csv_state._is_prerun(planned):
        planned_rows = [planned if row["id"] == row_id else row for row in rows]
        try:
            csv_state._validate_single_prerun(planned_rows, row_id)
        except csv_state.StateUpdateError as exc:
            errors.append(f"{label}: {exc}")
    return errors


def preflight(csv_path: Path, requests: list[tuple[str, dict]]) -> tuple[list[str], bool]:
    """返回 (errors, branch_check_skipped)。"""
    errors: list[str] = []
    try:
        rows, _has_bom = csv_state._read_csv(csv_path)
    except csv_state.StateUpdateError as exc:
        return [f"csv_unreadable: {exc}"], True
    try:
        csv_state._validate_rows(rows)
    except csv_state.StateUpdateError as exc:
        errors.append(f"csv_structure: {exc}")
    for row in rows:
        try:
            parse_note_tags(row["notes"])
        except ValueError as exc:
            errors.append(f"notes_unparseable:{row['id']}: {exc}")
    # 分支上下文：任何待写行（未闭环）在当前 checkout 下必须可写。
    # git 不可用（非 git 仓库、rev-parse 失败）时跳过该检查，但必须在输出中显式
    # 声明，避免静默通过给人“分支上下文已验证”的错觉。
    try:
        current = csv_state._git(["rev-parse", "--abbrev-ref", "HEAD"], csv_path.parent)
    except Exception:  # pragma: no cover - git 不可用时不做分支预检
        current = None
    branch_check_skipped = not bool(current)
    if current:
        for row in rows:
            if csv_state._is_closed(row):
                continue
            try:
                csv_state._assert_write_context(csv_path, row)
            except csv_state.StateUpdateError as exc:
                errors.append(f"branch_context:{row['id']}: {exc}")
    try:
        csv_state._validate_single_prerun(rows)
    except csv_state.StateUpdateError as exc:
        errors.append(f"prerun_structure: {exc}")
    try:
        csv_state._validate_claims(csv_path, rows)
    except csv_state.StateUpdateError as exc:
        errors.append(f"claims: {exc}")
    for label, request in requests:
        errors.extend(_check_request(request, label, rows))
    return errors, branch_check_skipped


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    csv_path = Path(argv[0]).expanduser().resolve()
    requests: list[tuple[str, dict]] = []
    errors: list[str] = []
    for label in argv[1:]:
        try:
            text = sys.stdin.read() if label == "-" else Path(label).read_text(encoding="utf-8")
            value = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{label}: request_unreadable: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{label}: request_invalid: expected object")
            continue
        requests.append((label, value))
    errors, branch_skipped = preflight(csv_path, requests)
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 2
    note = " (branch-context check skipped: not a git checkout)" if branch_skipped else ""
    print(f"PREFLIGHT OK: {csv_path.name} rows_checked_with_{len(requests)}_requests{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
