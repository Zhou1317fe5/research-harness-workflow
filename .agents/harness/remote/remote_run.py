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
from pathlib import Path, PurePosixPath

from harness.common.paths import REPO_ROOT
from harness.remote.build_rrctl_runspec import RunSpecBuildError, canonical_run_spec, run_spec_digest


def validate_mission_launch(spec: dict, *, resume: bool = False, spec_path: Path | None = None) -> None:
    """核对任务绑定；新启动复用路由，恢复只允许观察已绑定的运行。"""
    metadata = spec.get("metadata", {})
    if not metadata.get("spec_id") or not metadata.get("exp_id"):
        raise ValueError("Mission RunSpec requires both spec_id and exp_id")
    repo = Path(spec["source"]["repo_root"]).resolve()
    value, row_id = metadata.get("mission_csv"), metadata.get("mission_row_id")
    legacy_resume = resume and value is None and row_id is None
    if legacy_resume:
        # 旧 RunSpec 保持原 digest，仅由其规范位置找同一 Mission 的 CSV。
        path = spec_path.resolve() if spec_path is not None else None
        if (path is None or not path.is_relative_to(repo) or path.name != "runspec.json"
                or path.parent.name != spec["run_id"] or path.parent.parent.name != "runs"):
            raise ValueError("resume mission binding missing: expected runs/<RunID>/runspec.json")
        mission_root = path.parent.parent.parent
        candidates = list(mission_root.glob("*.csv"))
        if len(candidates) != 1:
            raise ValueError("resume mission CSV missing or ambiguous")
        value = candidates[0].relative_to(repo).as_posix()
    elif not isinstance(row_id, str) or not row_id.strip():
        raise ValueError("official Mission RunSpec requires metadata.mission_csv and mission_row_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("official Mission RunSpec requires metadata.mission_csv and mission_row_id")
    csv_path = (repo / value).resolve()
    if not csv_path.is_relative_to(repo):
        raise ValueError("mission CSV is outside source repository")
    skills = Path(__file__).resolve().parents[3] / ".codex/skills"
    for directory in ("mission-csv-execute", "mission-spec", "pre-run-implementation-review"):
        scripts = str(skills / directory / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
    from mission_completion import parse_note_tags, read_mission_csv
    from git_isolation import verify_commit
    from harness.workflow.mission_state import assert_launchable, task_for_csv
    from validate_spec import validate_text
    from remote_route import decide_remote_route
    from harness.remote.build_rrctl_runspec import _gate_provenance

    if resume:
        task = task_for_csv(repo, csv_path)
        if task and task["status"] == "preparing":
            raise ValueError("resume requires an existing Mission run, not a preparing task")
    else:
        assert_launchable(repo, csv_path)
    _, rows, _ = read_mission_csv(csv_path)
    matches = [row for row in rows if row["run_id"] == spec["run_id"]] if legacy_resume else [row for row in rows if row["id"] == row_id]
    if len(matches) != 1:
        raise ValueError("mission run row missing or ambiguous")
    row = matches[0]
    row_id = row["id"]
    tags = parse_note_tags(row["notes"])
    separate_results = tags.get("git_repo") and (repo / tags["git_repo"]).resolve() != repo
    if separate_results and not resume:
        raise ValueError("mission run commit must belong to the source repository")
    if not resume and row["remote_state"] not in ("", "not_applicable", "failed"):
        raise ValueError("mission run already started; inspect/resume the existing RunID")
    if tags.get("command_owner", "rrctl") != "rrctl":
        raise ValueError("Mission rrctl entry cannot change a legacy run's control owner")
    commit = spec["source"]["commit"]
    for field, expected in (("spec_id", metadata["spec_id"]), ("exp_id", metadata["exp_id"])):
        if row[field] != expected:
            raise ValueError(f"mission RunSpec identity mismatch: {field}")
    row_commit = tags.get("pre_run_code_commit") if separate_results else row["commit_hash"]
    if row_commit != commit:
        raise ValueError("mission RunSpec identity mismatch: commit_hash")
    if row["run_id"] and row["run_id"] != spec["run_id"]:
        raise ValueError("mission RunSpec identity mismatch: run_id")
    if row["branch"] and row["branch"] != spec["source"]["branch"]:
        raise ValueError("mission RunSpec identity mismatch: branch")
    if resume:
        if row["run_id"] != spec["run_id"] or row["remote_state"] not in {
            "running_remote", "completed", "artifacts_pulled", "ingested", "failed",
        }:
            raise ValueError("resume requires the CSV row's existing RunID and remote state")
        # paused/cancelled 等状态仅可观察；不改生命周期、不重新套用新启动 gate。
        # execute 随后以远端 binding digest 核实身份，且只执行 inspect/wait/pull。
        return
    sources = {parse_note_tags(item["notes"]).get("source_doc") for item in rows} - {None, ""}
    if len(sources) != 1:
        raise ValueError("mission source_doc missing or ambiguous")
    source_doc = next(iter(sources))
    source = (repo / source_doc).resolve()
    if not source.is_relative_to(repo):
        raise ValueError("mission source_doc outside repository")
    verify_commit(repo, commit, [source])
    frozen = subprocess.run(["git", "show", f"{commit}:{source.relative_to(repo).as_posix()}"], cwd=repo, capture_output=True, check=True).stdout
    approval, errors = validate_text(frozen.decode("utf-8"))
    if errors or approval.get("status") != "approved" or frozen != source.read_bytes():
        raise ValueError("mission source_doc must match the approved version in the source commit")

    purpose = metadata.get("execution_purpose", "official")
    restricted = purpose in {"pre_review_smoke", "preregistered_read_only_probe"}
    if spec["session"]["backend"] != "process":
        raise ValueError("new Mission runs require the rrctl process backend")
    pull_root = Path(spec["local_pull_root"]).resolve()
    if not pull_root.is_relative_to(repo):
        raise ValueError("Mission local_pull_root is outside source repository")
    if restricted:
        if (not pull_root.is_relative_to(csv_path.parent) or
                spec["run_id"] not in PurePosixPath(spec["remote"]["output_root"]).parts):
            raise ValueError("restricted run requires isolated Mission output roots bound to RunID")
        boundary = metadata.get(purpose)
        if not isinstance(boundary, dict) or boundary.get("candidate_commit") != commit:
            raise ValueError("restricted run boundary must bind the candidate commit")
        if purpose == "pre_review_smoke":
            resources = spec.get("resources", {})
            if resources.get("device") != "gpu" or (resources.get("gpu_ids") and
                    len(resources["gpu_ids"]) != boundary.get("gpu_count")):
                raise ValueError("smoke GPU resources disagree with the declared boundary")
            if spec.get("output_cleanup", {}).get("mode") != "pre_review_smoke":
                raise ValueError("smoke checkpoint cleanup is required")

    change = metadata.get("change_manifest")
    gate = metadata.get("gate_provenance")
    if gate is not None:
        gate = _gate_provenance(gate, source_commit=commit)
    request = {
        "schema_version": "mission.remote-route.v1", "execution_kind": "remote",
        "lifecycle": "failed_retry" if row["remote_state"] == "failed" else "not_started",
        "has_running_evidence": False, "command_owner": tags.get("command_owner", "rrctl"),
        # 本地准入先核路由；execute 在任何 rrctl 操作前另核实际安装和能力。
        "rrctl": {"available": True, "readiness": "not_checked", "launch": "not_checked"},
        "code_changed": change is not None, "change_manifest": change,
        "execution_purpose": "official" if purpose == "pilot" else purpose,
        "custom_control_scripts": metadata.get("custom_control_scripts", []),
    }
    if restricted:
        request[purpose] = metadata[purpose]
    if gate is not None:
        request["formal_review"] = {
            "passed": True, "candidate_commit": gate["pre_run_code_commit"],
            "review_mode": gate["review_mode"], "review_result": gate["review_result"],
            "reviewer_id": gate["reviewer_id"], "closure_evidence_paths": gate["blocker_closure_evidence"],
        }
    decision = decide_remote_route(request)
    if decision["decision"] != "proceed" or decision["route"] != "rrctl" or decision["fallback_allowed"]:
        raise ValueError("mission remote route blocked: " + "; ".join(decision["errors"] or decision["reason_codes"]))
    route = decision["change_route"]
    if route is not None:
        if not route["valid"] or route["candidate_commit"] != commit:
            raise ValueError("mission change route invalid or source commit mismatched")
        base = verify_commit(repo, route["reviewed_commit"])
        actual = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", base, commit], cwd=repo, capture_output=True, check=True).stdout
        if {x.decode() for x in actual.split(b"\0") if x} != {item["path"] for item in change["changes"]}:
            raise ValueError("mission change manifest does not cover the actual Git diff")
    needs_review = not restricted and (route is None or route["requires_prerun"])
    if needs_review or (gate is not None and not restricted):
        try:
            gate = _gate_provenance(gate, source_commit=commit)
        except RunSpecBuildError as exc:
            raise ValueError(str(exc)) from exc
        reviews = [parse_note_tags(item["notes"]) for item in rows
                   if item["id"].startswith("PRERUN-REVIEW-") and parse_note_tags(item["notes"]).get("gated_run") == row_id]
        if len(reviews) != 1 or any(reviews[0].get(key) != expected for key, expected in (
            ("pre_run_code_commit", commit), ("pre_run_result", "pass"),
            ("review_mode", gate["review_mode"]), ("review_result", gate["review_result"]),
        )):
            raise ValueError("mission scientific gate does not match the RunSpec")
        from validate_claim_ledger import reference_file
        for value in gate["blocker_closure_evidence"]:
            if reference_file(value, csv_path.parent, repo) is None:
                raise ValueError("scientific blocker closure requires a local evidence artifact")


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
    if not isinstance(report, dict):
        report = {}
    backends, capabilities = report.get("backends"), report.get("capabilities")
    required = {"observer-deadline", "worker-monitoring", "unknown-operation-outcome"}
    available = {item for item in capabilities if isinstance(item, str)} if isinstance(capabilities, list) else set()
    missing = sorted(required - available)
    if not isinstance(backends, list) or "process" not in backends:
        missing.insert(0, "process backend")
    if capability.returncode or missing:
        raise ValueError(
            f"rrctl at {executable} failed doctor or lacks required capabilities: {', '.join(missing)}; "
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
    spec = canonical_run_spec(spec)
    validate_mission_launch(spec, resume=resume, spec_path=spec_path)
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
    except (OSError, ValueError, KeyError, RuntimeError, RunSpecBuildError, subprocess.CalledProcessError) as exc:
        print(f"[remote-run] {exc}", file=sys.stderr)
        return 2
