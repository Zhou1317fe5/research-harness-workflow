"""Mission 状态事实与引用完整性回归；Git 操作仅发生在临时仓库。"""
import csv
import argparse
from contextlib import chdir
import hashlib
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))
sys.path.insert(0, str(ROOT / ".agents"))
from csv_state import SCHEMA, StateUpdateError, apply_update
from mission_completion import (EXPECTED_FIELDS, parse_note_tags, read_mission_csv,
                                row_terminal_errors, git_completion_errors, csv_completion_errors,
                                resolve_reference_path)
from git_isolation import commit_paths, verify_commit
from final_ready import _read_csv as read_closing_csv, check_final_ready, SCHEMA_VERSION as CLOSING_SCHEMA
from run_vision_review import discover_claim_ledger, resolve_existing_file, artifact_output_path
from check_handoff_contract import (load_review_notes, load_review_json, note_value_matches_handoff,
                                    check_contract, load_outcome_contract)
from harness.workflow.mission_state import update
from validate_deferred_ledger import load_csv_deferred
from validate_claim_ledger import validate_ledger
from ensure_result_analysis_row import ensure_result_analysis_row
from result_analysis import result_analysis_completion_errors


class MissionContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mission-contracts-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "tasks.csv"
        self._home_patch = patch.dict(os.environ, {"HOME": str(self.root)})
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

    def row(self, **values):
        row = dict.fromkeys(EXPECTED_FIELDS, "")
        row.update(id="I-1", dev_state="未开始", review_initial_state="未开始",
                   review_regression_state="未开始", git_state="未提交", remote_state="not_applicable")
        row.update(values)
        return row

    def write_csv(self, rows, fields=EXPECTED_FIELDS):
        with self.path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def git(self, *argv):
        return subprocess.run(["git", *argv], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def init_git(self):
        self.enterContext(chdir(self.root))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness-test@example.invalid")
        (self.root / "code.py").write_text("value = 1\n")
        (self.root / "user.txt").write_text("original\n")
        self.git("add", "code.py", "user.txt")
        self.git("commit", "-m", "fixture baseline")

    def test_all_readers_reject_conflicting_control_tags(self):
        self.write_csv([self.row(id="REVIEW-01", notes="review_result:vision_met; review_result:gaps_found")])
        with self.assertRaisesRegex(ValueError, "notes_conflict"):
            read_mission_csv(self.path)
        errors = []
        read_closing_csv(self.path, "REVIEW-01", errors)
        self.assertTrue(any("notes_conflict" in x for x in errors))
        with self.assertRaisesRegex(ValueError, "notes_conflict"):
            discover_claim_ledger(str(self.path), self.root)
        self.assertTrue(any("notes_conflict" in x for x in load_review_notes(self.path)[1]))
        self.assertTrue(any("notes_conflict" in x for x in load_csv_deferred(self.path, self.root)[2]))

    def test_explicit_upsert_repairs_only_selected_tag_and_preserves_history(self):
        self.write_csv([self.row(notes="pre_run_result:failed; event:a; pre_run_result:pass; event:b; free text")])
        result = apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                             "set_note_tags": {"pre_run_result": "failed"}})
        notes = result["row"]["notes"]
        self.assertEqual(parse_note_tags(notes)["pre_run_result"], "failed")
        self.assertEqual(notes.count("pre_run_result:"), 1)
        for item in ("event:a", "event:b", "free text"):
            self.assertIn(item, notes)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(StateUpdateError, "notes_conflict"):
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                   "append_notes": ["pre_run_result:pass"]})
        self.assertEqual(before, self.path.read_bytes())

    def test_identical_tags_and_repeated_evidence_are_accepted(self):
        tags = parse_note_tags("review_result:pending; evidence:first; review_result:pending; evidence:second")
        self.assertEqual(tags["review_result"], "pending")

    def test_file_references_share_roots_and_reject_ambiguous_names(self):
        self.path = self.root / "issues/T/tasks.csv"
        self.path.parent.mkdir(parents=True)
        review = self.path.parent / "review.json"
        review.write_text('{"result": "vision_met"}')
        self.write_csv([self.row(id="REVIEW-01", notes="review_json:issues/T/review.json; review_result:vision_met")])
        self.assertEqual(load_review_json(self.path, workdir=self.root), ({"result": "vision_met"}, []))
        self.assertTrue(note_value_matches_handoff(str(review), review, self.path, workdir=self.root))
        (self.root / "review.json").write_text("other evidence")
        with self.assertRaisesRegex(ValueError, "reference_ambiguous"):
            resolve_reference_path("review.json", self.path.parent, self.root)
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "reference_ambiguous"):
            resolve_existing_file("review.json", self.root, self.path.parent)
        self.write_csv([self.row(id="REVIEW-01", notes="review_json:review.json; deferred_ledger:review.json")])
        self.assertIn("reference_ambiguous", load_review_json(self.path, workdir=self.root)[1][0])
        self.assertIn("reference_ambiguous", load_csv_deferred(self.path, self.root)[2][0])
        self.assertFalse(note_value_matches_handoff("review.json", review, self.path, workdir=self.root))

    def test_review_handoff_and_outcome_references_cannot_escape_workspace(self):
        outside_temp = tempfile.TemporaryDirectory(prefix="outside-mission-", dir=self.root.parent)
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        evidence = outside / "review.json"
        evidence.write_text('{"result":"vision_met"}')
        handoff = outside / "tasks.handoff.md"
        handoff.write_text("outside workspace")
        (self.root / "escape.json").symlink_to(evidence)
        for value in (str(evidence), f"../{outside.name}/review.json", "escape.json"):
            with self.subTest(value=value):
                self.write_csv([self.row(id="REVIEW-01", notes=f"review_json:{value}; outcome_contract:{value}; review_result:vision_met")])
                with self.assertRaisesRegex(ValueError, "outside_workspace"):
                    resolve_reference_path(value, self.path.parent, self.root)
                self.assertIn("outside_workspace", load_review_json(self.path, workdir=self.root)[1][0])
                self.assertIn("outside_workspace", load_outcome_contract(self.path, workdir=self.root)[1][0])
                self.assertFalse(note_value_matches_handoff(value, evidence, self.path, workdir=self.root))
                with self.assertRaisesRegex(argparse.ArgumentTypeError, "outside_workspace"):
                    resolve_existing_file(value, self.root)
        self.assertTrue(any("outside_workspace" in error for error in check_contract(handoff, self.path, workdir=self.root)))
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "outside_workspace"):
            artifact_output_path(str(outside / "new-review.json"), self.root)

    def test_external_compatibility_workspace_must_be_explicitly_selected(self):
        outside_temp = tempfile.TemporaryDirectory(prefix="selected-mission-", dir=self.root.parent)
        self.addCleanup(outside_temp.cleanup)
        outside = Path(outside_temp.name)
        evidence = outside / "review.json"
        evidence.write_text('{"result":"vision_met"}')
        self.path = outside / "tasks.csv"
        self.write_csv([self.row(id="REVIEW-01", notes="review_json:review.json; review_result:vision_met")], EXPECTED_FIELDS[:19])
        self.assertIn("outside_workspace", load_review_json(self.path, workdir=self.root)[1][0])
        self.assertEqual(load_review_json(self.path, workdir=outside), ({"result": "vision_met"}, []))

    def test_preclosing_excludes_its_own_unfinished_row_without_humanizer_gate(self):
        self.init_git()
        self.path = self.root / "issues/T/tasks.csv"
        self.path.parent.mkdir(parents=True)
        review = self.root / "tasks.review.md"
        review.write_text("本地验证完成。")
        handoff = self.root / "tasks.handoff.md"
        handoff.write_text("# 施工交工单\n独立性: 基于现有证据\n## 总结\n| 目标 | 结果 |\n|---|---|\n| 修改 | 已验证 |\n## 验证情况\n见本地记录。\n")
        closed = self.row(dev_state="已完成", review_initial_state="已完成", review_regression_state="已完成",
                          git_state="已提交", commit_hash=self.git("rev-parse", "HEAD"), refs="code.py")
        self.write_csv([closed, self.row(id="REVIEW-01", notes="handoff:tasks.handoff.md")])
        result = check_final_ready({"schema_version": CLOSING_SCHEMA, "csv_path": str(self.path),
                    "review_row_id": "REVIEW-01", "review_path": str(review), "handoff_path": str(handoff),
                    "protected_paths_clean": True, "closing_context": {
                        "risk_level": "L1", "evidence_conflict": False, "current_scope_gap_suspected": False,
                        "independent_prerun_covered": False, "scientific_contract_changed_since_prerun": False,
                    }}, workdir=self.root)
        self.assertTrue(result["ready"], result["errors"])
        self.assertEqual(check_contract(handoff, self.path, workdir=self.root), [])
        self.assertTrue(any("row_not_closed:REVIEW-01" in x for x in csv_completion_errors(self.path, workdir=self.root)))
        update(self.root, "T", action="register", csv="issues/T/tasks.csv", source_ref="session:user#fixture")
        with self.assertRaisesRegex(ValueError, "尚未闭环"):
            update(self.root, "T", action="transition", status="completed", reason="preclosing ready only",
                   source_ref="session:agent#premature-completion")

    def test_worker_completion_does_not_close_scientific_row(self):
        row = self.row(dev_state="已完成", review_initial_state="已完成", review_regression_state="已完成",
                       git_state="已提交", remote_state="completed", exp_id="EXP-1", notes="artifact_policy:none")
        self.assertTrue(any("remote_state_not_terminal" in x for x in row_terminal_errors(row)))
        row["exp_id"] = ""
        self.assertEqual(row_terminal_errors(row), [])

    def test_compatibility_is_explicit_and_preserves_columns(self):
        self.write_csv([self.row()], EXPECTED_FIELDS[:19])
        with self.assertRaisesRegex(ValueError, "header_mismatch"):
            read_mission_csv(self.path)
        self.assertTrue(any("csv_schema_invalid:header_mismatch" in error for error in csv_completion_errors(self.path, workdir=self.root)))
        compat_errors = csv_completion_errors(self.path, workdir=self.root, allow_compat=True)
        self.assertFalse(any("csv_schema_invalid:header_mismatch" in error for error in compat_errors))
        result = apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1", "set": {"dev_state": "进行中"}})
        self.assertEqual(result["columns"], 19)
        row = self.row(dev_state="已完成", review_initial_state="已完成", review_regression_state="已完成", git_state="已提交")
        row.pop("remote_state")
        self.assertTrue(row_terminal_errors(row))
        self.assertEqual(row_terminal_errors(row, allow_compat=True), [])

    def test_writer_requires_real_commit_and_recovery_rechecks_it(self):
        self.init_git()
        self.write_csv([self.row(refs="code.py")])
        before = self.path.read_bytes()
        with self.assertRaises(StateUpdateError):
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                   "set": {"git_state": "已提交", "commit_hash": "a" * 40}})
        self.assertEqual(before, self.path.read_bytes())
        commit = self.git("rev-parse", "HEAD")
        result = apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                              "set": {"git_state": "已提交", "commit_hash": commit}})
        self.assertEqual(git_completion_errors(self.path, [result["row"]]), [])
        result["row"]["commit_hash"] = "b" * 40
        self.assertTrue(git_completion_errors(self.path, [result["row"]]))

    def test_scoped_commit_preserves_user_index_and_parent(self):
        self.init_git()
        parent = self.git("rev-parse", "HEAD")
        (self.root / "user.txt").write_text("user staged change\n")
        self.git("add", "user.txt")
        before = self.git("diff", "--cached", "--binary")
        (self.root / "code.py").write_text("value = 2\n")
        commit = commit_paths(self.root, [Path("code.py")], "fixture task change")
        self.assertEqual(self.git("rev-parse", commit + "^"), parent)
        self.assertEqual(self.git("diff", "--cached", "--binary"), before)
        self.assertEqual(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", commit), "code.py")
        with self.assertRaisesRegex(ValueError, "path_missing"):
            verify_commit(self.root, commit, [Path("missing.py")])
        blob = self.git("rev-parse", commit + ":code.py")
        with self.assertRaisesRegex(ValueError, "not_commit"):
            verify_commit(self.root, blob)

    def test_commit_failure_preserves_index_and_refuses_overlap(self):
        self.init_git()
        (self.root / "user.txt").write_text("user staged change\n")
        self.git("add", "user.txt")
        before = self.git("diff", "--cached", "--binary")
        with self.assertRaisesRegex(RuntimeError, "staged changes"):
            commit_paths(self.root, [Path("user.txt")], "must not commit")
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        (self.root / "new.py").write_text("value = 2\n")
        with self.assertRaises(RuntimeError):
            commit_paths(self.root, [Path("new.py")], "fixture rejected commit")
        self.assertEqual(self.git("diff", "--cached", "--binary"), before)
        self.assertEqual(self.git("ls-files", "new.py"), "")

    def test_nested_research_commit_has_explicit_repository_identity(self):
        self.init_git()
        workspace = self.root / "research_workspace"
        workspace.mkdir()
        def nested(*args):
            return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=True).stdout.strip()
        nested("init", "-b", "main")
        nested("config", "user.name", "Harness Test")
        nested("config", "user.email", "harness-test@example.invalid")
        (workspace / "analysis.md").write_text("fixture result interpretation")
        nested("add", "analysis.md")
        nested("commit", "-m", "fixture research result")
        row = self.row(phase="artifact", run_id="RUN-1", git_state="已提交", commit_hash=nested("rev-parse", "HEAD"),
                       refs="research_workspace/analysis.md",
                       notes=f"git_repo:research_workspace; pre_run_code_commit:{self.git('rev-parse', 'HEAD')}")
        self.write_csv([row])
        self.assertEqual(git_completion_errors(self.path, [row]), [])
        row["phase"] = "remote"
        self.assertTrue(git_completion_errors(self.path, [row]))
        row["phase"] = "artifact"
        row["notes"] = ""
        self.assertTrue(git_completion_errors(self.path, [row]))

    def claim_fixture(self):
        (self.root / "spec.md").write_text("An approved research question.\n")
        (self.root / "run.log").write_text("fixture execution output\n")
        rows = [self.row(notes="claims:C1; claim_ledger:claims.json; evidence_level:real_e2e; production_path:covered")]
        self.write_csv(rows)
        claim = {"claim_id": "C1", "source_ref": "spec.md:1", "promise": "exercise the production path",
                 "covered_by": ["I-1"], "evidence_required": "real_e2e", "production_path_required": True,
                 "status": "verified", "evidence_refs": ["run.log"]}
        return claim, rows

    def validate_claim(self, claims, rows):
        path = self.root / "claims.json"
        path.write_text(json.dumps({"csv": self.path.name, "claims": claims}))
        return validate_ledger(path, self.path, {"C1"}, rows=rows, workdir=self.root)

    def test_claim_requires_existing_source_issue_and_evidence(self):
        claim, rows = self.claim_fixture()
        self.assertEqual(self.validate_claim([claim], rows), [])
        for key, value in (("source_ref", "missing.md:1"), ("covered_by", ["missing"]), ("evidence_refs", ["missing.log"])):
            with self.subTest(key=key):
                self.assertTrue(self.validate_claim([{**claim, key: value}], rows))
        self.assertTrue(any("duplicate" in x for x in self.validate_claim([claim, claim], rows)))

    def result_analysis_fixture(self, *, include_analysis=True, run_ids=None):
        analysis_path = self.root / "research_workspace/experiments/EXP-1/analysis/analysis.md"
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_text = "## Change\ncode\n\n## Result\nmetric\n\n## Finding\nuncertain\n\n## Next\nrepeat\n"
        analysis_path.write_text(analysis_text, encoding="utf-8")
        run_ids = list(run_ids or ["RUN-1"])
        review_payload = {
            "exp_id": "EXP-1", "run_ids": run_ids,
            "analysis_markdown": analysis_text,
            "scientific_outcome": "inconclusive", "limitations": [], "validation_gaps": [],
        }
        review_output = json.dumps(review_payload, ensure_ascii=False, sort_keys=True)
        session_id = "00000000-0000-0000-0000-000000000001"
        tool_id = "call-test"
        session_path = self.root / ".pi/agent/sessions" / f"fixture_{session_id}.jsonl"
        session_path.parent.mkdir(parents=True, exist_ok=True)
        call_record = {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "toolCall", "name": "subagent", "id": tool_id,
                    "arguments": json.dumps({
                        "agent": "scientific-reviewer", "agentScope": "project",
                        "cwd": str(self.root),
                        "task": "Analyze EXP-1 RUN-1 from raw evidence. result_analysis_targets: "
                                + json.dumps({"exp_id": "EXP-1", "run_ids": run_ids}),
                    }),
                }],
            },
        }
        result_record = {
            "type": "message",
            "message": {
                "role": "toolResult", "toolName": "subagent", "toolCallId": tool_id,
                "details": {"results": [{
                    "agent": "scientific-reviewer", "model": "openai-codex/gpt-5.6-sol:max",
                    "exitCode": 0, "messages": [{
                        "role": "assistant", "content": [{"type": "text", "text": review_output}],
                    }],
                }]},
            },
        }
        session_path.write_text(
            json.dumps(call_record) + "\n" + json.dumps(result_record) + "\n", encoding="utf-8"
        )
        evidence_ref = f"session:{session_id}#tool:{tool_id}"
        output_digest = hashlib.sha256(review_output.strip().encode("utf-8")).hexdigest()
        ordinary_rows = [
            self.row(
                id=run_id, phase="remote", exp_id="EXP-1", run_id=run_id,
                remote_state="ingested", artifact_path=f"remote_artifacts/EXP-1/{run_id}",
            )
            for run_id in run_ids
        ]
        review = self.row(
            id="REVIEW-01", phase="review",
            notes="result_analysis:reviews/result-analysis.json",
        )
        rows = list(ordinary_rows)
        if include_analysis:
            analysis = self.row(
                id="RESULT-ANALYSIS-01", phase="analysis",
                required_skills="post-run-result-analysis",
                notes=(
                    "analysis_kind:post_run; "
                    "result_analysis:reviews/result-analysis.json; "
                    "analysis_agent_mode:scientific-reviewer-subagent; "
                    "analysis_independence:true; "
                    "analysis_requested_model:openai-codex/gpt-5.6-sol; "
                    "analysis_observed_model:openai-codex/gpt-5.6-sol; "
                    "analysis_model_evidence:session-metadata; "
                    f"analysis_model_evidence_ref:{evidence_ref}"
                ),
            )
            rows.append(analysis)
        rows.append(review)
        self.write_csv(rows)
        digest = hashlib.sha256(analysis_path.read_bytes()).hexdigest()
        index = {
            "schema_version": "post-run.result-analysis.v1",
            "status": "complete",
            "analysis_agent_mode": "scientific-reviewer-subagent",
            "analysis_independence": True,
            "requested_model": "openai-codex/gpt-5.6-sol",
            "observed_model": "openai-codex/gpt-5.6-sol",
            "model_evidence": "session-metadata",
            "model_evidence_ref": evidence_ref,
            "entries": [{
                "exp_id": "EXP-1", "run_id": run_id,
                "analysis_path": "research_workspace/experiments/EXP-1/analysis/analysis.md",
                "analysis_sha256": digest, "scientific_outcome": "inconclusive",
                "review_evidence_ref": evidence_ref, "review_output_sha256": output_digest,
                "evidence_refs": [f"command:fixture-{run_id}"], "limitations": [], "validation_gaps": [],
            } for run_id in run_ids],
        }
        index_path = self.root / "reviews/result-analysis.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index), encoding="utf-8")
        return rows, index_path

    def test_result_analysis_allows_multiple_runs_per_exp(self):
        rows, index_path = self.result_analysis_fixture(run_ids=["RUN-1", "RUN-2"])
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertEqual(errors, [])
        data = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [(entry["exp_id"], entry["run_id"]) for entry in data["entries"]],
            [("EXP-1", "RUN-1"), ("EXP-1", "RUN-2")],
        )

    def test_cross_exp_reviewer_reference_reuse_is_rejected(self):
        rows, index_path = self.result_analysis_fixture()
        foreign_analysis = self.root / "research_workspace/experiments/EXP-2/analysis/analysis.md"
        foreign_analysis.parent.mkdir(parents=True, exist_ok=True)
        foreign_analysis.write_text(
            "## Change\ncode\n\n## Result\nmetric\n\n## Finding\nuncertain\n\n## Next\nrepeat\n",
            encoding="utf-8",
        )
        data = json.loads(index_path.read_text(encoding="utf-8"))
        foreign_entry = {
            **data["entries"][0],
            "exp_id": "EXP-2",
            "run_id": "RUN-2",
            "analysis_path": "research_workspace/experiments/EXP-2/analysis/analysis.md",
            "analysis_sha256": hashlib.sha256(foreign_analysis.read_bytes()).hexdigest(),
        }
        data["entries"].append(foreign_entry)
        rows = [
            rows[0],
            self.row(
                id="RUN-2", phase="remote", exp_id="EXP-2", run_id="RUN-2",
                remote_state="ingested", artifact_path="remote_artifacts/EXP-2/RUN-2",
            ),
            *rows[1:],
        ]
        self.write_csv(rows)
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("review_evidence_reused_across_exp" in error for error in errors))

    def test_reviewer_scientific_fields_are_bound_to_output(self):
        rows, index_path = self.result_analysis_fixture()
        for field, value, error_code in (
            ("scientific_outcome", "hypothesis_supported", "review_scientific_outcome_mismatch"),
            ("limitations", ["tampered"], "review_limitations_mismatch"),
            ("validation_gaps", ["tampered"], "review_validation_gaps_mismatch"),
        ):
            with self.subTest(field=field):
                data = json.loads(index_path.read_text(encoding="utf-8"))
                data["entries"][0][field] = value
                index_path.write_text(json.dumps(data), encoding="utf-8")
                errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
                self.assertTrue(any(error_code in error for error in errors))
                rows, index_path = self.result_analysis_fixture()

    def test_reviewer_output_verdict_cannot_be_replaced(self):
        rows, index_path = self.result_analysis_fixture()
        session_path = next((self.root / ".pi/agent/sessions").glob("*.jsonl"))
        records = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()]
        result = records[1]["message"]["details"]["results"][0]
        payload = json.loads(result["messages"][0]["content"][0]["text"])
        payload["scientific_outcome"] = "hypothesis_supported"
        replacement = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        result["messages"][0]["content"][0]["text"] = replacement
        session_path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )
        data = json.loads(index_path.read_text(encoding="utf-8"))
        data["entries"][0]["review_output_sha256"] = hashlib.sha256(
            replacement.encode("utf-8")
        ).hexdigest()
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("review_scientific_outcome_mismatch" in error for error in errors))

    def test_reviewer_output_all_fields_are_bound(self):
        cases = {
            "exp_id": ("EXP-2", "review_exp_id_mismatch"),
            "run_ids": (["RUN-2"], "review_run_ids_mismatch"),
            "analysis_markdown": (
                "## Change\ncode\n\n## Result\ntampered\n\n## Finding\nuncertain\n\n## Next\nrepeat\n",
                "analysis_not_bound_to_reviewer_output",
            ),
            "limitations": (["tampered"], "review_limitations_mismatch"),
            "validation_gaps": (["tampered"], "review_validation_gaps_mismatch"),
        }
        for field, (value, error_code) in cases.items():
            with self.subTest(field=field):
                rows, index_path = self.result_analysis_fixture()
                session_path = next((self.root / ".pi/agent/sessions").glob("*.jsonl"))
                records = [
                    json.loads(line)
                    for line in session_path.read_text(encoding="utf-8").splitlines()
                ]
                result = records[1]["message"]["details"]["results"][0]
                payload = json.loads(result["messages"][0]["content"][0]["text"])
                payload[field] = value
                replacement = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                result["messages"][0]["content"][0]["text"] = replacement
                session_path.write_text(
                    "\n".join(json.dumps(record) for record in records) + "\n",
                    encoding="utf-8",
                )
                data = json.loads(index_path.read_text(encoding="utf-8"))
                data["entries"][0]["review_output_sha256"] = hashlib.sha256(
                    replacement.encode("utf-8")
                ).hexdigest()
                index_path.write_text(json.dumps(data), encoding="utf-8")
                errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
                self.assertTrue(any(error_code in error for error in errors))

    def test_reviewer_output_rejects_duplicate_json_keys(self):
        rows, index_path = self.result_analysis_fixture()
        session_path = next((self.root / ".pi/agent/sessions").glob("*.jsonl"))
        records = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()]
        result = records[1]["message"]["details"]["results"][0]
        original = result["messages"][0]["content"][0]["text"]
        duplicate = original.replace('"exp_id": "EXP-1",', '"exp_id": "EXP-1", "exp_id": "EXP-1",', 1)
        result["messages"][0]["content"][0]["text"] = duplicate
        session_path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )
        data = json.loads(index_path.read_text(encoding="utf-8"))
        data["entries"][0]["review_output_sha256"] = hashlib.sha256(
            duplicate.encode("utf-8")
        ).hexdigest()
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("review_output_json_invalid" in error for error in errors))

    def test_post_run_result_analysis_is_validated_and_fail_closed(self):
        rows, index_path = self.result_analysis_fixture()
        self.assertEqual(result_analysis_completion_errors(self.path, rows, workdir=self.root), [])

        cases = {
            "hash": lambda data: data["entries"][0].update(analysis_sha256="bad"),
            "output-hash": lambda data: data["entries"][0].update(review_output_sha256="0" * 64),
            "model": lambda data: data.update(observed_model="weak-model"),
            "scope": lambda data: data["entries"].__setitem__(0, {
                **data["entries"][0], "exp_id": "EXP-other", "run_id": "RUN-other",
            }),
            "evidence-ref": lambda data: data.update(model_evidence_ref="session:test"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                data = json.loads(index_path.read_text())
                mutate(data)
                index_path.write_text(json.dumps(data), encoding="utf-8")
                errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
                self.assertTrue(errors, name)
                if name == "hash":
                    self.assertTrue(any("hash_mismatch" in error for error in errors))
                elif name == "output-hash":
                    self.assertTrue(any("review_output_hash_mismatch" in error for error in errors))
                elif name == "model":
                    self.assertTrue(any("model_not_expected" in error for error in errors))
                elif name == "evidence-ref":
                    self.assertTrue(any("evidence_ref_invalid" in error for error in errors))
                else:
                    self.assertTrue(any("scope_mismatch" in error for error in errors))
                rows, index_path = self.result_analysis_fixture()

        rows, index_path = self.result_analysis_fixture()
        analysis = self.root / "research_workspace/experiments/EXP-1/analysis/analysis.md"
        analysis.write_text("## Change\nonly one section\n", encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("headings_invalid" in error for error in errors))

        rows, index_path = self.result_analysis_fixture(include_analysis=False)
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("row_missing" in error for error in errors))

        rows, index_path = self.result_analysis_fixture()
        _, analysis, review = rows
        self.write_csv([rows[0], review, analysis])
        errors = result_analysis_completion_errors(self.path, [rows[0], review, analysis], workdir=self.root)
        self.assertTrue(any("order_invalid" in error for error in errors))

    def test_reviewer_session_attestation_is_fail_closed(self):
        self.result_analysis_fixture()
        session_path = next((self.root / ".pi/agent/sessions").glob("*.jsonl"))
        mutations = {
            "model": lambda result: result.update(model="openai-codex/gpt-5.6-luna:max"),
            "agent": lambda result: result.update(agent="worker"),
            "exit": lambda result: result.update(exitCode=1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                records = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()]
                result = records[1]["message"]["details"]["results"][0]
                mutate(result)
                session_path.write_text(
                    "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
                )
                errors = result_analysis_completion_errors(self.path, [
                    self.row(id="RUN-1", phase="remote", exp_id="EXP-1", run_id="RUN-1",
                             remote_state="ingested", artifact_path="remote_artifacts/EXP-1/RUN-1"),
                    self.row(id="RESULT-ANALYSIS-01", phase="analysis",
                             required_skills="post-run-result-analysis",
                             notes=("analysis_kind:post_run; result_analysis:reviews/result-analysis.json; "
                                    "analysis_agent_mode:scientific-reviewer-subagent; "
                                    "analysis_independence:true; analysis_requested_model:openai-codex/gpt-5.6-sol; "
                                    "analysis_observed_model:openai-codex/gpt-5.6-sol; "
                                    "analysis_model_evidence:session-metadata; "
                                    "analysis_model_evidence_ref:session:00000000-0000-0000-0000-000000000001#tool:call-test")),
                    self.row(id="REVIEW-01", phase="review", notes="result_analysis:reviews/result-analysis.json"),
                ], workdir=self.root)
                self.assertTrue(any("review_" in error or "evidence_unverifiable" in error for error in errors))
                self.result_analysis_fixture()

        rows, _ = self.result_analysis_fixture()
        records = [json.loads(line) for line in session_path.read_text(encoding="utf-8").splitlines()]
        records.pop(0)
        session_path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
        )
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("tool_call_result_pair" in error for error in errors))


    def result_analysis_exec_fixture(self, run_ids=("RUN-1",)):
        rows, _ = self.result_analysis_fixture(run_ids=list(run_ids))
        job_dir = self.root / "reviews/result-analysis-EXP-1"
        job_dir.mkdir(parents=True, exist_ok=True)
        analysis_path = self.root / "research_workspace/experiments/EXP-1/analysis/analysis.md"
        analysis_text = analysis_path.read_text(encoding="utf-8")
        payload = {
            "exp_id": "EXP-1", "run_ids": list(run_ids),
            "analysis_markdown": analysis_text,
            "scientific_outcome": "inconclusive", "limitations": [], "validation_gaps": [],
        }
        review_output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        verdict = {
            "schema_version": "post-run.result-analysis-verdict.v1",
            "status": "completed",
            "backend": "codex-exec",
            "exp_id": "EXP-1",
            "run_ids": list(run_ids),
            "requested_model": "openai-codex/gpt-5.6-sol",
            "observed_model": "gpt-5.6-sol:high",
            "task_sha256": hashlib.sha256(b"task").hexdigest(),
            "events_sha256": hashlib.sha256(b"events").hexdigest(),
            "review_output": review_output,
            "review_output_sha256": hashlib.sha256(review_output.encode("utf-8")).hexdigest(),
        }
        verdict_path = job_dir / "verdict.json"
        verdict_path.write_text(json.dumps(verdict), encoding="utf-8")
        evidence_ref = "exec:reviews/result-analysis-EXP-1/verdict.json#verdict"
        index = {
            "schema_version": "post-run.result-analysis.v1",
            "status": "complete",
            "analysis_agent_mode": "codex-exec-independent",
            "analysis_independence": True,
            "requested_model": "gpt-5.6-sol",
            "observed_model": "gpt-5.6-sol",
            "model_evidence": "event-stream",
            "model_evidence_ref": evidence_ref,
            "entries": [{
                "exp_id": "EXP-1", "run_id": run_id,
                "analysis_path": "research_workspace/experiments/EXP-1/analysis/analysis.md",
                "analysis_sha256": hashlib.sha256(analysis_path.read_bytes()).hexdigest(),
                "scientific_outcome": "inconclusive",
                "review_evidence_ref": evidence_ref,
                "review_output_sha256": hashlib.sha256(review_output.strip().encode("utf-8")).hexdigest(),
                "evidence_refs": [f"command:fixture-{run_id}"], "limitations": [], "validation_gaps": [],
            } for run_id in run_ids],
        }
        index_path = self.root / "reviews/result-analysis.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        rows[1]["notes"] = (
            "analysis_kind:post_run; result_analysis:reviews/result-analysis.json; "
            "analysis_agent_mode:codex-exec-independent; analysis_independence:true; "
            "analysis_requested_model:gpt-5.6-sol; "
            "analysis_observed_model:gpt-5.6-sol; "
            "analysis_model_evidence:event-stream; "
            f"analysis_model_evidence_ref:{evidence_ref}"
        )
        self.write_csv(rows)
        return rows, index_path, verdict_path

    def test_result_analysis_codex_exec_channel_is_validated(self):
        rows, index_path, verdict_path = self.result_analysis_exec_fixture()
        self.assertEqual(result_analysis_completion_errors(self.path, rows, workdir=self.root), [])

        tamper_cases = (
            ("observed_model", "weak-model", "review_runtime_model_invalid"),
            ("review_output", json.dumps({
                "exp_id": "EXP-1", "run_ids": ["RUN-1"],
                "analysis_markdown": "## Change\ncode\n\n## Result\ntampered\n\n## Finding\nuncertain\n\n## Next\nrepeat\n",
                "scientific_outcome": "inconclusive", "limitations": [], "validation_gaps": [],
            }, ensure_ascii=False, sort_keys=True), "review_output_hash_mismatch"),
            ("schema_version", "other.v1", "review_evidence_unverifiable"),
        )
        for field, value, error_code in tamper_cases:
            with self.subTest(field=field):
                self.result_analysis_exec_fixture()
                verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
                verdict[field] = value
                verdict_path.write_text(json.dumps(verdict), encoding="utf-8")
                errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
                self.assertTrue(any(error_code in error for error in errors), errors)

        self.result_analysis_exec_fixture()
        verdict_path.rename(verdict_path.with_suffix(".json.moved"))
        try:
            errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
            self.assertTrue(any("review_evidence_unverifiable" in error for error in errors))
        finally:
            verdict_path.with_suffix(".json.moved").rename(verdict_path)

    def test_result_analysis_rejects_cross_channel_evidence_mismatch(self):
        rows, index_path, _ = self.result_analysis_exec_fixture()
        data = json.loads(index_path.read_text(encoding="utf-8"))
        data["model_evidence"] = "session-metadata"
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("analysis_model_evidence_unverifiable" in error for error in errors))

        rows, index_path = self.result_analysis_fixture()
        data = json.loads(index_path.read_text(encoding="utf-8"))
        data["model_evidence"] = "event-stream"
        data["model_evidence_ref"] = "event:whatever"
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("analysis_model_evidence_unverifiable" in error for error in errors))

    def test_result_analysis_exec_ref_resolves_outside_csv_dir(self):
        rows, index_path, verdict_path = self.result_analysis_exec_fixture()
        data = json.loads(index_path.read_text(encoding="utf-8"))
        escape_ref = "exec:../result-analysis.json#verdict"
        for entry in data["entries"]:
            entry["review_evidence_ref"] = escape_ref
        data["model_evidence_ref"] = escape_ref
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("review_evidence_unverifiable" in error for error in errors))

    def test_result_analysis_binds_paths_to_exp_and_run(self):
        rows, index_path = self.result_analysis_fixture()
        data = json.loads(index_path.read_text(encoding="utf-8"))
        foreign_analysis = self.root / "research_workspace/experiments/EXP-2/analysis/analysis.md"
        foreign_analysis.parent.mkdir(parents=True, exist_ok=True)
        foreign_analysis.write_text(
            "## Change\ncode\n\n## Result\nmetric\n\n## Finding\nuncertain\n\n## Next\nrepeat\n",
            encoding="utf-8",
        )
        data["entries"][0]["analysis_path"] = "research_workspace/experiments/EXP-2/analysis/analysis.md"
        data["entries"][0]["analysis_sha256"] = hashlib.sha256(foreign_analysis.read_bytes()).hexdigest()
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("analysis_path_exp_scope_invalid" in error for error in errors))

        rows, index_path = self.result_analysis_fixture()
        data = json.loads(index_path.read_text(encoding="utf-8"))
        foreign_artifact = self.root / "remote_artifacts/EXP-2/RUN-2/metrics.json"
        foreign_artifact.parent.mkdir(parents=True, exist_ok=True)
        foreign_artifact.write_text("{}", encoding="utf-8")
        data["entries"][0]["evidence_refs"] = ["remote_artifacts/EXP-2/RUN-2/metrics.json"]
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("analysis_evidence_scope_invalid" in error for error in errors))

    def test_codex_claude_skill_mirrors_match(self):
        mirrored = [
            "skills/post-run-result-analysis/SKILL.md",
            "skills/post-run-result-analysis/scripts/run_result_analysis.py",
            "skills/post-run-result-analysis/scripts/validate_result_analysis.py",
            "skills/mission-csv-execute/SKILL.md",
            "skills/mission-csv-execute/csv-schema.md",
            "skills/mission-csv-execute/references/closing-review.md",
            "skills/mission-csv-execute/scripts/ensure_review_row.py",
            "skills/mission-csv-execute/scripts/ensure_result_analysis_row.py",
            "skills/mission-csv-execute/scripts/result_analysis.py",
            "skills/mission-csv-execute/scripts/run_vision_review.py",
            "skills/mission-csv-execute/scripts/check_handoff_contract.py",
            "skills/mission-csv-execute/scripts/csv_state.py",
            "skills/mission-csv-execute/scripts/git_isolation.py",
            "skills/mission-csv-execute/scripts/validate_claim_ledger.py",
            "skills/mission-csv-execute/scripts/validate_outcome_contract.py",
            "skills/mission-csv-execute/scripts/mission_completion.py",
            "skills/mission-csv-execute/scripts/final_ready.py",
            "skills/mission-recovery/scripts/scan_recovery.py",
            "skills/mission-approved-doc/SKILL.md",
            "skills/pre-run-implementation-review/SKILL.md",
            "skills/pre-run-implementation-review/agents/openai.yaml",
            "skills/pre-run-implementation-review/scripts/prerun_core.py",
            "skills/pre-run-implementation-review/scripts/prerun_ready.py",
            "skills/pre-run-implementation-review/scripts/prerun_route.py",
        ]
        for relative in mirrored:
            with self.subTest(relative=relative):
                codex = ROOT / ".codex" / relative
                claude = ROOT / ".claude" / relative
                self.assertTrue(codex.is_file(), codex)
                self.assertEqual(codex.read_bytes(), claude.read_bytes())

    def test_final_ready_also_requires_post_run_analysis(self):
        rows, _ = self.result_analysis_fixture(include_analysis=False)
        self.write_csv(rows + [self.row(id="REVIEW-02", phase="review")])
        closing_errors: list[str] = []
        read_closing_csv(self.path, "REVIEW-01", closing_errors)
        self.assertTrue(any("review_row_not_final" in error for error in closing_errors))
        completion_errors = csv_completion_errors(self.path, workdir=self.root)
        self.assertTrue(any("result_analysis_row_missing" in error for error in completion_errors))
        payload = {
            "schema_version": CLOSING_SCHEMA,
            "csv_path": str(self.path),
            "review_row_id": "REVIEW-01",
            "review_path": str(self.root / "review.md"),
            "handoff_path": str(self.root / "handoff.md"),
            "expected_run_ids": ["RUN-1"],
            "required_review_tokens": [],
            "provenance_paths": [],
            "protected_paths_clean": True,
            "closing_context": {
                "risk_level": "L1", "independent_prerun_covered": False,
                "scientific_contract_changed_since_prerun": False,
                "evidence_conflict": False, "current_scope_gap_suspected": False,
            },
        }
        result = check_final_ready(payload, workdir=self.root)
        self.assertTrue(any("result_analysis_row_missing" in error for error in result["errors"]))

    def test_result_analysis_row_is_inserted_before_review_but_not_into_compat_csv(self):
        ordinary = self.row(id="RUN-1", exp_id="EXP-1", remote_state="completed")
        review = self.row(id="REVIEW-01")
        self.write_csv([ordinary, review])
        self.assertTrue(ensure_result_analysis_row(self.path))
        _, rows, _ = read_mission_csv(self.path)
        self.assertEqual([row["id"] for row in rows], ["RUN-1", "RESULT-ANALYSIS-01", "REVIEW-01"])
        self.assertIn("result_analysis:reviews/result-analysis.json", rows[-1]["notes"])

        compat = self.root / "compat.csv"
        fields = EXPECTED_FIELDS[:19]
        with compat.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerow({field: ordinary.get(field, "") for field in fields})
        self.assertFalse(ensure_result_analysis_row(compat))
        self.assertEqual(read_mission_csv(compat, allow_compat=True)[0], fields)

    def test_real_e2e_cannot_use_only_command_or_static_row(self):
        claim, rows = self.claim_fixture()
        self.assertTrue(self.validate_claim([{**claim, "evidence_refs": ["command:pytest -q"]}], rows))
        rows[0]["notes"] = rows[0]["notes"].replace("evidence_level:real_e2e", "evidence_level:static")
        self.assertTrue(any("real_e2e" in x for x in self.validate_claim([claim], rows)))


if __name__ == "__main__":
    unittest.main()
