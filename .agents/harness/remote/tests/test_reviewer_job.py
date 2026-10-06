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
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            calls.append(argv)
            if len(calls) == 1:
                on_session("session-fixture")
                return 1, '{"type":"thread.started","thread_id":"session-fixture"}\n', False, ""
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({
                "reviewer_id": "independent-fixture",
                "review_mode": "scientific_review",
                "result": "scientifically_correct",
                "decision": "allow_run",
                "report_markdown": "No blockers.",
            }))
            return 0, "", False, ""

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
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            on_session("session-fixture")
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({
                "reviewer_id": "independent-fixture", "review_mode": "scientific_review",
                "result": "not_evaluable", "decision": "do_not_run",
                "report_markdown": "Missing evidence.",
            }))
            return 0, "", False, ""
        with patch.object(reviewer_job, "validate_packet", return_value=(json.loads(self.packet.read_text()), "f" * 64)), patch.object(reviewer_job.shutil, "which", return_value="/fixture/codex"), patch.object(reviewer_job, "run_process", side_effect=run):
            self.assertEqual(reviewer_job.execute(self.args()), 0)
            self.assertEqual(reviewer_job.execute(self.args()), 0)
        self.assertTrue((self.job / "verdict.json").is_file())

    def test_runner_rejects_packet_that_did_not_pass_readiness(self):
        with self.assertRaisesRegex(ValueError, "review packet is not ready"):
            reviewer_job.validate_packet(self.packet, "prerun")

    def test_pi_argv_keeps_read_only_constraints_and_shared_model_registry(self):
        argv, _ = reviewer_job.command_for(
            "pi", self.root, self.job, 0, None, self.job / "response.json",
            self.job / "schema.json", self.task, "xiaojimao/gpt-6-astra:high",
        )
        # 审查侧只保留只读约束；不禁用扩展发现，以便与 /model 使用同一套 provider
        # 注册表（扩展注册的模型也要能直接当审查模型）。
        self.assertEqual(argv[argv.index("--tools") + 1], "read,grep,find,ls")
        self.assertIn("--no-skills", argv)
        self.assertIn("--no-context-files", argv)
        self.assertNotIn("--no-extensions", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "xiaojimao/gpt-6-astra:high")

    def test_execute_uses_canonical_model_when_launcher_omits_it(self):
        seen = {}

        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
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
            }), False, ""

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
        self.assertEqual(verdict["model_evidence"], "session-metadata")

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
            json.dumps({"type": "thread.started", "model": "gpt-6.1-sol"}) + "\n",
            encoding="utf-8",
        )
        session = self.root / "pi-session.jsonl"
        session.write_text(
            json.dumps({"type": "model_change", "provider": "xiaojimao",
                        "modelId": "gpt-6-astra"}) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            reviewer_job.observed_model_for_backend("codex", events, session),
            ("gpt-6.1-sol", "event-stream"),
        )
        self.assertEqual(
            reviewer_job.observed_model_for_backend("pi", events, session),
            ("xiaojimao/gpt-6-astra", "session-metadata"),
        )

    def test_codex_observed_model_falls_back_to_rollout_turn_context(self):
        thread_id = "01a0e636-6f48-7251-8318-0dac6b63bbe3"
        sessions = self.root / "codex-home" / "sessions" / "2026" / "09" / "28"
        sessions.mkdir(parents=True)
        rollout = sessions / f"rollout-2026-09-28T12-12-02-{thread_id}.jsonl"
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": {"session_id": thread_id}}) + "\n"
            + json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra"}}) + "\n",
            encoding="utf-8",
        )
        empty_events = self.root / "events-empty.jsonl"
        empty_events.write_text(
            json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root / "codex-home")}):
            self.assertEqual(
                reviewer_job.find_codex_rollout(thread_id), rollout,
            )
            self.assertEqual(
                reviewer_job.observed_model_for_backend(
                    "codex", empty_events, self.root / "missing.jsonl", thread_id,
                ),
                ("gpt-6-astra", "session-metadata"),
            )
        # stdout 事件流已有可信身份时不读 rollout。
        events = self.root / "events.jsonl"
        events.write_text(
            json.dumps({"type": "thread.started", "model": "gpt-6.1-sol"}) + "\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root / "codex-home")}):
            self.assertEqual(
                reviewer_job.observed_model_for_backend(
                    "codex", events, self.root / "missing.jsonl", thread_id,
                ),
                ("gpt-6.1-sol", "event-stream"),
            )
        # rollout 中的 reviewer 消息文本不能伪造身份；未知 thread 返回 None。
        forged = sessions / f"rollout-2026-09-28T12-12-02-{thread_id}.jsonl"
        forged.write_text(
            json.dumps({"type": "response_item", "payload": {
                "type": "message", "content": [{"type": "output_text",
                "text": '"model": "evil-model"'}]}}) + "\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root / "codex-home")}):
            self.assertEqual(
                reviewer_job.observed_model_for_backend(
                    "codex", empty_events, self.root / "missing.jsonl", thread_id,
                ),
                (None, None),
            )
            self.assertIsNone(reviewer_job.find_codex_rollout("unknown-thread"))

    def test_codex_rollout_lookup_escapes_glob_metacharacters(self):
        # thread_id 来自运行时事件；含 glob 元字符的 id 不得匹配到任何文件。
        sessions = self.root / "codex-home" / "sessions" / "2026" / "09" / "28"
        sessions.mkdir(parents=True)
        (sessions / "rollout-2026-09-28T12-12-02-victim.jsonl").write_text(
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra"}}) + "\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"CODEX_HOME": str(self.root / "codex-home")}):
            self.assertIsNone(reviewer_job.find_codex_rollout("*"))
            self.assertIsNone(reviewer_job.find_codex_rollout("victim?"))
            self.assertIsNotNone(reviewer_job.find_codex_rollout("victim"))

    def test_codex_verdict_records_rollout_observed_model(self):
        thread_id = "01a0e636-0000-0000-0000-000000000000"
        sessions = self.root / "codex-home" / "sessions" / "2026" / "09" / "28"
        sessions.mkdir(parents=True)
        (sessions / f"rollout-2026-09-28T00-00-00-{thread_id}.jsonl").write_text(
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra"}}) + "\n",
            encoding="utf-8",
        )

        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            on_session(thread_id)
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({
                "reviewer_id": "independent-fixture", "review_mode": "scientific_review",
                "result": "scientifically_correct", "decision": "allow_run",
                "report_markdown": "No blockers.",
            }))
            return 0, json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n", False, ""

        with patch.dict("os.environ", {"CODEX_HOME": str(self.root / "codex-home")}), patch.object(
            reviewer_job, "validate_packet",
            return_value=(json.loads(self.packet.read_text()), "f" * 64),
        ), patch.object(reviewer_job.shutil, "which", return_value="/fixture/codex"), patch.object(
            reviewer_job, "run_process", side_effect=run,
        ):
            self.assertEqual(reviewer_job.execute(self.args()), 0)
        verdict = json.loads((self.job / "verdict.json").read_text())
        self.assertEqual(verdict["observed_model"], "gpt-6-astra")
        # 回退通道是 rollout 会话文件（session-metadata），不是事件流。
        self.assertEqual(verdict["model_evidence"], "session-metadata")

    def test_valid_verdict_survives_trailing_transport_error(self):
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            on_session("session-fixture")
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({
                "reviewer_id": "independent-fixture", "review_mode": "scientific_review",
                "result": "scientifically_correct", "decision": "allow_run",
                "report_markdown": "Verdict completed before disconnect.",
            }))
            return 1, "", False, ""
        with patch.object(reviewer_job, "validate_packet", return_value=(json.loads(self.packet.read_text()), "f" * 64)), patch.object(reviewer_job.shutil, "which", return_value="/fixture/codex"), patch.object(reviewer_job, "run_process", side_effect=run):
            self.assertEqual(reviewer_job.execute(self.args()), 0)
        verdict = json.loads((self.job / "verdict.json").read_text())
        self.assertEqual(verdict["transport_exit_code"], 1)
        self.assertEqual(verdict["replacement_count"], 0)


    def test_result_analysis_verdict_keeps_response_aliases(self):
        packet = {
            "schema_version": "post-run.result-analysis.v1",
            "exp_id": "fixture-exp",
            "run_ids": ["fixture-run"],
            "csv_path": str(self.root / "mission.csv"),
            "repo_root": str(self.root),
        }
        packet_path = self.root / "result-packet.json"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        args = self.args()
        args.backend = "pi"
        args.packet = packet_path
        args.review_kind = "result-analysis"

        response = {
            "exp_id": "fixture-exp",
            "run_ids": ["fixture-run"],
            "analysis_markdown": "## Change\\nchange\\n\\n## Result\\nresult\\n\\n## Finding\\nfinding\\n\\n## Next\\nnext\\n",
            "scientific_outcome": "inconclusive",
            "limitations": ["fixture limitation"],
            "validation_gaps": ["fixture gap"],
        }

        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            on_session("pi-session-fixture")
            return 0, json.dumps(response), False, ""

        with (
            patch.object(reviewer_job, "validate_packet", return_value=(packet, "f" * 64)),
            patch.object(reviewer_job.shutil, "which", return_value="/fixture/pi"),
            patch.object(reviewer_job, "run_process", side_effect=run),
        ):
            self.assertEqual(reviewer_job.execute(args), 0)

        verdict = json.loads((self.job / "verdict.json").read_text())
        self.assertEqual(verdict["response_path"], verdict["raw_response_path"])
        self.assertEqual(verdict["response_sha256"], verdict["raw_response_sha256"])


