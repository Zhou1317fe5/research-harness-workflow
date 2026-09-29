"""mission_state 生命周期迁移矩阵；只使用隔离目录与最小 CSV/spec 夹具。

completed 门禁现场 importlib 加载 mission_completion 为独立模块
`lifecycle_completion`，所以 patch 一句 `mission_completion.csv_completion_errors`
不起作用——它活在另一个 module 对象上。这里直接 patch importlib 的工厂，让
mission_state 收到一个本地假 completion 模块。
"""
import csv
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))
from harness.workflow.mission_state import (
    SCHEMA, assert_launchable, load_registry, state_path, task_for_csv, update,
)
from mission_completion import EXPECTED_FIELDS


class _FakeCompletion:
    """模拟闭环已验证的 mission_completion；只服务 completed 分支。"""
    EXPECTED_FIELDS = EXPECTED_FIELDS
    def read_mission_csv(self, csv_path, allow_compat=False):
        return self.EXPECTED_FIELDS, [], None
    def csv_completion_errors(self, csv_path, *, workdir, allow_compat=False):
        return []


class _FakeSpec:
    name = "lifecycle_completion"

    class loader:
        @staticmethod
        def exec_module(module):
            return None


import importlib.util as _ilu
_real_spec_from_file = _ilu.spec_from_file_location
_real_module_from_spec = _ilu.module_from_spec


REF = "session:user#fixture"


class LifecycleMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mission-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def csv(self, name="task"):
        path = self.root / f"issues/{name}/{name}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = dict.fromkeys(EXPECTED_FIELDS, "")
        row.update(id="ISSUE-01", dev_state="进行中", review_initial_state="未开始",
                   review_regression_state="未开始", git_state="已提交",
                   remote_state="completed", notes="artifact_policy:none")
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerow(row)
        return path

    def spec(self, name="new"):
        path = self.root / f"docs/specs/{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture spec\n")
        return path

    def rel(self, path):
        return path.relative_to(self.root).as_posix()

    def register_csv(self, name, path):
        return update(self.root, name, action="register", source_ref=REF, csv=self.rel(path))

    def register_spec(self, name, path=None, **extra):
        target = path if path is not None else self.spec(name)
        return update(self.root, name, action="register", source_ref=REF,
                      spec=self.rel(target), **extra)

    def transition(self, name, status, **extra):
        return update(self.root, name, action="transition", status=status, source_ref=REF, **extra)

    def registry(self):
        return load_registry(self.root)

    def transition_to_completed(self, name):
        """把 mission_state 加载的 lifecycle_completion 模块替换为已通过闭环检查。"""
        fake = _FakeCompletion()
        fake_spec = _FakeSpec()
        with patch("importlib.util.spec_from_file_location",
                   side_effect=lambda n, loc: fake_spec if n == "lifecycle_completion" else _real_spec_from_file(n, loc)), \
             patch("importlib.util.module_from_spec",
                   side_effect=lambda spec: fake if getattr(spec, "name", None) == "lifecycle_completion" else _real_module_from_spec(spec)):
            return self.transition(name, "completed", reason="闭环")

    def test_terminal_states_reject_every_other_transition(self):
        self.register_spec("spec-task")
        self.transition("spec-task", "cancelled", reason="取消")
        self.register_csv("csv-task", self.csv("csv-task"))
        # 闭环的合规性由 test_mission_contracts / closing 测试保证；本测试只关心
        # completed 迁移在闭环成立时确实能走通且终态互斥正确。
        self.transition_to_completed("csv-task")
        self.register_csv("superseded-task", self.csv())
        self.register_spec("replacement")
        self.transition("superseded-task", "superseded", reason="被替换", replacement="replacement")
        terminal = {"spec-task": "cancelled", "csv-task": "completed", "superseded-task": "superseded"}
        for task_id, status in terminal.items():
            self.assertEqual(self.registry()["tasks"][task_id]["status"], status)
            for target in ("preparing", "active", "paused", "cancelled", "superseded", "completed"):
                if target == status:
                    continue
                with self.subTest(task_id=task_id, target=target), self.assertRaises(ValueError):
                    self.transition(task_id, target, reason="恢复")
            if task_id == "csv-task":
                self.assertEqual(self.registry()["tasks"][task_id]["status"], "completed")
        with self.assertRaises(ValueError):
            update(self.root, "spec-task", action="bind", source_ref=REF, csv=self.rel(self.csv("late")))
        with self.assertRaises(ValueError):
            update(self.root, "spec-task", action="select", source_ref=REF)

    def test_active_task_pause_and_resume_roundtrip(self):
        path = self.csv()
        self.register_csv("task", path)
        before = self.registry()["tasks"]["task"]
        self.transition("task", "active", reason="幂等")
        after = self.registry()["tasks"]["task"]
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(len(after["history"]), len(before["history"]) + 1)
        self.transition("task", "paused", reason="暂停")
        # paused 不清空 current_task；这是当前可观察的契约。
        self.assertEqual(self.registry()["current_task"], "task")
        with self.assertRaisesRegex(ValueError, "mission_not_active:task:paused"):
            assert_launchable(self.root, path)
        self.transition("task", "active", reason="恢复")
        self.assertEqual(self.registry()["current_task"], "task")
        # active 任务再次 select 自己是允许的（幂等 select）。
        update(self.root, "task", action="select", source_ref=REF)
        self.assertEqual(self.registry()["current_task"], "task")

    def test_cancelled_csv_still_cannot_be_rebound(self):
        path = self.csv()
        self.register_csv("old", path)
        self.transition("old", "cancelled", reason="换任务")
        # 取消后 csv 仍被持有，注册新 task 会被唯一绑定门禁拦截。
        # 见 BUG-CANDIDATE 报告。
        with self.assertRaises(ValueError):
            self.register_csv("new", path)
        self.assertEqual(self.registry()["tasks"]["old"]["status"], "cancelled")

    def test_active_csv_cannot_bind_a_second_task(self):
        path = self.csv()
        self.register_csv("first", path)
        with self.assertRaises(ValueError):
            self.register_csv("second", path)
        self.assertNotIn("second", self.registry()["tasks"])

    def test_spec_task_binds_csv_then_gates_completed(self):
        spec_path = self.spec("draft")
        self.register_spec("draft", spec_path)
        self.assertEqual(self.registry()["tasks"]["draft"]["status"], "preparing")
        with self.assertRaisesRegex(ValueError, "没有 CSV 的任务"):
            self.transition("draft", "active", reason="提前激活")
        with self.assertRaisesRegex(ValueError, "未建立 CSV 的任务不能声明完成"):
            self.transition("draft", "completed", reason="提前完成")
        csv_path = self.csv("draft")
        update(self.root, "draft", action="bind", source_ref=REF, csv=self.rel(csv_path))
        self.assertEqual(self.registry()["tasks"]["draft"]["status"], "active")
        self.assertEqual(self.registry()["current_task"], "draft")

    def test_superseded_requires_valid_replacement_and_reason(self):
        self.register_csv("victim", self.csv("victim"))
        self.register_spec("successor")
        with self.assertRaises(ValueError):
            self.transition("victim", "superseded", replacement="victim", reason="自替换")
        with self.assertRaises(ValueError):
            self.transition("victim", "superseded", replacement="absent", reason="缺失替代")
        with self.assertRaises(ValueError):
            self.transition("victim", "superseded", replacement="successor", reason="")
        self.assertEqual(self.registry()["tasks"]["victim"]["status"], "active")
        self.transition("victim", "superseded", replacement="successor", reason="交接")
        self.assertEqual(self.registry()["current_task"], "successor")
        self.assertEqual(self.registry()["tasks"]["victim"].get("replacement"), "successor")

    def test_inactive_status_requires_reason(self):
        self.register_csv("task", self.csv())
        for status in ("paused", "cancelled"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.transition("task", status, reason="")
        self.assertEqual(self.registry()["tasks"]["task"]["status"], "active")

    def test_rejections_leave_registry_unchanged(self):
        self.register_csv("task", self.csv())
        before = state_path(self.root).read_text()
        for kwargs in (
            dict(action="transition", status="completed", source_ref=REF, reason="未闭环"),
            dict(action="transition", status="bogus", source_ref=REF, reason="x"),
            dict(action="unknown", source_ref=REF),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                update(self.root, "task", **kwargs)
            self.assertEqual(state_path(self.root).read_text(), before)

    def test_current_pointer_moves_and_recovers(self):
        self.register_csv("first", self.csv("first"))
        self.register_csv("second", self.csv("second"))
        self.assertEqual(self.registry()["current_task"], "second")
        update(self.root, "first", action="select", source_ref=REF)
        self.assertEqual(self.registry()["current_task"], "first")
        self.transition("first", "paused", reason="暂停")
        # paused 不清空 current_task；由后续 select 切换或 cancelled/superseded/completed 才清空。
        self.assertEqual(self.registry()["current_task"], "first")
        update(self.root, "second", action="select", source_ref=REF)
        self.assertEqual(self.registry()["current_task"], "second")
        self.transition("second", "cancelled", reason="取消")
        self.assertIsNone(self.registry()["current_task"])

    def test_reference_and_identifier_validation(self):
        path = self.csv()
        for task_id in ("", "has space", "../up", "x" * 129, "包含中文"):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                update(self.root, task_id, action="register", source_ref=REF, csv=self.rel(path))
        with self.assertRaises(ValueError):
            update(self.root, "task", action="register", source_ref=REF)
        with self.assertRaises(ValueError):
            update(self.root, "task", action="transition", status="paused", source_ref=" \n", reason="x")
        with self.assertRaises(ValueError):
            update(self.root, "task", action="register", source_ref="fixture:seed\n--password=xx", csv=self.rel(path))
        for value in ("../outside.csv", "issues/../escape.csv", "issues", "issues/x.md",
                      "issues/x.csv ", "docs/specs/x.md\n", "/absolute/issues/x.csv",
                      "issues/missing.csv", "\\issues\\x.csv"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                update(self.root, "task", action="register", source_ref=REF, csv=value)
        for value in ("issues/x.md", "docs/specs/missing.md", "docs/specs/x.csv"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                update(self.root, "task", action="register", source_ref=REF, spec=value)

    def test_load_registry_schema_and_tombstone_rules(self):
        path = state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        def write(data):
            path.write_text(json.dumps(data, ensure_ascii=False))
        write({"schema_version": "other", "tasks": {}})
        with self.assertRaises(ValueError):
            load_registry(self.root)
        write({"schema_version": SCHEMA, "current_task": None, "tasks": {"a": {"status": "active", "history": []}}})
        with self.assertRaises(ValueError):
            load_registry(self.root)
        write({"schema_version": SCHEMA, "current_task": None, "tasks": {
            "a": {"status": "active", "csv": "issues/x/x.csv", "history": []},
            "b": {"status": "paused", "csv": "issues/x/x.csv", "history": []}}})
        with self.assertRaises(ValueError):
            load_registry(self.root)
        write({"schema_version": SCHEMA, "current_task": "ghost",
               "tasks": {"a": {"status": "active", "csv": "issues/x/x.csv", "history": []}}})
        with self.assertRaises(ValueError):
            load_registry(self.root)
        original = self.csv()
        path.unlink()  # 清理损坏的 registry，避免影响后续注册。
        self.register_csv("closed", original)
        self.transition("closed", "cancelled", reason="归档")
        original.unlink()
        data = self.registry()  # tombstone 保留，即使 CSV 已归档。
        self.assertEqual(data["tasks"]["closed"]["status"], "cancelled")

    def test_task_lookup_and_launch_gate(self):
        path = self.csv()
        self.assertIsNone(task_for_csv(self.root, path))
        assert_launchable(self.root, path)
        self.register_csv("task", path)
        self.assertEqual(task_for_csv(self.root, path)["task_id"], "task")
        assert_launchable(self.root, path)
        self.assertIsNone(task_for_csv(self.root, self.root.parent / "elsewhere.csv"))

    def test_main_cli_status_and_error_mapping(self):
        path = ROOT / ".agents/harness/workflow/mission_state.py"
        def run(*args, extra_env=None):
            env = dict(os.environ, **(extra_env or {}))
            return subprocess.run([sys.executable, str(path), "--repo-root", str(self.root), *args],
                                  capture_output=True, text=True, env=env)
        result = run("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema_version"], SCHEMA)
        csv_path = self.csv()
        result = run("register", "--task-id", "cli", "--csv", self.rel(csv_path), "--source-ref", REF)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "active")
        result = run("transition", "--task-id", "cli", "--status", "paused", "--source-ref", REF, "--reason", "暂停")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "paused")
        result = run("transition", "--task-id", "cli", "--status", "active", "--source-ref", REF)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "active")
        result = run("transition", "--task-id", "cli", "--status", "completed", "--source-ref", REF, "--reason", "未闭环")
        self.assertEqual(result.returncode, 2)
        self.assertIn("尚未闭环", json.loads(result.stdout)["error"])
        result = run("bogus")  # argparse choices 拒绝
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
