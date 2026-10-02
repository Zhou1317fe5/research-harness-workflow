"""cleanup_output 接受/拒绝矩阵：fail-closed 边界与删除语义。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

import test_process_backend as fixtures

from remote_run_control.cleanup import SUMMARY_PATH, cleanup_output
from remote_run_control.errors import RRCError
from remote_run_control.jsonutil import load_json
from remote_run_control.models import OutputCleanupSpec


class CleanupTests(fixtures.ProcessBackendTests):
    def smoke_spec(self, policy=None, metadata=None, run_id_suffix=""):
        spec = self.spec()
        output_root = Path(self.root) / "smoke" / (spec.run_id + run_id_suffix)
        spec = replace(
            spec,
            remote=replace(spec.remote, output_root=str(output_root)),
            metadata={"execution_purpose": "pre_review_smoke", **(metadata or {})},
            output_cleanup=policy,
        )
        output_root.mkdir(parents=True, exist_ok=True)
        return spec, output_root

    def default_policy(self, **overrides):
        values = {
            "mode": "pre_review_smoke",
            "retain": ("smoke_summary.json",),
            "delete_globs": ("checkpoints/*",),
        }
        values.update(overrides)
        return OutputCleanupSpec(**values)

    # ---- accept 分支 ----

    def test_no_policy_is_a_noop(self):
        spec = self.spec()
        self.assertIsNone(cleanup_output(spec, terminal_state="failed"))

    def test_glob_checkpoint_deleted_and_evidence_retained(self):
        spec, output = self.smoke_spec(policy=self.default_policy())
        checkpoints = output / "checkpoints"
        checkpoints.mkdir()
        (checkpoints / "step10.pt").write_bytes(b"weights")
        (output / "metrics.json").write_text("{}")

        summary = cleanup_output(spec, terminal_state="workload_exit_zero")
        self.assertIsNotNone(summary)
        self.assertTrue(summary["checkpoint_cleanup_completed"])
        self.assertEqual(summary["checkpoint_paths_remaining"], [])
        self.assertIn("checkpoints/step10.pt", summary["deleted_paths"])
        self.assertGreater(summary["released_bytes"], 0)
        self.assertFalse((checkpoints / "step10.pt").exists())
        self.assertTrue((output / "metrics.json").exists())  # 未匹配的文件保留
        self.assertTrue((output / SUMMARY_PATH).exists())

    def test_size_threshold_deletes_only_large_files(self):
        policy = self.default_policy(
            delete_globs=(), delete_files_larger_than_bytes=100
        )
        spec, output = self.smoke_spec(policy=policy)
        (output / "big.bin").write_bytes(b"x" * 200)
        (output / "small.bin").write_bytes(b"y" * 10)
        summary = cleanup_output(spec, terminal_state="failed")
        self.assertFalse((output / "big.bin").exists())
        self.assertTrue((output / "small.bin").exists())
        self.assertEqual(summary["released_bytes"], 200)

    def test_retain_list_protects_declared_evidence(self):
        policy = self.default_policy(retain=("smoke_summary.json", "keep/log.txt"))
        spec, output = self.smoke_spec(policy=policy)
        (output / "keep").mkdir()
        (output / "keep" / "log.txt").write_text("evidence")
        (output / "checkpoints").mkdir()
        (output / "checkpoints" / "a.pt").write_bytes(b"w")
        cleanup_output(spec, terminal_state="failed")
        self.assertTrue((output / "keep" / "log.txt").exists())
        self.assertFalse((output / "checkpoints" / "a.pt").exists())

    def test_summary_is_machine_readable_and_bound_to_run(self):
        spec, output = self.smoke_spec(policy=self.default_policy())
        cleanup_output(spec, terminal_state="workload_exit_zero")
        summary = load_json(output / SUMMARY_PATH)
        self.assertEqual(summary["schema_version"], "rrctl.smoke-summary.v1")
        self.assertEqual(summary["run_id"], spec.run_id)
        self.assertEqual(summary["terminal_state"], "workload_exit_zero")
        self.assertTrue(any("console.log" in p for p in summary["retained_evidence_paths"]))

    # ---- reject 分支（fail-closed） ----

    def test_unsupported_mode_is_rejected(self):
        spec, _output = self.smoke_spec(policy=self.default_policy(mode="aggressive"))
        with self.assertRaises(RRCError) as caught:
            cleanup_output(spec, terminal_state="failed")
        self.assertEqual(caught.exception.code, "cleanup_mode")

    def test_purpose_other_than_smoke_is_rejected(self):
        spec, _output = self.smoke_spec(
            policy=self.default_policy(), metadata={"execution_purpose": "science"}
        )
        with self.assertRaises(RRCError) as caught:
            cleanup_output(spec, terminal_state="failed")
        self.assertEqual(caught.exception.code, "cleanup_purpose")

    def test_output_root_without_run_id_binding_is_rejected(self):
        spec = self.spec()
        spec = replace(
            spec,
            remote=replace(
                spec.remote, output_root=str(Path(self.root) / "unbound-output")
            ),
            metadata={"execution_purpose": "pre_review_smoke"},
            output_cleanup=self.default_policy(),
        )
        with self.assertRaises(RRCError) as caught:
            cleanup_output(spec, terminal_state="failed")
        self.assertEqual(caught.exception.code, "cleanup_run_binding")

    def test_symlink_output_root_is_rejected(self):
        spec = self.spec()
        target = Path(self.root) / "target" / spec.run_id
        target.mkdir(parents=True)
        symlink = Path(self.root) / "link" / spec.run_id
        symlink.parent.mkdir(parents=True)
        symlink.symlink_to(target, target_is_directory=True)
        spec = replace(
            spec,
            remote=replace(spec.remote, output_root=str(symlink)),
            metadata={"execution_purpose": "pre_review_smoke"},
            output_cleanup=self.default_policy(),
        )
        with self.assertRaises(RRCError) as caught:
            cleanup_output(spec, terminal_state="failed")
        self.assertEqual(caught.exception.code, "cleanup_symlink")

    def test_absolute_retain_path_is_rejected(self):
        spec, _output = self.smoke_spec(
            policy=self.default_policy(retain=("/etc/passwd",))
        )
        with self.assertRaises(RRCError) as caught:
            cleanup_output(spec, terminal_state="failed")
        self.assertEqual(caught.exception.code, "cleanup_path")

    def test_dotdot_delete_glob_is_rejected(self):
        spec, _output = self.smoke_spec(
            policy=self.default_policy(delete_globs=("../escape/*",))
        )
        with self.assertRaises(RRCError) as caught:
            cleanup_output(spec, terminal_state="failed")
        self.assertEqual(caught.exception.code, "cleanup_path")

    def test_unmatched_glob_pattern_raises_cleanup_incomplete(self):
        # glob 语义：模式声明了可删除项，但清理后仍可匹配 => 视为未完成。
        # 构造一个 retain 集合与 delete_glob 重叠的路径：walk 时 retain 跳过该路径不删，
        # 收尾检查时该路径仍匹配 delete_glob => cleanup_incomplete。
        policy = self.default_policy(
            retain=("smoke_summary.json", "checkpoints/protected.pt"),
            delete_globs=("checkpoints/*",),
        )
        spec, output = self.smoke_spec(policy=policy)
        checkpoints = output / "checkpoints"
        checkpoints.mkdir()
        (checkpoints / "protected.pt").write_bytes(b"keep")
        with self.assertRaises(RRCError) as caught:
            cleanup_output(spec, terminal_state="failed")
        self.assertEqual(caught.exception.code, "cleanup_incomplete")
        self.assertIn(
            "checkpoints/protected.pt", caught.exception.details["checkpoint_paths_remaining"]
        )


if __name__ == "__main__":
    unittest.main()
