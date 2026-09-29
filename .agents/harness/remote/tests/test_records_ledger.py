"""EXPERIMENTS.csv 派生一致性与写入边界；所有路径都替换到隔离目录。"""
from argparse import Namespace
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack, redirect_stdout

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
from harness.records import experiment_records as records


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

    def test_identifier_rejects_malformed_exp_ids(self):
        self.assertEqual(records.identifier("EXP.2024-01_a"), "EXP.2024-01_a")
        for value in ("", ".hidden", "a b", "../up", "x" * 129, "包含中文", "a/b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                records.identifier(value)


if __name__ == "__main__":
    unittest.main()
