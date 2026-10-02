"""EXPERIMENTS.csv 派生一致性与写入边界；所有路径都替换到隔离目录。"""
from argparse import Namespace
import csv
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack, redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
from harness.records import experiment_records as records  # noqa: E402


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="records-ledger-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.experiments = self.root / "research_workspace/experiments"
        self.ledger = self.root / "research_workspace/EXPERIMENTS.csv"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in (("REPO_ROOT", self.root), ("ARTIFACTS", self.root / "remote_artifacts"),
                           ("EXPERIMENTS", self.experiments), ("LEDGER", self.ledger)):
            self.stack.enter_context(patch.object(records, key, value))

    def record(self, exp_id, **overrides):
        base = {
            "exp_id": exp_id, "parent": None, "relation": None,
            "source": {"spec_id": ["SPEC-A", "SPEC-X"], "branch": ["main"],
                       "commit": sorted(["a" * 40, "c" * 40]), "mission_csv": []},
            "metrics": {"protocol": "official", "baseline_id": None, "baseline_run_id": None,
                        "baseline_metric": None, "ours_metric": 0.5, "delta_metric": None},
            "runs": [{"run_id": "R1", "metric": 0.5}],
            "outcome": "supported", "artifact_path": None,
        }
        base.update(overrides)
        path = self.experiments / exp_id / "record.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n")
        return base

    def derive(self):
        return records.cmd_derive(Namespace())

    def read_ledger(self):
        with self.ledger.open(newline="") as stream:
            return list(csv.DictReader(stream))

    def test_index_row_mapping_defaults_and_joining(self):
        row = records.record_index_row(self.record("E1"))
        self.assertEqual(row["SpecID"], "SPEC-A;SPEC-X")
        self.assertEqual(row["Commit"], ";".join(["a" * 40, "c" * 40]))
        self.assertEqual(row["ArtifactPath"], "")
        row = records.record_index_row(self.record("E2", artifact_path="remote_artifacts/E2/",
                                                   metrics={"ours_metric": None, "delta_metric": -0.25,
                                                            "protocol": None, "baseline_id": "BASE1"},
                                                   runs=[{"run_id": "R2"}, {"run_id": "R2"},
                                                         {"run_id": "R3"}, {"run_id": ""}, {}]))
        self.assertEqual(row["Runs"], 2)
        self.assertEqual(row["OursMetric"], "")
        self.assertEqual(row["DeltaMetric"], -0.25)
        self.assertEqual(row["Protocol"], "")
        self.assertEqual(row["BaselineID"], "BASE1")

    def test_derive_sorts_by_exp_id_and_matches_records(self):
        self.record("E3")
        self.record("E1", outcome="pending")
        self.record("E2")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.derive(), 0)
        rows = self.read_ledger()
        self.assertEqual([row["ExpID"] for row in rows], ["E1", "E2", "E3"])
        self.assertEqual(rows[0]["Outcome"], "pending")
        for row, path in zip(rows, sorted(self.experiments.glob("*/record.json"))):
            source = json.loads(path.read_text())
            self.assertEqual(row["ExpID"], source["exp_id"])
            self.assertEqual(row["Protocol"], source["metrics"].get("protocol") or "")
            self.assertEqual(row["SpecID"], ";".join(source["source"].get("spec_id") or []))

    def test_derive_without_records_returns_1_and_writes_nothing(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.derive(), 1)
        self.assertFalse(self.ledger.exists())

    def test_write_if_changed_is_atomic_idempotent_and_no_wider_permissions(self):
        target = self.root / "nested/out.txt"
        self.assertTrue(records._write_if_changed(target, b"one\n"))
        before = target.stat().st_mtime_ns
        self.assertFalse(records._write_if_changed(target, b"one\n"))
        self.assertEqual(target.stat().st_mtime_ns, before)
        # mkstemp 创建的临时文件 0o600，os.replace 保留其 mode。
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertTrue(records._write_if_changed(target, b"two\n"))
        self.assertEqual(target.read_bytes(), b"two\n")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["out.txt"])

    def test_write_if_changed_failure_leaves_no_temp_files(self):
        target = self.root / "fail.txt"
        with patch("tempfile.mkstemp", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                records._write_if_changed(target, b"x")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.iterdir()), [])
        real_write = records._write_if_changed
        def failing(path, content):
            original_replace = records.os.replace
            def boom(*args, **kwargs):
                raise OSError("write failed")
            records.os.replace = boom
            try:
                return real_write(path, content)
            finally:
                records.os.replace = original_replace
        with self.assertRaises(OSError):
            failing(target, b"x")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_result_analysis_outcome_writeback_groups_runs_and_is_idempotent(self):
        self.record("E1", runs=[{"run_id": "R1"}, {"run_id": "R2"}], outcome="pending")
        index = self.root / "issues/spec/reviews/result-analysis.json"
        index.parent.mkdir(parents=True)
        entries = [{
            "exp_id": "E1", "run_id": run_id,
            "scientific_outcome": "hypothesis_supported",
            "review_evidence_ref": "exec:reviews/result-analysis-E1/verdict.json#verdict",
            "review_output_sha256": "f" * 64,
        } for run_id in ("R1", "R2")]
        index.write_text(json.dumps({"entries": entries}, ensure_ascii=False))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (1, 0))
        record_path = self.experiments / "E1/record.json"
        record = json.loads(record_path.read_text())
        self.assertEqual(record["outcome"], "hypothesis_supported")
        self.assertEqual(record["outcome_meta"]["run_ids"], ["R1", "R2"])
        self.assertNotIn("outcome", record.get("_pending", []))
        self.assertEqual(self.read_ledger()[0]["Outcome"], "hypothesis_supported")
        record_before = record_path.read_bytes()
        ledger_before = self.ledger.read_bytes()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (0, 1))
        self.assertEqual(record_path.read_bytes(), record_before)
        self.assertEqual(self.ledger.read_bytes(), ledger_before)

    def test_result_analysis_writeback_retries_failed_ledger_derivation(self):
        self.record("E1", runs=[{"run_id": "R1"}], outcome="pending")
        index = self.root / "issues/spec/reviews/result-analysis.json"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({"entries": [{
            "exp_id": "E1", "run_id": "R1", "scientific_outcome": "inconclusive",
            "review_evidence_ref": "session:abc#tool:def", "review_output_sha256": "a" * 64,
        }]}))
        real_write = records._write_if_changed
        failed = [True]
        def fail_once(path, content):
            if path == self.ledger and failed[0]:
                failed[0] = False
                raise OSError("ledger unavailable")
            return real_write(path, content)
        with patch.object(records, "_write_if_changed", side_effect=fail_once), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (1, 0))
        self.assertFalse(self.ledger.exists())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (0, 1))
        self.assertEqual(self.read_ledger()[0]["Outcome"], "inconclusive")

    def test_result_analysis_writeback_rejects_incomplete_or_conflicting_runs(self):
        self.record("E1", runs=[{"run_id": "R1"}, {"run_id": "R2"}], outcome="pending")
        index = self.root / "issues/spec/reviews/result-analysis.json"
        index.parent.mkdir(parents=True)
        entry = {
            "exp_id": "E1", "run_id": "R1",
            "scientific_outcome": "hypothesis_supported",
            "review_evidence_ref": "session:abc#tool:def",
            "review_output_sha256": "a" * 64,
        }
        index.write_text(json.dumps({"entries": [entry]}))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (0, 1))
        self.assertEqual(json.loads((self.experiments / "E1/record.json").read_text())["outcome"], "pending")
        entry["run_id"] = "R2"
        entry["scientific_outcome"] = "hypothesis_not_supported"
        index.write_text(json.dumps({"entries": [entry, {**entry, "run_id": "R1", "scientific_outcome": "hypothesis_supported"}]}))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (0, 1))
        self.assertEqual(json.loads((self.experiments / "E1/record.json").read_text())["outcome"], "pending")

    def test_build_preserves_only_still_validated_outcome(self):
        self.record("E1", runs=[{"run_id": "R1"}], outcome="pending")
        record_path = self.experiments / "E1/record.json"
        previous = json.loads(record_path.read_text())
        previous.update({"_generated_by": records.GENERATOR, "_projection_version": 2, "_pending": ["outcome"]})
        record_path.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n")
        index = self.root / "issues/spec/reviews/result-analysis.json"
        index.parent.mkdir(parents=True)
        entry = {
            "exp_id": "E1", "run_id": "R1", "scientific_outcome": "inconclusive",
            "review_evidence_ref": "session:abc#tool:def", "review_output_sha256": "a" * 64,
        }
        index.write_text(json.dumps({"entries": [entry]}))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (1, 0))
        generated = json.loads(record_path.read_text())
        generated["outcome"] = "pending"
        generated["_pending"] = ["outcome"]
        generated.pop("outcome_meta")
        with patch.object(records, "build_record", return_value=generated), redirect_stdout(io.StringIO()):
            self.assertEqual(records.cmd_build(Namespace(exp="E1", force=False, config=self.root / "missing.toml"), repo_root=self.root), 0)
        preserved = json.loads(record_path.read_text())
        self.assertEqual(preserved["outcome"], "inconclusive")
        self.assertNotIn("outcome", preserved["_pending"])
        preserved["outcome_meta"]["run_ids"] = [1]
        record_path.write_text(json.dumps(preserved, ensure_ascii=False, indent=2) + "\n")
        generated["outcome"] = "pending"
        generated["_pending"] = ["outcome"]
        generated.pop("outcome_meta", None)
        with patch.object(records, "build_record", return_value=generated), redirect_stdout(io.StringIO()):
            self.assertEqual(records.cmd_build(Namespace(exp="E1", force=False, config=self.root / "missing.toml"), repo_root=self.root), 0)
        malformed_reset = json.loads(record_path.read_text())
        self.assertEqual(malformed_reset["outcome"], "pending")
        entry["scientific_outcome"] = "hypothesis_supported"
        index.write_text(json.dumps({"entries": [entry]}))
        generated["outcome"] = "pending"
        generated["_pending"] = ["outcome"]
        generated.pop("outcome_meta", None)
        with patch.object(records, "build_record", return_value=generated), redirect_stdout(io.StringIO()):
            self.assertEqual(records.cmd_build(Namespace(exp="E1", force=False, config=self.root / "missing.toml"), repo_root=self.root), 0)
        reset = json.loads(record_path.read_text())
        self.assertEqual(reset["outcome"], "pending")

    def test_result_analysis_writeback_rejects_non_pending_and_allows_same_run_summaries(self):
        index = self.root / "issues/spec/reviews/result-analysis.json"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({"entries": [{
            "exp_id": "E1", "run_id": "R1", "scientific_outcome": "inconclusive",
            "review_evidence_ref": "session:abc#tool:def", "review_output_sha256": "a" * 64,
        }]}))
        self.record("E1", runs=[{"run_id": "R1"}], outcome=False)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (0, 1))
        self.assertEqual(json.loads((self.experiments / "E1/record.json").read_text())["outcome"], False)
        self.record("E1", runs=[{"run_id": "R1", "summary_path": "a"},
                                {"run_id": "R1", "summary_path": "b"}], outcome="pending")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (1, 0))
        self.assertEqual(json.loads((self.experiments / "E1/record.json").read_text())["outcome"], "inconclusive")

    def test_result_analysis_writeback_rejects_symlinked_record_directory(self):
        outside = Path(tempfile.mkdtemp(prefix="records-outside-"))
        self.addCleanup(lambda: shutil.rmtree(outside, ignore_errors=True))
        outside_record = outside / "E1/record.json"
        outside_record.parent.mkdir(parents=True)
        outside_record.write_text(json.dumps({"outcome": "pending"}))
        experiments = self.root / "research_workspace/experiments"
        experiments.mkdir(parents=True, exist_ok=True)
        (experiments / "E1").symlink_to(outside / "E1", target_is_directory=True)
        index = self.root / "issues/spec/reviews/result-analysis.json"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({"entries": [{
            "exp_id": "E1", "run_id": "R1", "scientific_outcome": "inconclusive",
            "review_evidence_ref": "session:abc#tool:def", "review_output_sha256": "a" * 64,
        }]}))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (0, 1))
        self.assertEqual(json.loads(outside_record.read_text())["outcome"], "pending")

    def test_result_analysis_writeback_does_not_overwrite_existing_outcome(self):
        self.record("E1", runs=[{"run_id": "R1"}], outcome="manual_review")
        index = self.root / "issues/spec/reviews/result-analysis.json"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({"entries": [{
            "exp_id": "E1", "run_id": "R1", "scientific_outcome": "inconclusive",
            "review_evidence_ref": "session:abc#tool:def", "review_output_sha256": "a" * 64,
        }]}))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(records.apply_result_analysis_outcomes(index, repo_root=self.root), (0, 1))
        self.assertEqual(json.loads((self.experiments / "E1/record.json").read_text())["outcome"], "manual_review")

    def test_identifier_rejects_malformed_exp_ids(self):
        self.assertEqual(records.identifier("EXP.2024-01_a"), "EXP.2024-01_a")
        for value in ("", ".hidden", "a b", "../up", "x" * 129, "包含中文", "a/b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                records.identifier(value)


if __name__ == "__main__":
    unittest.main()
