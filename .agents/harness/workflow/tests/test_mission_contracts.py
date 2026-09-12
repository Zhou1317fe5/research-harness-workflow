"""Mission 状态事实与引用完整性回归；Git 操作仅发生在临时仓库。"""
import csv
import argparse
from contextlib import chdir
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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


class MissionContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mission-contracts-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "tasks.csv"

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

    def test_real_e2e_cannot_use_only_command_or_static_row(self):
        claim, rows = self.claim_fixture()
        self.assertTrue(self.validate_claim([{**claim, "evidence_refs": ["command:pytest -q"]}], rows))
        rows[0]["notes"] = rows[0]["notes"].replace("evidence_level:real_e2e", "evidence_level:static")
        self.assertTrue(any("real_e2e" in x for x in self.validate_claim([claim], rows)))


if __name__ == "__main__":
    unittest.main()
