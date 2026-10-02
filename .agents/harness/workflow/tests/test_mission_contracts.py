"""Mission 状态事实与引用完整性回归；Git 操作仅发生在临时仓库。"""
# ruff: noqa: E402 - 测试按运行时脚本路径导入工作流模块。
import csv
import argparse
from contextlib import chdir
import hashlib
import os
import json
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))
sys.path.insert(0, str(ROOT / ".agents"))
from csv_state import SCHEMA, StateUpdateError, apply_update
import preflight
from mission_completion import (EXPECTED_FIELDS, parse_note_tags, read_mission_csv,
                                row_terminal_errors, git_completion_errors, csv_completion_errors,
                                resolve_reference_path)
from git_isolation import commit_paths, verify_commit
from final_ready import _read_csv as read_closing_csv, check_final_ready, SCHEMA_VERSION as CLOSING_SCHEMA
from run_vision_review import discover_claim_ledger, resolve_existing_file, artifact_output_path
from check_handoff_contract import (load_review_notes, load_review_json, note_value_matches_handoff,
                                    check_contract, load_outcome_contract)
from harness.workflow.mission_state import update
from harness.records import experiment_records as records
from validate_deferred_ledger import load_csv_deferred
from validate_claim_ledger import validate_ledger
from ensure_result_analysis_row import ensure_result_analysis_row
from result_analysis import (
    result_analysis_completion_errors,
    review_model as _review_model,
)