if __name__ == "__main__":
    unittest.main()


class ReviewerJobP0Tests(unittest.TestCase):
    """P0: bounded invocations, failure classification, quota wait, no empty files."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="reviewer-job-p0-")
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

    def args(self, **overrides):
        defaults = dict(
            backend="codex", packet=self.packet, task=self.task,
            job_dir=self.job, cwd=None, model=None, max_resumes=3,
            max_replacements=1, attempt_timeout_seconds=30,
        )
        defaults.update(overrides)
        return Namespace(**defaults)

    def _patched(self, run_side_effect):
        return (
            patch.object(reviewer_job, "validate_packet",
                         return_value=(json.loads(self.packet.read_text()), "f" * 64)),
            patch.object(reviewer_job.shutil, "which", return_value="/fixture/codex"),
            patch.object(reviewer_job, "run_process", side_effect=run_side_effect),
        )

    def test_no_session_failure_bounded_by_budget(self):
        """No-session immediate failure must stop after budget (not infinite)."""
        calls = []
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            calls.append(argv)
            return 1, "Model ambiguous across providers", False, ""

        v, w, r = self._patched(run)
        with v, w, r:
            code = reviewer_job.execute(self.args())
        self.assertEqual(code, 3)
        self.assertLessEqual(len(calls), 8)  # (1+3)*(1+1) = 8 max

    def test_quota_waiting_short_no_file(self):
        """Quota <=3h returns 4 with no response file."""
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            return 1, 'Error: You have hit your ChatGPT usage limit. Try again in ~172 min.', False, ""

        v, w, r = self._patched(run)
        with v, w, r:
            code = reviewer_job.execute(self.args())
        self.assertEqual(code, 4)
        state = json.loads((self.job / "job.json").read_text())
        self.assertEqual(state["status"], "quota_waiting")
        self.assertIn("retry_at", state["quota_waiting"])
        self.assertEqual(state["quota_waiting"]["model"], reviewer_job.review_model.MODELS["codex"])
        # No empty response-*.json files should be created
        for p in self.job.glob("response-*.json"):
            if p.name != "response-schema.json":
                self.assertGreater(p.stat().st_size, 0, f"empty file: {p}")

    def test_quota_waiting_exceeds_3h_gives_capability_gap(self):
        """Quota >3h (monthly) hits capability_gap immediately."""
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            return 1, 'Error 429: monthly Clinepass limit, resets in 11d 3h', False, ""

        v, w, r = self._patched(run)
        with v, w, r:
            code = reviewer_job.execute(self.args())
        self.assertEqual(code, 3)
        state = json.loads((self.job / "job.json").read_text())
        self.assertEqual(state["status"], "capability_gap")

    def test_config_error_immediate_stop(self):
        """Model ambiguity config error stops after 1 call (stderr carries the marker)."""
        calls = []
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            calls.append(argv)
            # Real-world model ambiguity appears in stderr increment from the child process.
            with open(_stderr, "a") as f:
                f.write('Error: Model "foo" is ambiguous across providers: a, b. Use --provider or provider/model.\n')
            return 1, "Error: Model \"foo\" is ambiguous across providers: a, b. Use --provider or provider/model.", False, ""

        v, w, r = self._patched(run)
        with v, w, r:
            code = reviewer_job.execute(self.args())
        self.assertEqual(code, 3)
        self.assertEqual(len(calls), 1)

    def test_negative_verdict_not_retried(self):
        """scientifically_incorrect / not_evaluable complete the gate, no retry."""
        calls = []
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            calls.append(argv)
            on_session("session-neg")
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text(json.dumps({
                "reviewer_id": "independent-fixture",
                "review_mode": "scientific_review",
                "result": "not_evaluable",
                "decision": "do_not_run",
                "report_markdown": "Missing baseline equivalence probe.",
            }))
            return 0, "", False, ""

        v, w, r = self._patched(run)
        with v, w, r:
            code = reviewer_job.execute(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        verdict = json.loads((self.job / "verdict.json").read_text())
        self.assertEqual(verdict["result"], "not_evaluable")

    def test_no_empty_response_file_on_failure(self):
        """Empty stdout must not leave a response file; attempts.log instead."""
        def run(argv, _prompt, _events, _stderr, _timeout, on_session, cwd, *_args):
            return 1, "", False, ""

        v, w, r = self._patched(run)
        with v, w, r:
            code = reviewer_job.execute(self.args())
        self.assertEqual(code, 3)
        # No empty response files
        for p in self.job.glob("response-*.json"):
            self.assertGreater(p.stat().st_size, 0, f"empty file: {p}")
        # attempts.log records the failure
        self.assertTrue((self.job / "attempts.log").is_file())
        log_lines = (self.job / "attempts.log").read_text().strip().split("\n")
        self.assertGreaterEqual(len(log_lines), 1)
