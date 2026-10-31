"""closing_audit：一次性聚合收尾门禁错误。"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))

from closing_audit import audit  # noqa: E402
from mission_completion import EXPECTED_FIELDS  # noqa: E402


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=EXPECTED_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def bad_row(**overrides) -> dict:
    row = dict.fromkeys(EXPECTED_FIELDS, "")
    row.update(
        id="EVAL-01", phase="04", area="evaluation", dev_state="未完成",
        review_initial_state="未完成", review_regression_state="未完成",
        git_state="未提交", remote_state="running",
    )
    row.update(overrides)
    return row


class ClosingAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="closing-audit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv = self.root / "tasks.csv"

    def test_unfinished_row_reports_evidence_errors(self):
        write_csv(self.csv, [bad_row()])
        groups = audit(self.csv, workdir=self.root)
        self.assertIn("evidence", groups)
        self.assertTrue(any("row_not_closed" in e for e in groups["evidence"]))

    def test_missing_csv_is_reported_not_raised(self):
        groups = audit(self.root / "nope.csv", workdir=self.root)
        self.assertIn("csv", groups)

    def test_missing_handoff_is_reported(self):
        write_csv(self.csv, [bad_row()])
        groups = audit(self.csv, workdir=self.root, handoff_path=self.root / "tasks.handoff.md")
        self.assertIn("handoff", groups)


if __name__ == "__main__":
    unittest.main()
