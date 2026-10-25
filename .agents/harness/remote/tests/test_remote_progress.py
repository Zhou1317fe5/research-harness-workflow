"""Unit tests for remote_progress.py."""
from __future__ import annotations

import datetime
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))

from harness.remote.remote_progress import (
    format_progress,
    get_progress,
    resolve_run_target,
)


class RemoteProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="harness-progress-test-")
        self.root = Path(self.temp_dir.name)
        self.addCleanup(self.temp_dir.cleanup)

    def test_resolve_run_target_from_spec_file(self):
        spec_file = self.root / "runspec.json"
        spec_file.write_text(json.dumps({"run_id": "RUN-TEST-01"}))
        run_id, resolved_path = resolve_run_target(str(spec_file), self.root)
        self.assertEqual(run_id, "RUN-TEST-01")
        self.assertEqual(resolved_path, spec_file.resolve())

    def test_format_progress_running_heartbeat_and_detailed(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        info = {
            "run_id": "RUN-MOCK-01",
            "state": "running",
            "exit_code": None,
            "started_at": (now - datetime.timedelta(minutes=30)).isoformat(),
            "updated_at": None,
            "elapsed_seconds": 1800.0,
            "current_step": 300,
            "step_field": "step",
            "total_steps": 600,
            "pct": 50.0,
            "speed_seconds_per_step": 6.0,
            "remaining_seconds": 1800.0,
            "eta_datetime": (now + datetime.timedelta(minutes=30)).isoformat(),
            "metrics": {"miou": 65.4},
        }
        heartbeat = format_progress(info, mode="heartbeat")
        self.assertIn("RUN-MOCK-01", heartbeat)
        self.assertIn("300/600 (50.0%)", heartbeat)
        self.assertIn("速度: 6.0s/步", heartbeat)
        self.assertIn("miou=65.4000", heartbeat)

        detailed = format_progress(info, mode="detailed")
        self.assertIn("运行状态: 运行中", detailed)
        self.assertIn("当前进度: 300/600 (50.0%)", detailed)
        self.assertIn("预计剩余: 30m", detailed)
        self.assertIn("预计完成时刻: ", detailed)
        # 本地时刻 + 明确偏移，不出现 UTC 或地区缩写。
        self.assertRegex(detailed, r"预计完成时刻: \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC[+-]\d")
        self.assertIn("已用时长: ", detailed)

    def test_duration_and_timezone_formatting(self):
        from harness.remote.remote_progress import format_duration, timezone_label

        self.assertEqual(format_duration(41 * 60), "41m")
        self.assertEqual(format_duration(366 * 60), "6h6m")
        self.assertEqual(format_duration(3600 * 25), "1d1h")
        self.assertEqual(format_duration(45), "45s")
        self.assertRegex(timezone_label(), r"^UTC[+-]\d+(:\d{2})?$")

    def test_heartbeat_uses_hours_and_minutes(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        info = {
            "run_id": "RUN-MOCK-03", "state": "running", "exit_code": None,
            "started_at": (now - datetime.timedelta(minutes=41)).isoformat(),
            "updated_at": now.isoformat(), "elapsed_seconds": 41 * 60.0,
            "current_step": 60, "step_field": "step", "total_steps": 600, "pct": 10.0,
            "speed_seconds_per_step": 40.7,
            "remaining_seconds": 366 * 60.0,
            "eta_datetime": (now + datetime.timedelta(minutes=366)).isoformat(),
            "metrics": {"miou": 87.4448},
        }
        heartbeat = format_progress(info, mode="heartbeat")
        self.assertIn("预计剩余: 6h6m", heartbeat)
        self.assertIn("已用: 41m", heartbeat)
        self.assertRegex(heartbeat, r"预计完成: \d{2}:\d{2} UTC[+-]\d")
        self.assertNotIn("m (预计完成: 366", heartbeat)

    def test_format_progress_completed(self):
        info = {
            "run_id": "RUN-MOCK-02",
            "state": "completed",
            "exit_code": 0,
            "started_at": "2026-10-08T10:00:00+00:00",
            "updated_at": "2026-10-08T11:00:00+00:00",
            "elapsed_seconds": 3600.0,
            "current_step": 600,
            "step_field": "step",
            "total_steps": 600,
            "pct": 100.0,
            "speed_seconds_per_step": 6.0,
            "remaining_seconds": None,
            "eta_datetime": None,
            "metrics": {"miou": 68.2},
        }
        detailed = format_progress(info, mode="detailed")
        self.assertIn("已完成 (completed, exit_code 0)", detailed)
        self.assertIn("实际总耗时: 1h0m", detailed)
        self.assertIn("miou=68.2000", detailed)

    def test_estimate_runtime(self):
        from harness.remote.remote_progress import estimate_runtime, format_estimate

        # 直接指定 marginal 与 init
        est = estimate_runtime(total_steps=1000, marginal_seconds=2.0, init_seconds=100.0)
        self.assertEqual(est["total_steps"], 1000)
        self.assertEqual(est["init_seconds"], 100.0)
        self.assertEqual(est["marginal_seconds_per_step"], 2.0)
        self.assertEqual(est["estimated_total_seconds"], 2100.0)
        self.assertTrue(est["eta_datetime"])

        text = format_estimate(est)
        self.assertIn("[启动前耗时沙盘推算]", text)
        self.assertIn("全量规模: 1000 步/样本", text)
        self.assertIn("单步边际耗时: 2.00 秒/步", text)
        self.assertIn("一次性初始化/冷启动耗时: 100.0 秒", text)
        self.assertIn("预计总耗时: 35m", text)

        # 尝试从 smoke_summary.json 读取
        summary_path = self.root / "smoke_summary.json"
        summary_path.write_text(json.dumps({
            "schema_version": "rrctl.smoke-summary.v1",
            "marginal_seconds_per_step": 1.5,
            "init_seconds": 30.0,
        }))
        est_file = estimate_runtime(total_steps=500, smoke_summary_path=summary_path)
        self.assertEqual(est_file["estimated_total_seconds"], 30.0 + 500 * 1.5)

    def test_zero_progress_and_missing_start_time_are_displayable(self):
        for count, started in ((0, None), (0, "2026-10-23T00:00:00+00:00"), (5, None)):
            with self.subTest(count=count, started=started):
                inspected = {"result": {"status": {"state": "running"},
                                        "binding": {"workload_started_at": started}}}
                health = {"result": {"adapter": {"progress": {"count": count, "total": 100}}}}
                replies = [subprocess.CompletedProcess([], 0, json.dumps(x), "") for x in (inspected, health)]
                with patch("harness.remote.remote_progress.resolve_rrctl", return_value="rrctl"), patch("harness.remote.remote_progress.rrctl_call", side_effect=replies):
                    info = get_progress("RUN-ZERO", self.root)
                self.assertEqual(info["pct"], float(count))
                for mode in ("heartbeat", "detailed"):
                    self.assertIn(f"{count}/100", format_progress(info, mode=mode))

    def test_formatter_handles_legacy_snapshot_without_percentage(self):
        for mode in ("heartbeat", "detailed"):
            text = format_progress({"run_id": "RUN-ZERO", "state": "running",
                                    "current_step": 0, "total_steps": 100, "pct": None}, mode=mode)
            self.assertIn("0/100 (0.0%)", text)

    def test_estimate_reports_source_and_rejects_nonfinite_cost(self):
        from harness.remote.remote_progress import estimate_runtime, format_estimate
        estimate = estimate_runtime(10, marginal_seconds=2)
        self.assertEqual(estimate["estimate_source"], "explicit_input")
        self.assertEqual(estimate["eta_scope"], "compute_only")
        self.assertIn("不含回传", format_estimate(estimate))
        for marginal in (float("nan"), float("inf"), -1, 0):
            with self.subTest(marginal=marginal), self.assertRaises(ValueError):
                estimate_runtime(10, marginal_seconds=marginal)

    def test_estimate_runtime_with_fallback(self):
        from harness.remote.remote_progress import estimate_runtime
        est = estimate_runtime(600, fallback_marginal_seconds=35.0)
        self.assertEqual(est["total_steps"], 600)
        self.assertEqual(est["marginal_seconds_per_step"], 35.0)
        self.assertEqual(est["estimated_total_seconds"], 600 * 35.0)
        self.assertEqual(est["estimate_source"], "fallback_heuristic")

    def test_get_progress_reads_local_cache_when_fresh(self):
        run_dir = Path.home() / ".local/state/rrctl/runs/RUN-TEST-CACHE"
        run_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: [f.unlink() for f in run_dir.glob("*")] and run_dir.rmdir())
        cache_file = run_dir / "latest_progress.json"
        cached_data = {
            "run_id": "RUN-TEST-CACHE",
            "state": "running",
            "current_step": 100,
            "total_steps": 600,
            "pct": 16.666666666666668,
            "cached_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        cache_file.write_text(json.dumps(cached_data), encoding="utf-8")
        info = get_progress("RUN-TEST-CACHE", self.root)
        self.assertEqual(info["run_id"], "RUN-TEST-CACHE")
        self.assertEqual(info["current_step"], 100)
        self.assertEqual(info["total_steps"], 600)


if __name__ == "__main__":
    unittest.main()