# 审查模型是项目自有契约（`config/review_contract.toml`），测试不得复述模板默认值。
PI_MODEL = _review_model.RECORDED_MODEL
CODEX_MODEL = _review_model.RECORDED_MODELS["codex"]


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
        self.write_csv([self.row(dev_state="已完成", refs="code.py")])
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

    def _write_job_verdict(self, exp_id, run_ids, *, analysis_text, packet_sha=None,
                           task_sha=None, raw_sha=None,
                           requested=None, observed=None,
                           model_evidence="job-verdict"):
        """Write a reviewer_job verdict for the result-analysis fixture.

        All digests are recomputed here from the written packet/task/raw/response
        files so the validator's hash checks pass; tests can override specific
        digests via kwargs to simulate tampering.
        """
        job_rel = f"reviews/result-analysis-{exp_id}"
        job_dir = self.root / job_rel
        job_dir.mkdir(parents=True, exist_ok=True)

        # reviewer_job writes its payload with sort_keys=True and no indent; the
        # validator reads back both sha and content, so we match that.
        payload = {
            "exp_id": exp_id,
            "run_ids": list(run_ids),
            "analysis_markdown": analysis_text,
            "scientific_outcome": "inconclusive",
            "limitations": [],
            "validation_gaps": [],
        }
        review_output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        review_sha = hashlib.sha256(review_output.strip().encode("utf-8")).hexdigest()

        packet_path = job_dir / "packet.json"
        packet_text = json.dumps({"exp_id": exp_id, "run_ids": list(run_ids)}, indent=2, ensure_ascii=False)
        packet_path.write_text(packet_text, encoding="utf-8")
        packet_sha = packet_sha or hashlib.sha256(packet_text.encode("utf-8")).hexdigest()

        task_path = job_dir / "task.md"
        task_text = f"Analyze {exp_id} {sorted(run_ids)}\n"
        task_path.write_text(task_text, encoding="utf-8")
        task_sha = task_sha or hashlib.sha256(task_text.encode("utf-8")).hexdigest()

        raw_path = job_dir / "raw-response.json"
        raw_text = json.dumps({"raw": review_output}, indent=2, ensure_ascii=False)
        raw_path.write_text(raw_text, encoding="utf-8")
        raw_sha = raw_sha or hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

        requested = requested if requested is not None else f"{PI_MODEL}:max"
        observed = observed if observed is not None else f"{PI_MODEL}:max"

        verdict = {
            "schema_version": "post-run.result-analysis-verdict.v1",
            "status": "completed",
            "review_mode": "implementation_review",
            "review_kind": "result-analysis",
            "job_id": job_rel,
            "backend": "pi",
            "reviewer_session_id": "fixture-session-id",
            "requested_model": requested,
            "observed_model": observed,
            "model_evidence": model_evidence,
            "model_source": "contract",
            "packet_path": str(packet_path),
            "packet_sha256": packet_sha,
            "task_path": str(task_path),
            "task_sha256": task_sha,
            "raw_response_path": str(raw_path),
            "raw_response_sha256": raw_sha,
            "response_path": str(raw_path),
            "response_sha256": raw_sha,
            "replacement_count": 0,
            "resume_count": 0,
            "transport_exit_code": 0,
            "completed_at": "2026-01-01T00:00:00Z",
            "exp_id": exp_id,
            "run_ids": list(run_ids),
            "review_output": review_output,
            "review_output_sha256": review_sha,
        }
        verdict_path = job_dir / "verdict.json"
        verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return job_rel, review_sha

    def result_analysis_fixture(self, *, include_analysis=True, run_ids=None, backend="pi"):
        analysis_path = self.root / "research_workspace/experiments/EXP-1/analysis/analysis.md"
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_text = "## Change\ncode\n\n## Result\nmetric\n\n## Finding\nuncertain\n\n## Next\nrepeat\n"
        analysis_path.write_text(analysis_text, encoding="utf-8")
        run_ids = list(run_ids or ["RUN-1"])

        job_rel, review_sha = self._write_job_verdict("EXP-1", run_ids, analysis_text=analysis_text)
        evidence_ref = f"job:{job_rel}/verdict.json#verdict"

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
                    "analysis_agent_mode:result-analysis-reviewer-job; "
                    "analysis_independence:true; "
                    f"analysis_requested_model:{PI_MODEL}:max; "
                    f"analysis_observed_model:{PI_MODEL}:max; "
                    "analysis_model_evidence:job-verdict; "
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
            "analysis_agent_mode": "result-analysis-reviewer-job",
            "analysis_independence": True,
            "requested_model": f"{PI_MODEL}:max",
            "observed_model": f"{PI_MODEL}:max",
            "model_evidence": "job-verdict",
            "model_evidence_ref": evidence_ref,
            "entries": [{
                "exp_id": "EXP-1", "run_id": run_id,
                "analysis_path": "research_workspace/experiments/EXP-1/analysis/analysis.md",
                "analysis_sha256": digest, "scientific_outcome": "inconclusive",
                "review_evidence_ref": evidence_ref, "review_output_sha256": review_sha,
                "evidence_refs": [f"command:fixture-{run_id}"], "limitations": [], "validation_gaps": [],
            } for run_id in run_ids],
        }
        index_path = self.root / "reviews/result-analysis.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index), encoding="utf-8")
        return rows, index_path

    def result_analysis_exec_fixture(self, run_ids=("RUN-1",)):
        """Codex-side reviewer_job fixture. Identical structure to Pi; only the
        backend / requested_model strings change (Codex uses the bare model name
        without a thinking suffix)."""
        rows, _ = self.result_analysis_fixture(run_ids=list(run_ids))
        analysis_path = self.root / "research_workspace/experiments/EXP-1/analysis/analysis.md"
        analysis_text = analysis_path.read_text(encoding="utf-8")
        job_rel = f"reviews/result-analysis-EXP-1"
        job_dir = self.root / job_rel
        # Rewrite verdict as a codex-backend one.
        payload = {
            "exp_id": "EXP-1", "run_ids": list(run_ids),
            "analysis_markdown": analysis_text,
            "scientific_outcome": "inconclusive", "limitations": [], "validation_gaps": [],
        }
        review_output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        review_sha = hashlib.sha256(review_output.strip().encode("utf-8")).hexdigest()
        packet_path = job_dir / "packet.json"
        task_path = job_dir / "task.md"
        raw_path = job_dir / "raw-response.json"
        verdict = {
            "schema_version": "post-run.result-analysis-verdict.v1",
            "status": "completed",
            "review_mode": "implementation_review",
            "review_kind": "result-analysis",
            "job_id": job_rel,
            "backend": "codex",
            "reviewer_session_id": "fixture-session-id",
            "requested_model": CODEX_MODEL,
            "observed_model": f"{CODEX_MODEL}:high",
            "model_evidence": "job-verdict",
            "model_source": "contract",
            "packet_path": str(packet_path),
            "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
            "task_path": str(task_path),
            "task_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
            "raw_response_path": str(raw_path),
            "raw_response_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "response_path": str(raw_path),
            "response_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "replacement_count": 0,
            "resume_count": 0,
            "transport_exit_code": 0,
            "completed_at": "2026-01-01T00:00:00Z",
            "exp_id": "EXP-1",
            "run_ids": list(run_ids),
            "review_output": review_output,
            "review_output_sha256": review_sha,
        }
        verdict_path = job_dir / "verdict.json"
        verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        evidence_ref = f"job:{job_rel}/verdict.json#verdict"
        index = {
            "schema_version": "post-run.result-analysis.v1",
            "status": "complete",
            "analysis_agent_mode": "result-analysis-reviewer-job",
            "analysis_independence": True,
            "requested_model": CODEX_MODEL,
            "observed_model": f"{CODEX_MODEL}:high",
            "model_evidence": "job-verdict",
            "model_evidence_ref": evidence_ref,
            "entries": [{
                "exp_id": "EXP-1", "run_id": run_id,
                "analysis_path": "research_workspace/experiments/EXP-1/analysis/analysis.md",
                "analysis_sha256": hashlib.sha256(analysis_path.read_bytes()).hexdigest(),
                "scientific_outcome": "inconclusive",
                "review_evidence_ref": evidence_ref,
                "review_output_sha256": review_sha,
                "evidence_refs": [f"command:fixture-{run_id}"], "limitations": [], "validation_gaps": [],
            } for run_id in run_ids],
        }
        index_path = self.root / "reviews/result-analysis.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        analysis_row = next(r for r in rows if r.get("id") == "RESULT-ANALYSIS-01")
        analysis_row["notes"] = (
            "analysis_kind:post_run; result_analysis:reviews/result-analysis.json; "
            "analysis_agent_mode:result-analysis-reviewer-job; analysis_independence:true; "
            f"analysis_requested_model:{CODEX_MODEL}; "
            f"analysis_observed_model:{CODEX_MODEL}:high; "
            "analysis_model_evidence:job-verdict; "
            f"analysis_model_evidence_ref:{evidence_ref}"
        )
        self.write_csv(rows)
        return rows, index_path, verdict_path

    def test_result_analysis_validator_cli_writes_record_and_ledger(self):
        rows, index_path = self.result_analysis_fixture()
        agents_link = self.root / ".agents"
        agents_link.symlink_to(ROOT / ".agents", target_is_directory=True)
        record_path = self.root / "research_workspace/experiments/EXP-1/record.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps({
            "exp_id": "EXP-1", "parent": None, "relation": None,
            "source": {"spec_id": ["SPEC-1"], "branch": ["main"], "commit": ["a" * 40], "mission_csv": []},
            "metrics": {"protocol": "official", "baseline_id": None, "baseline_run_id": None,
                        "baseline_metric": 0.5, "ours_metric": 0.6, "delta_metric": 0.1},
            "runs": [{"run_id": "RUN-1", "summary_path": "remote_artifacts/EXP-1/RUN-1/summary.json"}],
            "outcome": "pending", "_pending": ["outcome"],
            "_generated_by": ".agents/harness/records/experiment_records.py", "_projection_version": 2,
        }, ensure_ascii=False, indent=2) + "\n")
        validator = ROOT / ".codex/skills/post-run-result-analysis/scripts/validate_result_analysis.py"
        def run_validator():
            return subprocess.run(
                [sys.executable, str(validator), "--csv", str(self.path), "--index", str(index_path),
                 "--workdir", str(self.root)],
                cwd=ROOT, capture_output=True, text=True,
            )
        result = run_validator()
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = json.loads(record_path.read_text())
        self.assertEqual(updated["outcome"], "inconclusive")
        self.assertEqual(updated["outcome_meta"]["run_ids"], ["RUN-1"])
        with (self.root / "research_workspace/EXPERIMENTS.csv").open(newline="") as stream:
            self.assertEqual(next(csv.DictReader(stream))["Outcome"], "inconclusive")
        generated = json.loads(record_path.read_text())
        generated["outcome"] = "pending"
        generated["_pending"] = ["outcome"]
        generated.pop("outcome_meta")
        with patch.object(records, "build_record", return_value=generated), chdir(self.root):
            self.assertEqual(records.cmd_build(argparse.Namespace(
                exp="EXP-1", force=False, config=self.root / "missing.toml"), repo_root=self.root), 0)
        rebuilt = json.loads(record_path.read_text())
        self.assertEqual(rebuilt["outcome"], "inconclusive")
        self.assertNotIn("outcome", rebuilt["_pending"])
        analysis_path = self.root / "research_workspace/experiments/EXP-1/analysis/analysis.md"
        analysis_before = analysis_path.read_text()
        analysis_path.write_text(analysis_before.replace("uncertain", "changed"))
        generated = json.loads(record_path.read_text())
        generated["outcome"] = "pending"
        generated["_pending"] = ["outcome"]
        generated.pop("outcome_meta")
        with patch.object(records, "build_record", return_value=generated), chdir(self.root):
            self.assertEqual(records.cmd_build(argparse.Namespace(
                exp="EXP-1", force=False, config=self.root / "missing.toml"), repo_root=self.root), 0)
        self.assertEqual(json.loads(record_path.read_text())["outcome"], "pending")
        analysis_path.write_text(analysis_before)
        self.assertEqual(run_validator().returncode, 0)
        verdict_path = self.root / "reviews/result-analysis-EXP-1/verdict.json"
        verdict_before = verdict_path.read_bytes()
        verdict_path.unlink()
        generated = json.loads(record_path.read_text())
        generated["outcome"] = "pending"
        generated["_pending"] = ["outcome"]
        generated.pop("outcome_meta")
        with patch.object(records, "build_record", return_value=generated), chdir(self.root):
            self.assertEqual(records.cmd_build(argparse.Namespace(
                exp="EXP-1", force=False, config=self.root / "missing.toml"), repo_root=self.root), 0)
        self.assertEqual(json.loads(record_path.read_text())["outcome"], "pending")
        verdict_path.write_bytes(verdict_before)
        self.assertEqual(run_validator().returncode, 0)
        generated = json.loads(record_path.read_text())
        generated["outcome"] = "pending"
        generated["_pending"] = ["outcome"]
        generated["runs"][0]["summary_path"] = "remote_artifacts/EXP-1/RUN-1/changed.json"
        generated.pop("outcome_meta")
        with patch.object(records, "build_record", return_value=generated), chdir(self.root):
            self.assertEqual(records.cmd_build(argparse.Namespace(
                exp="EXP-1", force=False, config=self.root / "missing.toml"), repo_root=self.root), 0)
        self.assertEqual(json.loads(record_path.read_text())["outcome"], "pending")

    def test_result_analysis_validator_cli_allows_same_run_multiple_summaries(self):
        _, index_path = self.result_analysis_fixture()
        (self.root / ".agents").symlink_to(ROOT / ".agents", target_is_directory=True)
        record_path = self.root / "research_workspace/experiments/EXP-1/record.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps({
            "exp_id": "EXP-1", "source": {"spec_id": [], "branch": [], "commit": [], "mission_csv": []},
            "metrics": {"protocol": None, "baseline_id": None},
            "runs": [{"run_id": "RUN-1", "summary_path": "a.json"},
                     {"run_id": "RUN-1", "summary_path": "b.json"}],
            "outcome": "pending", "_pending": ["outcome"],
        }) + "\n")
        result = subprocess.run(
            [sys.executable, str(ROOT / ".codex/skills/post-run-result-analysis/scripts/validate_result_analysis.py"),
             "--csv", str(self.path), "--index", str(index_path), "--workdir", str(self.root)],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(record_path.read_text())["outcome"], "inconclusive")

    def test_result_analysis_validator_cli_rejects_invalid_index_without_writes(self):
        _, index_path = self.result_analysis_fixture()
        (self.root / ".agents").symlink_to(ROOT / ".agents", target_is_directory=True)
        record_path = self.root / "research_workspace/experiments/EXP-1/record.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps({"exp_id": "EXP-1", "runs": [{"run_id": "RUN-1"}], "outcome": "pending"}) + "\n")
        before = record_path.read_bytes()
        payload = json.loads(index_path.read_text())
        payload["entries"][0]["scientific_outcome"] = "invalid"
        index_path.write_text(json.dumps(payload))
        result = subprocess.run(
            [sys.executable, str(ROOT / ".codex/skills/post-run-result-analysis/scripts/validate_result_analysis.py"),
             "--csv", str(self.path), "--index", str(index_path), "--workdir", str(self.root)],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(record_path.read_bytes(), before)
        self.assertFalse((self.root / "research_workspace/EXPERIMENTS.csv").exists())

    def test_result_analysis_validator_cli_warns_on_writeback_failure(self):
        _, index_path = self.result_analysis_fixture()
        (self.root / ".agents").symlink_to(ROOT / ".agents", target_is_directory=True)
        record_path = self.root / "research_workspace/experiments/EXP-1/record.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        outside = Path(tempfile.mkdtemp(prefix="result-analysis-outside-")) / "record.json"
        self.addCleanup(lambda: shutil.rmtree(outside.parent, ignore_errors=True))
        outside.write_text(json.dumps({"outcome": "pending"}) + "\n")
        record_path.symlink_to(outside)
        result = subprocess.run(
            [sys.executable, str(ROOT / ".codex/skills/post-run-result-analysis/scripts/validate_result_analysis.py"),
             "--csv", str(self.path), "--index", str(index_path), "--workdir", str(self.root)],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("record path outside repo", result.stderr)
        self.assertIn("post-run result analysis: valid", result.stdout)

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
        verdict_path = self.root / "reviews/result-analysis-EXP-1/verdict.json"
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        payload = json.loads(verdict["review_output"])
        payload["scientific_outcome"] = "hypothesis_supported"
        replacement = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        verdict["review_output"] = replacement
        verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
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
                verdict_path = self.root / "reviews/result-analysis-EXP-1/verdict.json"
                verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
                payload = json.loads(verdict["review_output"])
                payload[field] = value
                replacement = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                verdict["review_output"] = replacement
                verdict_path.write_text(
                    json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
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
        verdict_path = self.root / "reviews/result-analysis-EXP-1/verdict.json"
        verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        original = verdict["review_output"]
        duplicate = original.replace('"exp_id": "EXP-1",', '"exp_id": "EXP-1", "exp_id": "EXP-1",', 1)
        verdict["review_output"] = duplicate
        verdict_path.write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
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
        rows, index_path = self.result_analysis_fixture()
        verdict_path = self.root / "reviews/result-analysis-EXP-1/verdict.json"
        mutations = (
            ("backend", "unknown-host", "review_backend_invalid"),
            ("observed_model", "unknown-model-x", "review_runtime_model_invalid"),
            ("packet_sha256", "0" * 64, "review_evidence_unverifiable"),
        )
        for field, value, expected_error in mutations:
            with self.subTest(field=field):
                self.result_analysis_fixture()
                verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
                verdict[field] = value
                verdict_path.write_text(
                    json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
                self.assertTrue(
                    any(expected_error in error for error in errors),
                    f"{field}: expected {expected_error} in {errors!r}",
                )

        # verifiable hash chain — after each fixture load, packet/task/raw digests verifier recomputes will diverge
        # only if actual files were tampered; the validator closed-loop should reject.
        rows, index_path = self.result_analysis_fixture()
        data = json.loads(index_path.read_text(encoding="utf-8"))
        verdict = json.loads((self.root / "reviews/result-analysis-EXP-1/verdict.json").read_text(encoding="utf-8"))
        verdict["packet_sha256"] = "0" * 64  # diverges from actual packet file
        (self.root / "reviews/result-analysis-EXP-1/verdict.json").write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("review_evidence_unverifiable" in error for error in errors), errors)


    def result_analysis_exec_fixture(self, run_ids=("RUN-1",)):
        """Codex-side reviewer_job fixture. Identical structure to Pi; only the
        backend / requested_model strings change (Codex uses the bare model name
        without a thinking suffix)."""
        rows, _ = self.result_analysis_fixture(run_ids=list(run_ids))
        analysis_path = self.root / "research_workspace/experiments/EXP-1/analysis/analysis.md"
        analysis_text = analysis_path.read_text(encoding="utf-8")
        job_rel = "reviews/result-analysis-EXP-1"
        job_dir = self.root / job_rel
        payload = {
            "exp_id": "EXP-1", "run_ids": list(run_ids),
            "analysis_markdown": analysis_text,
            "scientific_outcome": "inconclusive", "limitations": [], "validation_gaps": [],
        }
        review_output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        review_sha = hashlib.sha256(review_output.strip().encode("utf-8")).hexdigest()
        packet_path = job_dir / "packet.json"
        task_path = job_dir / "task.md"
        raw_path = job_dir / "raw-response.json"
        verdict = {
            "schema_version": "post-run.result-analysis-verdict.v1",
            "status": "completed",
            "review_mode": "implementation_review",
            "review_kind": "result-analysis",
            "job_id": job_rel,
            "backend": "codex",
            "reviewer_session_id": "fixture-session-id",
            "requested_model": CODEX_MODEL,
            "observed_model": f"{CODEX_MODEL}:high",
            "model_evidence": "job-verdict",
            "model_source": "contract",
            "packet_path": str(packet_path),
            "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
            "task_path": str(task_path),
            "task_sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
            "raw_response_path": str(raw_path),
            "raw_response_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "response_path": str(raw_path),
            "response_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "replacement_count": 0,
            "resume_count": 0,
            "transport_exit_code": 0,
            "completed_at": "2026-01-01T00:00:00Z",
            "exp_id": "EXP-1",
            "run_ids": list(run_ids),
            "review_output": review_output,
            "review_output_sha256": review_sha,
        }
        verdict_path = job_dir / "verdict.json"
        verdict_path.write_text(json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        evidence_ref = f"job:{job_rel}/verdict.json#verdict"
        index = {
            "schema_version": "post-run.result-analysis.v1",
            "status": "complete",
            "analysis_agent_mode": "result-analysis-reviewer-job",
            "analysis_independence": True,
            "requested_model": CODEX_MODEL,
            "observed_model": f"{CODEX_MODEL}:high",
            "model_evidence": "job-verdict",
            "model_evidence_ref": evidence_ref,
            "entries": [{
                "exp_id": "EXP-1", "run_id": run_id,
                "analysis_path": "research_workspace/experiments/EXP-1/analysis/analysis.md",
                "analysis_sha256": hashlib.sha256(analysis_path.read_bytes()).hexdigest(),
                "scientific_outcome": "inconclusive",
                "review_evidence_ref": evidence_ref,
                "review_output_sha256": review_sha,
                "evidence_refs": [f"command:fixture-{run_id}"], "limitations": [], "validation_gaps": [],
            } for run_id in run_ids],
        }
        index_path = self.root / "reviews/result-analysis.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        analysis_row = next(r for r in rows if r.get("id") == "RESULT-ANALYSIS-01")
        analysis_row["notes"] = (
            "analysis_kind:post_run; result_analysis:reviews/result-analysis.json; "
            "analysis_agent_mode:result-analysis-reviewer-job; analysis_independence:true; "
            f"analysis_requested_model:{CODEX_MODEL}; "
            f"analysis_observed_model:{CODEX_MODEL}:high; "
            "analysis_model_evidence:job-verdict; "
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
        # The reviewer_job channel accepts any model_evidence label recorded by the
        # index writer; the actual reviewer output is verified from the verdict +
        # packet/task files. Stale labels from legacy channels must be rejected so
        # the recorded channel cannot be silently rewritten.
        rows, index_path, _ = self.result_analysis_exec_fixture()
        data = json.loads(index_path.read_text(encoding="utf-8"))
        data["model_evidence"] = "session-metadata"
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        self.assertTrue(any("analysis_model_evidence_invalid" in error for error in errors))

        rows, index_path = self.result_analysis_fixture()
        data = json.loads(index_path.read_text(encoding="utf-8"))
        data["model_evidence"] = "event-stream"
        data["model_evidence_ref"] = "event:whatever"
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        # base _JOB_REF_RE only accepts job: prefixed refs; legacy channels are rejected by format.
        self.assertTrue(any("model_evidence" in error or "evidence_ref_invalid" in error for error in errors))

    def test_result_analysis_exec_ref_resolves_outside_csv_dir(self):
        rows, index_path, verdict_path = self.result_analysis_exec_fixture()
        data = json.loads(index_path.read_text(encoding="utf-8"))
        escape_ref = "exec:../result-analysis.json#verdict"
        for entry in data["entries"]:
            entry["review_evidence_ref"] = escape_ref
        data["model_evidence_ref"] = escape_ref
        index_path.write_text(json.dumps(data), encoding="utf-8")
        errors = result_analysis_completion_errors(self.path, rows, workdir=self.root)
        # escape_refs no longer satisfy the reviewer_job ref format
        self.assertTrue(
            any(
                "review_evidence_ref_invalid" in error or "analysis_model_evidence_ref_invalid" in error
                for error in errors
            ),
            errors,
        )

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
        # .claude/skills 是指向 .codex/skills 的 symlink（物理同源），
        # 镜像一致性由文件系统保证，测试只需断言链接存在且指向正确。
        claude_skills = ROOT / ".claude" / "skills"
        self.assertTrue(claude_skills.is_symlink(), f".claude/skills 应为 symlink: {claude_skills}")
        self.assertEqual(
            claude_skills.readlink(), Path("../.codex/skills"),
            f".claude/skills 应指向 ../.codex/skills，实际: {claude_skills.readlink()}",
        )
        # 抽验几个关键文件经 symlink 可读且与 canonical 一致。
        for relative in (
            "mission-csv-execute/SKILL.md",
            "mission-csv-execute/scripts/csv_state.py",
            "pre-run-implementation-review/SKILL.md",
        ):
            with self.subTest(relative=relative):
                codex = ROOT / ".codex" / "skills" / relative
                claude = claude_skills / relative
                self.assertTrue(codex.is_file(), codex)
                self.assertEqual(codex.read_bytes(), claude.read_bytes())

    def test_preflight_catches_runtime_update_errors_before_first_write(self):
        # 9-27 hera-gsr-scnp 会话中三类真实运行期错误必须在首次写入前被 preflight 抓住。
        self.init_git()
        self.write_csv([
            self.row(id="PRERUN-REVIEW-01", phase="prerun",
                     notes="review_kind:pre_run_implementation; review_mode:scientific_review; gated_run:RUN-1"),
            self.row(id="INGEST-01", phase="ingest"),
        ])
        bad_boundary = {"schema_version": SCHEMA, "row_id": "INGEST-01",
                        "set": {"dev_state": "已完成"}, "commit_boundary": "bogus_boundary"}
        bad_result = {"schema_version": SCHEMA, "row_id": "PRERUN-REVIEW-01",
                      "set_note_tags": {"review_result": "scientifically_incorrect_closed"}}
        bad_row = {"schema_version": SCHEMA, "row_id": "NOPE-01",
                   "set": {"dev_state": "已完成"}}
        ok_request = {"schema_version": SCHEMA, "row_id": "INGEST-01",
                      "append_notes": ["preflight_probe:ok"], "commit_boundary": "none"}
        errors, branch_skipped = preflight.preflight(self.path, [
            ("bad_boundary", bad_boundary),
            ("bad_result", bad_result),
            ("bad_row", bad_row),
            ("ok_request", ok_request),
        ])
        self.assertTrue(any("commit_boundary_invalid" in e and "bad_boundary" in e for e in errors), errors)
        self.assertTrue(any("prerun_review_result_invalid" in e and "bad_result" in e for e in errors), errors)
        self.assertTrue(any("row_lookup_invalid" in e and "bad_row" in e for e in errors), errors)
        self.assertFalse(any("ok_request" in e for e in errors), errors)
        # 只读保证：preflight 不得改动 CSV 字节。
        before = self.path.read_bytes()
        preflight.preflight(self.path, [("ok_request", ok_request)])
        self.assertEqual(before, self.path.read_bytes())
        # 全部行闭环且无请求时 preflight 通过。
        closed = self.row(id="X-1", dev_state="已完成", review_initial_state="已完成",
                          review_regression_state="已完成", git_state="已提交",
                          remote_state="not_applicable")
        self.write_csv([closed])
        self.assertEqual(preflight.preflight(self.path, []), ([], False))

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


class CsvStateTransitionMatrixTests(unittest.TestCase):
    """CSV 全局状态机迁移表完备性回归（加测；非 TDD，一次性校验现状）。

    测试方法：把 `csv_state.apply_update` 当作黑盒，核对四元组 (dev, review_initial,
    review_regression, git) × remote_state 上每一处被允许/被拒绝的写入。

    命名约定：
      - `*_rejected_*`：迁移必须被拒绝，断言 `StateUpdateError`；
      - `*_bug_candidate_*`：迁移应当被拒但当前代码允许（记录 BUG-CANDIDATE，
        **不断言期望行为**，只断言现状，由主代理裁定是否修）；
      - `*_accepted_*`：合法迁移可达性烟测。

    Fixture 与 `MissionContractTests` 共用（tempdir + HOME patch + 可选 git repo），
    单独成 class 以避免对既有 31 个测试造成命名冲突。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="csv-matrix-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "tasks.csv"
        self._home_patch = patch.dict(os.environ, {"HOME": str(self.root)})
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

    def row(self, **values):
        row = dict.fromkeys(EXPECTED_FIELDS, "")
        row.update(id="I-1", dev_state="未开始", review_initial_state="未开始",
                   review_regression_state="未开始", git_state="未提交",
                   remote_state="not_applicable")
        row.update(values)
        return row

    def write_csv(self, rows, fields=EXPECTED_FIELDS):
        with self.path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def init_git(self):
        """与 MissionContractTests.init_git 语义一致；chdir 不写到类共享状态。

        需要 `_assert_write_context` 通过，因此进入临时仓 cwd；`addCleanup`
        在测试结束时恢复原 cwd，不影响其他测试与 CLI 运行。
        """
        self._prev_cwd = Path.cwd()
        os.chdir(self.root)

        def _restore_cwd():
            try:
                os.chdir(self._prev_cwd)
            except FileNotFoundError:
                pass  # tempdir 已清理；不阻塞测试退出
        self.addCleanup(_restore_cwd)
        subprocess.run(["git", "init", "-b", "main"], cwd=self.root, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=self.root,
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "harness-test@example.invalid"],
                       cwd=self.root, check=True, capture_output=True)
        (self.root / "code.py").write_text("value = 1\n")
        subprocess.run(["git", "add", "code.py"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "fixture baseline"], cwd=self.root,
                       check=True, capture_output=True)

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    # ------------------------------------------------------------------
    # 1) 已被 csv_state 拒绝的迁移——guardrail 必须保留
    # ------------------------------------------------------------------

    def test_rejected_close_git_state_without_commit_evidence(self):
        """git_state 未提交→已提交 必须携带 commit_hash + refs + 真实 HEAD（§A）。

        否则 `git_isolation.row_git_errors` 返回 `git_evidence_invalid` 并拒绝。
        """
        self.init_git()
        self.write_csv([self.row(dev_state="已完成", review_initial_state="已完成",
                                 review_regression_state="已完成")])
        with self.assertRaisesRegex(StateUpdateError, "git_evidence_invalid"):
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                     "set": {"git_state": "已提交"}})

    def test_rejected_remote_state_ingested_without_terminal_evidence(self):
        """remote_state→ingested 必须有可追溯 RunSpec + spec_id + commit_hash + artifacts。

        这里清空的 row 缺少全部 ingest 证据，必须被拒（§B）。
        """
        self.write_csv([self.row(remote_state="completed",
                                 exp_id="EXP-1", run_id="RUN-1",
                                 artifact_path="artifact.file")])
        with self.assertRaisesRegex(StateUpdateError, "ingest_"):
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                     "set": {"remote_state": "ingested"}})

    def test_rejected_close_git_state_with_fake_commit_not_in_repo(self):
        """提供伪 commit_hash（非 repo HEAD）也不能过关（§C）。"""
        self.init_git()
        self.write_csv([self.row(dev_state="已完成", review_initial_state="已完成",
                                 review_regression_state="已完成")])
        with self.assertRaisesRegex(StateUpdateError, "git_evidence_invalid"):
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                     "set": {"git_state": "已提交",
                                             "commit_hash": "a" * 40}})

    # ------------------------------------------------------------------
    # 2) BUG-CANDIDATE —— 应当被拒但 csv_state 当前允许
    #    （只记录现状，不修代码；待主代理裁定）
    # ------------------------------------------------------------------

    def test_bug_candidate_reopen_stage1_git_commit_unrestricted(self):
        """FIXED BC-1/F-012: git_state=已提交 不可再回未提交。"""
        self.init_git()
        self.write_csv([self.row(git_state="已提交", commit_hash=self.head(),
                                 refs="code.py")])
        with self.assertRaises(StateUpdateError) as exc:
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"git_state": "未提交"}})
        self.assertIn("git_reopen", str(exc.exception))

    def test_bug_candidate_remote_failure_resume_to_running(self):
        """FIXED BC-2/F-012: failed 不可直跳 running_remote。"""
        self.write_csv([self.row(remote_state="failed")])
        with self.assertRaises(StateUpdateError) as exc:
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"remote_state": "running_remote"}})
        self.assertIn("remote_regression", str(exc.exception))

    def test_bug_candidate_remote_failed_jump_to_completed(self):
        """FIXED BC-3/F-012: failed 不可直跳 completed。"""
        self.write_csv([self.row(remote_state="failed")])
        with self.assertRaises(StateUpdateError) as exc:
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"remote_state": "completed"}})
        self.assertIn("remote_regression", str(exc.exception))

    def test_bug_candidate_remote_blank_jump_to_terminal_completed(self):
        """FIXED BC-4/F-012: 空 remote_state 不可直跳 completed（兼容 19 列漏洞）。"""
        self.write_csv([self.row(remote_state="")])
        with self.assertRaises(StateUpdateError) as exc:
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"remote_state": "completed"}})
        self.assertIn("remote_regression", str(exc.exception))

    def test_bug_candidate_remote_state_not_applicable_reopen_to_running(self):
        """FIXED BC-5/F-012: not_applicable 不可跳回 running_remote。"""
        self.write_csv([self.row()])
        with self.assertRaises(StateUpdateError) as exc:
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"remote_state": "running_remote"}})
        self.assertIn("remote_regression", str(exc.exception))

    def test_bug_candidate_junction_git_committed_review_initial_backward(self):
        """FIXED BC-6/F-012: git=已提交 后 review_initial 不得回退。"""
        self.init_git()
        self.write_csv([self.row(git_state="已提交", commit_hash=self.head(),
                                 review_initial_state="已完成",
                                 refs="code.py")])
        with self.assertRaises(StateUpdateError) as exc:
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"review_initial_state": "未开始"}})
        self.assertIn("state_regression", str(exc.exception))

    def test_bug_candidate_junction_git_committed_dev_state_unstarted(self):
        """FIXED BC-7/F-012: git=已提交 且 dev_state=未开始 的组合不可写。"""
        self.init_git()
        self.write_csv([self.row(dev_state="未开始", remote_state="completed",
                                 git_state="已提交",
                                 commit_hash=self.head(), refs="code.py")])
        with self.assertRaises(StateUpdateError) as exc:
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"remote_state": "completed"}})
        self.assertIn("git_committed_with_dev_unstarted", str(exc.exception))

    def test_bug_candidate_junction_git_committed_remote_running(self):
        """FIXED BC-8/F-012: git=已提交 且 remote_state=running_remote 的组合不可写。"""
        self.init_git()
        self.write_csv([self.row(git_state="已提交", commit_hash=self.head(),
                                 refs="code.py", remote_state="not_applicable")])
        with self.assertRaises(StateUpdateError) as exc:
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"remote_state": "running_remote"}})
        # remote_regression 或 git_committed_with_remote_running 任一都算修复到位
        self.assertTrue(
            "git_committed_with_remote_running" in str(exc.exception)
            or "remote_regression" in str(exc.exception),
            f"unexpected error: {exc.exception}",
        )

    # ------------------------------------------------------------------
    # 3) 合法迁移的可达性烟测——不应被未来的拒绝规则误伤
    # ------------------------------------------------------------------

    def test_accepted_remote_blocked_until_first_step_gate(self):
        """合法迁移覆盖：remote_state 在运维证据未到手前不会被禁死（烟测）。

        只允许在 dev=已完成（实现收工）后才写 remote_state=running_remote。
        测试中模拟发布运行的标准顺序。
        """
        self.init_git()
        self.write_csv([self.row(dev_state="已完成", remote_state="not_applicable")])
        # not_applicable 行在 F-012 后不可改 running_remote（junction 宁严）
        with self.assertRaises(StateUpdateError):
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"remote_state": "running_remote"}})

    def test_accepted_remote_running_to_completed(self):
        """running_remote→completed 是正常完成路径。"""
        self.write_csv([self.row(remote_state="running_remote")])
        result = apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                          "set": {"remote_state": "completed"}})
        self.assertEqual(result["row"]["remote_state"], "completed")

    def test_accepted_remote_failed_to_completed_via_pulled(self):
        """failed→artifacts_pulled→completed 是修复后合法证据链。"""
        self.write_csv([self.row(remote_state="failed")])
        result = apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                          "set": {"remote_state": "artifacts_pulled"}})
        self.assertEqual(result["row"]["remote_state"], "artifacts_pulled")

    def test_accepted_remote_failed_to_ingested_directly(self):
        """failed→ingested 是失败后拉走诊断证据的标准路径，合法（受 ingest_evidence 守卫）。"""
        self.init_git()
        self.write_csv([self.row(remote_state="failed")])
        # 缺 RunSpec/证据时拒；有证据时路径可通（被 ingest_completion_errors 拒）。
        with self.assertRaises(StateUpdateError):
            apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                      "set": {"remote_state": "ingested"}})

    def test_accepted_dev_state_progression_chain(self):
        """dev_state 单步推进 未开始→进行中→已完成：合法链。"""
        self.write_csv([self.row()])
        apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                 "set": {"dev_state": "进行中"}})
        result = apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                          "set": {"dev_state": "已完成"}})
        self.assertEqual(result["row"]["dev_state"], "已完成")

    def test_accepted_close_git_state_with_real_commit(self):
        """git_state 在提供真实 HEAD commit_hash + refs 下可正常关闭。"""
        self.init_git()
        head = self.head()
        self.write_csv([self.row(dev_state="已完成", review_initial_state="已完成",
                                 review_regression_state="已完成")])
        result = apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                          "set": {"git_state": "已提交",
                                                  "commit_hash": head,
                                                  "refs": "code.py"}})
        self.assertEqual(result["row"]["git_state"], "已提交")
        self.assertEqual(result["row"]["commit_hash"], head)

    def test_accepted_remote_running_to_failed(self):
        """worker 失败时合法路径。"""
        self.write_csv([self.row(remote_state="running_remote")])
        result = apply_update(self.path, {"schema_version": SCHEMA, "row_id": "I-1",
                                          "set": {"remote_state": "failed"}})
        self.assertEqual(result["row"]["remote_state"], "failed")

    def test_rejected_direct_validate_rows_enum_invalid(self):
        """F-019: _validate_rows 直接拒绝非法枚举值（不经过 apply_update）。"""
        import sys
        sys.path.insert(0, ".codex/skills/mission-csv-execute/scripts")
        from csv_state import _validate_rows, StateUpdateError

        def make_row(**over):
            row = {"id": "I-1", "dev_state": "已完成", "review_initial_state": "已完成",
                   "review_regression_state": "已完成", "git_state": "已提交",
                   "remote_state": "not_applicable"}
            row.update(over)
            return row

        # dev_state 非法值
        with self.assertRaisesRegex(StateUpdateError, "enum_invalid"):
            _validate_rows([make_row(dev_state="INVALID")])
        # remote_state 非法值
        with self.assertRaisesRegex(StateUpdateError, "enum_invalid"):
            _validate_rows([make_row(remote_state="hallucinated")])
        # 空 dev_state
        with self.assertRaisesRegex(StateUpdateError, "enum_invalid"):
            _validate_rows([make_row(dev_state="")])


if __name__ == "__main__":
    unittest.main()
