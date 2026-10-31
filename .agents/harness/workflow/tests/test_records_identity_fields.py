"""records.identity_fields 是合法 [records] 键（项目侧已在用，模板白名单缺失导致
unknown records configuration field 阻塞入账）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))

from harness.common.project_config import load_config  # noqa: E402

BASE = """version = 1

[[pipelines.train.stages]]
name = "s1"
argv = ["python", "x.py"]

[records]
summary_glob = "summary.json"
primary_metric = "miou"
dimensions = ["dataset"]
"""


def write_config(tmp: Path, extra: str) -> Path:
    path = tmp / "project.toml"
    path.write_text(BASE + extra, encoding="utf-8")
    return path


class IdentityFieldsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="identity-fields-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_identity_fields_is_accepted(self):
        path = write_config(self.root, 'identity_fields = ["config_id", "actta_lr"]\n')
        records = load_config(path)["records"]
        self.assertEqual(records["identity_fields"], ["config_id", "actta_lr"])

    def test_identity_fields_must_be_strings(self):
        path = write_config(self.root, "identity_fields = [1]\n")
        with self.assertRaisesRegex(ValueError, "identity_fields"):
            load_config(path)

    def test_identity_fields_may_duplicate_dimensions_downstream_contract(self):
        # 与下游不同：模板只做类型校验；重复维度由下游契约层拒绝（保持模板通用）。
        path = write_config(self.root, 'identity_fields = ["dataset"]\n')
        self.assertEqual(load_config(path)["records"]["identity_fields"], ["dataset"])

    def test_unknown_records_field_still_rejected(self):
        path = write_config(self.root, "nope = 1\n")
        with self.assertRaisesRegex(ValueError, "unknown records configuration field"):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
