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
from harness.records import experiment_records as records
from harness.remote.build_rrctl_runspec import build_runspec, run_spec_digest


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

    def summary(self, exp="E1", run="R1", value=1, name="summary.json", **extra):
        path = self.artifacts / exp / run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"metric": value, "protocol": "p1", "commit": "a" * 40, **extra}))
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
                                 "completion_min_count": 1, "summary_path": name, "summary_required_fields": ["metric"]},
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

    def build(self, exp="E1", force=False):
        return records.cmd_build(Namespace(exp=exp, force=force, config=self.root / "absent.toml"))

    def record(self, exp="E1"):
        return self.experiments / exp / "record.json"

    def register_runs(self, *run_ids, exp="E1"):
        with (self.root / "issues" / exp / "tasks.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["exp_id", "spec_id", "branch", "commit_hash", "run_id"])
            writer.writeheader()
            writer.writerows(dict(exp_id=exp, spec_id="SPEC", branch="main", commit_hash="a" * 40, run_id=run)
                             for run in run_ids)

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
