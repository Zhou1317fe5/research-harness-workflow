#!/usr/bin/env python3
"""Deterministically locate incomplete Mission CSVs under issues/."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / ".agents"))
from harness.workflow.mission_state import INACTIVE, load_registry


SCHEMA_VERSION = "mission.recovery-scan.v1"


def _load_completion():
    path = (
        Path(__file__).resolve().parents[2]
        / "mission-csv-execute/scripts/mission_completion.py"
    )
    spec = importlib.util.spec_from_file_location("mission_completion", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load completion contract: {path}")
    module = importlib.util.module_from_spec(spec)
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def scan(repo_root: Path) -> dict[str, Any]:
    root = repo_root.expanduser().resolve()
    issues = root / "issues"
    completion = _load_completion()
    registry = load_registry(root)
    current_id = registry.get("current_task")
    current = registry["tasks"].get(current_id, {})
    registered = {}
    for task_id, task in registry["tasks"].items():
        if task.get("csv"):
            if task["csv"] in registered:
                raise ValueError("同一 CSV 绑定了多个任务")
            registered[task["csv"]] = {"task_id": task_id, **task}
    directory_csvs = sorted(issues.glob("*/*.csv"))
    flat_csvs = sorted(
        path for path in issues.glob("*.csv") if path.name != "TEMPLATE.csv"
    )
    directory_stems = {path.stem for path in directory_csvs}
    candidates: list[dict[str, Any]] = []
    complete: list[str] = []
    inactive: list[dict[str, Any]] = []
    for kind, paths in (("directory", directory_csvs), ("legacy_flat", flat_csvs)):
        for path in paths:
            if kind == "legacy_flat" and path.stem in directory_stems:
                continue
            relative = str(path.relative_to(root))
            task = registered.get(relative)
            if task and task["status"] in INACTIVE:
                inactive.append({"path": relative, "task_id": task["task_id"], "status": task["status"]})
                continue
            reasons = completion.csv_completion_errors(path, workdir=root)
            if reasons:
                candidates.append(
                    {
                        "path": relative,
                        "kind": kind,
                        "mtime_ns": path.stat().st_mtime_ns,
                        "reasons": reasons,
                        "task_id": task["task_id"] if task else None,
                    }
                )
            else:
                complete.append(relative)
    candidates.sort(
        key=lambda item: (
            item["task_id"] != current_id if current_id else False,
            item["kind"] != "directory", -item["mtime_ns"], item["path"]
        )
    )
    resume_target = None
    if current and current["status"] == "paused":
        resume_target = {"task_id": current_id, "kind": "paused", "path": current.get("csv") or current.get("spec")}
    elif current and current["status"] not in INACTIVE:
        if current.get("csv") and not (root / current["csv"]).is_file():
            raise ValueError("当前任务指向不存在的 CSV；不自动恢复其他旧任务")
        if current.get("csv") and any(item["path"] == current["csv"] for item in candidates):
            resume_target = {"task_id": current_id, "kind": "csv", "path": current["csv"]}
        elif not current.get("csv") and current.get("spec"):
            # 只读取 issues 中已登记的明确指针，不扫描 docs/specs。
            target = (root / current["spec"]).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError("当前任务指向不存在的 spec")
            resume_target = {"task_id": current_id, "kind": "spec", "path": current["spec"]}
    return {
        "schema_version": SCHEMA_VERSION,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "complete": sorted(complete),
        "inactive": inactive,
        "current_task": current_id,
        "resume_target": resume_target,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()
    try:
        result = scan(Path(args.repo_root))
    except (OSError, RuntimeError, ValueError) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "candidates": [],
            "candidate_count": 0,
            "complete": [],
            "errors": [f"scan_failed:{exc}"],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
