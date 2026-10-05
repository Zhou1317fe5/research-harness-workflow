#!/usr/bin/env python3
"""csv_state 派生产物的回归：commit 边界建议与 progress 视图。

覆盖 A1/A2 的核心断言：
- 纯状态叙述写回得到「不单独提交」的 advisory，到达逻辑边界时得到可提交；
- progress.md 只把真正产出结果的事件计为科学完成，绑定/重试/pre-review smoke 分开计数；
- progress.md 是派生视图：删除后下一次写回能重建，且不影响 CSv 写成功。
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents/skills/mission-csv-execute/scripts"))
sys.path.insert(0, str(ROOT / ".agents"))

from csv_state import apply_update  # noqa: E402
from mission_completion import EXPECTED_FIELDS  # noqa: E402

SCHEMA = "mission.csv-state-update.v1"


class ProgressAndCommitAdviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="csv-progress-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "tasks.csv"
        self._write([
            self._row("ISSUE-01", remote_state="", run_id="r0"),
            self._row("ISSUE-02", remote_state="", run_id="r0"),
        ])

    def _row(self, row_id, **overrides):
        row = dict.fromkeys(EXPECTED_FIELDS, "")
        row.update(
            id=row_id,
            phase="1",
            dev_state="未开始",
            review_initial_state="未开始",
            review_regression_state="未开始",
            git_state="未提交",
            remote_state="not_applicable",
        )
        row.update(overrides)
        return row

    def _write(self, rows):
        with self.path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _update(self, **request):
        payload = {
            "schema_version": SCHEMA,
            "row_id": request.pop("row_id", "ISSUE-01"),
            **request,
        }
        return apply_update(self.path, payload)

    def _progress_text(self):
        return self.path.with_suffix(".progress.md").read_text(encoding="utf-8")

    def test_plain_state_write_advises_against_separate_commit(self):
        result = self._update(set={"dev_state": "进行中"}, commit_boundary="none")
        self.assertTrue(result["ok"])
        self.assertFalse(result["git_commit_recommended"])
        self.assertIn("不单独提交", result["commit_advice"])

    def test_logical_boundary_allows_commit(self):
        result = self._update(set={"dev_state": "进行中"}, commit_boundary="implementation")
        self.assertTrue(result["ok"])
        self.assertTrue(result["git_commit_recommended"])
        self.assertIn("可以提交", result["commit_advice"])

    def test_progress_counts_only_real_completions_as_scientific(self):
        self._update(
            event={"kind": "retry_reconciliation", "prior_run_id": "r1", "new_run_id": "r2"},
            set={"remote_state": "running_remote", "run_id": "r2"},
            commit_boundary="none",
        )
        self._update(
            event={"kind": "pre_review_smoke_completed", "run_id": "s1"},
            commit_boundary="none",
        )
        text = self._progress_text()
        self.assertIn("scientific_completions: 0", text)
        self.assertIn("retry_events: 1", text)
        self.assertIn("restricted_run_events: 1", text)
        self.assertIn("last_scientific_completion: none", text)
        self.assertIn("last_retry_event: ISSUE-01 retry_reconciliation", text)

    def test_progress_tracks_terminal_event_as_heuristic(self):
        """事件名只作启发式线索，权威完成数来自 CSV 的 ingested 状态。"""
        self._update(
            row_id="ISSUE-02", set={"remote_state": "running_remote"}, commit_boundary="none"
        )
        self._update(
            row_id="ISSUE-02",
            event={"kind": "remote_completed", "run_id": "run-42"},
            set={"remote_state": "completed", "run_id": "run-42"},
            commit_boundary="terminal",
        )
        text = self._progress_text()
        self.assertIn("heuristic_terminal_events: 1", text)
        self.assertIn("last_terminal_event: ISSUE-02 remote_completed run-42", text)
        # completed 不等于 ingested：未 ingest 时权威计数仍为 0。
        self.assertIn("scientific_completions: 0", text)
        self.assertIn("open_remote_rows: ISSUE-02", text)

    def test_progress_authoritative_count_comes_from_ingested(self):
        """权威计数只看 CSV 的 remote_state=ingested（不必走完整 ingest 校验）。"""
        self._write([
            self._row("ISSUE-01", remote_state="", run_id="r0"),
            self._row("ISSUE-02", remote_state="ingested", run_id="run-42"),
        ])
        self._update(set={"dev_state": "进行中"}, commit_boundary="none")
        text = self._progress_text()
        self.assertIn("scientific_completions: 1（权威：remote_state=ingested）", text)
        self.assertIn("last_scientific_completion: ISSUE-02", text)

    def test_progress_says_unknown_when_no_remote_lifecycle(self):
        self._update(set={"dev_state": "进行中"}, commit_boundary="none")
        text = self._progress_text()
        self.assertIn("scientific_completions: unknown", text)
        self.assertIn("last_scientific_completion: unknown", text)

    def test_progress_is_derived_and_rebuildable(self):
        self._update(set={"dev_state": "进行中"}, commit_boundary="none")
        progress = self.path.with_suffix(".progress.md")
        self.assertTrue(progress.is_file())
        progress.unlink()
        result = self._update(set={"dev_state": "进行中"}, commit_boundary="none")
        self.assertTrue(result["ok"])
        self.assertTrue(progress.is_file(), "丢失的派生视图应能被下一次写回重建")
        self.assertEqual(result["progress"], str(progress))

    def test_progress_does_not_alter_state_authority(self):
        """progress.md 存在与否不得改变 CSV 写回结果。"""
        before = self._update(set={"dev_state": "进行中"}, commit_boundary="none")
        progress = self.path.with_suffix(".progress.md")
        progress.write_text("tampered\n", encoding="utf-8")
        after = self._update(set={"dev_state": "进行中"}, commit_boundary="none")
        self.assertTrue(after["ok"])
        self.assertEqual(after["csv_sha256"], before["csv_sha256"])


if __name__ == "__main__":
    unittest.main()
