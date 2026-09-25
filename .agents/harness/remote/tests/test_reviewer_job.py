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

    def test_pi_argv_always_disables_extension_discovery(self):
        argv, _ = reviewer_job.command_for(
            "pi", self.root, self.job, 0, None, self.job / "response.json",
            self.job / "schema.json", self.task, "openai-codex/gpt-5.6-sol:high",
        )
        # 隔离是无条件不变量：审查进程不执行任何扩展代码，也不能被配置放宽。
        self.assertIn("--no-extensions", argv)
        self.assertNotIn("--extension", argv)
        self.assertIn("--no-skills", argv)
        self.assertIn("--no-context-files", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "read,grep,find,ls")

    def test_execute_uses_canonical_model_when_launcher_omits_it(self):
        seen = {}

        def run(argv, _prompt, _events, _stderr, _timeout, on_session, _cwd):
            seen["argv"] = argv
            on_session("pi-session-fixture")
            (self.job / "pi-session-0.jsonl").write_text(
                json.dumps({"type": "session", "id": "01a0d664"}) + "\n"
                + json.dumps({"type": "message", "role": "assistant", "content": []}) + "\n"
                + json.dumps({"type": "model_change", "provider": "xiaojimao",
                              "modelId": "gpt-6-astra"}) + "\n",
                encoding="utf-8",
            )
            return 0, json.dumps({
                "reviewer_id": "independent-fixture",
                "review_mode": "scientific_review",
                "result": "scientifically_correct",
                "decision": "allow_run",
                "report_markdown": "No blockers.",
            }), False

        args = self.args()
        args.backend = "pi"
        args.model = None
        with patch.object(reviewer_job, "validate_packet", return_value=(json.loads(self.packet.read_text()), "f" * 64)), patch.object(reviewer_job.shutil, "which", return_value="/fixture/pi"), patch.object(reviewer_job, "run_process", side_effect=run):
            self.assertEqual(reviewer_job.execute(args), 0)
        self.assertEqual(
            seen["argv"][seen["argv"].index("--model") + 1],
            reviewer_job.review_model.review_job_model("pi"),
        )
        verdict = json.loads((self.job / "verdict.json").read_text())
        self.assertEqual(
            verdict["requested_model"], reviewer_job.review_model.review_job_model("pi")
        )
        # 取证来自 Pi 会话事件（provider/modelId 组合），而不是 --mode text 的 stdout。
        self.assertEqual(verdict["observed_model"], "xiaojimao/gpt-6-astra")
        self.assertEqual(verdict["model_evidence"], "event-stream")

    def test_pi_observed_model_reads_only_runtime_model_change_events(self):
        cases = {
            "provider 前缀缺失时拼接": (
                {"type": "model_change", "provider": "xiaojimao", "modelId": "gpt-6-astra"},
                "xiaojimao/gpt-6-astra",
            ),
            "modelId 自带前缀时不再拼接": (
                {"type": "model_change", "provider": "clinePass",
                 "modelId": "cline-pass/deepseek-v4.1-flash"},
                "cline-pass/deepseek-v4.1-flash",
            ),
            "无 modelId 的 model_change 不算证据": (
                {"type": "model_change", "provider": "xiaojimao"}, None,
            ),
        }
        for label, (event, expected) in cases.items():
            with self.subTest(label=label):
                path = self.root / "pi-session.jsonl"
                path.write_text(json.dumps(event) + "\n", encoding="utf-8")
                self.assertEqual(reviewer_job.observed_model_from_pi_session(path), expected)

        # reviewer 自己的 message 文本不能伪造身份。
        forged = self.root / "forged.jsonl"
        forged.write_text(
            json.dumps({"type": "message", "role": "assistant",
                        "content": [{"type": "text", "text": "model_change provider=evil"}]}) + "\n",
            encoding="utf-8",
        )
        self.assertIsNone(reviewer_job.observed_model_from_pi_session(forged))
        self.assertIsNone(reviewer_job.observed_model_from_pi_session(self.root / "missing.jsonl"))

    def test_observed_model_source_follows_the_backend(self):
        events = self.root / "events.jsonl"
        events.write_text(
            json.dumps({"type": "thread.started", "model": "gpt-5.6-sol"}) + "\n",
            encoding="utf-8",
        )
        session = self.root / "pi-session.jsonl"
        session.write_text(
            json.dumps({"type": "model_change", "provider": "xiaojimao",
                        "modelId": "gpt-6-astra"}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            reviewer_job.observed_model_for_backend("codex", events, session), "gpt-5.6-sol"
        )
        self.assertEqual(
            reviewer_job.observed_model_for_backend("pi", events, session), "xiaojimao/gpt-6-astra"
        )

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
