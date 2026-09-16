from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


HARNESS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HARNESS.parent))

from harness import reviewer_job  # noqa: E402


class ReviewerJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="reviewer-job-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.packet = self.root / "packet.json"
        self.task = self.root / "task.md"
        self.job = self.root / "job"
        self.packet.write_text(json.dumps({
            "schema_version": "prerun.scientific-review.v1",
            "review_mode": "scientific_review",
            "pre_run_code_commit": "a" * 40,
            "repo_root": str(self.root),
        }))
        self.task.write_text("Review the supplied implementation.\n")

    def args(self):
        return Namespace(
            backend="codex", packet=self.packet, task=self.task,
            job_dir=self.job, cwd=None, model=None, max_resumes=3,
            max_replacements=1, attempt_timeout_seconds=30,
        )

    def test_transport_failure_resumes_same_session_before_replacement(self):
        calls = []
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, _cwd):
            calls.append(argv)
            if len(calls) == 1:
                on_session("session-fixture")
                return 1, '{"type":"thread.started","thread_id":"session-fixture"}\n', False
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({
                "reviewer_id": "independent-fixture",
                "review_mode": "scientific_review",
                "result": "scientifically_correct",
                "decision": "allow_run",
                "report_markdown": "No blockers.",
            }))
            return 0, "", False

        with patch.object(reviewer_job, "validate_packet", return_value=(json.loads(self.packet.read_text()), "f" * 64)), patch.object(reviewer_job.shutil, "which", return_value="/fixture/codex"), patch.object(reviewer_job, "run_process", side_effect=run):
            self.assertEqual(reviewer_job.execute(self.args()), 0)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("resume", calls[0])
        self.assertIn("resume", calls[1])
        verdict = json.loads((self.job / "verdict.json").read_text())
        self.assertEqual(verdict["reviewer_session_id"], "session-fixture")
        self.assertEqual(verdict["replacement_count"], 0)
        self.assertEqual(verdict["resume_count"], 1)

    def test_existing_verdict_is_idempotent(self):
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, _cwd):
            on_session("session-fixture")
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({
                "reviewer_id": "independent-fixture", "review_mode": "scientific_review",
                "result": "not_evaluable", "decision": "do_not_run",
                "report_markdown": "Missing evidence.",
            }))
            return 0, "", False
        with patch.object(reviewer_job, "validate_packet", return_value=(json.loads(self.packet.read_text()), "f" * 64)), patch.object(reviewer_job.shutil, "which", return_value="/fixture/codex"), patch.object(reviewer_job, "run_process", side_effect=run):
            self.assertEqual(reviewer_job.execute(self.args()), 0)
            self.assertEqual(reviewer_job.execute(self.args()), 0)
        self.assertTrue((self.job / "verdict.json").is_file())

    def test_runner_rejects_packet_that_did_not_pass_readiness(self):
        with self.assertRaisesRegex(ValueError, "review packet is not ready"):
            reviewer_job.validate_packet(self.packet)

    def test_valid_verdict_survives_trailing_transport_error(self):
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, _cwd):
            on_session("session-fixture")
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({
                "reviewer_id": "independent-fixture", "review_mode": "scientific_review",
                "result": "scientifically_correct", "decision": "allow_run",
                "report_markdown": "Verdict completed before disconnect.",
            }))
            return 1, "", False
        with patch.object(reviewer_job, "validate_packet", return_value=(json.loads(self.packet.read_text()), "f" * 64)), patch.object(reviewer_job.shutil, "which", return_value="/fixture/codex"), patch.object(reviewer_job, "run_process", side_effect=run):
            self.assertEqual(reviewer_job.execute(self.args()), 0)
        verdict = json.loads((self.job / "verdict.json").read_text())
        self.assertEqual(verdict["transport_exit_code"], 1)
        self.assertEqual(verdict["replacement_count"], 0)


if __name__ == "__main__":
    unittest.main()
