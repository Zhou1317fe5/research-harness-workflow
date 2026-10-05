#!/usr/bin/env python3
"""科学行为指纹的回归：区分行为变更与记账 churn。

这是 B1/B2 的核心判据，因此单独覆盖：排除规则、fail-closed 默认、
行为差异列表、以及「只有文档变化时指纹不变」。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))

from harness.remote.change_fingerprint import (  # noqa: E402
    FingerprintError,
    behavior_delta,
    behavior_fingerprint,
    is_behavior_path,
    same_behavior,
)


class BehaviorPathTests(unittest.TestCase):
    def test_source_and_config_paths_are_behavior(self):
        for path in (
            "src/model/runtime.py",
            "scripts/train_smoke.py",
            "configs/exp.yaml",
            "main.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_behavior_path(path))

    def test_documentation_and_bookkeeping_are_not_behavior(self):
        for path in (
            "docs/specs/plan.md",
            "docs/reviews/findings.md",
            "issues/2026-10-02_x/item.csv",
            "tests/test_runtime.py",
            "remote_artifacts/EXP-1/RUN-1/log.txt",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_behavior_path(path))

    def test_generated_artifacts_are_not_behavior(self):
        for path in (
            "src/pkg/__pycache__/runtime.cpython-311.pyc",
            "src/pkg/runtime.pyc",
            "src/pkg/runtime.pyo",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_behavior_path(path))

    def test_ambiguous_filenames_stay_fail_closed(self):
        """同名生成物与同名源输入无法按文件名区分，因此算作行为相关。

        `progress.jsonl` 可能是运行产物，也可能是离线 manifest；`summary.json` 可能是
        生成摘要，也可能是源输入。按目录排除（issues/、remote_artifacts/）才是安全的
        方式；靠文件名宽恕会放过真实变更。
        """
        for path in (
            "run/progress.jsonl",
            "run/summary.json",
            "run/smoke_summary.json",
            "configs/prompts/model.txt",
            "data/manifest.jsonl",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_behavior_path(path))

    def test_workflow_and_project_config_are_behavior(self):
        """工作流与项目适配器里存在真实行为代码，不得整体排除。"""
        for path in (
            ".agents/harness/remote/project_adapters.py",
            ".agents/harness/config/project.toml",
            ".codex/skills/mission-csv-execute/scripts/csv_state.py",
            ".pi/skills/pre-run-implementation-review/scripts/prerun_route.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_behavior_path(path))

    def test_test_directories_excluded_but_hidden_paths_kept(self):
        self.assertFalse(is_behavior_path(".codex/skills/x/tests/test_a.py"))
        self.assertFalse(is_behavior_path("src/tests/test_b.py"))
        self.assertTrue(is_behavior_path(".env"))
        self.assertTrue(is_behavior_path(".github/workflows/ci.yml")),

    def test_unknown_paths_default_to_behavior(self):
        """fail-closed：未识别的路径算作行为相关。"""
        self.assertTrue(is_behavior_path("weird/thing.bin"))
        self.assertTrue(is_behavior_path("notebooks/explore.unknownext"))


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="change-fingerprint-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "test")
        (self.repo / "src").mkdir()
        (self.repo / "src/model.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "notes.md").write_text("initial\n", encoding="utf-8")
        self.base = self.commit("initial")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_docs_only_commit_keeps_behavior_identical(self):
        (self.repo / "notes.md").write_text("more notes\n", encoding="utf-8")
        head = self.commit("docs")
        self.assertTrue(same_behavior(self.repo, self.base, head))
        self.assertEqual(behavior_delta(self.repo, self.base, head), [])

    def test_source_commit_changes_behavior(self):
        (self.repo / "src/model.py").write_text("VALUE = 2\n", encoding="utf-8")
        head = self.commit("behaviour")
        self.assertFalse(same_behavior(self.repo, self.base, head))
        self.assertEqual(behavior_delta(self.repo, self.base, head), ["src/model.py"])

    def test_test_only_commit_keeps_behavior_identical(self):
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_model.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        head = self.commit("test")
        self.assertTrue(same_behavior(self.repo, self.base, head))

    def test_adding_source_file_changes_fingerprint(self):
        before = behavior_fingerprint(self.repo, self.base)
        (self.repo / "src/extra.py").write_text("X = 1\n", encoding="utf-8")
        head = self.commit("add module")
        self.assertNotEqual(before, behavior_fingerprint(self.repo, head))

    def test_identical_commit_is_behavior_identical(self):
        self.assertTrue(same_behavior(self.repo, self.base, self.base))

    def test_unresolvable_commit_is_fail_closed(self):
        with self.assertRaises(FingerprintError):
            behavior_fingerprint(self.repo, "0" * 40)


if __name__ == "__main__":
    unittest.main()
