"""跨真实临时 Git、RunSpec、manifest 和 record 的接口回归；不调用远端。"""
from argparse import Namespace
from contextlib import ExitStack
import copy
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))
from mission_completion import EXPECTED_FIELDS, ingest_completion_errors
from remote_route import decide_remote_route
from harness.remote.build_rrctl_runspec import build_runspec, run_spec_digest
from harness.remote.remote_run import validate_mission_launch, execute
from harness.remote import remote_run
from harness.records import experiment_records as records
from harness.workflow.mission_state import update


class ResearchBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-binding-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Harness Test")
        self.git("config", "user.email", "harness-test@example.invalid")
        source = self.root / "docs/specs/spec.md"
        source.parent.mkdir(parents=True)
        source.write_text("---\nmission: spec\nstatus: approved\ncreated: 2026-09-12\napproved_at: 2026-09-12T00:00:00Z\n---\n\n## Goal\nResearch question\n## Scope\nOne run\n## Design\nFrozen protocol\n## Acceptance Criteria\nRecord the outcome\n")
        (self.root / "train.sh").write_text("#!/bin/sh\nexit 0\n")
        self.git("add", "docs/specs/spec.md", "train.sh")
        self.git("commit", "-m", "fixture approved implementation")
        self.commit = self.git("rev-parse", "HEAD")
        self.csv = self.root / "issues/T/T.csv"
        self.csv.parent.mkdir(parents=True)
        self.row = dict.fromkeys(EXPECTED_FIELDS, "")
        self.row.update(id="RUN-ROW", spec_id="SPEC-A", exp_id="EXP-A", run_id="RUN-A", branch="main",
                        commit_hash=self.commit, git_state="已提交", remote_state="", refs="train.sh",
                        dev_state="进行中", review_initial_state="已完成", review_regression_state="已完成",
                        notes="source_doc:docs/specs/spec.md; command_owner:rrctl")
        self.review = dict(self.row, id="PRERUN-REVIEW-1", run_id="", exp_id="", remote_state="not_applicable",
                           notes=f"gated_run:RUN-ROW; pre_run_code_commit:{self.commit}; pre_run_result:pass; review_mode:scientific_review; review_result:scientifically_correct")
        self.write_csv()
        request = {
            "schema_version": "mission.rrctl-request.v1", "spec_id": "SPEC-A", "exp_id": "EXP-A", "run_id": "RUN-A",
            "project": "test", "source": {"repo_root": str(self.root), "branch": "main", "commit": self.commit},
            "remote": {"profile": "fixture", **{key + "_root": str(self.root / "remote" / key) for key in ("stage", "repo", "control", "output")}},
            "environment": {"kind": "conda", "name": "fixture", "conda_sh": "/fixture/conda.sh"},
            "workload": {"argv": ["bash", "train.sh"]},
            "adapter_contract": {"progress_path": "progress.json", "progress_count_field": "step", "first_step_min_count": 1,
                                 "completion_min_count": 1, "summary_path": "summary.json", "summary_required_fields": ["metric"], "summary_finite_fields": ["metric"]},
            "artifacts": [{"path": "summary.json", "required": True}],
            "metadata": {"mission_csv": "issues/T/T.csv", "mission_row_id": "RUN-ROW"},
            "gate_provenance": {"schema_version": "prerun.gate-provenance.v2", "pre_run_code_commit": self.commit,
                                "review_mode": "scientific_review", "review_result": "scientifically_correct", "reviewer_id": "fixture-independent",
                                "blocker_closure_evidence": []},
        }
        self.request = request
        self.spec = build_runspec(request)
        self.spec_path = self.csv.parent / "runs/RUN-A/runspec.json"
        self.spec_path.parent.mkdir(parents=True)
        self.spec_path.write_text(json.dumps(self.spec))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, check=True).stdout.strip()

    def write_csv(self, rows=None):
        with self.csv.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerows(rows if rows is not None else [self.row, self.review])

    def test_formal_launch_requires_binding_before_any_remote_call(self):
        validate_mission_launch(self.spec)
        broken = copy.deepcopy(self.spec)
        broken["metadata"].pop("mission_csv")
        self.spec_path.write_text(json.dumps(broken))
        with patch.object(remote_run, "resolve_rrctl") as remote:
            with self.assertRaisesRegex(ValueError, "mission_csv"):
                execute(self.spec_path, profiles=None, poll_seconds=600)
            remote.assert_not_called()

    def test_invalid_runspec_cli_reports_validation_error_without_traceback(self):
        broken = copy.deepcopy(self.spec)
        broken["source"]["commit"] = "invalid"
        self.spec_path.write_text(json.dumps(broken))
        result = subprocess.run([sys.executable, str(Path(remote_run.__file__)), str(self.spec_path), "--execute"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("[remote-run]", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_source_drift_wrong_run_or_missing_gate_blocks_launch(self):
        for mutate in (
            lambda s: s["metadata"].pop("gate_provenance"),
            lambda s: s.update(run_id="RUN-other"),
            lambda s: s["metadata"].update(exp_id="EXP-other"),
        ):
            broken = copy.deepcopy(self.spec)
            mutate(broken)
            with self.assertRaises(ValueError):
                validate_mission_launch(broken)
        (self.root / "docs/specs/spec.md").write_text("changed acceptance after review")
        with self.assertRaisesRegex(ValueError, "approved version"):
            validate_mission_launch(self.spec)

    def test_not_evaluable_is_not_a_pass_and_paused_mission_cannot_launch(self):
        original = self.review["notes"]
        self.review["notes"] = original.replace("scientifically_correct", "not_evaluable")
        self.write_csv()
        with self.assertRaisesRegex(ValueError, "scientific gate"):
            validate_mission_launch(self.spec)
        self.review["notes"] = original
        self.write_csv()
        update(self.root, "T", action="register", source_ref="session:user#fixture", csv="issues/T/T.csv")
        update(self.root, "T", action="transition", status="paused", reason="fixture pause", source_ref="session:user#pause")
        with self.assertRaisesRegex(ValueError, "mission_not_active"):
            validate_mission_launch(self.spec)

    def test_low_risk_route_must_cover_the_real_diff(self):
        (self.root / "notes.md").write_text("documentation change")
        self.git("add", "notes.md")
        self.git("commit", "-m", "fixture documentation")
        candidate = self.git("rev-parse", "HEAD")
        spec = copy.deepcopy(self.spec)
        spec["source"]["commit"] = candidate
        spec["metadata"].pop("gate_provenance")
        manifest = {"schema_version": "prerun.change-route.v1", "reviewed_commit": self.commit, "candidate_commit": candidate,
                    "changes": [{"path": "notes.md", "change_class": "documentation", "production_reachable": False,
                                 "dependency_closure_changed": False, "blocker_ids": [], "probes": []}]}
        spec["metadata"]["change_manifest"] = manifest
        self.row["commit_hash"] = candidate
        self.write_csv()
        validate_mission_launch(spec)
        original = self.review["notes"]
        self.review["notes"] = original.replace("pre_run_result:pass", "pre_run_result:failed")
        self.write_csv()
        validate_mission_launch(spec)
        self.write_csv([self.row])
        validate_mission_launch(spec)
        manifest["changes"] = []
        with self.assertRaisesRegex(ValueError, "actual Git diff"):
            validate_mission_launch(spec)

    def changed_request(self, change_class):
        (self.root / "train.sh").write_text(f"#!/bin/sh\n# {change_class}\nexit 0\n")
        self.git("add", "train.sh")
        self.git("commit", "-m", f"fixture {change_class}")
        candidate = self.git("rev-parse", "HEAD")
        request = copy.deepcopy(self.request)
        request["source"]["commit"] = candidate
        request.pop("gate_provenance")
        request["metadata"]["change_manifest"] = {
            "schema_version": "prerun.change-route.v1", "reviewed_commit": self.commit, "candidate_commit": candidate,
            "changes": [{"path": "train.sh", "change_class": change_class, "production_reachable": True,
                         "dependency_closure_changed": False, "blocker_ids": [], "probes": []}],
        }
        self.row["commit_hash"] = candidate
        self.write_csv([self.row])
        return request

    def test_micro_and_smoke_validation_use_probe_evidence_without_historical_prerun(self):
        for change_class in ("shell_syntax", "artifact_transport"):
            with self.subTest(change_class=change_class):
                request = self.changed_request(change_class)
                change = request["metadata"]["change_manifest"]["changes"][0]
                change["probes"] = [{"command": "fixture production check", "exit_code": 0,
                                     "observation": "production path exercised", "reaches_production": True}]
                validate_mission_launch(build_runspec(request))
                change["probes"][0]["exit_code"] = 1
                with self.assertRaisesRegex(ValueError, "route blocked"):
                    validate_mission_launch(build_runspec(request))

    def test_targeted_and_full_changes_still_require_scientific_gate(self):
        for change_class in ("credential", "metric"):
            with self.subTest(change_class=change_class):
                with self.assertRaisesRegex(ValueError, "route blocked"):
                    validate_mission_launch(build_runspec(self.changed_request(change_class)))

    def test_restricted_runs_keep_identity_output_and_control_boundaries(self):
        for purpose in ("pre_review_smoke", "preregistered_read_only_probe"):
            with self.subTest(purpose=purpose):
                request = self.changed_request("loss" if purpose == "pre_review_smoke" else "data")
                request["execution_purpose"] = purpose
                request["remote"]["output_root"] = str(self.root / "remote" / purpose / "RUN-A")
                request["local_pull_root"] = str(self.csv.parent / purpose / "RUN-A")
                common = {"candidate_commit": request["source"]["commit"], "artifact_ingest_disabled": True,
                          "user_authorized": True, "production_command_bound": True, "fail_on_output_collision": True}
                if purpose == "pre_review_smoke":
                    common.update(max_steps=2, gpu_count=1, isolated_output=True,
                                  official_metrics_disabled=True, checkpoint_cleanup_required=True)
                    request["resources"] = {"device": "gpu", "gpu_ids": ["0"]}
                else:
                    common.update(base_validation_only=True, parameter_updates_disabled=True,
                                  official_access_disabled=True, preregistered_before_review=True)
                request["metadata"][purpose] = common
                spec = build_runspec(request)
                validate_mission_launch(spec)
                for mutate in (
                    lambda s: s["metadata"].pop("mission_row_id"),
                    lambda s: s["metadata"][purpose].update(candidate_commit="b" * 40),
                    lambda s: s.update(local_pull_root=str(self.root / "remote_artifacts/EXP-A/RUN-A")),
                    lambda s: s["remote"].update(output_root=str(self.root / "unbound-output")),
                    lambda s: s["metadata"][purpose].update(user_authorized=False),
                    lambda s: s["session"].update(backend="tmux"),
                    lambda s: s["metadata"].update(custom_control_scripts=["temporary-launcher.py"]),
                ):
                    broken = copy.deepcopy(spec)
                    mutate(broken)
                    with self.assertRaises(ValueError):
                        validate_mission_launch(broken)

    def test_inactive_resume_only_observes_bound_run_without_mutating_lifecycle(self):
        self.row["remote_state"] = "running_remote"
        self.write_csv()
        update(self.root, "T", action="register", source_ref="session:user#fixture", csv="issues/T/T.csv")
        for status in ("paused", "cancelled"):
            with self.subTest(status=status):
                update(self.root, "T", action="transition", status=status, reason="fixture stop", source_ref="session:user#stop")
                lifecycle = self.root / "issues/.missions.json"
                before = lifecycle.read_bytes()
                calls = []
                def call(argv, _root):
                    stage = argv[2]
                    calls.append(stage)
                    result = ({"binding": {"run_spec_sha256": run_spec_digest(self.spec)}}
                              if stage == "inspect" else {"status": {"state": "completed"}})
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": result}), "")
                with patch.object(remote_run, "resolve_rrctl", return_value="rrctl"), patch.object(remote_run, "rrctl_call", side_effect=call), patch.object(remote_run, "_emit_stage"):
                    self.assertEqual(execute(self.spec_path, profiles=None, poll_seconds=600, resume=True), 0)
                self.assertEqual(calls, ["inspect", "wait", "pull"])
                self.assertEqual(lifecycle.read_bytes(), before)
                with self.assertRaisesRegex(ValueError, "mission_not_active"):
                    validate_mission_launch(self.spec)

    def test_resume_rejects_unbound_row_and_wrong_remote_digest_before_wait_or_pull(self):
        for field, value in (("run_id", "RUN-other"), ("commit_hash", "b" * 40), ("remote_state", "")):
            with self.subTest(field=field):
                original = dict(self.row)
                self.row["remote_state"] = "running_remote"
                self.row[field] = value
                self.write_csv()
                with patch.object(remote_run, "resolve_rrctl") as remote:
                    with self.assertRaises(ValueError):
                        execute(self.spec_path, profiles=None, poll_seconds=600, resume=True)
                    remote.assert_not_called()
                self.row = original
        self.row["remote_state"] = "running_remote"
        self.write_csv()
        result = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "result": {"binding": {"run_spec_sha256": "0" * 64}}}), "")
        with patch.object(remote_run, "resolve_rrctl", return_value="rrctl"), patch.object(remote_run, "rrctl_call", return_value=result) as remote:
            with self.assertRaisesRegex(ValueError, "bound remote run"):
                execute(self.spec_path, profiles=None, poll_seconds=600, resume=True)
            self.assertEqual([call.args[0][2] for call in remote.call_args_list], ["inspect"])

    def test_old_runspec_resume_uses_canonical_location_without_rewriting_it(self):
        self.row["remote_state"] = "running_remote"
        self.write_csv()
        old = copy.deepcopy(self.spec)
        old["metadata"].pop("mission_csv")
        old["metadata"].pop("mission_row_id")
        self.spec_path.write_text(json.dumps(old))
        before = self.spec_path.read_bytes()
        validate_mission_launch(old, resume=True, spec_path=self.spec_path)
        self.assertEqual(self.spec_path.read_bytes(), before)
        with self.assertRaises(ValueError):
            validate_mission_launch(old)
        with self.assertRaisesRegex(ValueError, "runs/<RunID>"):
            validate_mission_launch(old, resume=True, spec_path=self.root / "arbitrary.json")

    def test_legacy_owner_never_enters_rrctl_launch_or_resume(self):
        self.row["notes"] += "; command_owner:legacy"
        # 只保留一个控制归属，避免把 parser 冲突当成归属校验。
        self.row["notes"] = self.row["notes"].replace("command_owner:rrctl; ", "")
        self.write_csv()
        for resume in (False, True):
            with self.assertRaisesRegex(ValueError, "legacy run's control owner"):
                validate_mission_launch(self.spec, resume=resume, spec_path=self.spec_path)

    def publish_record(self, extra_artifacts=None):
        run_root = self.root / "remote_artifacts/EXP-A/RUN-A"
        run_root.mkdir(parents=True)
        files = {"summary.json": json.dumps({"metric": 1.25, "protocol": "fixed-v1"}),
                 "progress.json": '{"step": 1}', **(extra_artifacts or {})}
        entries = []
        for relative, content in files.items():
            path = run_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            raw = path.read_bytes()
            entries.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
        manifest = {"schema_version": "rrctl.artifacts.v1", "run_id": "RUN-A",
                    "provenance": {"spec_id": "SPEC-A", "exp_id": "EXP-A", "commit": self.commit,
                                   "run_spec_sha256": run_spec_digest(self.spec)},
                    "entries": entries}
        (run_root / "artifact_manifest.json").write_text(json.dumps(manifest))
        with ExitStack() as stack:
            for key, value in (("REPO_ROOT", self.root), ("ARTIFACTS", self.root / "remote_artifacts"),
                               ("EXPERIMENTS", self.root / "research_workspace/experiments"),
                               ("LEDGER", self.root / "research_workspace/EXPERIMENTS.csv")):
                stack.enter_context(patch.object(records, key, value))
            records.cmd_build(Namespace(exp="EXP-A", force=False, config=self.root / "absent.toml"))
            records.cmd_derive(Namespace())
        self.row.update(remote_state="ingested", artifact_path="remote_artifacts/EXP-A/RUN-A")
        return run_root

    def test_ingestion_requires_matching_runspec_manifest_record_and_index(self):
        run_root = self.publish_record()
        self.assertEqual(ingest_completion_errors(self.csv, [self.row], workdir=self.root), [])
        record_path = self.root / "research_workspace/experiments/EXP-A/record.json"
        record = json.loads(record_path.read_text())
        record["runs"][0]["metric"] = 999
        record_path.write_text(json.dumps(record))
        self.assertTrue(any("record_stale" in x for x in ingest_completion_errors(self.csv, [self.row], workdir=self.root)))
        (run_root / "artifact_manifest.json").unlink()
        self.assertTrue(ingest_completion_errors(self.csv, [self.row], workdir=self.root))

    def test_ingestion_rechecks_required_non_summary_artifacts(self):
        self.request["artifacts"].append({"path": "evaluation_details.json", "required": True})
        self.spec = build_runspec(self.request)
        self.spec_path.write_text(json.dumps(self.spec))
        original = '{"samples": 128}'
        run_root = self.publish_record({"evaluation_details.json": original})
        path = run_root / "evaluation_details.json"
        manifest_path = run_root / "artifact_manifest.json"
        manifest = manifest_path.read_text()
        self.assertEqual(ingest_completion_errors(self.csv, [self.row], workdir=self.root), [])
        for change in ("missing", "same_size_edit", "unlisted", "symlink"):
            with self.subTest(change=change):
                if change == "missing":
                    path.unlink()
                elif change == "same_size_edit":
                    path.write_text('{"samples": 999}')
                    self.assertEqual(path.stat().st_size, len(original.encode()))
                elif change == "unlisted":
                    value = json.loads(manifest)
                    value["entries"] = [entry for entry in value["entries"] if entry["path"] != path.name]
                    manifest_path.write_text(json.dumps(value))
                else:
                    replacement = self.root / "replacement-details.json"
                    replacement.write_text(original)
                    path.unlink()
                    path.symlink_to(replacement)
                errors = ingest_completion_errors(self.csv, [self.row], workdir=self.root)
                self.assertTrue(any("required_artifact" in error for error in errors), errors)
                record = records.build_record("EXP-A", None, repo_root=self.root, artifacts=self.root / "remote_artifacts")
                self.assertEqual(record["runs"], [])
                self.assertIsNone(record["metrics"]["ours_metric"])
                self.assertTrue(any("runs.RUN-A.provenance" in gap for gap in record["_pending"]))
                path.unlink(missing_ok=True)
                path.write_text(original)
                manifest_path.write_text(manifest)
                self.assertEqual(ingest_completion_errors(self.csv, [self.row], workdir=self.root), [])

    def test_required_directory_checks_all_of_its_manifest_files(self):
        self.request["artifacts"].append({"path": "evaluation", "required": True})
        self.spec = build_runspec(self.request)
        self.spec_path.write_text(json.dumps(self.spec))
        run_root = self.publish_record({"evaluation/a.json": "{}", "evaluation/nested/b.json": "{}"})
        self.assertEqual(ingest_completion_errors(self.csv, [self.row], workdir=self.root), [])
        for relative in ("evaluation/a.json", "evaluation/nested/b.json"):
            with self.subTest(relative=relative):
                path = run_root / relative
                path.unlink()
                errors = ingest_completion_errors(self.csv, [self.row], workdir=self.root)
                self.assertTrue(any("required_artifact_manifest_mismatch" in error for error in errors), errors)
                path.write_text("{}")
        (run_root / "evaluation/new.json").write_text("{}")
        self.assertTrue(ingest_completion_errors(self.csv, [self.row], workdir=self.root))

    def test_absent_optional_and_on_demand_artifacts_do_not_block_ingestion(self):
        self.request["artifacts"].append({"path": "optional.json", "required": False})
        self.request["artifact_pull_policy"] = {"mode": "minimal", "on_demand": ["debug_trace.jsonl"]}
        self.spec = build_runspec(self.request)
        self.spec_path.write_text(json.dumps(self.spec))
        run_root = self.publish_record()
        self.assertFalse((run_root / "optional.json").exists())
        self.assertFalse((run_root / "debug_trace.jsonl").exists())
        self.assertEqual(ingest_completion_errors(self.csv, [self.row], workdir=self.root), [])

    def test_mixed_historical_run_cannot_enter_ingested_projection(self):
        self.publish_record()
        extra = self.root / "remote_artifacts/EXP-A/RUN-legacy"
        extra.mkdir()
        summary = extra / "summary.json"
        summary.write_text('{"metric":999,"commit":"untrusted"}')
        self.assertTrue(ingest_completion_errors(self.csv, [self.row], workdir=self.root))
        with ExitStack() as stack:
            for key, value in (("REPO_ROOT", self.root), ("ARTIFACTS", self.root / "remote_artifacts"),
                               ("EXPERIMENTS", self.root / "research_workspace/experiments"),
                               ("LEDGER", self.root / "research_workspace/EXPERIMENTS.csv")):
                stack.enter_context(patch.object(records, key, value))
            records.cmd_build(Namespace(exp="EXP-A", force=False, config=self.root / "absent.toml"))
            records.cmd_derive(Namespace())
        record = json.loads((self.root / "research_workspace/experiments/EXP-A/record.json").read_text())
        self.assertEqual([run["run_id"] for run in record["runs"]], ["RUN-A"])
        self.assertTrue(any("RUN-legacy" in gap for gap in record["_pending"]))
        self.assertEqual(ingest_completion_errors(self.csv, [self.row], workdir=self.root), [])
        legacy_row = dict(self.row, run_id="RUN-legacy", artifact_path="remote_artifacts/EXP-A/RUN-legacy")
        self.assertTrue(ingest_completion_errors(self.csv, [legacy_row], workdir=self.root))
        self.assertEqual(json.loads(summary.read_text())["metric"], 999)

    def test_record_source_labels_must_include_the_run_provenance(self):
        self.publish_record()
        path = self.root / "research_workspace/experiments/EXP-A/record.json"
        original = path.read_text()
        for field in ("spec_id", "branch", "mission_csv"):
            with self.subTest(field=field):
                record = json.loads(original)
                record["source"][field] = ["unrelated"]
                path.write_text(json.dumps(record))
                errors = ingest_completion_errors(self.csv, [self.row], workdir=self.root)
                self.assertTrue(any("record_source_mismatch" in item for item in errors), errors)

    def test_manifest_identity_and_summary_tampering_are_rejected(self):
        run_root = self.publish_record()
        manifest_path = run_root / "artifact_manifest.json"
        original = manifest_path.read_text()
        for key, value in (("spec_id", "SPEC-other"), ("exp_id", "EXP-other"),
                           ("commit", "b" * 40), ("run_spec_sha256", "0" * 64)):
            with self.subTest(key=key):
                manifest = json.loads(original)
                manifest["provenance"][key] = value
                manifest_path.write_text(json.dumps(manifest))
                self.assertTrue(any("provenance_mismatch" in x for x in ingest_completion_errors(self.csv, [self.row], workdir=self.root)))
        manifest = json.loads(original)
        manifest["run_id"] = "RUN-other"
        manifest_path.write_text(json.dumps(manifest))
        self.assertTrue(any("manifest_identity_mismatch" in x for x in ingest_completion_errors(self.csv, [self.row], workdir=self.root)))
        manifest_path.write_text(original)
        (run_root / "summary.json").write_text('{"metric": 999, "protocol": "fixed-v1"}')
        self.assertTrue(any("manifest" in x for x in ingest_completion_errors(self.csv, [self.row], workdir=self.root)))

    def test_result_row_keeps_source_commit_separate_from_research_commit(self):
        self.publish_record()
        self.row.update(phase="artifact", commit_hash="f" * 40,
                        notes=f"git_repo:research_workspace; pre_run_code_commit:{self.commit}")
        self.assertEqual(ingest_completion_errors(self.csv, [self.row], workdir=self.root), [])
        self.row["notes"] = "git_repo:research_workspace"
        self.assertTrue(any("source_identity_missing" in x for x in ingest_completion_errors(self.csv, [self.row], workdir=self.root)))
        self.row["remote_state"] = ""
        self.row["commit_hash"] = self.commit
        self.write_csv()
        with self.assertRaisesRegex(ValueError, "source repository"):
            validate_mission_launch(self.spec)

    def test_legacy_launch_blocked_but_existing_run_resume_preserved(self):
        request = {"schema_version": "mission.remote-route.v1", "execution_kind": "remote", "lifecycle": "not_started",
                   "has_running_evidence": False, "command_owner": "legacy", "code_changed": False,
                   "rrctl": {"available": True, "readiness": "not_checked", "launch": "not_checked"}}
        result = decide_remote_route(request)
        self.assertIn("legacy_new_launch_forbidden", result["reason_codes"])
        request.update(lifecycle="running_remote", has_running_evidence=True)
        result = decide_remote_route(request)
        self.assertEqual(result["decision"], "resume")
        self.assertEqual(result["route"], "legacy")


if __name__ == "__main__":
    unittest.main()
