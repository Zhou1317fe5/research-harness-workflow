#!/usr/bin/env python3
"""检查已有输出是否满足 RunSpec 的 adapter 契约。"""

from __future__ import annotations

# 直接运行脚本和通过 Python 包导入时使用同一实现。
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.remote.validate_adapter import main
    raise SystemExit(main())

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


PHASES = ("first_step", "periodic", "completion")


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{field} must be a non-empty string array")
    return value


def validate_outputs(
    runspec: dict[str, Any], output_root: Path, phases: list[str]
) -> dict[str, Any]:
    source = _object(runspec.get("source"), "source")
    health = _object(runspec.get("health"), "health")
    metadata = _object(runspec.get("metadata"), "metadata")
    repo_root = Path(str(source.get("repo_root", ""))).expanduser().resolve()
    workload = _object(runspec.get("workload"), "workload")
    cwd = (repo_root / str(workload.get("cwd", "."))).resolve()
    output_root = output_root.expanduser().resolve()
    if not output_root.is_dir():
        raise ValueError(f"output root does not exist: {output_root}")
    if not cwd.is_dir():
        raise ValueError(f"adapter cwd does not exist: {cwd}")

    checked: list[dict[str, Any]] = []
    for phase in phases:
        phase_spec = _object(health.get(phase), f"health.{phase}")
        argv = _string_list(phase_spec.get("adapter_argv"), f"health.{phase}.adapter_argv")
        context = {
            "protocol": "rrctl.adapter.context.v1",
            "phase": phase,
            "run_id": runspec.get("run_id"),
            "project": runspec.get("project"),
            "repo_root": str(repo_root),
            "output_root": str(output_root),
            "control_root": str(output_root / ".rrctl-control"),
            "status": {
                "state": "workload_complete" if phase == "completion" else "running"
            },
            "binding": {},
            "metadata": metadata,
        }
        completed = subprocess.run(
            argv,
            input=json.dumps(context, ensure_ascii=False, separators=(",", ":")),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=cwd,
            env=os.environ.copy(),
            check=False,
            timeout=int(phase_spec.get("adapter_timeout_seconds", 30)),
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-2000:]
            raise ValueError(f"adapter output failed for {phase}: {detail}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"adapter output emitted invalid JSON for {phase}: {exc}") from exc
        result = _object(result, f"adapter result {phase}")
        if result.get("protocol") != "rrctl.adapter.v1":
            raise ValueError(f"adapter output protocol invalid for {phase}")
        if result.get("healthy") is not True:
            raise ValueError(f"adapter output is not healthy for {phase}")
        if phase == "completion" and result.get("complete") is not True:
            raise ValueError("adapter output completion did not report complete=true")
        checked.append(
            {
                "phase": phase,
                "healthy": result.get("healthy"),
                "complete": result.get("complete"),
            }
        )
    return {
        "ok": True,
        "run_id": runspec.get("run_id"),
        "output_root": str(output_root),
        "checked": checked,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runspec", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--phase", action="append", choices=PHASES, dest="phases")
    args = parser.parse_args(argv)
    try:
        runspec = _object(
            json.loads(args.runspec.read_text(encoding="utf-8")), "runspec"
        )
        result = validate_outputs(runspec, args.output_root, args.phases or list(PHASES))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
