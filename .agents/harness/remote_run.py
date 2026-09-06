#!/usr/bin/env python3
"""生成或执行 ready → launch → wait → pull；所有远程操作均交给 rrctl。"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from project_config import REPO_ROOT


def rrctl_call(argv: list[str], repo_root: Path) -> subprocess.CompletedProcess:
    env_file = repo_root / ".agents/harness/.env"
    if env_file.is_file():
        # 只传文件路径和参数，凭据由 shell 在进程内读取。
        argv = ["bash", "-c", 'set -e; set -a; source "$1"; set +a; shift; exec "$@"', "harness", str(env_file), *argv]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def execute(spec_path: Path, *, profiles: Path | None, poll_seconds: float) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    run_id = spec.get("run_id")
    if spec.get("schema_version") != "rrctl.run.v1" or not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("expected a valid rrctl.run.v1 RunSpec")
    if shutil.which("rrctl") is None:
        raise ValueError("rrctl is unavailable; install remote-run-control first")
    prefix = ["rrctl", "--json"]
    if profiles:
        prefix += ["--profiles", str(profiles.resolve())]
    for stage, arguments in (
        ("ready", [str(spec_path)]),
        ("launch", [str(spec_path)]),
        ("wait", [run_id, "--poll-seconds", str(poll_seconds)]),
        ("pull", [run_id]),
    ):
        result = rrctl_call([*prefix, stage, *arguments], REPO_ROOT)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"rrctl {stage} did not return JSON (exit={result.returncode})") from exc
        print(json.dumps({"stage": stage, "response": value}, ensure_ascii=False), flush=True)
        failed_state = value.get("result", {}).get("status", {}).get("state") in {"failed", "aborted"}
        if result.returncode or value.get("ok") is False or failed_state:
            if stage in {"launch", "wait"}:
                diagnostic = rrctl_call([*prefix, "pull", run_id, "--diagnostic"], REPO_ROOT)
                # 诊断拉取失败不能覆盖原始运行错误。
                if diagnostic.stdout.strip():
                    print(diagnostic.stdout.strip(), flush=True)
            return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runspec", type=Path)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=600)
    parser.add_argument("--execute", action="store_true", help="execute the generated rrctl sequence")
    args = parser.parse_args()
    try:
        if args.poll_seconds <= 0:
            raise ValueError("poll-seconds must be positive")
        spec = args.runspec.resolve()
        if not spec.is_file():
            raise ValueError(f"RunSpec not found: {spec}")
        profiles = args.profiles
        default_profiles = REPO_ROOT / ".agents/harness/profiles.json"
        if profiles is None and default_profiles.is_file():
            profiles = default_profiles
        if args.execute:
            return execute(spec, profiles=profiles, poll_seconds=args.poll_seconds)
        command = ["python", ".agents/harness/remote_run.py", str(spec), "--execute", "--poll-seconds", str(args.poll_seconds)]
        if profiles:
            command += ["--profiles", str(profiles)]
        print(shlex.join(command))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"[remote-run] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
