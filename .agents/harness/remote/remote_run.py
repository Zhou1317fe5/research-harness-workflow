#!/usr/bin/env python3
"""生成或执行 ready → launch → wait → pull；所有远程操作均交给 rrctl。"""
from __future__ import annotations

# 直接运行脚本和通过 Python 包导入时使用同一实现。
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.remote.remote_run import main
    raise SystemExit(main())

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from harness.common.paths import REPO_ROOT
from harness.remote.build_rrctl_runspec import run_spec_digest


def rrctl_call(argv: list[str], repo_root: Path) -> subprocess.CompletedProcess:
    env_file = repo_root / ".agents/harness/config/.env"
    if env_file.is_file():
        # 只传文件路径和参数，凭据由 shell 在进程内读取。
        argv = ["bash", "-c", 'set -e; set -a; source "$1"; set +a; shift; exec "$@"', "harness", str(env_file), *argv]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def resolve_rrctl() -> str:
    executable = shutil.which("rrctl")
    install_hint = "python -m pip install -e .agents/harness/remote/rrctl"
    if executable is None:
        raise ValueError(f"rrctl is unavailable; install with {install_hint}")
    try:
        capability = subprocess.run(
            [executable, "--json", "doctor"], capture_output=True, text=True,
            check=False, timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"rrctl capability check timed out: {executable}") from exc
    try:
        report = json.loads(capability.stdout)
    except ValueError:
        report = {}
    if capability.returncode or "process" not in report.get("backends", []) or "observer-deadline" not in report.get("capabilities", []):
        raise ValueError(
            f"rrctl at {executable} lacks the process backend/observer deadline; "
            f"install this repository's control package with {install_hint} "
            "and place that environment first in PATH"
        )
    return executable


