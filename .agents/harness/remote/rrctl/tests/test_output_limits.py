"""Unit coverage for bounded control-output capture and truncation markers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remote_run_control.output_limits import (
    CONTROL_OUTPUT_LIMIT,
    OutputCapture,
    TailBuffer,
    _MARKER,
    run_bounded,
)


class TailBufferTests(unittest.TestCase):
    def test_unlimited_buffer_keeps_everything(self):
        buffer = TailBuffer(None)
        buffer.feed(b"a" * 1000)
        self.assertFalse(buffer.truncated)
        self.assertEqual(buffer.bytes(), b"a" * 1000)

    def test_short_input_is_not_truncated(self):
        buffer = TailBuffer(32)
        buffer.feed(b"hello")
        self.assertFalse(buffer.truncated)
        self.assertEqual(buffer.bytes(), b"hello")

    def test_oversized_output_preserves_head_and_tail_with_marker(self):
        limit = 256
        buffer = TailBuffer(limit)
        for index in range(8):
            buffer.feed((f"chunk{index}:" + "x" * 57).encode())
        self.assertTrue(buffer.truncated)
        result = buffer.bytes()
        self.assertEqual(len(result), limit)
        self.assertIn(_MARKER, result)
        self.assertTrue(result.startswith(b"chunk0:"))
        self.assertTrue(result.rstrip(b"x\n").endswith(b"chunk7:").real if False else result.endswith(b"chunk7:" + b"x" * (len(result.rsplit(b"chunk7:", 1)[1]))))

    def test_truncation_across_many_small_feeds_matches_single_feed(self):
        for pattern in ("single", "chunked"):
            buffer = TailBuffer(64)
            if pattern == "single":
                buffer.feed(b"y" * 1000)
            else:
                for _ in range(100):
                    buffer.feed(b"y" * 10)
            self.assertTrue(buffer.truncated)
            self.assertEqual(len(buffer.bytes()), 64)

    def test_total_tracks_fed_bytes(self):
        buffer = TailBuffer(16)
        buffer.feed(b"z" * 100)
        self.assertEqual(buffer.total, 100)


class OutputCaptureTests(unittest.TestCase):
    def test_capture_without_redactor_reports_metadata(self):
        capture = OutputCapture(16)
        capture.feed(b"0123456789" * 4)
        capture.finish()
        metadata = capture.metadata(complete=True)
        self.assertTrue(metadata["truncated"])
        self.assertEqual(metadata["limit_bytes"], 16)
        self.assertEqual(metadata["original_bytes"], 40)
        self.assertEqual(metadata["observed_bytes"], 40)

    def test_incomplete_metadata_hides_original_size(self):
        capture = OutputCapture(16)
        capture.feed(b"data")
        metadata = capture.metadata(complete=False)
        self.assertIsNone(metadata["original_bytes"])
        self.assertEqual(metadata["observed_bytes"], 4)

    def test_redacted_preview_is_separate_from_raw(self):
        class MaskAll:
            def feed(self, data, *, final=False):
                return b"<masked>" if data else b""

        capture = OutputCapture(64, redactor=MaskAll())
        capture.feed(b"password=hunter2")
        capture.finish()
        self.assertIn(b"hunter2", capture.raw.bytes())
        self.assertEqual(capture.preview(), b"<masked>")


class RunBoundedTests(unittest.TestCase):
    def test_successful_command_captures_both_streams(self):
        result = run_bounded(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            timeout_seconds=20,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertIn(b"out", result.stdout.preview())
        self.assertIn(b"err", result.stderr.preview())

    def test_nonzero_exit_is_preserved(self):
        result = run_bounded(
            [sys.executable, "-c", "raise SystemExit(7)"], timeout_seconds=20
        )
        self.assertEqual(result.returncode, 7)
        self.assertFalse(result.timed_out)

    def test_timeout_kills_process_and_marks_capture(self):
        result = run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.2
        )
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)

    def test_oversized_stdout_is_bounded(self):
        result = run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('y' * 1000000)"],
            timeout_seconds=30,
            stdout_limit=1024,
        )
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout.preview()), 1024)
        self.assertTrue(result.stdout.metadata(complete=True)["truncated"])

    def test_stderr_stays_capped_even_when_stdout_is_unlimited(self):
        oversized = CONTROL_OUTPUT_LIMIT + 100_000
        result = run_bounded(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stderr.write('e' * {oversized})",
            ],
            timeout_seconds=30,
            stdout_limit=None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stderr.preview()), CONTROL_OUTPUT_LIMIT)
        self.assertTrue(result.stderr.metadata(complete=True)["truncated"])


if __name__ == "__main__":
    unittest.main()
