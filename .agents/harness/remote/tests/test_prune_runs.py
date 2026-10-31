"""prune_runs：远端 run 目录清理器的选择逻辑与安全护栏（不触网）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))

from harness.remote import prune_runs  # noqa: E402


def entry(run_id: str, mtime: int, sizes: list[int]) -> dict:
    return {"run_id": run_id, "mtime": mtime, "sizes_kb": sizes}


class TargetValidationTests(unittest.TestCase):
    def test_default_targets_are_repo_and_stage(self):
        self.assertEqual(prune_runs.validate_targets("repo,stage"), ["repo", "stage"])

    def test_output_and_control_are_forbidden(self):
        for target in ("output", "control", "/", "."):
            with self.assertRaises(ValueError):
                prune_runs.validate_targets(target)

    def test_nested_or_parent_targets_rejected(self):
        for target in ("repo/cache", "../x"):
            with self.assertRaises(ValueError):
                prune_runs.validate_targets(target)

    def test_empty_targets_rejected(self):
        with self.assertRaises(ValueError):
            prune_runs.validate_targets(" , ")


class ScriptTests(unittest.TestCase):
    def test_list_script_covers_every_target(self):
        script = prune_runs.build_list_script("/srv/runs", ["repo", "stage"])
        self.assertIn("/srv/runs", script)
        self.assertIn('du -sk "$d"/repo', script)
        self.assertIn('du -sk "$d"/stage', script)

    def test_delete_script_only_touches_requested_targets(self):
        script = prune_runs.build_delete_script("/srv/runs", ["repo", "stage"], ["RUN-A"])
        self.assertIn('rm -rf "$ROOT"/RUN-A/repo', script)
        self.assertIn('rm -rf "$ROOT"/RUN-A/stage', script)
        self.assertNotIn("output", script)
        self.assertNotIn("control", script)


class ParseTests(unittest.TestCase):
    def test_parse_skips_malformed_lines(self):
        stdout = "\n".join([
            "RUNINFO RUN-A 1700000000 71680 15360",
            "garbage line",
            "RUNINFO RUN-B 1700000100 100",
            "RUNINFO RUN-C 1700000200 1024 2048",
        ]) + "\n"
        entries = prune_runs.parse_list_output(stdout, 2)
        self.assertEqual([e["run_id"] for e in entries], ["RUN-A", "RUN-C"])
        self.assertEqual(entries[1]["sizes_kb"], [1024, 2048])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry("RUN-old-smoke1", 1000, [70000, 15000]),
            entry("RUN-mid-smoke1", 2000, [100, 100]),
            entry("RUN-new-full600", 3000, [0, 0]),
            entry("RUN-other", 4000, [10, 10]),
        ]

    def test_stem_filters_by_substring(self):
        selected = prune_runs.select(self.entries, stem="smoke", keep=None,
                                     older_than_days=None, now=10_000)
        self.assertEqual([e["run_id"] for e in selected], ["RUN-mid-smoke1", "RUN-old-smoke1"])

    def test_keep_newest_n_within_filtered_set(self):
        selected = prune_runs.select(self.entries, stem="smoke", keep=1,
                                     older_than_days=None, now=10_000)
        self.assertEqual([e["run_id"] for e in selected], ["RUN-old-smoke1"])

    def test_older_than_days_cutoff(self):
        # now=3000、阈值 1500s ⇒ cutoff=1500，只有 mtime=1000 的 run 落入清理窗口。
        selected = prune_runs.select(self.entries, stem=None, keep=None,
                                     older_than_days=1500 / 86400, now=3000)
        self.assertEqual([e["run_id"] for e in selected], ["RUN-old-smoke1"])

    def test_keep_none_means_no_count_limit(self):
        selected = prune_runs.select(self.entries, stem=None, keep=None,
                                     older_than_days=None, now=10_000)
        self.assertEqual(len(selected), 4)


if __name__ == "__main__":
    unittest.main()
