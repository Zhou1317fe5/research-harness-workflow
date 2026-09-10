#!/usr/bin/env python3
"""记录当前任务及暂停、取消、替换状态；不启动实验，也不授予执行权限。"""
from __future__ import annotations

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.workflow.mission_state import main
    raise SystemExit(main())

import argparse
from datetime import datetime, timezone
import json
import importlib.util
from pathlib import Path, PurePosixPath
import re
import sys

from harness.common.locking import file_lock
from harness.memory.research_memory import atomic, sensitive

SCHEMA = "mission.lifecycle.v1"
STATUSES = {"preparing", "active", "paused", "cancelled", "superseded", "completed"}
INACTIVE = {"paused", "cancelled", "superseded", "completed"}


def state_path(root):
    return Path(root).resolve() / "issues/.missions.json"


def _reference(root, value, kind):
    if not isinstance(value, str) or not value or any(c in value for c in "\0\n\r\\"):
        raise ValueError("任务路径必须是项目相对路径")
    relative = PurePosixPath(value)
    prefix = "issues/" if kind == "csv" else "docs/specs/"
    suffix = ".csv" if kind == "csv" else ".md"
    if relative.is_absolute() or ".." in relative.parts or not value.startswith(prefix) or relative.suffix != suffix:
        raise ValueError("任务路径必须位于 issues 或 docs/specs 的对应工件中")
    path = (Path(root) / value).resolve()
    if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
        raise ValueError("任务工件不存在或路径越界")
    return relative.as_posix()


def load_registry(root):
    path = state_path(root)
    if not path.exists():
        return {"schema_version": SCHEMA, "current_task": None, "tasks": {}}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA or not isinstance(data.get("tasks"), dict):
        raise ValueError("无效的任务生命周期文件")
    seen_csvs = set()
    for task_id, task in data["tasks"].items():
        if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id)
                or not isinstance(task, dict) or task.get("status") not in STATUSES):
            raise ValueError("无效的任务生命周期条目")
        if not task.get("spec") and not task.get("csv"):
            raise ValueError("任务缺少 spec/CSV 指针")
        if not isinstance(task.get("history"), list):
            raise ValueError("任务缺少生命周期来源记录")
        # 生命周期 tombstone 即使原文件已归档也需保留，因此这里不要求工件仍存在。
        for field in ("csv", "spec"):
            value = task.get(field)
            if value and (not isinstance(value, str) or PurePosixPath(value).is_absolute()
                          or ".." in PurePosixPath(value).parts or "\\" in value
                          or any(c in value for c in "\n\r\0")
                          or not value.startswith("issues/" if field == "csv" else "docs/specs/")
                          or not value.endswith(".csv" if field == "csv" else ".md")):
                raise ValueError("任务工件路径越界")
        if task.get("csv"):
            if task["csv"] in seen_csvs:
                raise ValueError("同一 CSV 绑定了多个任务")
            seen_csvs.add(task["csv"])
    if data.get("current_task") is not None and (not isinstance(data["current_task"], str)
                                                or data["current_task"] not in data["tasks"]):
        raise ValueError("当前任务指针不存在")
    return data


def task_for_csv(root, csv_path):
    root = Path(root).resolve()
    try:
        relative = Path(csv_path).resolve().relative_to(root).as_posix()
    except ValueError:
        return None
    data = load_registry(root)
    matches = [{"task_id": key, **task} for key, task in data["tasks"].items() if task.get("csv") == relative]
    if len(matches) > 1:
        raise ValueError("同一 CSV 绑定了多个任务")
    return matches[0] if matches else None


def assert_launchable(root, csv_path):
    task = task_for_csv(root, csv_path)
    if task and task["status"] != "active":
        raise ValueError(f"mission_not_active:{task['task_id']}:{task['status']}")


