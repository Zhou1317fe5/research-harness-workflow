#!/usr/bin/env python3
"""在 rrctl 的 workload 中顺序执行项目命令；任一阶段失败即停止。"""
from __future__ import annotations

# 直接运行脚本和通过 Python 包导入时使用同一实现。
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.pipeline.run_pipeline import main
    raise SystemExit(main())

import argparse
import json
import os
import ast
import shutil
import subprocess
import sys
from pathlib import Path

from harness.common.project_config import DEFAULT_CONFIG, REPO_ROOT, list_pipelines, load_config, pipeline_digest


def confined(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes root: {relative}")
    return path


def check_pipeline(config: dict, repo_root: Path) -> list[str]:
    """预检真实脚本和显式声明的输入检查；不执行训练入口。"""
    names = []
    for stage in config.get("pipeline", {}).get("stages", []):
        argv = stage["argv"]
        cwd = confined(repo_root, stage.get("cwd", "."))
        if shutil.which(argv[0]) is None:
            raise ValueError(f"{stage['name']}: executable unavailable: {argv[0]}")
        executable = Path(argv[0]).name
        if len(argv) > 1 and not argv[1].startswith("-") and executable in {"bash", "sh", "python", "python3"}:
            script = confined(cwd, argv[1])
            if not script.is_file():
                raise ValueError(f"{stage['name']}: entrypoint missing: {argv[1]}")
            if executable in {"bash", "sh"}:
                result = subprocess.run([argv[0], "-n", str(script)], capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise ValueError(f"{stage['name']}: shell syntax check failed: {result.stderr.strip()[:1000]}")
            else:
                ast.parse(script.read_text(), filename=str(script))
        if stage.get("check_argv"):
            result = subprocess.run(stage["check_argv"], cwd=cwd, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise ValueError(f"{stage['name']}: input check failed: {result.stderr.strip()[:1000]}")
        names.append(stage["name"])
    if not names:
        raise ValueError("configure at least one pipeline stage")
    return names


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
    # 脚本直接读取本次输出目录；显式 --output-root 同样覆盖继承的旧环境值。
    environment = {**os.environ, "RRCTL_OUTPUT_ROOT": str(output_root.resolve())}
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
            with log.open("xb") as stream, subprocess.Popen(argv, cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
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
    parser.add_argument("--pipeline", help="select a named script combination from project config")
    parser.add_argument("--list-pipelines", action="store_true", help="list available combinations without running scripts")
    parser.add_argument("--pipeline-sha256", help="verify the frozen stage definition from the RunSpec")
    parser.add_argument("--check", action="store_true", help="only validate configuration")
    parser.add_argument("--output-root", type=Path, help="override the rrctl workload output root")
    args = parser.parse_args()
    try:
        if args.list_pipelines:
            print(json.dumps({"ok": True, **list_pipelines(args.config)}, ensure_ascii=False))
            return 0
        config = load_config(args.config, pipeline=args.pipeline)
        if args.pipeline_sha256 and args.pipeline_sha256 != pipeline_digest(config):
            raise ValueError("pipeline definition differs from the frozen RunSpec")
        if args.check:
            print(json.dumps({"ok": True, "pipeline": config["pipeline"]["name"], "stages": check_pipeline(config, REPO_ROOT)}))
            return 0
        output = args.output_root or os.environ.get("RRCTL_OUTPUT_ROOT")
        if not output:
            raise ValueError("RRCTL_OUTPUT_ROOT is required; launch the workload through rrctl")
        return run_pipeline(config, REPO_ROOT, Path(output).resolve())
    except (OSError, ValueError, KeyError, SyntaxError, subprocess.TimeoutExpired) as exc:
        print(f"[pipeline] {exc}", file=sys.stderr)
        return 2