def _emit_stage(stage: str, value: dict, run_id: str, returncode: int, *, full: bool) -> None:
    directory = Path.home() / ".local/state/rrctl/client-results" / run_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / f"{stage}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".result-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if full:
        print(json.dumps({"stage": stage, "response": value}, ensure_ascii=False), flush=True)
        return
    result = value.get("result", value)
    summary = {"stage": stage, "run_id": run_id, "exit_code": returncode,
               "ok": value.get("ok", value.get("ready", True)), "details_path": str(destination)}
    for key in ("status", "destination", "observation", "remote_workload_preserved", "reused", "layout", "resume_argv"):
        if key in result:
            summary[key] = result[key]
    if "error" in value:
        summary["error"] = {key: value["error"].get(key) for key in ("code", "message", "phase")}
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def execute(
    spec_path: Path, *, profiles: Path | None, poll_seconds: float,
    max_wait_seconds: float = 900, resume: bool = False, full_output: bool = False,
) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise ValueError("RunSpec must be a JSON object")
    run_id = spec.get("run_id")
    if spec.get("schema_version") != "rrctl.run.v1" or not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("expected a valid rrctl.run.v1 RunSpec")
    prefix = [resolve_rrctl(), "--json"]
    if profiles:
        prefix += ["--profiles", str(profiles.resolve())]
    stages = ([("inspect", [run_id])] if resume else [("ready", [str(spec_path)]), ("launch", [str(spec_path), "--max-wait-seconds", str(max_wait_seconds)])]) + [
        ("wait", [run_id, "--poll-seconds", str(poll_seconds), "--max-wait-seconds", str(max_wait_seconds)]),
        ("pull", [run_id]),
    ]
    for stage, arguments in stages:
        result = rrctl_call([*prefix, stage, *arguments], REPO_ROOT)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"rrctl {stage} did not return JSON (exit={result.returncode})") from exc
        if not isinstance(value, dict):
            raise ValueError(f"rrctl {stage} must return a JSON object")
        if stage == "inspect" and value.get("ok") is True:
            binding = value.get("result", {}).get("binding", {})
            if binding.get("run_spec_sha256") != run_spec_digest(spec):
                raise ValueError("resume RunSpec differs from the bound remote run")
        _emit_stage(stage, value, run_id, result.returncode, full=full_output)
        preserved = value.get("error", {}).get("details", {}).get("remote_workload_preserved")
        if result.returncode == 124 or preserved:
            command = [sys.executable, str(Path(__file__).resolve()), str(spec_path), "--execute", "--resume",
                       "--poll-seconds", str(poll_seconds), "--max-wait-seconds", str(max_wait_seconds)]
            if profiles:
                command += ["--profiles", str(profiles.resolve())]
            print(json.dumps({"run_id": run_id, "remote_workload_preserved": True, "resume_argv": command}, ensure_ascii=False), flush=True)
            return result.returncode or 2
        failed_state = value.get("result", {}).get("status", {}).get("state") in {"failed", "aborted"}
        if result.returncode or value.get("ok") is False or failed_state:
            if stage in {"launch", "wait"}:
                diagnostic = rrctl_call([*prefix, "pull", run_id, "--diagnostic"], REPO_ROOT)
                # 诊断拉取失败不能覆盖原始运行错误。
                if diagnostic.stdout.strip():
                    try:
                        _emit_stage("diagnostic", json.loads(diagnostic.stdout), run_id, diagnostic.returncode, full=full_output)
                    except ValueError:
                        pass
            return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runspec", type=Path)
    parser.add_argument("--profiles", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=600)
    parser.add_argument("--max-wait-seconds", type=float, default=900)
    parser.add_argument("--resume", action="store_true", help="inspect the bound run and continue wait/pull without launching again")
    parser.add_argument("--full-output", action="store_true", help="print full responses instead of summaries and local detail paths")
    parser.add_argument("--request", type=Path, help="prepare the RunSpec from a request before executing")
    parser.add_argument("--project-config", type=Path)
    parser.add_argument("--pipeline", help="choose a named combination when preparing; verify it when resuming")
    parser.add_argument("--execute", action="store_true", help="execute the generated rrctl sequence")
    args = parser.parse_args()
    try:
        if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0 or not math.isfinite(args.max_wait_seconds) or args.max_wait_seconds < 0:
            raise ValueError("poll must be positive and max-wait nonnegative finite seconds")
        spec = args.runspec.resolve()
        if args.request:
            if args.resume:
                raise ValueError("resume uses the existing RunSpec; omit --request")
            from harness.remote.build_rrctl_runspec import build_runspec, write_exclusive
            from harness.common.project_config import apply_project_config
            request = json.loads(sys.stdin.read() if str(args.request) == "-" else args.request.read_text())
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            project_config = args.project_config or REPO_ROOT / ".agents/harness/config/project.toml"
            if project_config.is_file():
                request = apply_project_config(request, project_config, pipeline=args.pipeline)
            elif args.pipeline is not None:
                raise ValueError("--pipeline requires an existing project config")
            write_exclusive(spec, build_runspec(request))
        if not spec.is_file():
            raise ValueError(f"RunSpec not found: {spec}")
        if args.pipeline is not None and not args.request:
            frozen = json.loads(spec.read_text())
            if not isinstance(frozen, dict) or frozen.get("metadata", {}).get("pipeline_name") != args.pipeline:
                raise ValueError("--pipeline differs from the frozen RunSpec; prepare a new run")
        profiles = args.profiles
        default_profiles = REPO_ROOT / ".agents/harness/config/profiles.json"
        if profiles is None and default_profiles.is_file():
            profiles = default_profiles
        if args.execute:
            return execute(spec, profiles=profiles, poll_seconds=args.poll_seconds, max_wait_seconds=args.max_wait_seconds,
                           resume=args.resume, full_output=args.full_output)
        command = [sys.executable, ".agents/harness/remote/remote_run.py", str(spec), "--execute", "--poll-seconds", str(args.poll_seconds),
                   "--max-wait-seconds", str(args.max_wait_seconds)]
        if args.resume:
            command.append("--resume")
        if args.full_output:
            command.append("--full-output")
        if profiles:
            command += ["--profiles", str(profiles)]
        print(shlex.join(command))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"[remote-run] {exc}", file=sys.stderr)
        return 2
