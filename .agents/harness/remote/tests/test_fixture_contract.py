#!/usr/bin/env python3
"""本地 adapter 契约 fixture 的回归。

覆盖：按契约合成 progress/summary、正常契约通过、缺字段契约提前报错、
adapter 不健康/未完成时被发现，以及 fixture 不写进仓库内路径。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))

from harness.remote.fixture_contract import FixtureError, run, synthesize  # noqa: E402

GOOD_ADAPTER = """#!/usr/bin/env python3
import json, sys, os
ctx = json.load(sys.stdin)
root, phase = ctx["output_root"], ctx["phase"]
c = ctx["metadata"]["adapter_contract"]
res = {"protocol": "rrctl.adapter.v1", "healthy": True}
records = sum(1 for line in open(os.path.join(root, c["progress_path"])) if line.strip())
res["records"] = records
if records < c.get("first_step_min_count", 0):
    res["healthy"] = False
if phase == "completion":
    summary = json.load(open(os.path.join(root, c["summary_path"])))
    for key, expected in c.get("summary_identity_fields", {}).items():
        if key not in summary:
            res["healthy"] = False
        elif summary[key] != expected:
            res["healthy"] = False
    res["complete"] = True
print(json.dumps(res))
"""

UNHEALTHY_ADAPTER = """#!/usr/bin/env python3
import json, sys
json.load(sys.stdin)
print(json.dumps({"protocol": "rrctl.adapter.v1", "healthy": False, "complete": False}))
"""


class FixtureContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="contract-fixture-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.contract = {
            "progress_path": "progress.jsonl",
            "progress_format": "jsonl_last",
            "progress_count_field": "step",
            "first_step_min_count": 2,
            "progress_finite_fields": ["elapsed_s"],
            "progress_identity_fields": {},
            "summary_path": "summary.json",
            "summary_required_fields": ["retained_evidence_paths"],
            "summary_finite_fields": [],
            "summary_identity_fields": {"run_id": "RUN-TEST-1", "schema_version": "test.v1"},
            "artifacts": ["summary.json"],
        }

    def _runspec(self, adapter_source: str, contract: dict | None = None) -> Path:
        adapter = self.root / "adapter.py"
        adapter.write_text(adapter_source, encoding="utf-8")
        spec = {
            "run_id": "RUN-TEST-1",
            "project": {"name": "fixture"},
            "source": {"repo_root": str(self.repo)},
            "workload": {"cwd": "."},
            "metadata": {"adapter_contract": contract if contract is not None else dict(self.contract)},
            "health": {
                phase: {"adapter_argv": [sys.executable, str(adapter)], "adapter_timeout_seconds": 30}
                for phase in ("first_step", "periodic", "completion")
            },
        }
        path = self.root / "runspec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_good_contract_passes_without_gpu(self):
        result = run(self._runspec(GOOD_ADAPTER), self.root / "out", ["first_step", "periodic", "completion"])
        self.assertTrue(result["adapter"]["ok"])
        phases = {item["phase"]: item for item in result["adapter"]["checked"]}
        self.assertTrue(phases["completion"]["complete"])
        self.assertEqual(result["fixture"]["progress"]["records"], 2)

    def test_synthesize_writes_contract_shaped_files(self):
        written = synthesize(
            json.loads(self._runspec(GOOD_ADAPTER).read_text(encoding="utf-8")),
            self.root / "out2",
        )
        self.assertEqual(written["progress"]["records"], 2)
        summary = json.loads((self.root / "out2/summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["run_id"], "RUN-TEST-1")
        self.assertEqual(summary["schema_version"], "test.v1")
        self.assertEqual(summary["retained_evidence_paths"], [])

    def test_placeholder_identity_is_resolved_from_runspec(self):
        from harness.remote.fixture_contract import _identity

        resolved = _identity(
            {"summary_identity_fields": {"run_id": "$run_id", "fixed": 1}},
            "summary_identity_fields",
            {"run_id": "RUN-TEST-9"},
        )
        self.assertEqual(resolved, {"run_id": "RUN-TEST-9", "fixed": 1})

    def test_missing_contract_field_fails_before_any_run(self):
        contract = dict(self.contract)
        contract.pop("progress_path")
        with self.assertRaises(FixtureError):
            synthesize(
                json.loads(self._runspec(GOOD_ADAPTER, contract).read_text(encoding="utf-8")),
                self.root / "out3",
            )

    def test_contract_paths_cannot_escape_output_root(self):
        """契约是外部输入：绝对路径、`..`、符号链接逃逸都必须被拒。"""
        outside = self.root / "outside.json"
        for bad in (
            "/etc/passwd",
            "../outside.json",
            "nested/../../outside.json",
            "C:/windows/system32/x.json",
        ):
            with self.subTest(path=bad):
                contract = dict(self.contract)
                contract["summary_path"] = bad
                with self.assertRaises(FixtureError):
                    synthesize(
                        json.loads(self._runspec(GOOD_ADAPTER, contract).read_text(encoding="utf-8")),
                        self.root / "out-escape",
                    )
                self.assertFalse(outside.exists())

    def test_absent_summary_path_is_optional_not_an_escape(self):
        contract = dict(self.contract)
        contract["summary_path"] = ""
        written = synthesize(
            json.loads(self._runspec(GOOD_ADAPTER, contract).read_text(encoding="utf-8")),
            self.root / "out-optional",
        )
        self.assertNotIn("summary", written)

    def test_contract_artifact_paths_cannot_escape(self):
        contract = dict(self.contract)
        contract["artifacts"] = ["../escaped.txt"]
        with self.assertRaises(FixtureError):
            synthesize(
                json.loads(self._runspec(GOOD_ADAPTER, contract).read_text(encoding="utf-8")),
                self.root / "out-escape2",
            )

    def test_symlinked_output_root_escape_is_rejected(self):
        """output_root 内的符号链接不得把写入带到外面。"""
        out = self.root / "out-link"
        out.mkdir()
        target = self.root / "secret"
        target.mkdir()
        (out / "link").symlink_to(target, target_is_directory=True)
        contract = dict(self.contract)
        contract["summary_path"] = "link/summary.json"
        with self.assertRaises(FixtureError):
            synthesize(
                json.loads(self._runspec(GOOD_ADAPTER, contract).read_text(encoding="utf-8")),
                out,
            )
        self.assertFalse((target / "summary.json").exists())

    def test_non_empty_output_root_is_rejected(self):
        """非空目录会被拒绝，绝不覆盖用户已有文件。"""
        out = self.root / "userdir"
        out.mkdir()
        precious = out / "summary.json"
        precious.write_text("USER DATA\n", encoding="utf-8")
        with self.assertRaises(FixtureError):
            synthesize(
                json.loads(self._runspec(GOOD_ADAPTER).read_text(encoding="utf-8")), out
            )
        self.assertEqual(precious.read_text(encoding="utf-8"), "USER DATA\n")

    def test_output_root_inside_repo_is_rejected_by_cli(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / ".agents/harness/remote/fixture_contract.py"),
             str(self._runspec(GOOD_ADAPTER)), "--output-root", str(self.repo / "inside")],
            capture_output=True, text=True, check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("outside the repository", completed.stdout)

    def test_unhealthy_adapter_is_detected_locally(self):
        with self.assertRaisesRegex(ValueError, "not healthy"):
            run(self._runspec(UNHEALTHY_ADAPTER), self.root / "out4", ["first_step"])

    def test_fixture_default_output_stays_outside_repo(self):
        """fixture 输出不得写进仓库内：默认目录在系统临时区。"""
        import subprocess

        completed = subprocess.run(
            [sys.executable, str(ROOT / ".agents/harness/remote/fixture_contract.py"),
             str(self._runspec(GOOD_ADAPTER))],
            capture_output=True, text=True, check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["adapter"]["output_root"].startswith(str(self.repo)))


if __name__ == "__main__":
    unittest.main()
