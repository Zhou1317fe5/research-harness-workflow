"""sample_health 分支矩阵：相位校验、门禁判定、降级与状态持久化。"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

import test_process_backend as fixtures

from remote_run_control import health, state
from remote_run_control.errors import RRCError
from remote_run_control.jsonutil import atomic_write_json, utc_now


class SamplingFixtures:
    def sample_root(self, spec, binding=None, *, up_to="launched"):
        root = Path(spec.remote.control_root)
        root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(root / "run_spec.json", spec.to_dict())
        ladder = {
            "launched": ("prepared", "staged", "launched"),
            "running": ("prepared", "staged", "launched", "first_step_passed", "running"),
            "workload_complete": (
                "prepared", "staged", "launched", "first_step_passed",
                "running", "workload_complete",
            ),
        }[up_to]
        for value in ladder:
            state.transition(root, run_id=spec.run_id, next_state=value, reason="fixture")
        default_binding = {
            "run_id": spec.run_id,
            "run_spec_sha256": spec.digest,
            "worker_sha256": "a" * 64,
            "created_at": utc_now(),
        }
        atomic_write_json(root / "binding.json", binding or default_binding)
        return root

    def codes(self, result):
        return {item["code"] for item in result.issues}


class SampleHealthTests(fixtures.ProcessBackendTests, SamplingFixtures):
    # ---- 相位与门禁 ----

    def test_unknown_phase_raises_fail_closed(self):
        spec = self.spec()
        root = self.sample_root(spec)
        with self.assertRaises(RRCError) as caught:
            health.sample_health(spec, root, phase="bogus")
        self.assertEqual(caught.exception.code, "health_phase")

    def test_accepts_all_declared_phases(self):
        spec = self.spec()
        root = self.sample_root(spec)
        for phase in ("first_step", "periodic", "completion"):
            result = health.sample_health(
                spec, root, phase=phase, process_required=False
            )
            self.assertEqual(result.phase, phase)

    def test_missing_process_is_hard_error(self):
        spec = self.spec()
        # binding 指向一个不存在的 pid -> missing probe，无 owned 子进程 -> process_missing
        binding = {
            "run_id": spec.run_id,
            "run_spec_sha256": spec.digest,
            "worker_sha256": "a" * 64,
            "created_at": utc_now(),
            "boot_id": "0" * 32,
            "workload_pid": 2**22 + 9,
            "workload_start_ticks": 0,
            "workload_process_group_id": 0,
        }
        root = self.sample_root(spec, binding=binding)
        original = health.probe_bound_process
        health.probe_bound_process = lambda *_args, **_kwargs: {
            "state": "missing", "pid": binding["workload_pid"]
        }
        try:
            result = health.sample_health(
                spec, root, phase="first_step", process_required=True
            )
        finally:
            health.probe_bound_process = original
        self.assertIn("process_missing", self.codes(result))
        issue = next(item for item in result.issues if item["code"] == "process_missing")
        self.assertTrue(issue["required"])
        self.assertFalse(issue["retryable"])
        self.assertEqual(result.status, "unhealthy")
        self.assertFalse(result.healthy)

    def test_pending_identity_is_retryable_degraded_not_unhealthy(self):
        spec = self.spec()
        root = self.sample_root(spec)
        # binding 不带 pid -> probe 状态为 pending
        result = health.sample_health(spec, root, phase="first_step", process_required=True)
        self.assertIn("process_identity_pending", self.codes(result))
        self.assertEqual(result.status, "unavailable")
        # unavailable 不是 healthy：只意味着当前无法判定，而非通过门禁
        self.assertFalse(result.healthy)

    def test_process_not_required_accepts_dead_binding_info(self):
        spec = self.spec()
        root = self.sample_root(spec, up_to="workload_complete")
        result = health.sample_health(spec, root, phase="completion", process_required=False)
        self.assertNotIn("process_missing", self.codes(result))
        self.assertEqual(result.status, "healthy")
        self.assertTrue(result.complete)

    def test_terminal_state_is_hard_error(self):
        spec = self.spec()
        root = self.sample_root(spec)
        state.transition(root, run_id=spec.run_id, next_state="failed", reason="fixture")
        result = health.sample_health(spec, root, phase="periodic", process_required=False)
        self.assertIn("run_terminal", self.codes(result))
        self.assertEqual(result.status, "unhealthy")
        self.assertFalse(result.healthy)

    # ---- 控制台 / 进展信号 ----

    def test_fresh_console_log_is_accepted(self):
        spec = self.spec()
        root = self.sample_root(spec)
        (root / "console.log").write_text("Epoch 1: loss=1.0\n")
        phase = replace(spec.health.first_step, console_stale_seconds=600)
        spec = replace(spec, health=replace(spec.health, first_step=phase))
        result = health.sample_health(spec, root, phase="first_step", process_required=False)
        self.assertNotIn("console_missing", self.codes(result))
        self.assertNotIn("console_stale", self.codes(result))
        self.assertTrue(result.gate_passed)

    def test_missing_console_blocks_first_step_gate(self):
        spec = self.spec()
        root = self.sample_root(spec)
        phase = replace(spec.health.first_step, console_stale_seconds=600)
        spec = replace(spec, health=replace(spec.health, first_step=phase))
        result = health.sample_health(spec, root, phase="first_step", process_required=False)
        self.assertIn("console_missing", self.codes(result))
        self.assertFalse(result.gate_passed)
        self.assertEqual(result.status, "degraded")  # required=False -> 降级而非失败

    def test_stale_console_is_detected(self):
        spec = self.spec()
        root = self.sample_root(spec)
        console = root / "console.log"
        console.write_text("old output\n")
        import os
        stale = 1_000_000  # 远大于阈值的旧 mtime
        os.utime(console, (stale, stale))
        phase = replace(spec.health.first_step, console_stale_seconds=10)
        spec = replace(spec, health=replace(spec.health, first_step=phase))
        result = health.sample_health(spec, root, phase="first_step", process_required=False)
        self.assertIn("console_stale", self.codes(result))
        self.assertFalse(result.gate_passed)

    def test_fatal_pattern_in_console_is_hard_error(self):
        spec = self.spec()
        root = self.sample_root(spec)
        (root / "console.log").write_text(
            "Epoch 1: loss=1.0\nRuntimeError: CUDA out of memory\n"
        )
        phase = replace(
            spec.health.first_step, fatal_patterns=(r"CUDA out of memory",)
        )
        spec = replace(spec, health=replace(spec.health, first_step=phase))
        result = health.sample_health(spec, root, phase="first_step", process_required=False)
        self.assertIn("fatal_pattern", self.codes(result))
        self.assertFalse(result.healthy)
        self.assertEqual(result.status, "unhealthy")

    def test_progress_missing_blocks_gate_and_fresh_progress_passes(self):
        output = None
        spec = self.spec()
        root = self.sample_root(spec)
        phase = replace(spec.health.first_step, progress_path="progress.json")
        spec = replace(spec, health=replace(spec.health, first_step=phase))
        output = Path(spec.remote.output_root)
        output.mkdir(parents=True, exist_ok=True)

        result = health.sample_health(spec, root, phase="first_step", process_required=False)
        self.assertIn("progress_missing", self.codes(result))
        self.assertFalse(result.gate_passed)

        (output / "progress.json").write_text('{"step": 1}\n')
        result = health.sample_health(spec, root, phase="first_step", process_required=False)
        self.assertNotIn("progress_missing", self.codes(result))
        self.assertTrue(result.gate_passed)

    # ---- GPU 采样 ----

    def test_gpu_unavailable_is_retryable_degraded_under_required_policy(self):
        spec = self.spec()
        root = self.sample_root(spec)
        phase = replace(
            spec.health.periodic,
            gpu_utilization_policy="required",
            gpu_min_percent=10,
            low_gpu_limit_seconds=60,
        )
        spec = replace(spec, health=replace(spec.health, periodic=phase))
        original = health._sample_gpu
        health._sample_gpu = lambda _ids: None
        try:
            result = health.sample_health(
                spec, root, phase="periodic", process_required=False
            )
        finally:
            health._sample_gpu = original
        self.assertIn("gpu_unavailable", self.codes(result))
        self.assertEqual(result.status, "unavailable")

    def test_gpu_below_min_is_advisory_only_until_window_reached(self):
        spec = self.spec()
        root = self.sample_root(spec)
        phase = replace(
            spec.health.periodic,
            gpu_utilization_policy="required",
            gpu_min_percent=50,
            low_gpu_limit_seconds=3600,
        )
        spec = replace(spec, health=replace(spec.health, periodic=phase))
        original = health._sample_gpu
        health._sample_gpu = lambda _ids: 5
        try:
            result = health.sample_health(
                spec, root, phase="periodic", process_required=False
            )
        finally:
            health._sample_gpu = original
        self.assertIn("gpu_below_advisory", self.codes(result))
        self.assertNotIn("gpu_below_required", self.codes(result))
        self.assertEqual(result.status, "degraded")
        # tracking 记录了低 GPU 起算时间
        self.assertIn("periodic", result.tracking["low_gpu_since"])

    def test_gpu_below_required_window_is_hard_error(self):
        spec = self.spec()
        root = self.sample_root(spec)
        phase = replace(
            spec.health.periodic,
            gpu_utilization_policy="required",
            gpu_min_percent=50,
            low_gpu_limit_seconds=1,
        )
        spec = replace(spec, health=replace(spec.health, periodic=phase))
        original = health._sample_gpu
        health._sample_gpu = lambda _ids: 5
        # 预置 tracking：低 GPU 已持续 100 秒，超过 1 秒窗口
        tracking = {"low_gpu_since": {"periodic": 1.0}}
        try:
            result = health.sample_health(
                spec, root, phase="periodic", process_required=False, tracking=tracking
            )
        finally:
            health._sample_gpu = original
        self.assertIn("gpu_below_required", self.codes(result))
        self.assertEqual(result.status, "unhealthy")

    def test_low_gpu_tracking_resets_when_gpu_recovers(self):
        tracking = {"low_gpu_since": {"periodic": 1.0}}
        duration = health._track_low_gpu(tracking, phase="periodic", now=2.0, low=False)
        self.assertEqual(duration, 0.0)
        self.assertEqual(tracking["low_gpu_since"], {})

    # ---- 追踪状态校验 ----

    def test_tracking_bool_since_is_replaced(self):
        tracking = {"low_gpu_since": {"periodic": True}}
        duration = health._track_low_gpu(tracking, phase="periodic", now=100.0, low=True)
        self.assertEqual(duration, 0.0)  # bool 被视为坏值并重新计时
        self.assertEqual(tracking["low_gpu_since"]["periodic"], 100.0)


class PersistHealthTests(fixtures.ProcessBackendTests, SamplingFixtures):
    def test_evaluate_health_persists_events_and_tracking(self):
        spec = self.spec()
        root = self.sample_root(spec)
        health.evaluate_health(spec, root, phase="first_step", process_required=False)
        self.assertTrue((root / "health.jsonl").is_file())
        self.assertTrue((root / "health_state.json").is_file())
        events = (root / "health.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(events), 1)
        event = json.loads(events[0])
        self.assertEqual(event["run_id"], spec.run_id)
        self.assertEqual(event["phase"], "first_step")

    def test_gate_passed_first_step_marks_lifecycle_once(self):
        spec = self.spec()
        root = self.sample_root(spec)
        # process_required=False + 无 console 阈值 -> 门禁应直接通过
        result = health.evaluate_health(
            spec, root, phase="first_step", process_required=False
        )
        self.assertTrue(result.gate_passed)
        # mark_first_step_passed 原子推进 launched -> first_step_passed -> running
        self.assertEqual(state.read_status(root)["state"], "running")

    def test_transition_disabled_keeps_lifecycle_untouched(self):
        spec = self.spec()
        root = self.sample_root(spec)
        result = health.evaluate_health(
            spec,
            root,
            phase="first_step",
            process_required=False,
            transition_lifecycle=False,
        )
        self.assertTrue(result.gate_passed)
        self.assertEqual(state.read_status(root)["state"], "launched")


if __name__ == "__main__":
    unittest.main()
