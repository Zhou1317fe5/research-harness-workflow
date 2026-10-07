#!/usr/bin/env python3
"""Atomically bind a new candidate RunID after a terminal smoke RunID.

This is a ledger operation, not a retry shortcut: the old terminal RunID and
artifact path are preserved in the event sidecar, while the row is reset to an
empty launchable state for the new candidate commit.
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

from csv_state import apply_update  # noqa: E402
from mission_completion import read_mission_csv  # noqa: E402


def _commit(repo: Path) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("row_id")
    parser.add_argument("new_run_id")
    parser.add_argument("new_commit")
    parser.add_argument("--prior-artifact-path")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--next-action", required=True)
    args = parser.parse_args()

    csv_path = args.csv_path.resolve()
    repo = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=csv_path.parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    actual_commit = _commit(repo)
    if args.new_commit != actual_commit:
        raise SystemExit(
            f"new_commit_mismatch: supplied {args.new_commit}, current {actual_commit}"
        )
    _, rows, _ = read_mission_csv(csv_path, allow_compat=True, validate_notes=False)
    matches = [row for row in rows if row["id"] == args.row_id]
    if len(matches) != 1:
        raise SystemExit(f"row_lookup_invalid: {args.row_id} matched {len(matches)} rows")
    row = matches[0]
    prior_run_id = row.get("run_id", "")
    prior_commit = row.get("commit_hash", "")
    prior_remote_state = row.get("remote_state", "")
    prior_artifact = args.prior_artifact_path or row.get("artifact_path", "")
    if not prior_run_id or not prior_commit or not prior_artifact:
        raise SystemExit("supersession requires an existing RunID, commit, and artifact_path")
    if prior_remote_state not in {"completed", "artifacts_pulled", "ingested", "failed", "not_applicable"}:
        raise SystemExit(f"supersession requires terminal remote_state, got {prior_remote_state!r}")
    if prior_remote_state == "not_applicable" and not args.prior_artifact_path:
        raise SystemExit("not_applicable supersession requires --prior-artifact-path from launch diagnostics")
    if any(item.get("run_id") == args.new_run_id for item in rows):
        raise SystemExit(f"new RunID is already bound: {args.new_run_id}")

    event = {
        "kind": "candidate_supersession",
        "prior_run_id": prior_run_id,
        "new_run_id": args.new_run_id,
        "prior_commit": prior_commit,
        "new_commit": args.new_commit,
        "prior_remote_state": prior_remote_state,
        "prior_artifact_path": prior_artifact,
        "reason": args.reason,
        "prior_launch_failed": prior_remote_state == "not_applicable",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    request = {
        "schema_version": "mission.csv-state-update.v1",
        "row_id": args.row_id,
        "set": {
            "run_id": args.new_run_id,
            "commit_hash": args.new_commit,
            "remote_state": "not_applicable",
            "artifact_path": "",
            "next_action": args.next_action,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        "append_notes": [f"supersedes_run:{prior_run_id}", f"supersedes_commit:{prior_commit}"],
        "set_note_tags": {
            "commit_hash": args.new_commit,
            "pre_run_code_commit": args.new_commit,
        },
        "event": event,
        "supersession_binding": {
            "prior_run_id": prior_run_id,
            "new_run_id": args.new_run_id,
            "prior_commit": prior_commit,
            "new_commit": args.new_commit,
            "prior_remote_state": prior_remote_state,
            "prior_artifact_path": prior_artifact,
            "reason": args.reason,
        },
        "commit_boundary": "launch",
    }
    result = apply_update(csv_path, request)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(2)
