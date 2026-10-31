"""交付类行的 Git 绑定判定：数字 phase 也应放行。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))

from git_isolation import _is_git_bound_row  # noqa: E402


def row(**overrides) -> dict[str, str]:
    base = {"id": "RESULT-ANALYSIS-01", "phase": "06", "area": "analysis"}
    base.update(overrides)
    return base


class GitBoundRowTests(unittest.TestCase):
    def test_numeric_phase_with_analysis_area_passes(self):
        self.assertTrue(_is_git_bound_row(row()))

    def test_numeric_phase_with_analysis_row_id_passes(self):
        self.assertTrue(_is_git_bound_row(row(id="RESULT-ANALYSIS-01", area="")))

    def test_review_and_ingest_rows_pass(self):
        self.assertTrue(_is_git_bound_row(row(id="REVIEW-01", area="", phase="07")))
        self.assertTrue(_is_git_bound_row(row(id="INGEST-01", area="", phase="05")))

    def test_semantic_phase_tokens_pass(self):
        for phase in ("artifact", "analysis", "review", "ingest"):
            self.assertTrue(_is_git_bound_row(row(id="X-01", area="", phase=phase)))

    def test_implementation_row_is_not_git_bound(self):
        self.assertFalse(_is_git_bound_row(row(id="IMPL-01", area="implementation", phase="01")))

    def test_smoke_row_is_not_git_bound(self):
        self.assertFalse(_is_git_bound_row(row(id="SMOKE-01", area="validation", phase="02")))


if __name__ == "__main__":
    unittest.main()
