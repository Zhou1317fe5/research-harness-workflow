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
            "RUNINFO RUN-A 1700000000 completed 71680 15360",
            "garbage line",
            "RUNINFO RUN-B 1700000100 completed 100",
            "RUNINFO RUN-C 1700000200 failed 1024 2048",
        ]) + "\n"
        entries = prune_runs.parse_list_output(stdout, 2)
        self.assertEqual([e["run_id"] for e in entries], ["RUN-A", "RUN-C"])
        self.assertEqual(entries[1]["sizes_kb"], [1024, 2048])
        self.assertEqual(entries[1]["state"], "failed")


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


class LocalStateTests(unittest.TestCase):
    def test_prune_local_state_only_touches_runid_dirs(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RRCTL_STATE_ROOT"] = tmp
            try:
                run_id = "RUN-20261101-demo-smoke1"
                for sub in ("runs", "client-results", "specs"):
                    (Path(tmp) / sub / run_id).mkdir(parents=True)
                    (Path(tmp) / sub / run_id / "keep.txt").write_text("x")
                (Path(tmp) / "specs" / "RUN-other").mkdir(parents=True)
                removed = prune_runs.prune_local_state([run_id], apply=True)
                self.assertEqual(len(removed), 3)
                for sub in ("runs", "client-results", "specs"):
                    self.assertFalse((Path(tmp) / sub / run_id).exists())
                self.assertTrue((Path(tmp) / "specs" / "RUN-other").exists())
            finally:
                os.environ.pop("RRCTL_STATE_ROOT", None)

    def test_prune_local_state_dry_run_keeps_files(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RRCTL_STATE_ROOT"] = tmp
            try:
                run_id = "RUN-20261101-demo2"
                (Path(tmp) / "runs" / run_id).mkdir(parents=True)
                removed = prune_runs.prune_local_state([run_id], apply=False)
                self.assertEqual(len(removed), 1)
                self.assertTrue((Path(tmp) / "runs" / run_id).exists())
            finally:
                os.environ.pop("RRCTL_STATE_ROOT", None)

    def test_non_run_idents_rejected(self):
        import io
        self.assertEqual(prune_runs.main(["--local-state", "--run-ids", "../etc"]), 2)
