"""当前实验刷新与历史 CLI 兼容；仅使用临时原始证据。"""
from argparse import Namespace
from contextlib import ExitStack
import csv
import hashlib
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
from harness.records import experiment_records as records  # noqa: E402
from harness.remote.build_rrctl_runspec import build_runspec, run_spec_digest  # noqa: E402


class RecordRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="record-refresh-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "remote_artifacts"
        self.experiments = self.root / "research_workspace/experiments"
        self.ledger = self.root / "research_workspace/EXPERIMENTS.csv"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in (("REPO_ROOT", self.root), ("ARTIFACTS", self.artifacts),
                           ("EXPERIMENTS", self.experiments), ("LEDGER", self.ledger)):
            self.stack.enter_context(patch.object(records, key, value))

    def summary(self, exp="E1", run="R1", value=1, name="summary.json", required=("metric",),
                with_metric=True, **extra):
        path = self.artifacts / exp / run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"protocol": "p1", "commit": "a" * 40, **extra}
        if with_metric:
            payload["metric"] = value
        path.write_text(json.dumps(payload))
        run_root = self.artifacts / exp / run
        progress = run_root / "progress.json"
        progress.write_text('{"step": 1}')
        summaries = sorted(run_root.rglob("summary.json"))
        mission = self.root / "issues" / exp
        mission.mkdir(parents=True, exist_ok=True)
        (mission / "tasks.csv").write_text(f"exp_id,spec_id,branch,commit_hash\n{exp},SPEC,main,{'b' * 40}\n")
        spec = build_runspec({
            "schema_version": "mission.rrctl-request.v1", "spec_id": "SPEC", "exp_id": exp, "run_id": run,
            "project": "fixture", "source": {"repo_root": str(self.root), "branch": "main", "commit": "a" * 40},
            "remote": {"profile": "fixture", **{key + "_root": str(self.root / "remote" / run / key)
                                                 for key in ("stage", "repo", "control", "output")}},
            "environment": {"kind": "conda", "name": "fixture", "conda_sh": "/fixture/conda.sh"},
            "workload": {"argv": ["bash", "train.sh"]},
            "adapter_contract": {"progress_path": "progress.json", "progress_count_field": "step", "first_step_min_count": 1,
                                 "completion_min_count": 1, "summary_path": name, "summary_required_fields": list(required)},
            "artifacts": [{"path": item.relative_to(run_root).as_posix(), "required": True} for item in summaries],
            "metadata": {"mission_csv": f"issues/{exp}/tasks.csv"},
        })
        spec_path = mission / "runs" / run / "runspec.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(spec))
        manifest = {"schema_version": "rrctl.artifacts.v1", "run_id": run,
                    "provenance": {"exp_id": exp, "spec_id": "SPEC", "commit": "a" * 40,
                                   "run_spec_sha256": run_spec_digest(spec)},
                    "entries": [{"path": item.relative_to(run_root).as_posix(), "size": len(item.read_bytes()),
                                 "sha256": hashlib.sha256(item.read_bytes()).hexdigest()} for item in [progress, *summaries]]}
        (run_root / "artifact_manifest.json").write_text(json.dumps(manifest))
        return path

    def build(self, exp="E1", force=False, config=None):
        return records.cmd_build(Namespace(exp=exp, force=force,
                                           config=config or self.root / "absent.toml"))

    def attach_validated_outcome(self, record_path):
        validator = self.root / ".codex/skills/post-run-result-analysis/scripts/validate_result_analysis.py"
        validator.parent.mkdir(parents=True, exist_ok=True)
        validator.write_text("def validate_index(index_path, csv_path, *, workdir):\n    return []\n")
        index = self.root / "issues/E1/result-analysis.json"
        entry = {"exp_id": "E1", "run_id": "R1", "scientific_outcome": "inconclusive",
                 "review_evidence_ref": "session:fixture#tool:review",
                 "review_output_sha256": "a" * 64}
        index.write_text(json.dumps({"entries": [entry]}))
        record = json.loads(record_path.read_text())
        record["outcome"] = "inconclusive"
        record["_pending"] = [field for field in record.get("_pending", []) if field != "outcome"]
        record["outcome_meta"] = {
            "schema_version": records.OUTCOME_META_SCHEMA,
            "source": "issues/E1/result-analysis.json",
            "csv_source": "issues/E1/tasks.csv",
            "entries_sha256": hashlib.sha256(json.dumps([entry], sort_keys=True).encode()).hexdigest(),
            "runs_sha256": records._runs_digest(record), "run_ids": ["R1"],
            "review_evidence_ref": entry["review_evidence_ref"],
            "review_output_sha256": entry["review_output_sha256"],
            "applied_at": "2026-01-01T00:00:00+00:00", "applied_by": "test",
        }
        record_path.write_text(json.dumps(record, indent=2) + "\n")

    def record(self, exp="E1"):
        return self.experiments / exp / "record.json"

    def register_runs(self, *run_ids, exp="E1"):
        with (self.root / "issues" / exp / "tasks.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["exp_id", "spec_id", "branch", "commit_hash", "run_id"])
            writer.writeheader()
            writer.writerows(dict(exp_id=exp, spec_id="SPEC", branch="main", commit_hash="a" * 40, run_id=run)
                             for run in run_ids)

    def test_real_rebuild_preserves_validated_outcome_and_resets_on_run_change(self):
        self.summary()
        self.assertEqual(self.build(), 0)
        record_path = self.record()
        self.attach_validated_outcome(record_path)
        config = self.root / "project.toml"
        config.write_text("version = 1\n[records]\nsummary_glob = 'summary.json'\n")
        self.assertEqual(self.build(config=config), 0)
        self.assertEqual(json.loads(record_path.read_text())["outcome"], "inconclusive")
        self.summary(value=2)
        self.assertEqual(self.build(config=config), 0)
        rebuilt = json.loads(record_path.read_text())
        self.assertEqual(rebuilt["outcome"], "pending")
        self.assertIn("outcome", rebuilt["_pending"])

    def test_selected_experiment_refreshes_new_run_and_unchanged_does_not_write(self):
        self.summary()
        self.assertEqual(self.build(), 0)
        path = self.record()
        before = path.stat().st_mtime_ns
        self.build()
        self.assertEqual(before, path.stat().st_mtime_ns)
        self.summary(run="R2", value=2)
        self.build()
        data = json.loads(path.read_text())
        self.assertEqual({x["run_id"] for x in data["runs"]}, {"R1", "R2"})
        self.assertIsNone(data["metrics"]["ours_metric"])

    def test_no_argument_preserves_existing_and_force_remains_explicit(self):
        self.summary()
        self.build()
        before = self.record().read_bytes()
        self.summary(value=2)
        self.summary(exp="E2", value=3)
        self.build(exp=None)
        self.assertEqual(before, self.record().read_bytes())
        self.assertTrue(self.record("E2").is_file())
        self.build(exp=None, force=True)
        self.assertEqual(json.loads(self.record().read_text())["metrics"]["ours_metric"], 2)

    def test_replace_failure_preserves_existing_record(self):
        self.summary()
        self.build()
        before = self.record().read_bytes()
        self.summary(value=2)
        with patch.object(records.os, "replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual(self.record().read_bytes(), before)
        self.assertEqual(list(self.record().parent.glob("*.tmp")), [])

    def test_unknown_record_is_not_overwritten_even_with_force(self):
        self.summary()
        self.build()
        path = self.record()
        original = path.read_text()
        for location in ("top", "run", "dimensions"):
            with self.subTest(location=location):
                data = json.loads(original)
                target = data if location == "top" else data["runs"][0]
                if location == "dimensions":
                    target = target["dimensions"]
                target["custom_analysis"] = "preserve this"
                path.write_text(json.dumps(data))
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "unknown.*fields"):
                    self.build(force=True)
                self.assertEqual(path.read_bytes(), before)

    def test_run_count_is_not_summary_count_and_no_implicit_aggregation(self):
        self.summary(name="fold0/summary.json")
        self.summary(name="fold1/summary.json", value=2)
        record = records.build_record("E1", None, {"summary_glob": "*/summary.json"})
        self.assertEqual(len(record["runs"]), 2)
        self.assertIsNone(record["metrics"]["ours_metric"])
        self.record().parent.mkdir(parents=True)
        self.record().write_text(json.dumps(record))
        records.cmd_derive(Namespace())
        with self.ledger.open() as stream:
            self.assertEqual(list(csv.DictReader(stream))[0]["Runs"], "1")
        stamp = self.ledger.stat().st_mtime_ns
        records.cmd_derive(Namespace())
        self.assertEqual(stamp, self.ledger.stat().st_mtime_ns)

    def test_source_comes_from_run_not_metadata_commit(self):
        self.summary()
        record = records.build_record("E1", {"commit": {"b" * 40}})
        self.assertEqual(record["source"]["commit"], ["a" * 40])
        self.summary(commit=None)
        record = records.build_record("E1", {"commit": {"b" * 40}})
        self.assertEqual(record["runs"][0]["commit"], "a" * 40)
        self.assertEqual(record["source"]["commit"], ["a" * 40])

    def test_summary_cannot_claim_another_run_or_experiment(self):
        for extra in ({"run_id": "R-other"}, {"exp_id": "E-other"}, {"commit": "c" * 40}):
            with self.subTest(extra=extra):
                self.summary(**extra)
                record = records.build_record("E1", None)
                self.assertEqual(record["runs"], [])
                self.assertIsNone(record["source"]["commit"])
                self.assertTrue(any("identity mismatch" in gap for gap in record["_pending"]))

    def test_missing_provenance_refreshes_formal_record_to_pending_without_touching_raw(self):
        for missing in ("manifest", "runspec"):
            with self.subTest(missing=missing):
                summary = self.summary()
                original = summary.read_bytes()
                self.build()
                path = (summary.parent / "artifact_manifest.json" if missing == "manifest"
                        else self.root / "issues/E1/runs/R1/runspec.json")
                path.unlink()
                self.build()
                record = json.loads(self.record().read_text())
                self.assertEqual(record["runs"], [])
                self.assertIsNone(record["metrics"]["ours_metric"])
                self.assertIsNone(record["source"]["commit"])
                self.assertTrue(any("runs.R1.provenance" in gap for gap in record["_pending"]))
                self.assertEqual(summary.read_bytes(), original)

    def test_unbound_second_run_does_not_pollute_formal_metrics_or_source(self):
        self.summary()
        second = self.summary(run="R2", value=999, commit="c" * 40)
        (second.parent / "artifact_manifest.json").unlink()
        self.build()
        record = json.loads(self.record().read_text())
        self.assertEqual([run["run_id"] for run in record["runs"]], ["R1"])
        self.assertEqual(record["metrics"]["ours_metric"], 1)
        self.assertEqual(record["source"]["commit"], ["a" * 40])
        self.assertTrue(any("runs.R2.provenance" in gap for gap in record["_pending"]))

    def test_ambiguous_runspec_is_pending_instead_of_guessing_a_mission(self):
        self.summary()
        other = self.root / "issues/other"
        spec_path = other / "runs/R1/runspec.json"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_bytes((self.root / "issues/E1/runs/R1/runspec.json").read_bytes())
        (other / "tasks.csv").write_text("exp_id\nE1\n")
        record = records.build_record("E1", None)
        self.assertEqual(record["runs"], [])
        self.assertTrue(any("ambiguous" in gap for gap in record["_pending"]))

    def test_removed_artifacts_do_not_leave_an_old_formal_record_on_refresh(self):
        self.summary()
        self.register_runs("R1")
        self.build()
        shutil.rmtree(self.artifacts / "E1")
        self.build()
        record = json.loads(self.record().read_text())
        self.assertEqual(record["runs"], [])
        self.assertIsNone(record["source"]["commit"])
        self.assertIsNone(record["metrics"]["ours_metric"])
        self.assertIn("runs.R1.provenance: artifacts missing", record["_pending"])

    def test_missing_registered_run_is_pending_until_evidence_is_restored(self):
        self.summary()
        self.summary(run="R2", value=2)
        self.register_runs("R1", "R2")
        self.build()
        records.cmd_derive(Namespace())
        before = self.record().read_bytes()
        backup = self.root / "saved-R2"
        shutil.copytree(self.artifacts / "E1/R2", backup)
        for replacement in ("missing", "file"):
            with self.subTest(replacement=replacement):
                shutil.rmtree(self.artifacts / "E1/R2")
                if replacement == "file":
                    (self.artifacts / "E1/R2").write_text("not a run directory")
                self.build()
                record = json.loads(self.record().read_text())
                self.assertEqual([run["run_id"] for run in record["runs"]], ["R1"])
                self.assertEqual(record["runs"][0]["metric"], 1)
                self.assertIsNone(record["metrics"]["ours_metric"])
                self.assertIn("runs.R2.provenance: artifacts missing", record["_pending"])
                self.assertIn("metrics.ours_metric", record["_pending"])
                records.cmd_derive(Namespace())
                with self.ledger.open() as stream:
                    index = list(csv.DictReader(stream))[0]
                self.assertEqual(index["Runs"], "1")
                self.assertEqual(index["OursMetric"], "")
                stamp = self.record().stat().st_mtime_ns
                self.build()
                self.assertEqual(stamp, self.record().stat().st_mtime_ns)
                (self.artifacts / "E1/R2").unlink(missing_ok=True)
                shutil.copytree(backup, self.artifacts / "E1/R2")
                self.build()
                self.assertEqual(before, self.record().read_bytes())

    def test_invalid_registered_run_does_not_reduce_experiment_to_one_result(self):
        self.summary()
        second = self.summary(run="R2", value=2)
        self.register_runs("R1", "R2")
        (second.parent / "artifact_manifest.json").unlink()
        record = records.build_record("E1", None)
        self.assertEqual([run["run_id"] for run in record["runs"]], ["R1"])
        self.assertIsNone(record["metrics"]["ours_metric"])
        self.assertTrue(any("runs.R2.provenance" in gap for gap in record["_pending"]))

    def test_contract_without_scalar_metric_still_projects_the_run(self):
        # 链/控制类 pipeline：合同不声明标量指标，摘要里也就没有它。
        self.summary(with_metric=False, required=())
        record = records.build_record("E1", None)
        self.assertEqual([run["run_id"] for run in record["runs"]], ["R1"])
        self.assertIsNone(record["runs"][0]["metric"])
        self.assertIsNone(record["metrics"]["ours_metric"])
        self.assertIn("metrics.ours_metric", record["_pending"])
        self.assertEqual(record["source"]["commit"], ["a" * 40])

    def test_contract_requiring_the_metric_keeps_a_missing_metric_fatal(self):
        self.summary(with_metric=False, required=("metric",))
        record = records.build_record("E1", None)
        self.assertEqual(record["runs"], [])
        self.assertTrue(any("summary_missing: metric" in gap for gap in record["_pending"]))

    def test_present_but_non_finite_metric_stays_fatal_without_contract_requirement(self):
        for extra in ({"metric": None}, {"metric": "0.5"}, {"metric": float("nan")}):
            with self.subTest(extra=extra):
                self.summary(with_metric=False, required=(), **extra)
                record = records.build_record("E1", None)
                self.assertEqual(record["runs"], [])
                self.assertTrue(any("not finite" in gap for gap in record["_pending"]))

    def test_restricted_runs_do_not_require_official_artifacts(self):
        self.summary()
        mission = self.root / "issues/E1"
        original = json.loads((mission / "runs/R1/runspec.json").read_text())
        for purpose in ("pre_review_smoke", "preregistered_read_only_probe"):
            with self.subTest(purpose=purpose):
                restricted = json.loads(json.dumps(original))
                restricted["run_id"] = "R-probe"
                restricted["metadata"]["execution_purpose"] = purpose
                path = mission / "runs/R-probe/runspec.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(restricted))
                self.register_runs("R1", "R-probe")
                record = records.build_record("E1", None)
                self.assertEqual(record["metrics"]["ours_metric"], 1)
                self.assertFalse(any("R-probe" in gap for gap in record["_pending"]))
        restricted["metadata"]["execution_purpose"] = ["invalid-purpose"]
        path.write_text(json.dumps(restricted))
        record = records.build_record("E1", None)
        self.assertIsNone(record["metrics"]["ours_metric"])
        self.assertIn("runs.R-probe.provenance: artifacts missing", record["_pending"])

    def recovery(self, *, state="failed", exit_code=1, **summary_fields):
        self.summary(**summary_fields)
        run_root = self.artifacts / "E1/R1"
        spec_path = self.root / "issues/E1/runs/R1/runspec.json"
        spec = json.loads(spec_path.read_text())
        manifest_path = run_root / "artifact_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("provenance")
        manifest.update(recovery_id="fixture-recovery", run_spec_sha256=run_spec_digest(spec))
        for entry in manifest["entries"]:
            original = run_root / entry["path"]
            target = run_root / "output" / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            original.rename(target)
            entry["path"] = "output/" + entry["path"]
        manifest_path.write_text(json.dumps(manifest))
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        validation = self.root / "issues/E1/validation"
        validation.mkdir(parents=True, exist_ok=True)
        status = validation / "inspect-terminal.json"
        status.write_text(json.dumps({"result": {"status": {
            "run_id": "R1", "run_spec_sha256": run_spec_digest(spec),
            "state": state, "detail": {"exit_code": exit_code}}}}))
        records.cmd_bind_recovery(Namespace(csv=validation.parent / "tasks.csv", exp="E1", run="R1",
            status=status, reason="Retain independently verified assets, not a successful execution"), repo_root=self.root)
        binding_path = validation / "recovery-binding.json"
        return binding_path, validation / "recovery-coverage-R1.json"

    def test_failure_cannot_be_laundered_by_normal_manifest_provenance(self):
        for failure in ({"complete": False}, {"status": "failed"}, {"exit_code": 1},
                        {"workload_exit_code": 2}, {"terminal_state": "failed"},
                        {"healthy": False}, {"completion_contract_passed": False}):
            with self.subTest(failure=failure):
                summary = self.summary(**failure)
                before = {p: p.read_bytes() for p in summary.parent.rglob("*") if p.is_file()}
                csv_path = self.root / "issues/E1/tasks.csv"
                with self.assertRaisesRegex(ValueError, "summary_failure"):
                    records.load_run_provenance(csv_path, "E1", "R1", repo_root=self.root)
                self.assertEqual(records.build_record("E1", None)["runs"], [])
                self.assertTrue(all(p.read_bytes() == content for p, content in before.items()))

    def test_terminal_failure_wins_even_when_summary_and_manifest_look_successful(self):
        self.summary(complete=True)
        validation = self.root / "issues/E1/runs/R1"
        status = validation / "terminal.json"
        spec = json.loads((validation / "runspec.json").read_text())
        for state, code in (("failed", 0), ("failed", 1), ("completed", 1)):
            with self.subTest(state=state, code=code):
                status.write_text(json.dumps({"result": {"status": {
                    "run_id": "R1", "run_spec_sha256": run_spec_digest(spec),
                    "state": state, "detail": {"exit_code": code}}}}))
                record = records.build_record("E1", None)
                self.assertEqual(record["runs"], [])
                self.assertTrue(any("terminal_failure" in gap for gap in record["_pending"]))

    def completion_receipt(self):
        self.summary(scientific_outcome="gate_failed")
        run_root = self.artifacts / "E1/R1"
        spec = json.loads((self.root / "issues/E1/runs/R1/runspec.json").read_text())
        manifest = json.loads((run_root / "artifact_manifest.json").read_text())
        from remote_run_control.jsonutil import sha256_json
        control = run_root / ".rrctl"
        control.mkdir()
        binding = {"run_id": "R1", "run_spec_sha256": run_spec_digest(spec),
                   "control_root": spec["remote"]["control_root"],
                   "worker_sha256": "b" * 64, "created_at": "2026-01-01T00:00:00Z"}
        receipt = {"schema_version": "rrctl.completion.v1", "run_id": "R1",
                   "run_spec_sha256": run_spec_digest(spec), "run_identity": sha256_json(binding),
                   "manifest_sha256": sha256_json(manifest), "exit_code": 0,
                   "health": {"schema_version": "rrctl.health.v2", "phase": "completion",
                              "healthy": True, "complete": True}}
        status = {"run_id": "R1", "state": "completed", "detail": {
            "exit_code": 0, "completion_sha256": sha256_json(receipt)}}
        for name, value in (("completion", receipt), ("status", status), ("binding", binding)):
            (control / f"{name}.json").write_text(json.dumps(value))
        return control, spec

    def test_retained_receipt_proves_completion_and_tampering_is_rejected(self):
        control, spec = self.completion_receipt()
        run = records.build_record("E1", None)["runs"][0]
        self.assertIs(run["completion_contract_passed"], True)
        self.assertEqual((run["terminal"]["state"], run["terminal"]["exit_code"]), ("completed", 0))
        for name in ("completion", "status", "binding"):
            path = control / f"{name}.json"
            before = path.read_bytes()
            payload = json.loads(before)
            if name == "completion":
                payload["manifest_sha256"] = "f" * 64
            elif name == "status":
                payload["detail"]["completion_sha256"] = "f" * 64
            else:
                payload["run_spec_sha256"] = "f" * 64
            path.write_text(json.dumps(payload))
            self.assertEqual(records.build_record("E1", None)["runs"], [])
            path.write_bytes(before)

    def test_legacy_record_comparison_preserves_strict_identity_and_failure_semantics(self):
        self.summary()
        current = records.build_record("E1", None)["runs"]
        old = [{key: value for key, value in current[0].items()
                if key not in {"evidence_mode", "terminal", "completion_contract_passed"}}]
        before = json.dumps(old, sort_keys=True)
        self.assertTrue(records.runs_match_projection(old, current))
        proven = [{**current[0], "completion_contract_passed": True}]
        self.assertTrue(records.runs_match_projection(old, proven))
        self.assertEqual(json.dumps(old, sort_keys=True), before)
        for change in ({"metric": 2}, {"run_spec_sha256": "f" * 64}, {"commit": "f" * 40},
                       {"evidence_mode": "recovery", "completion_contract_passed": False},
                       {"terminal": {"state": "failed", "exit_code": 1}}):
            self.assertFalse(records.runs_match_projection(old, [{**current[0], **change}]))
        claimed = [{**old[0], "completion_contract_passed": True}]
        self.assertFalse(records.runs_match_projection(claimed, current))
        self.assertFalse(records.runs_match_projection(claimed, [{**current[0], "evidence_mode": "recovery",
                                                                 "completion_contract_passed": False}]))

    def test_legacy_receipt_and_workload_completion_filename_are_distinct(self):
        control, _ = self.completion_receipt()
        for path in control.iterdir():
            path.rename(control.parent / path.name)
        control.rmdir()
        self.assertIs(records.build_record("E1", None)["runs"][0]["completion_contract_passed"], True)
        manifest_path = control.parent / "artifact_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        workload = control.parent / "completion.json"
        workload.write_text('{"application_state": "done"}')
        manifest["entries"].append({"path": "completion.json", "size": len(workload.read_bytes()),
                                    "sha256": hashlib.sha256(workload.read_bytes()).hexdigest()})
        manifest_path.write_text(json.dumps(manifest))
        self.assertIsNone(records.build_record("E1", None)["runs"][0]["completion_contract_passed"])

    def test_retained_failure_cannot_be_overridden_by_real_clean_receipt(self):
        _, spec = self.completion_receipt()
        terminal = self.root / "issues/E1/runs/R1/terminal.json"
        terminal.write_text(json.dumps({"run_id": "R1", "run_spec_sha256": run_spec_digest(spec),
                                        "state": "failed", "detail": {"exit_code": 1}}))
        self.assertEqual(records.build_record("E1", None)["runs"], [])

    def test_only_current_run_control_observations_can_block_ingest(self):
        self.summary(outcome="failed", scientific_outcome="gate_failed")
        mission = self.root / "issues/E1"
        validation = mission / "validation"
        validation.mkdir()
        (validation / "unrelated-status.json").write_text("[]")
        (self.artifacts / "E1/R1/model-status.json").write_text("[]")
        other = validation / "rrctl-observations/R-other"
        other.mkdir(parents=True)
        (other / "wait-other.json").write_text("[]")
        self.assertEqual(len(records.build_record("E1", None)["runs"]), 1)
        spec = json.loads((mission / "runs/R1/runspec.json").read_text())
        observations = validation / "rrctl-observations/R1"
        observations.mkdir()
        failure = {"schema_version": "research.rrctl-observation.v1", "run_id": "R1",
                   "run_spec_sha256": run_spec_digest(spec), "stage": "wait", "response": {
                       "result": {"status": {"run_id": "R1", "state": "failed", "detail": {"exit_code": 2}}}}}
        from harness.remote.remote_run import retain_observation
        timeout = {"ok": False, "error": {"code": "observer_timeout", "details": {"remote_workload_preserved": True}}}
        retain_observation(spec, "wait", timeout)
        self.assertEqual(len(records.build_record("E1", None)["runs"]), 1)
        retain_observation(spec, "wait", failure["response"])
        clean = json.loads(json.dumps(failure))
        clean["response"]["result"]["status"].update(state="completed", detail={"exit_code": 0})
        retain_observation(spec, "wait", clean["response"])
        record = records.build_record("E1", None)
        self.assertEqual(record["runs"], [])
        self.assertTrue(any("terminal_failure" in gap for gap in record["_pending"]))

    def test_real_local_worker_pull_and_nonzero_recovery_to_records(self):
        # Exercise the production Controller + detached worker, not a fabricated recovery manifest.
        import importlib.util
        fixture_path = ROOT / ".agents/harness/remote/rrctl/tests/test_process_backend.py"
        module_spec = importlib.util.spec_from_file_location("record_process_fixture", fixture_path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        from remote_run_control.models import RunSpec
        from remote_run_control.errors import RRCError
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                fixture = module.ProcessBackendTests()
                fixture.setUp()
                try:
                    spec_dict = fixture.spec(delay=0.1, exit_code=exit_code).to_dict()
                    spec_dict["metadata"] = {"spec_id": "SPEC", "exp_id": "E1",
                                             "mission_csv": "issues/E1/tasks.csv"}
                    spec_dict["local_pull_root"] = str(fixture.repo / "remote_artifacts/E1")
                    spec = RunSpec.from_dict(spec_dict)
                    mission = fixture.repo / "issues/E1"
                    csv_path = mission / "tasks.csv"
                    try:
                        fixture.control.launch(spec)
                    except RRCError as exc:
                        self.assertEqual(exc.code, "first_step_terminal")
                    observed = fixture.control.wait(spec.run_id, poll_seconds=0.05, max_wait_seconds=5)
                    self.assertEqual(observed["status"]["state"], "completed" if exit_code == 0 else "failed")
                    mission.mkdir(parents=True)
                    csv_path.write_text(f"exp_id,spec_id,run_id\nE1,SPEC,{spec.run_id}\n")
                    spec_path = mission / "runs" / spec.run_id / "runspec.json"
                    spec_path.parent.mkdir(parents=True)
                    spec_path.write_text(json.dumps(spec.to_dict()))
                    if exit_code == 0:
                        fixture.control.pull(spec.run_id)
                        _, manifest = records.load_run_provenance(csv_path, "E1", spec.run_id, repo_root=fixture.repo)
                        self.assertIs(manifest["completion_contract_passed"], True)
                    else:
                        destination = fixture.repo / "remote_artifacts/E1" / spec.run_id
                        with self.assertRaises(RRCError):
                            fixture.control.recover_evidence(spec.run_id, paths=["summary.json", "started.json"],
                                                            destination=destination)
                        fixture.control.recover_evidence(spec.run_id, paths=["summary.json", "started.json"],
                                                        destination=destination, allow_nonzero_exit=True)
                        raw_before = {p: p.read_bytes() for base in (destination, Path(spec.remote.control_root))
                                      for p in base.rglob("*") if p.is_file()}
                        # Use the production Mission observation writer; no hand-filled status/digest.
                        from harness.remote.remote_run import retain_observation
                        status_path = retain_observation(spec.to_dict(), "wait", {"ok": True, "result": observed})
                        with patch.object(records, "REPO_ROOT", fixture.repo), patch.object(sys, "argv", [
                                "experiment_records.py", "bind-recovery", "--csv", str(csv_path),
                                "--exp", "E1", "--run", spec.run_id, "--status", str(status_path),
                                "--reason", "Preserve failed-run assets"]):
                            self.assertEqual(records.main(), 0)
                        _, manifest = records.load_run_provenance(csv_path, "E1", spec.run_id, repo_root=fixture.repo)
                        self.assertIs(manifest["completion_contract_passed"], False)
                        self.assertEqual(manifest["evidence_mode"], "recovery")
                        self.assertEqual((manifest["terminal"]["state"], manifest["terminal"]["exit_code"]), ("failed", 7))
                        self.assertTrue(all(path.read_bytes() == data for path, data in raw_before.items()))
                finally:
                    fixture.tearDown()

    def test_healthy_legacy_ingest_does_not_invent_completion_or_terminal_facts(self):
        self.summary(scientific_outcome="hypothesis_not_supported")
        run = records.build_record("E1", None)["runs"][0]
        self.assertEqual(run["evidence_mode"], "completion")
        self.assertIsNone(run["completion_contract_passed"])
        self.assertIsNone(run["terminal"]["state"])
        self.assertIsNone(run["terminal"]["exit_code"])

    def test_recovered_failed_assets_are_not_normal_completion_and_raw_is_unchanged(self):
        self.recovery(complete=False)
        before = {p: p.read_bytes() for base in (self.artifacts, self.root / "issues")
                  for p in base.rglob("*") if p.is_file()}
        self.assertEqual(self.build(), 0)
        record = json.loads(self.record().read_text())
        run = record["runs"][0]
        self.assertEqual(run["evidence_mode"], "recovery")
        self.assertIs(run["completion_contract_passed"], False)
        self.assertEqual((run["terminal"]["state"], run["terminal"]["exit_code"]), ("failed", 1))
        self.assertIn("/output/summary.json", run["summary_path"])
        self.build()  # refresh recognizes its own new fields
        records.cmd_derive(Namespace())
        self.assertEqual(record, json.loads(self.record().read_text()))
        self.assertTrue(all(p.read_bytes() == content for p, content in before.items()))

    def test_recovery_binding_is_generic_and_cannot_be_replaced_with_local_provenance(self):
        binding_path, report = self.recovery(state="workload_complete", exit_code=0)
        manifest_path = self.artifacts / "E1/R1/artifact_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["provenance"] = {"exp_id": "E1", "spec_id": "SPEC", "commit": "a" * 40,
                                  "run_spec_sha256": manifest["run_spec_sha256"]}
        manifest_path.write_text(json.dumps(manifest))
        record = records.build_record("E1", None)
        self.assertEqual(record["runs"], [])
        self.assertTrue(any("binding_identity_mismatch" in gap for gap in record["_pending"]))

    def test_recovery_rejects_unbound_changed_or_incomplete_evidence(self):
        for bad in ("binding_digest", "manifest_digest", "status_hash", "report_hash", "report_count",
                    "report_entries", "missing_required", "symlink", "status_running"):
            with self.subTest(bad=bad):
                # Recreate the fixture in a fresh isolated root for each mutation.
                with tempfile.TemporaryDirectory(prefix="record-recovery-") as directory:
                    old_root, old_artifacts = self.root, self.artifacts
                    self.root = Path(directory)
                    self.artifacts = self.root / "remote_artifacts"
                    try:
                        binding_path, report_path = self.recovery()
                        binding = json.loads(binding_path.read_text())
                        entry = binding["bindings"][0]
                        if bad == "binding_digest":
                            entry["run_spec_sha256"] = "f" * 64
                        elif bad == "manifest_digest":
                            entry["manifest_sha256"] = "f" * 64
                        elif bad in ("status_hash", "report_hash"):
                            entry["status_evidence" if bad == "status_hash" else "coverage_report"]["sha256"] = "f" * 64
                        elif bad in ("report_count", "report_entries"):
                            report = json.loads(report_path.read_text())
                            report["verified_count" if bad == "report_count" else "verified_entries"] = 0 if bad == "report_count" else []
                            report_path.write_text(json.dumps(report))
                            entry["coverage_report"]["sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
                        elif bad == "missing_required":
                            (self.artifacts / "E1/R1/output/summary.json").unlink()
                        elif bad == "symlink":
                            status = self.root / "issues/E1" / entry["status_evidence"]["path"]
                            saved = status.with_suffix(".saved")
                            status.rename(saved)
                            status.symlink_to(saved)
                        elif bad == "status_running":
                            status = self.root / "issues/E1" / entry["status_evidence"]["path"]
                            payload = json.loads(status.read_text())
                            payload["result"]["status"]["state"] = "running"
                            status.write_text(json.dumps(payload))
                            entry["status_evidence"]["sha256"] = hashlib.sha256(status.read_bytes()).hexdigest()
                        binding_path.write_text(json.dumps(binding))
                        record = records.build_record("E1", None, repo_root=self.root, artifacts=self.artifacts)
                        self.assertEqual(record["runs"], [])
                        self.assertTrue(any("runs.R1.provenance" in gap for gap in record["_pending"]))
                    finally:
                        self.root, self.artifacts = old_root, old_artifacts

    def test_artifact_symlink_outside_project_stays_pending(self):
        outside_temp = tempfile.TemporaryDirectory(prefix="outside-evidence-")
        self.addCleanup(outside_temp.cleanup)
        self.artifacts.mkdir()
        (self.artifacts / "E1").symlink_to(outside_temp.name, target_is_directory=True)
        record = records.build_record("E1", None)
        self.assertEqual(record["runs"], [])
        self.assertTrue(any("outside workspace" in gap for gap in record["_pending"]))


if __name__ == "__main__":
    unittest.main()
