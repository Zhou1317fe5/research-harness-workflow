"""pull 总预算：超额时按体积淘汰，优先保住轻量摘要。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents/harness/remote/rrctl/src"))

from remote_run_control.controller import (  # noqa: E402
    PULL_TOTAL_BUDGET_BYTES,
    _apply_total_budget,
)


def entry(path: str, size: int) -> dict:
    return {"path": path, "size": size}


class TotalBudgetTests(unittest.TestCase):
    def test_default_budget_is_hundred_megabytes(self):
        self.assertEqual(PULL_TOTAL_BUDGET_BYTES, 100 * 1024 * 1024)

    def test_under_budget_is_untouched(self):
        entries = [entry("summary.json", 1000), entry("progress.jsonl", 2000)]
        self.assertEqual(_apply_total_budget(entries, budget=10_000), entries)

    def test_oversized_entries_are_dropped_largest_first(self):
        entries = [
            entry("summary.json", 100),
            entry("huge.jsonl", 900),
            entry("medium.jsonl", 500),
        ]
        kept = _apply_total_budget(entries, budget=600)
        self.assertIn("summary.json", [e["path"] for e in kept])
        self.assertNotIn("huge.jsonl", [e["path"] for e in kept])
        self.assertLessEqual(sum(e["size"] for e in kept), 600)

    def test_equal_sizes_drop_in_stable_path_order(self):
        # 淘汰顺序按 (-size, path) 升序：600 超额后先丢 a.json（路径更小），保住 b.json。
        entries = [entry("b.json", 500), entry("a.json", 500)]
        kept = _apply_total_budget(entries, budget=500)
        self.assertEqual([e["path"] for e in kept], ["b.json"])

    def test_keep_order_matches_input(self):
        entries = [entry("b.json", 500), entry("a.json", 400)]
        kept = _apply_total_budget(entries, budget=10_000)
        self.assertEqual([e["path"] for e in kept], ["b.json", "a.json"])

    def test_missing_size_treated_as_zero(self):
        kept = _apply_total_budget([{"path": "x.json"}], budget=100)
        self.assertEqual([e["path"] for e in kept], ["x.json"])

    def test_non_positive_budget_disables_cap(self):
        entries = [entry("a.jsonl", 10**9)]
        self.assertEqual(_apply_total_budget(entries, budget=0), entries)


if __name__ == "__main__":
    unittest.main()
