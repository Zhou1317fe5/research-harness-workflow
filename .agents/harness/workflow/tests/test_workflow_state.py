"""生命周期恢复与 CSV 并发回归，不调用远端或模型。"""
import csv
import hashlib
import importlib.util
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))
from harness.workflow.mission_state import assert_launchable, load_registry, update
from csv_state import SCHEMA, StateUpdateError, apply_update
from ensure_review_row import ensure_review_row
from mission_completion import EXPECTED_FIELDS, read_mission_csv


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECOVERY = load(".codex/skills/mission-recovery/scripts/scan_recovery.py", "test_recovery")
SPEC = load(".codex/skills/mission-spec/scripts/validate_spec.py", "test_spec")


def concurrent_update(path, row_id, barrier, queue, review=False):
    try:
        barrier.wait(timeout=10)
        result = ensure_review_row(Path(path)) if review else apply_update(Path(path), {
            "schema_version": SCHEMA, "row_id": row_id, "set": {"dev_state": "进行中"},
            "event": {"kind": "fixture", "row": row_id},
        })
        queue.put({"ok": True, "result": result})
    except Exception as error:
        queue.put({"ok": False, "error": str(error)})


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workflow-state-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def csv(self, name="task"):
        path = self.root / f"issues/{name}/{name}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for row_id in ("ISSUE-01", "ISSUE-02"):
            row = dict.fromkeys(EXPECTED_FIELDS, "")
            row.update(id=row_id, dev_state="未开始", review_initial_state="未开始",
                       review_regression_state="未开始", git_state="未提交", remote_state="not_applicable")
            rows.append(row)
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def register(self, name, path):
        return update(self.root, name, action="register", source_ref="session:user#request",
                      csv=path.relative_to(self.root).as_posix())

    def test_concurrent_updates_preserve_both_rows_and_events(self):
        path = self.csv()
        context = multiprocessing.get_context("fork")
        barrier, queue = context.Barrier(2), context.Queue()
        workers = [context.Process(target=concurrent_update, args=(str(path), row, barrier, queue))
                   for row in ("ISSUE-01", "ISSUE-02")]
        for worker in workers:
            worker.start()
        replies = [queue.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(timeout=15)
            self.assertEqual(worker.exitcode, 0)
        self.assertTrue(all(reply["ok"] for reply in replies), replies)
        self.assertTrue(all(row["dev_state"] == "进行中" for row in read_mission_csv(path)[1]))
        self.assertEqual(len(json.loads(path.with_suffix(".events.json").read_text())), 2)

    def test_review_append_and_update_share_lock(self):
        path = self.csv()
        context = multiprocessing.get_context("fork")
        barrier, queue = context.Barrier(2), context.Queue()
        workers = [context.Process(target=concurrent_update, args=(str(path), "ISSUE-01", barrier, queue, review))
                   for review in (False, True)]
        for worker in workers:
            worker.start()
        replies = [queue.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(timeout=15)
        self.assertTrue(all(reply["ok"] for reply in replies), replies)
        rows = {row["id"]: row for row in read_mission_csv(path)[1]}
        self.assertEqual(rows["ISSUE-01"]["dev_state"], "进行中")
        self.assertIn("REVIEW-01", rows)

    def test_stale_version_is_rejected_without_overwrite(self):
        path = self.csv()
        old_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        request = {"schema_version": SCHEMA, "row_id": "ISSUE-01", "set": {"dev_state": "进行中"}}
        result = apply_update(path, request)
        after = path.read_bytes()
        self.assertEqual(result["csv_sha256"], hashlib.sha256(after).hexdigest())
        with self.assertRaisesRegex(StateUpdateError, "csv_version_conflict"):
            apply_update(path, {**request, "expected_sha256": old_hash, "set": {"dev_state": "未开始"}})
        self.assertEqual(path.read_bytes(), after)

    def test_cancelled_task_is_excluded_and_cannot_be_reactivated_by_register(self):
        path = self.csv()
        self.register("old", path)
        update(self.root, "old", action="transition", status="cancelled", source_ref="session:user#cancel", reason="用户取消")
        self.register("old", path)
        result = RECOVERY.scan(self.root)
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["inactive"][0]["status"], "cancelled")
        with self.assertRaises(ValueError):
            assert_launchable(self.root, path)

    def test_current_task_has_priority_over_newer_file_mtime(self):
        old = self.csv("chosen")
        other = self.csv("other")
        self.register("chosen", old)
        result = RECOVERY.scan(self.root)
        self.assertEqual(result["candidates"][0]["path"], old.relative_to(self.root).as_posix())
        self.assertEqual(result["resume_target"]["task_id"], "chosen")

    def test_switch_to_spec_retires_old_task_atomically(self):
        path = self.csv("old")
        self.register("old", path)
        spec = self.root / "docs/specs/new.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# Draft fixture")
        update(self.root, "new", action="register", spec="docs/specs/new.md", replaces="old",
               reason="用户切换任务", source_ref="session:user#new")
        registry = load_registry(self.root)
        self.assertEqual(registry["tasks"]["old"]["status"], "superseded")
        self.assertEqual(registry["current_task"], "new")
        result = RECOVERY.scan(self.root)
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["resume_target"]["kind"], "spec")

    def test_paused_preparing_task_can_resume_without_csv(self):
        spec = self.root / "docs/specs/new.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# Draft fixture")
        update(self.root, "new", action="register", spec="docs/specs/new.md", source_ref="session:user#new")
        update(self.root, "new", action="transition", status="paused", reason="用户暂停", source_ref="session:user#pause")
        self.assertEqual(RECOVERY.scan(self.root)["resume_target"]["kind"], "paused")
        update(self.root, "new", action="transition", status="preparing", source_ref="session:user#resume")
        self.assertEqual(RECOVERY.scan(self.root)["resume_target"]["kind"], "spec")

    def test_missing_current_csv_does_not_fall_back_to_other_task(self):
        current = self.csv("current")
        self.csv("other")
        self.register("current", current)
        current.unlink()
        with self.assertRaisesRegex(ValueError, "不存在的 CSV"):
            RECOVERY.scan(self.root)

    def test_delegated_approval_requires_source_and_legacy_stays_valid(self):
        body = "\n## Goal\n目标\n## Scope\n范围\n## Design\n设计\n## Acceptance Criteria\n验收\n"
        header = "---\nmission: spec\nstatus: approved\ncreated: 2026-09-10\napproved_at: 2026-09-10T00:00:00Z\n"
        self.assertEqual(SPEC.validate_text(header + "---\n" + body)[1], [])
        self.assertTrue(SPEC.validate_text(header + "approval_mode: delegated\n---\n" + body)[1])
        delegated = header + "approval_mode: delegated\napproval_source: session:user#grant\n---\n" + body
        self.assertEqual(SPEC.validate_text(delegated)[1], [])

    def test_lifecycle_cannot_hide_unfinished_csv_as_completed(self):
        path = self.csv()
        self.register("current", path)
        with self.assertRaisesRegex(ValueError, "尚未闭环"):
            update(self.root, "current", action="transition", status="completed",
                   reason="检查完成", source_ref="fixture:completion")
        self.assertEqual(load_registry(self.root)["tasks"]["current"]["status"], "active")


if __name__ == "__main__":
    unittest.main()