def update(root, task_id, *, action, source_ref, reason="", spec=None, csv=None, status=None, replacement=None, replaces=None):
    root = Path(root).resolve()
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id):
        raise ValueError("无效的 task_id")
    if (not isinstance(source_ref, str) or not source_ref.strip() or len(source_ref) > 1000
            or any(c in source_ref for c in "\n\r\0")):
        raise ValueError("生命周期变化必须记录 source_ref")
    if not isinstance(reason, str) or len(reason) > 1000 or any(c in reason for c in "\n\r\0"):
        raise ValueError("reason 必须是有界的单段文本")
    if sensitive(json.dumps([source_ref, reason, spec, csv])):
        raise ValueError("任务来源或原因可能含凭据")
    if spec is not None:
        spec = _reference(root, spec, "spec")
    if csv is not None:
        csv = _reference(root, csv, "csv")
    path = state_path(root)
    with file_lock(path.with_suffix(".lock")):
        data = load_registry(root)
        task = data["tasks"].get(task_id)
        if action == "register":
            if not spec and not csv:
                raise ValueError("注册任务需要 spec 或 CSV")
            if task:
                if (spec and task.get("spec") != spec) or (csv and task.get("csv") != csv):
                    raise ValueError("task_id 已用于其他工件")
                # 重放注册绝不恢复已取消或被替换的任务。
                return {"task_id": task_id, **task}
            task = {"status": "active" if csv else "preparing", "spec": spec, "csv": csv, "history": []}
            data["tasks"][task_id] = task
            if replaces:
                old = data["tasks"].get(replaces)
                if not old or replaces == task_id or not reason.strip() or old["status"] in {"cancelled", "superseded", "completed"}:
                    raise ValueError("替换当前任务需要有效的旧 task_id 和原因")
                old.update(status="superseded", replacement=task_id, source_ref=source_ref,
                           updated_at=datetime.now(timezone.utc).isoformat())
                old["history"].append({"action": "replace", "status": "superseded", "source_ref": source_ref,
                                       "reason": reason, "at": old["updated_at"]})
            data["current_task"] = task_id
        elif task is None:
            raise ValueError("任务未注册")
        elif action == "bind":
            if not csv or task["status"] in INACTIVE:
                raise ValueError("只能为准备中或活动任务绑定 CSV")
            if task.get("csv") and task["csv"] != csv:
                raise ValueError("任务已绑定其他 CSV")
            if task.get("csv") == csv and task["status"] == "active":
                return {"task_id": task_id, **task}
            task.update(csv=csv, status="active")
            data["current_task"] = task_id
        elif action == "transition":
            if status not in STATUSES:
                raise ValueError("无效的任务目标状态")
            if status == "preparing" and (task.get("csv") or task["status"] not in {"paused", "preparing"}):
                raise ValueError("只有无 CSV 的暂停任务可恢复 preparing")
            if status == "active" and not task.get("csv"):
                raise ValueError("没有 CSV 的任务应恢复 spec 准备阶段")
            if task["status"] in {"cancelled", "superseded", "completed"} and status not in {task["status"]}:
                raise ValueError("终态任务不可重启；新授权应注册新 task_id")
            if status in INACTIVE and not reason.strip():
                raise ValueError("暂停或关闭任务必须记录原因")
            if status == "completed":
                if not task.get("csv"):
                    raise ValueError("未建立 CSV 的任务不能声明完成")
                scripts = Path(__file__).resolve().parents[3] / ".codex/skills/mission-csv-execute/scripts"
                if str(scripts) not in sys.path:
                    sys.path.insert(0, str(scripts))
                module_spec = importlib.util.spec_from_file_location("lifecycle_completion", scripts / "mission_completion.py")
                completion = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(completion)
                if completion.csv_completion_errors(root / task["csv"], workdir=root):
                    raise ValueError("任务 CSV 尚未闭环，不能将生命周期标为 completed")
            if status == "superseded":
                if (replacement == task_id or replacement not in data["tasks"]
                        or data["tasks"][replacement]["status"] in INACTIVE):
                    raise ValueError("被替换任务必须指向另一个已登记任务")
                task["replacement"] = replacement
            task["status"] = status
            if status in {"active", "preparing"}:
                data["current_task"] = task_id
            elif status != "paused" and data.get("current_task") == task_id:
                data["current_task"] = replacement if status == "superseded" else None
        elif action == "select":
            if task["status"] in INACTIVE:
                raise ValueError("暂停或关闭的任务不能直接选为当前任务")
            data["current_task"] = task_id
        else:
            raise ValueError("未知的生命周期动作")
        if csv and any(key != task_id and item.get("csv") == csv for key, item in data["tasks"].items()):
            raise ValueError("同一 CSV 只能绑定一个任务")
        task.update(updated_at=datetime.now(timezone.utc).isoformat(), source_ref=source_ref)
        task["history"].append({"action": action, "status": task["status"], "source_ref": source_ref,
                                "reason": reason, "at": task["updated_at"]})
        atomic(path, data)
        return {"task_id": task_id, **task}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("action", choices=["status", "register", "bind", "transition", "select"])
    parser.add_argument("--task-id")
    parser.add_argument("--source-ref")
    parser.add_argument("--reason", default="")
    parser.add_argument("--spec")
    parser.add_argument("--csv")
    parser.add_argument("--status", choices=sorted(STATUSES))
    parser.add_argument("--replacement")
    parser.add_argument("--replaces")
    args = parser.parse_args()
    try:
        result = load_registry(args.repo_root) if args.action == "status" else update(
            args.repo_root, args.task_id, action=args.action, source_ref=args.source_ref, reason=args.reason,
            spec=args.spec, csv=args.csv, status=args.status, replacement=args.replacement, replaces=args.replaces)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
