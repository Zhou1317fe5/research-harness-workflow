"""run_pipeline 的 stage 传播与预检分支；只在本仓库内执行 fixture 脚本。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
from harness.pipeline.run_pipeline import check_pipeline, confined, run_pipeline
from harness.common.project_config import REPO_ROOT


class PipelineStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pipeline-stages-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.out = self.repo / "out"
        self.scripts = self.repo / "scripts"
        self.scripts.mkdir()

    def script(self, name, text):
        path = self.scripts / name
        path.write_text(text)
        return path

    def config(self, stages):
        return {"pipeline": {"stages": stages}}

    def execute(self, config):
        return run_pipeline(config, self.repo, self.out)

    def marker(self):
        return json.loads((self.out / "pipeline-status.json").read_text())

    def test_successful_stages_write_logs_with_owned_permissions(self):
        self.script("ok.sh", "#!/bin/bash\necho hello-from-stage\n")
        result = self.execute(self.config([
            {"name": "first", "argv": ["bash", "scripts/ok.sh"], "cwd": "."},
            {"name": "second", "argv": ["bash", "-c", "exit 0"]},
        ]))
        self.assertEqual(result, 0)
        self.assertEqual(self.marker(), {"state": "completed"})
        for name in ("first", "second"):
            log = self.out / f"{name}.log"
            self.assertTrue(log.is_file())
        self.assertIn("hello-from-stage", (self.out / "first.log").read_text())

    def test_failed_stage_propagates_exit_code_and_stops_later_stages(self):
        self.script("fail.sh", "#!/bin/bash\nexit 7\n")
        self.script("later.sh", "#!/bin/bash\ntouch SHOULD_NOT_EXIST\n")
        result = self.execute(self.config([
            {"name": "bad", "argv": ["bash", "scripts/fail.sh"]},
            {"name": "later", "argv": ["bash", "scripts/later.sh"]},
        ]))
        self.assertEqual(result, 7)
        self.assertEqual(self.marker(), {"state": "failed", "stage": "bad", "exit_code": 7})
        self.assertFalse((self.out / "later.log").exists())
        self.assertFalse((self.repo / "SHOULD_NOT_EXIST").exists())

    def test_output_root_env_overrides_inherited_value_and_formatting(self):
        self.script("env.sh", "#!/bin/bash\n"
                    "echo \"run=$RRCTL_RUN_ID out=$RRCTL_OUTPUT_ROOT arg=$1\" > env.txt\n")
        old_output, old_run = os.environ.get("RRCTL_OUTPUT_ROOT"), os.environ.get("RRCTL_RUN_ID")
        os.environ["RRCTL_OUTPUT_ROOT"] = "/stale/inherited"
        os.environ["RRCTL_RUN_ID"] = "RUN-X"
        self.addCleanup(self._restore, "RRCTL_OUTPUT_ROOT", old_output)
        self.addCleanup(self._restore, "RRCTL_RUN_ID", old_run)
        result = self.execute(self.config([
            {"name": "env", "argv": ["bash", "scripts/env.sh", "{run_id}:{repo_root}"]},
        ]))
        self.assertEqual(result, 0)
        text = (self.repo / "env.txt").read_text()
        self.assertIn(f"out={self.out.resolve()}", text)
        self.assertNotIn("/stale/inherited", text)
        self.assertIn(f"arg=RUN-X:{self.repo}", text)

    def _restore(self, key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def test_requires_and_outputs_gate_each_stage(self):
        self.script("touch.sh", "#!/bin/bash\nmkdir -p \"$1\" && touch \"$1/seed\"\n")
        result = self.execute(self.config([
            {"name": "prep", "argv": ["bash", "scripts/touch.sh", str(self.out / "data")],
             "log": "prep.log", "outputs": ["data/seed"]},
            {"name": "use", "argv": ["bash", "-c", "exit 0"], "requires": ["data/seed"]},
        ]))
        self.assertEqual(result, 0)
        with self.assertRaisesRegex(ValueError, "required input missing"):
            run_pipeline(self.config([
                {"name": "use", "argv": ["bash", "-c", "exit 0"], "requires": ["absent.bin"]},
            ]), self.repo, self.repo / "second-out")
        self.script("none.sh", "#!/bin/bash\nexit 0\n")
        with self.assertRaisesRegex(ValueError, "required output missing"):
            run_pipeline(self.config([
                {"name": "claim", "argv": ["bash", "scripts/none.sh"], "outputs": ["never.bin"]},
            ]), self.repo, self.repo / "third-out")
        self.assertEqual(self.marker(), {"state": "completed"})

    def test_second_invocation_same_output_root_fails_and_keeps_first_marker(self):
        self.script("ok.sh", "#!/bin/bash\nexit 0\n")
        self.assertEqual(self.execute(self.config([{"name": "a", "argv": ["bash", "scripts/ok.sh"]}])), 0)
        with self.assertRaises(OSError):
            self.execute(self.config([{"name": "a", "argv": ["bash", "scripts/ok.sh"]}]))
        self.assertEqual(self.marker(), {"state": "completed"})

    def test_confined_and_input_validation(self):
        self.assertEqual(confined(self.repo, "a/b.txt"), (self.repo / "a/b.txt").resolve())
        with self.assertRaises(ValueError):
            confined(self.repo, "../escape")
        with self.assertRaises(ValueError):
            run_pipeline(self.config([]), self.repo, self.out)
        with self.assertRaises(KeyError):
            run_pipeline(self.config([{"argv": ["true"]}]), self.repo, self.repo / "no-name")
        with self.assertRaises(KeyError):
            self.execute(self.config([{"name": "x", "argv": ["true", "{unknown_key}"]}]))

    def test_check_pipeline_input_and_executable_branches(self):
        self.script("ok.py", "value = 1\n")
        script_path = self.repo / "scripts/ok.py"
        broken_json = self.repo / "contracts/fixture.json"
        checks = self.repo / "checks"
        checks.mkdir()
        (checks / "contracts.json").write_text(json.dumps([str(broken_json)]))
        gate = checks / "gate.py"
        gate.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "missing = [p for p in json.loads(Path(sys.argv[1]).read_text()) if not Path(p).is_file()]\n"
            "sys.exit(1 if missing else 0)\n")
        with self.assertRaisesRegex(ValueError, "input check failed"):
            check_pipeline(self.config([
                {"name": "py", "argv": [sys.executable, "scripts/ok.py"],
                 "check_argv": [sys.executable, "checks/gate.py", "checks/contracts.json"]},
            ]), self.repo)
        broken_json.parent.mkdir(parents=True)
        broken_json.write_text("{}")
        self.assertEqual(check_pipeline(self.config([
            {"name": "py", "argv": [sys.executable, "scripts/ok.py"],
             "check_argv": [sys.executable, "checks/gate.py", "checks/contracts.json"]},
        ]), self.repo), ["py"])
        with self.assertRaisesRegex(ValueError, "executable unavailable"):
            check_pipeline(self.config([{"name": "x", "argv": ["definitely-absent-executable"]}]), self.repo)
        with self.assertRaisesRegex(ValueError, "entrypoint missing"):
            check_pipeline(self.config([{"name": "x", "argv": [sys.executable, "scripts/absent.py"]}]), self.repo)
        self.script("bad.py", "def broken(:\n")
        with self.assertRaises(SyntaxError):
            check_pipeline(self.config([{"name": "x", "argv": [sys.executable, "scripts/bad.py"]}]), self.repo)
        with self.assertRaisesRegex(ValueError, "at least one pipeline stage"):
            check_pipeline(self.config([]), self.repo)

    def test_repo_root_resolves_to_project_root(self):
        self.assertEqual(REPO_ROOT, ROOT.resolve())


if __name__ == "__main__":
    unittest.main()
