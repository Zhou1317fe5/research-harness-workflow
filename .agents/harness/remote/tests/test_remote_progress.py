"""Unit tests for remote_progress.py."""
from __future__ import annotations

import datetime
import json
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
        self.assertIn("预计剩余: 30.0 分钟", detailed)
        self.assertIn("预计完成时刻:", detailed)

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
        self.assertIn("实际总耗时: 60.0 分钟", detailed)
        self.assertIn("miou=68.2000", detailed)


if __name__ == "__main__":
    unittest.main()
