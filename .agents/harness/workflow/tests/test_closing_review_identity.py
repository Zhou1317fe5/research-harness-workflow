#!/usr/bin/env python3
"""Closing round/material identity and human-facing independence regressions."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))
from harness import reviewer_job
import run_vision_review as vision
from check_handoff_contract import check_contract, load_review_json
from mission_completion import EXPECTED_FIELDS
from test_closing_review_model import base_result


class ClosingFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "closing@example.invalid")
        self.git("config", "user.name", "Closing tests")
        self.csv = self.root / "issues/T/tasks.csv"
        self.csv.parent.mkdir(parents=True)
        self.rows = [dict.fromkeys(EXPECTED_FIELDS, "") for _ in range(2)]
        self.rows[0].update(id="IMPL-01", refs="code.py", dev_state="已完成")
        self.rows[1].update(id="REVIEW-01", area="review", notes="review_kind:vision; review_result:pending")
        self.write_csv()
        (self.root / "code.py").write_text("value = 1\n")
        (self.root / ".gitignore").write_text("ignored/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.args = Namespace(
            csv=str(self.csv), source_doc=None, claim_ledger=None, outcome_contract=None,
            deferred_ledger=None, review_log=str(self.csv.with_suffix(".review.md")),
            output="reviews/review.json", handoff="tasks.handoff.md", extra=None,
            backend="pi", model="fixture/model",
        )

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, text=True,
                              capture_output=True, check=True).stdout.strip()

    def write_csv(self):
        with self.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerows(self.rows)

    def prepare(self):
        return vision.prepare_closing_job(self.args, self.root, "read-only closing task")

    def payload(self, **overrides):
        result = base_result(
            reviewer_id="fixture-independent", report_markdown="Synthetic fixture review only.",
            scientific_outcome="inconclusive", result="limited_review",
            claim_coverage="unknown", claim_coverage_status="unknown",
            validation_limited=["Synthetic local fixture; no real research evidence assessed."],
            handoff_markdown=("# Fixture -- 施工交工单\n\n## 总结\n仅验证本地合成链路。\n"
                              "| 目标 | 结果 |\n|---|---|\n| 本地链路 | 已验证 |\n"
                              "## 验证情况\n未调用真实模型或远端服务。\n"),
        )
        result.update(overrides)
        return {key: value for key, value in result.items() if not key.startswith("review_")}

    def fake_pi(self, payload, *, attest=True):
        fake = self.root / ".git/fake-pi"
        fake.write_text(
            "#!/usr/bin/env python3\nimport json,sys,pathlib,os\n"
            "session=pathlib.Path(sys.argv[sys.argv.index('--session')+1])\n"
            f"session.write_text({(json.dumps({'type': 'model_change', 'provider': 'fixture', 'modelId': 'model'}) + chr(10) if attest else '')!r})\n"
            "(session.parent/'fake-child.json').write_text(json.dumps({'pid':os.getpid(),'argv':sys.argv}))\n"
            f"print({json.dumps(payload)!r})\n"
        )
        fake.chmod(0o755)
        return fake

    def launch(self, fake):
        argv = ["run_vision_review.py", "--csv", str(self.csv), "--workdir", str(self.root),
                "--backend", "pi", "--model", "fixture/model", "--output", self.args.output,
                "--handoff", self.args.handoff, "--review-log", self.args.review_log]
        if self.args.outcome_contract:
            argv += ["--outcome-contract", self.args.outcome_contract]
        original_which = reviewer_job.shutil.which
        with patch.object(sys, "argv", argv), patch.object(
            reviewer_job.shutil, "which",
            side_effect=lambda cmd: str(fake) if cmd == "pi" else original_which(cmd),
        ), patch.object(vision, "detect_session_model", return_value="fixture/model"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = vision.main()
        return code, stderr.getvalue()




class ClosingIdentityTests(ClosingFixture):
    def test_round_two_does_not_conflict_and_preserves_old_job(self):
        first, packet, _ = self.prepare()
        original = packet.read_bytes()
        self.rows.append(dict(self.rows[-1], id="REVIEW-02"))
        self.write_csv()
        second, second_packet, _ = self.prepare()
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(second_packet.read_text())["review_row_id"], "REVIEW-02")
        self.assertEqual(packet.read_bytes(), original)
        self.assertEqual(self.prepare()[0], second)

    def test_committed_material_change_invalidates_but_empty_commit_does_not(self):
        before = self.prepare()[0]
        self.git("commit", "--allow-empty", "-qm", "bookkeeping")
        self.assertEqual(self.prepare()[0], before)
        (self.root / "code.py").write_text("value = 2\n")
        changed = self.prepare()[0]
        self.assertNotEqual(changed, before)
        self.git("add", "code.py")
        self.git("commit", "-qm", "changed implementation")
        self.assertEqual(self.prepare()[0], changed)
        self.assertTrue(before.is_dir())

    def test_outputs_current_review_state_log_and_events_do_not_self_invalidate(self):
        before = self.prepare()[0]
        (self.csv.parent / self.args.output).write_text('{"result":"vision_met"}')
        (self.csv.parent / self.args.handoff).write_text("generated handoff")
        Path(self.args.review_log).write_text("## REVIEW-01\nThis round's report\n")
        self.csv.with_suffix(".events.json").write_text(json.dumps([
            {"row_id": "REVIEW-01", "event": {"phase": "close"}},
        ]))
        self.csv.with_suffix(".progress.md").write_text("derived progress")
        self.csv.with_name("." + self.csv.name + ".lock").touch()
        self.rows[-1].update(
            dev_state="已完成", review_initial_state="已完成", review_regression_state="已完成",
            git_state="已提交", commit_hash="a" * 40, updated_at="later", next_action="done",
            refs="reviews/review.json; tasks.handoff.md",
            notes="review_kind:vision; review_result:vision_met; review_json:reviews/review.json; "
                  "handoff:tasks.handoff.md; event:tasks.events.json#abc",
        )
        self.write_csv()
        self.git("add", ".")
        self.git("commit", "-qm", "closing bookkeeping")
        self.assertEqual(self.prepare()[0], before)
        self.rows[-1]["acceptance_criteria"] = "a genuinely new scope"
        self.write_csv()
        self.assertNotEqual(self.prepare()[0], before)

    def test_other_rows_and_prior_review_evidence_still_invalidate(self):
        before = self.prepare()[0]
        self.rows[0]["notes"] = "evidence:changed"
        self.write_csv()
        self.assertNotEqual(self.prepare()[0], before)
        Path(self.args.review_log).write_text("## REVIEW-00\nPrevious finding\n")
        old = self.prepare()[0]
        Path(self.args.review_log).write_text("## REVIEW-00\nChanged previous finding\n")
        self.assertNotEqual(self.prepare()[0], old)

    def test_git_ignored_explicit_evidence_is_content_bound(self):
        evidence = self.root / "ignored/trace.json"
        evidence.parent.mkdir()
        evidence.write_text('{"metric":1}')
        self.args.claim_ledger = str(self.csv.parent / "claims.json")
        Path(self.args.claim_ledger).write_text(json.dumps({
            "claims": [{"evidence_refs": ["ignored/trace.json:1"]}],
        }))
        before = self.prepare()[0]
        evidence.write_text('{"metric":2}')
        self.assertNotEqual(self.prepare()[0], before)

    def test_explicit_ignored_run_directory_binds_manifest_and_actual_results(self):
        run = self.root / "remote_artifacts/EXP/RUN-01"
        run.mkdir(parents=True)
        (self.root / ".gitignore").write_text("ignored/\nremote_artifacts/\n")
        summary = run / "summary.json"
        summary.write_text('{"metric":1}')
        manifest = run / "artifact_manifest.json"
        manifest.write_text(json.dumps({"entries": [{"path": "summary.json", "sha256": "stale-digest"}]}))
        self.rows[0]["artifact_path"] = "remote_artifacts/EXP/RUN-01/"
        self.write_csv()
        before, packet, _ = self.prepare()
        old_packet = packet.read_bytes()
        self.assertIn("remote_artifacts/EXP/RUN-01/summary.json", json.loads(old_packet)["materials"])
        summary.write_text('{"metric":999}')  # Unchanged manifest must not hide changed bytes.
        changed = self.prepare()[0]
        self.assertNotEqual(changed, before)
        self.assertEqual(packet.read_bytes(), old_packet)
        self.assertEqual(self.prepare()[0], changed)
        manifest.write_text(json.dumps({"entries": [{"path": "summary.json"}], "revision": 2}))
        self.assertNotEqual(self.prepare()[0], changed)
        manifest.write_text(json.dumps({"entries": [{"path": ".env.secret"}]}))
        secret = run / ".env.secret"
        secret.write_text("synthetic fixture")
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.resolve() == secret:
                raise AssertionError("must not open secret manifest member")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            with self.assertRaisesRegex(ValueError, "unsafe closing review material"):
                self.prepare()
        manifest.write_text(json.dumps({"entries": [{"path": "../outside.json"}]}))
        with self.assertRaisesRegex(ValueError, "escapes root"):
            self.prepare()
        manifest.unlink()
        with self.assertRaisesRegex(ValueError, "requires artifact_manifest"):
            self.prepare()

    def test_secret_and_unrelated_artifacts_are_never_opened(self):
        before = self.prepare()[0]
        names = (".env.local", "profiles.json", "id_ed25519", "private.pem", "secrets.yaml")
        forbidden = set()
        for name in names:
            path = self.root / name
            path.write_text("synthetic non-secret fixture\n")
            forbidden.add(path)
        (self.root / "alias.py").symlink_to(self.root / "private.pem")
        for name in ("unrelated.bin", "results/old.py", "data/huge.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                handle.truncate(64 * 1024 * 1024)
            forbidden.add(path)
        self.git("add", ".")  # Also protect tracked credential/artifact names.
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.resolve() in forbidden:
                raise AssertionError(f"must not open excluded material: {path.name}")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            self.assertEqual(self.prepare()[0], before)
            for name in (*names, "alias.py"):
                self.args.extra = name
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe closing review material"):
                    self.prepare()
            self.args.extra = None
            self.args.claim_ledger = str(self.root / "profiles.json")
            with self.assertRaisesRegex(ValueError, "unsafe closing review material"):
                self.prepare()

    def test_long_display_slugs_remain_usable_and_exact_ids_still_differ(self):
        self.csv = self.csv.with_name("mission-" + "x" * 150 + ".csv")
        self.args.csv = str(self.csv)
        self.args.review_log = str(self.csv.with_suffix(".review.md"))
        self.rows[-1]["id"] = "REVIEW-" + "y" * 150
        self.write_csv()
        first, packet, _ = self.prepare()
        self.assertLessEqual(len(os.fsencode(first.name)), 255)
        self.assertEqual(self.prepare()[0], first)
        old_packet = packet.read_bytes()
        self.rows[-1]["id"] += "2"  # Same truncated display name, different exact round.
        self.write_csv()
        second = self.prepare()[0]
        self.assertNotEqual(first, second)
        self.assertEqual(packet.read_bytes(), old_packet)

    def test_model_backend_and_prompt_are_part_of_identity(self):
        before = self.prepare()[0]
        self.args.model = "different/model"
        self.assertNotEqual(self.prepare()[0], before)
        self.args.model = "fixture/model"
        self.args.backend = "codex"
        self.assertNotEqual(self.prepare()[0], before)
        self.args.backend = "pi"
        self.assertNotEqual(vision.prepare_closing_job(self.args, self.root, "changed task")[0], before)

    def test_conflicting_packet_or_task_is_rejected_without_overwrite(self):
        _, packet, task = self.prepare()
        original_packet = packet.read_text()
        packet.write_text("{}")
        with self.assertRaisesRegex(ValueError, "identity conflicts"):
            self.prepare()
        self.assertEqual(packet.read_text(), "{}")
        packet.write_text(original_packet)
        task.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "identity conflicts"):
            self.prepare()
        self.assertEqual(task.read_text(), "tampered")

    def test_real_reviewer_job_reuses_same_verdict_and_retains_budget(self):
        job, packet, task = self.prepare()
        fake = self.root / ".git/fake-pi"
        fake.write_text("#!/usr/bin/env python3\nimport json,sys,pathlib\n"
                        "session=pathlib.Path(sys.argv[sys.argv.index('--session')+1])\n"
                        "session.write_text(json.dumps({'type':'model_change','provider':'fixture',"
                        "'modelId':'model'})+'\\n')\n"
                        f"print({json.dumps(self.payload())!r})\n")
        fake.chmod(0o755)
        args = Namespace(packet=packet, task=task, job_dir=job, backend="pi", model="fixture/model",
                         model_source="invoker", review_kind="closing", cwd=None,
                         max_resumes=0, max_replacements=0, attempt_timeout_seconds=10)
        original_which = reviewer_job.shutil.which
        with patch.object(reviewer_job.shutil, "which", side_effect=lambda cmd: str(fake) if cmd == "pi" else original_which(cmd)):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reviewer_job.execute(args), 0)
                verdict = (job / "verdict.json").read_bytes()
                self.assertEqual(self.prepare()[0], job)
                self.assertEqual(reviewer_job.execute(args), 0)
            self.assertEqual((job / "verdict.json").read_bytes(), verdict)
            state = json.loads((job / "job.json").read_text())
            self.assertEqual(state["invocations"], 1)
            # A changed material gets another identity, not the old verdict.
            (self.root / "code.py").write_text("value = 3\n")
            new_job, _, _ = self.prepare()
            self.assertNotEqual(new_job, job)
            self.assertFalse((new_job / "verdict.json").exists())
            # Removing a verdict does not reset the persisted invocation budget.
            (job / "verdict.json").unlink()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertNotEqual(reviewer_job.execute(args), 0)
            self.assertEqual(json.loads((job / "job.json").read_text())["invocations"], 1)


class ClosingDeliveryTests(ClosingFixture):
    def outcome(self):
        contract = {
            "source": "code.py", "execution_scope": "Synthetic local delivery fixture",
            "artifact_role": {"kind": "task", "producer": "fixture", "authority": "local only",
                              "not_authority": "research conclusion", "consumers": ["test"]},
            "desired_effects": [{"id": "EFFECT-001", "statement": "Local handoff delivery", "source_ref": "code.py"}],
            "reader_questions": [{"id": "OUTCOME-001", "question": "真实科学效果是否已验证？",
                                  "why_it_matters": "Do not invent conclusions", "scope": "research",
                                  "source_ref": "code.py", "evidence_required": "real_e2e", "status": "pending"}],
            "decisive_result": {"question": "Can delivery complete locally?", "success_condition": "JSON and handoff",
                                "failure_condition": "Missing artifacts", "source_refs": ["code.py"]},
            "blocked_claims": [{"claim": "真实模型效果已确认", "reason": "本测试未运行真实模型",
                                "release_condition": "取得真实运行证据", "source_ref": "code.py"}],
        }
        path = self.csv.parent / "outcome.json"
        path.write_text(json.dumps(contract))
        self.args.outcome_contract = str(path)
        self.rows[-1]["notes"] += "; outcome_contract:outcome.json"
        self.write_csv()
        answer = {"question_id": "OUTCOME-001", "verdict": "unknown", "answer": "未验证真实科学效果",
                  "evidence_refs": ["code.py"], "confidence": "unknown", "boundary": "仅本地合成链路",
                  "next_action": "取得真实运行证据"}
        body = self.payload()["handoff_markdown"]
        body += ("## 这份交工单告诉你什么\n本地测试，不代表科学结论。\n"
                 "## 你现在可以确定什么\n"
                 "| 你关心的问题 | 判定 | 直接答案 | 证据 | 可信度 | 边界 | 下一步 |\n"
                 "|---|---|---|---|---|---|---|\n"
                 "| 真实科学效果是否已验证？ | unknown | 未验证真实科学效果 | code.py | unknown | 仅本地合成链路 | 取得真实运行证据 |\n"
                 "## 决定整体状态的结果\n未取得真实科学证据。\n"
                 "## 目前仍不能声称什么\n"
                 "| 声称 | 原因 | 放行条件 |\n|---|---|---|\n"
                 "| 真实模型效果已确认 | 本测试未运行真实模型 | 取得真实运行证据 |\n")
        return contract, self.payload(outcome_answers=[answer], handoff_markdown=body)

    def assert_delivery(self, payload, contract=None):
        fake = self.fake_pi(payload)
        code, stderr = self.launch(fake)
        self.assertEqual(code, 0, stderr)
        jobs = list((self.csv.parent / "reviews").glob("closing-reviewer-job-*"))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        child = json.loads((job / "fake-child.json").read_text())
        self.assertNotEqual(child["pid"], os.getpid())  # Actual transport subprocess, not mocked execute.
        self.assertIn("--no-context-files", child["argv"])
        self.assertEqual(child["argv"][child["argv"].index("--model") + 1], "fixture/model")
        verdict = json.loads((job / "verdict.json").read_text())
        result_path = self.csv.parent / self.args.output
        result = json.loads(result_path.read_text())
        for key, value in payload.items():
            self.assertEqual(verdict[key], value, key)
            if key != "handoff_markdown":
                self.assertEqual(result[key], value, key)
        self.assertEqual(vision.validate_review_result(result, contract), [])
        handoff = self.csv.parent / self.args.handoff
        self.assertEqual(handoff.read_text().rstrip(), result["handoff_markdown"].rstrip())
        self.assertEqual(result["review_model_evidence"], "session-metadata")
        self.assertEqual(result["review_observed_model"], "fixture/model")
        self.rows[-1]["notes"] = self.rows[-1]["notes"].replace("review_result:pending", "review_result:" + result["result"])
        self.rows[-1]["notes"] += "; review_json:reviews/review.json; handoff:tasks.handoff.md"
        self.write_csv()
        self.assertEqual(check_contract(handoff, self.csv, workdir=self.root), [])
        # Delivered output/CSV bookkeeping do not self-invalidate material identity.
        before = (job / "verdict.json").read_bytes()
        self.assertEqual(self.launch(fake)[0], 0)
        self.assertEqual((job / "verdict.json").read_bytes(), before)
        self.assertEqual(json.loads((job / "job.json").read_text())["invocations"], 1)
        self.assertEqual(len(list(job.parent.glob("closing-reviewer-job-*"))), 1)
        (self.root / "code.py").write_text("value = 2\n")
        self.assertEqual(self.launch(fake)[0], 0)
        self.assertEqual(len(list(job.parent.glob("closing-reviewer-job-*"))), 2)
        self.assertEqual((job / "verdict.json").read_bytes(), before)

    def test_launcher_to_real_local_pi_delivers_without_outcome(self):
        self.assert_delivery(self.payload())

    def test_launcher_to_real_local_pi_delivers_with_outcome(self):
        contract, payload = self.outcome()
        self.assert_delivery(payload, contract)

    def test_launcher_delivers_negative_scientific_outcome_as_execution_complete(self):
        # No claim ledger in this compatibility fixture: the fake reviewer reports
        # zero claims, not an inferred passing coverage value from the launcher.
        self.assert_delivery(self.payload(result="vision_met", claim_coverage="0/0",
                                          claim_coverage_status="complete",
                                          scientific_outcome="hypothesis_not_supported"))

    def test_launcher_preserves_actionable_gap_details(self):
        gap = {"id": "FOLLOWUP-01", "title": "Synthetic missing evidence",
               "source_ref": "code.py:1", "evidence_ref": "code.py:1",
               "why_it_matters": "Fixture tests gap transport", "suggested_followup_issue": "Inspect fixture evidence"}
        self.assert_delivery(self.payload(result="gaps_found", claim_coverage="0/1",
                                          claim_coverage_status="gaps", gaps=[gap]))

    def test_missing_outcome_answer_is_not_filled_by_launcher(self):
        _, payload = self.outcome()
        payload["outcome_answers"] = []
        code, stderr = self.launch(self.fake_pi(payload))
        self.assertNotEqual(code, 0)
        self.assertIn("missing outcome answer: OUTCOME-001", stderr)
        self.assertFalse((self.csv.parent / self.args.output).exists())
        self.assertFalse((self.csv.parent / self.args.handoff).exists())

    def test_no_runtime_model_evidence_cannot_deliver(self):
        code, stderr = self.launch(self.fake_pi(self.payload(), attest=False))
        self.assertNotEqual(code, 0)
        self.assertFalse((self.csv.parent / self.args.output).exists())
        self.assertFalse((self.csv.parent / self.args.handoff).exists())

    def test_lean_payload_requires_re_review_without_fabricating_fields(self):
        lean = {"reviewer_id": "legacy", "result": "pass", "report_markdown": "old report", "gaps": []}
        code, stderr = self.launch(self.fake_pi(lean))
        self.assertNotEqual(code, 0)
        self.assertFalse((self.csv.parent / self.args.output).exists())
        self.assertFalse((self.csv.parent / self.args.handoff).exists())
        self.assertIn("rerun review", reviewer_job.validate_response(lean, "closing", None, {}))
        self.assertFalse(list((self.csv.parent / "reviews").glob("*/verdict.json")))

    def test_canonical_schema_prompt_and_nested_shapes_match(self):
        schema = reviewer_job.output_schema("closing")
        self.assertEqual(set(schema["required"]), set(self.payload()))
        prompt = reviewer_job.reviewer_prompt("task", "closing")
        self.assertIn(json.dumps(schema, ensure_ascii=False, sort_keys=True), prompt)
        for result in ("vision_met", "gaps_found", "limited_review"):
            self.assertIsNone(reviewer_job.validate_response(self.payload(result=result), "closing", None, {}))
        _, payload = self.outcome()
        del payload["outcome_answers"][0]["evidence_refs"]
        self.assertIn("outcome_answers[0]", reviewer_job.validate_response(payload, "closing", None, {}))

    def test_cached_lean_job_is_preserved_and_not_reused(self):
        job, packet, task = self.prepare()
        lean = {"schema_version": reviewer_job.VERDICT_SCHEMA_CLOSING,
                "packet_sha256": reviewer_job.sha256_bytes(reviewer_job.canonical_json(json.loads(packet.read_text()))),
                "task_sha256": reviewer_job.sha256_bytes(task.read_bytes()),
                "reviewer_id": "legacy", "result": "pass", "report_markdown": "old", "gaps": []}
        verdict = job / "verdict.json"
        verdict.write_text(json.dumps(lean))
        before = verdict.read_bytes()
        args = Namespace(packet=packet, task=task, job_dir=job, backend="pi", model="fixture/model",
                         model_source="invoker", review_kind="closing", cwd=None,
                         max_resumes=0, max_replacements=0, attempt_timeout_seconds=10)
        with self.assertRaisesRegex(ValueError, "preserve this job"):
            reviewer_job.execute(args)
        self.assertEqual(verdict.read_bytes(), before)


class HandoffIdentityTests(ClosingFixture):
    def review(self, **overrides):
        review = base_result(**overrides)
        review["handoff_markdown"] = vision.render_handoff_identity(
            "# Test -- 施工交工单\n\n## 总结\n| 目标 | 结果 |\n|---|---|\n| A | 已完成 |\n"
            "## 验证情况\nfixture\n", review)
        return review

    def handoff_fixture(self, review):
        review_path = self.csv.parent / "review.json"
        review_path.write_text(json.dumps(review))
        handoff = self.csv.with_suffix(".handoff.md")
        handoff.write_text(review["handoff_markdown"])
        self.rows[-1]["notes"] = (
            "review_json:review.json; review_result:vision_met; handoff:tasks.handoff.md"
        )
        self.write_csv()
        return handoff

    def test_structured_independent_header_passes_without_outcome_contract(self):
        review = self.review()
        handoff = self.handoff_fixture(review)
        self.assertEqual(check_contract(handoff, self.csv, workdir=self.root), [])
        self.assertEqual(vision.validate_handoff_identity(review["handoff_markdown"], review), [])

    def test_warning_plus_independence_true_is_rejected_even_without_json(self):
        handoff = self.csv.with_suffix(".handoff.md")
        handoff.write_text("WARNING: self-review only, NOT independently verified\n" + self.review()["handoff_markdown"])
        self.rows[-1]["notes"] = "handoff:tasks.handoff.md"
        self.write_csv()
        self.assertTrue(any("WARNING" in e for e in check_contract(handoff, self.csv, workdir=self.root)))

    def test_json_header_csv_conflicts_and_wrong_round_are_rejected(self):
        review = self.review(review_agent_mode="self-review", review_independence=False)
        handoff = self.handoff_fixture(review)
        self.assertEqual(check_contract(handoff, self.csv, workdir=self.root), [])
        handoff.write_text(review["handoff_markdown"].replace("独立性: false", "独立性: true"))
        self.assertTrue(any("independence differs" in e for e in check_contract(handoff, self.csv, workdir=self.root)))
        handoff.write_text(review["handoff_markdown"])
        self.rows[-1]["notes"] += "; review_independence:true"
        self.write_csv()
        self.assertTrue(any("CSV review_independence" in e for e in load_review_json(self.csv, workdir=self.root)[1]))
        review["review_row_id"] = "REVIEW-02"
        (self.csv.parent / "review.json").write_text(json.dumps(review))
        self.assertTrue(any("different REVIEW row" in e for e in load_review_json(self.csv, workdir=self.root)[1]))

    def test_generator_rejects_conflicting_identity_but_adds_absent_header(self):
        review = self.review()
        self.assertIn("observed_model=", review["handoff_markdown"])
        for text in (
            "> 独立性: false | mode=self-review | requested_model=x\n",
            "WARNING: self-review only, NOT independently verified\n",
            review["handoff_markdown"] + "\n" + review["handoff_markdown"],
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    vision.render_handoff_identity(text, review)


if __name__ == "__main__":
    unittest.main()
