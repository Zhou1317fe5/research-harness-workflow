#!/usr/bin/env python3
"""把已存在的 rrctl 运行认领进 Mission 行，不启动任何东西。

问题
----
rrctl 已经启动了运行，但记账没落盘时（额度中断、崩溃、上下文丢失、GPU 被占），行会停在
`not_applicable`，而远端确实有活着的 RunID。此时两难：

- 直接启动 → 远端被占，覆盖或撞车；
- 直接改 CSV 状态 → 凭什么证明这个远端运行就是本行要的那个。

本工具用 rrctl 的 **真实 inspect 证据**回答后者，全部核对通过才认领：

- inspect 自身 `status == succeeded`，且运行状态可认领；
- binding 的 `run_id` / `commit` / `run_spec_sha256` 与 RunSpec 完全一致；
- `backend == process`（本工作流只允许 process 后端）；
- `launched` / `running` / `first_step_passed` 时，进程观测
  `executor_alive` / `process_alive` / `workload_alive` 必须全为 true；
- 证据文件必须位于仓库内、存在且可解析；CSV 行的 run_id/commit 也必须自身一致。

任何一项不过就拒绝——认领是"核对已有运行"，不是"绕过启动前置条件"。

用法
----
    python reconcile_remote_binding.py <csv> <row_id> <runspec.json> <inspect.json> \
        --next-action "<下一步>"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parents[3] / ".agents" / "harness" / "remote"))

from build_rrctl_runspec import run_spec_digest  # noqa: E402
from csv_state import apply_update  # noqa: E402
from mission_completion import read_mission_csv  # noqa: E402

RECONCILABLE_STATES = {"launched", "running", "first_step_passed", "failed", "completed"}
LIVE_STATES = {"launched", "running", "first_step_passed"}


def _repo_root(path: Path) -> Path:
    return Path(subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("row_id")
    parser.add_argument("runspec", type=Path)
    parser.add_argument("inspect_evidence", type=Path)
    parser.add_argument("--next-action", required=True)
    args = parser.parse_args()

    csv_path = args.csv_path.resolve()
    runspec_path = args.runspec.resolve()
    inspect_path = args.inspect_evidence.resolve()
    repo = _repo_root(csv_path)
    if not runspec_path.is_file() or not inspect_path.is_file():
        raise SystemExit("runspec_or_inspect_missing")
    spec = json.loads(runspec_path.read_text(encoding="utf-8"))
    inspect = json.loads(inspect_path.read_text(encoding="utf-8"))
    result = inspect.get("result", {})
    binding = result.get("binding", {})
    state = result.get("status", {}).get("state")
    if inspect.get("status") != "succeeded" or state not in RECONCILABLE_STATES:
        raise SystemExit(f"inspect_state_not_reconcilable: {inspect.get('status')}:{state}")
    if binding.get("run_id") != spec.get("run_id"):
        raise SystemExit("binding_run_id_mismatch")
    if binding.get("commit") != spec.get("source", {}).get("commit"):
        raise SystemExit("binding_commit_mismatch")
    digest = run_spec_digest(spec)
    if binding.get("run_spec_sha256") != digest:
        raise SystemExit("binding_runspec_digest_mismatch")
    monitor = result.get("monitor")
    observations = monitor.get("observations") if isinstance(monitor, dict) else None
    if not isinstance(observations, dict):
        raise SystemExit("process_observations_missing")
    if state in LIVE_STATES and any(
        observations.get(key) is not True
        for key in ("executor_alive", "process_alive", "workload_alive")
    ):
        raise SystemExit("live_process_observations_missing_or_false")
    if binding.get("backend") != "process":
        raise SystemExit("binding_backend_not_process")
    _, rows, _ = read_mission_csv(csv_path, allow_compat=True, validate_notes=False)
    matches = [row for row in rows if row["id"] == args.row_id]
    if len(matches) != 1:
        raise SystemExit(f"row_lookup_invalid: {args.row_id} matched {len(matches)} rows")
    row = matches[0]
    if row.get("run_id") != spec.get("run_id") or row.get("commit_hash") != spec.get("source", {}).get("commit"):
        raise SystemExit("csv_identity_mismatch")
    evidence = inspect_path.relative_to(repo).as_posix()
    event = {
        "kind": "remote_binding_reconciliation",
        "run_id": spec["run_id"],
        "commit": spec["source"]["commit"],
        "run_spec_sha256": digest,
        "remote_state": state,
        "evidence_path": evidence,
        "reason": "verified existing rrctl binding after bookkeeping interruption",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    request = {
        "schema_version": "mission.csv-state-update.v1",
        "row_id": args.row_id,
        "set": {
            "remote_state": "running_remote" if state in LIVE_STATES else state,
            "next_action": args.next_action,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        "append_notes": ["state_reconciliation:verified_same_rrctl_binding"],
        "event": event,
        "remote_binding_reconciliation": {
            "run_id": spec["run_id"],
            "commit": spec["source"]["commit"],
            "run_spec_sha256": digest,
            "remote_state": state,
            "evidence_path": evidence,
            "reason": event["reason"],
        },
        "commit_boundary": "launch",
    }
    print(json.dumps(apply_update(csv_path, request), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(2)
