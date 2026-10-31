"""smoke/probe 行与交付 Run 的解耦：终态判定与 record 闭包。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))

from mission_completion import (  # noqa: E402
    _is_prereview_smoke,
    ingest_completion_errors,
    row_terminal_errors,
)


def row(**overrides) -> dict[str, str]:
    base = {
        "id": "SMOKE-01",
        "phase": "02",
        "area": "validation",
        "dev_state": "已完成",
        "review_initial_state": "已完成",
        "review_regression_state": "已完成",
        "git_state": "已提交",
        "remote_state": "completed",
        "run_id": "RUN-20261031-demo-smoke1",
        "artifact_path": "eve:/srv/runs/RUN-20261031-demo-smoke1/output",
        "notes": "source_doc:docs/spec.md",
    }
    base.update(overrides)
    return base


class PrereviewSmokeDetectionTests(unittest.TestCase):
    def test_smoke_row_id_is_recognised(self):
        self.assertTrue(_is_prereview_smoke(row()))

    def test_smoke_run_id_suffix_is_recognised(self):
        self.assertTrue(_is_prereview_smoke(row(id="RUN-01", run_id="RUN-x-smoke1")))

    def test_execution_purpose_tag_is_recognised(self):
        self.assertTrue(_is_prereview_smoke(
            row(id="X-01", run_id="RUN-x", notes="execution_purpose:pre_review_smoke")
        ))

    def test_regular_run_is_not_smoke(self):
        self.assertFalse(_is_prereview_smoke(
            row(id="EVAL-01", run_id="RUN-20261031-demo-full600", area="evaluation")
        ))


class SmokeTerminalTests(unittest.TestCase):
    def test_smoke_completed_is_terminal_without_ingest(self):
        self.assertEqual(row_terminal_errors(row()), [])

    def test_smoke_failed_is_still_flagged(self):
        errors = row_terminal_errors(row(remote_state="running"))
        self.assertTrue(any("remote_state_not_terminal" in e for e in errors), errors)

    def test_smoke_never_enters_ingest_closure(self):
        # 即使被误标 ingested，也不要求实验 record/证据文件。
        errors = ingest_completion_errors(
            Path("/nonexistent/tasks.csv"),
            [row(remote_state="ingested")],
            workdir=Path("/nonexistent"),
        )
        self.assertEqual(errors, [])

    def test_formal_run_still_requires_record(self):
        errors = ingest_completion_errors(
            Path("/nonexistent/tasks.csv"),
            [row(id="EVAL-01", run_id="RUN-demo-full600", remote_state="ingested",
                 artifact_path="")],
            workdir=Path("/nonexistent"),
        )
        self.assertTrue(any("ingest_evidence_missing" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
