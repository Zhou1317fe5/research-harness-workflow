#!/usr/bin/env python3
"""在 rrctl 的 workload 中顺序执行项目命令；任一阶段失败即停止。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from project_config import DEFAULT_CONFIG, REPO_ROOT, load_config


def confined(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes root: {relative}")
    return path


def run_pipeline(config: dict, repo_root: Path, output_root: Path) -> int:
    stages = config.get("pipeline", {}).get("stages", [])
    if not stages:
        raise ValueError("configure at least one pipeline stage")
    output_root.mkdir(parents=True, exist_ok=True)
    # 保留一次执行标记，避免误把已有运行重新训练一遍。
    marker = output_root / "pipeline-status.json"
    with marker.open("x", encoding="utf-8") as stream:
        json.dump({"state": "starting"}, stream)
    values = {"repo_root": str(repo_root), "output_root": str(output_root), "run_id": os.environ.get("RRCTL_RUN_ID", "")}
    try:
        for stage in stages:
            name = stage["name"]
            for relative in stage.get("requires", []):
                if not confined(output_root, relative).is_file():
                    raise ValueError(f"{name}: required input missing: {relative}")
            argv = [value.format_map(values) for value in stage["argv"]]
            cwd = confined(repo_root, stage.get("cwd", "."))
            log = confined(output_root, stage.get("log", f"{name}.log"))
            log.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"state": "running", "stage": name}) + "\n")
            print(f"[pipeline] {name}: started; log={log}", flush=True)
            with log.open("xb") as stream, subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
                assert process.stdout is not None
                for chunk in iter(lambda: process.stdout.read1(65536), b""):
                    stream.write(chunk)
                    stream.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                return_code = process.wait()
            if return_code:
                marker.write_text(json.dumps({"state": "failed", "stage": name, "exit_code": return_code}) + "\n")
                print(f"[pipeline] {name}: failed ({return_code})", flush=True)
                return return_code if return_code > 0 else 128 - return_code
            for relative in stage.get("outputs", []):
                if not confined(output_root, relative).is_file():
                    raise ValueError(f"{name}: required output missing: {relative}")
            print(f"[pipeline] {name}: completed", flush=True)
        marker.write_text(json.dumps({"state": "completed"}) + "\n")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        marker.write_text(json.dumps({"state": "failed", "error": str(exc)}) + "\n")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--check", action="store_true", help="only validate configuration")
    parser.add_argument("--output-root", type=Path, help="override output root for isolated local fixtures")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.check:
            print(json.dumps({"ok": True, "stages": [s["name"] for s in config.get("pipeline", {}).get("stages", [])]}))
            return 0
        output = args.output_root or os.environ.get("RRCTL_OUTPUT_ROOT")
        if not output:
            raise ValueError("RRCTL_OUTPUT_ROOT is required; launch the workload through rrctl")
        return run_pipeline(config, REPO_ROOT, Path(output).resolve())
    except (OSError, ValueError, KeyError) as exc:
        print(f"[pipeline] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
