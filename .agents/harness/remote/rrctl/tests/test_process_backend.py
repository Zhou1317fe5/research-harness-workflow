from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remote_run_control import cli
from remote_run_control.controller import Controller
from remote_run_control.environment import make_environment
from remote_run_control.errors import RRCError
from remote_run_control.models import RunSpec
from remote_run_control.processes import bound_group, owned_processes


class ProcessBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rrctl-process-test-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        (self.repo / "work.py").write_text(
            "import json,os,sys,time\nfrom pathlib import Path\n"
            "output=Path(os.environ['RRCTL_OUTPUT_ROOT']); output.mkdir()\n"
            "(output/'started.json').write_text(json.dumps({'pid':os.getpid(), 'sid':os.getsid(0), "
            "'pgid':os.getpgrp(), 'declared':os.environ.get('AUDIT_DECLARED'), "
            "'old_library':os.environ.get('LD_LIBRARY_PATH'), "
            "'gpu':os.environ.get('CUDA_VISIBLE_DEVICES')}))\n"
            "print('started',flush=True)\ntime.sleep(float(sys.argv[1]))\n"
            "(output/'summary.json').write_text(json.dumps({'complete':True}))\n"
            "raise SystemExit(int(sys.argv[2]))\n"
        )
        for command in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.name", "Local Test"],
            ["git", "config", "user.email", "local@example.invalid"],
            ["git", "add", "work.py"],
            ["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"],
        ):
            subprocess.run(command, cwd=self.repo, check=True, capture_output=True)
        self.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
        ).strip()
        binary = self.root / "bin"
        binary.mkdir()
        (binary / "python").symlink_to(sys.executable)
        conda = self.root / "conda.sh"
        conda.write_text(
            f"conda() {{ export PATH={shlex.quote(str(binary) + ':' + os.defpath)}; "
            "export LD_LIBRARY_PATH=/audit/stale-from-activation "
            "CUDA_VISIBLE_DEVICES=wrong-device; }\n"
        )
        profiles = self.root / "profiles.json"
        profiles.write_text(json.dumps({"profiles": {"local-test": {"kind": "local"}}}))
        self.control = Controller(profiles_path=profiles, state_root=self.root / "index")
        self.specs = []
        self.conda = conda

    def tearDown(self):
        for spec in self.specs:
            root = Path(spec.remote.control_root)
            binding = root / "binding.json"
            if binding.is_file() and owned_processes(spec.run_id, root):
                try:
                    pgid, _ = bound_group(json.loads(binding.read_text()), spec.run_id, root)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, RRCError):
                    pass
            deadline = time.monotonic() + 2
            while owned_processes(spec.run_id, root) and time.monotonic() < deadline:
                time.sleep(0.02)
        self.temporary.cleanup()

    def spec(self, delay=0.5, exit_code=0):
        run_id = "audit-" + uuid.uuid4().hex[:10]
        remote = self.root / run_id
        phase = {"timeout_seconds": 10, "poll_interval_seconds": 0.05}
        spec = RunSpec.from_dict(
            {
                "schema_version": "rrctl.run.v1",
                "run_id": run_id,
                "project": "audit",
                "source": {"repo_root": str(self.repo), "branch": "main", "commit": self.commit},
                "remote": {
                    "profile": "local-test",
                    "python": sys.executable,
                    **{
                        key + "_root": str(remote / key)
                        for key in ("stage", "repo", "control", "output")
                    },
                },
                "session": {"backend": "process", "name": "label.with.dots"},
                "environment": {
                    "kind": "conda",
                    "name": "test",
                    "conda_sh": str(self.conda),
                    "required_modules": ["json"],
                    "variables": {"AUDIT_DECLARED": "literal $(no-shell-eval)"},
                },
                "resources": {"device": "cpu"},
                "workload": {"argv": ["python", "work.py", str(delay), str(exit_code)]},
                "health": {name: dict(phase) for name in ("first_step", "periodic", "completion")},
                "artifacts": [{"path": "summary.json"}, {"path": "started.json"}],
                "local_pull_root": str(self.root / "pulled"),
            }
        )
        self.specs.append(spec)
        return spec

    def test_detached_lifecycle_environment_pull_and_recovery(self):
        spec = self.spec()
        with patch.dict(
            os.environ, {"LD_LIBRARY_PATH": "/audit/stale", "BASH_ENV": "/audit/not-present"}
        ):
            self.assertTrue(self.control.ready(spec)["ready"])
            self.control.launch(spec)
        terminal = self.control.wait(spec.run_id, poll_seconds=0.05, max_wait_seconds=5)
        self.assertEqual(terminal["status"]["state"], "completed")
        binding = terminal["binding"]
        self.assertEqual(binding["executor_pid"], binding["executor_session_id"])
        self.assertEqual(binding["executor_pid"], binding["executor_process_group_id"])
        self.assertEqual(binding["backend"], "process")
        pulled = self.control.pull(spec.run_id)
        destination = Path(pulled["destination"])
        actual = json.loads((destination / "started.json").read_text())
        self.assertIsNone(actual["old_library"])
        self.assertEqual(actual["declared"], "literal $(no-shell-eval)")
        self.assertEqual(actual["gpu"], "")
        self.assertEqual(actual["sid"], binding["executor_pid"])
        self.assertTrue(self.control.pull(spec.run_id)["reused"])
        self.assertEqual(
            self.control.resume(profile_name="local-test", control_root=spec.remote.control_root)[
                "status"
            ]["state"],
            "completed",
        )
        manifest = json.loads((destination / "artifact_manifest.json").read_text())
        self.assertEqual(manifest["provenance"]["commit"], self.commit)
        (destination / "summary.json").write_text("user edit")
        with self.assertRaises(RRCError) as caught:
            self.control.pull(spec.run_id)
        self.assertEqual(caught.exception.code, "pull_collision")

    def test_observation_deadline_preserves_workload_then_resumes(self):
        spec = self.spec(delay=2)
        self.control.launch(spec)
        result = self.control.wait(spec.run_id, poll_seconds=0.05, max_wait_seconds=0.15)
        self.assertEqual(result["observation"], "timeout")
        self.assertTrue(result["remote_workload_preserved"])
        self.assertTrue(owned_processes(spec.run_id, spec.remote.control_root))
        self.assertEqual(
            self.control.wait(spec.run_id, poll_seconds=0.05, max_wait_seconds=5)["status"][
                "state"
            ],
            "completed",
        )

    def test_killing_local_observer_does_not_kill_remote_worker(self):
        spec = self.spec(delay=3)
        self.control.launch(spec)
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        observer = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "remote_run_control",
                "--profiles",
                str(self.root / "profiles.json"),
                "--state-root",
                str(self.root / "index"),
                "--json",
                "wait",
                spec.run_id,
                "--poll-seconds",
                "0.05",
                "--max-wait-seconds",
                "0",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.15)
            observer.terminate()
            observer.wait(timeout=5)
            self.assertTrue(owned_processes(spec.run_id, spec.remote.control_root))
            self.assertEqual(
                self.control.wait(spec.run_id, poll_seconds=0.05, max_wait_seconds=5)["status"][
                    "state"
                ],
                "completed",
            )
        finally:
            if observer.poll() is None:
                observer.kill()
                observer.wait(timeout=5)

    def test_lost_supervisor_preserves_child_and_owned_abort_still_works(self):
        spec = self.spec(delay=30)
        self.control.launch(spec)
        binding = self.control.inspect(spec.run_id)["binding"]
        os.kill(binding["executor_pid"], signal.SIGKILL)
        time.sleep(0.1)
        report = self.control.health(spec.run_id, phase="periodic")
        self.assertTrue(report["observations"]["workload_alive"])
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(self.control.abort(spec.run_id, confirmed=True)["state"], "aborted")

    def test_explicit_abort_checks_pid_identity_and_preserves_other_process(self):
        spec = self.spec(delay=30)
        self.control.launch(spec)
        path = Path(spec.remote.control_root) / "binding.json"
        original = json.loads(path.read_text())
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as sentinel:
            try:
                changed = {**original, "workload_pid": sentinel.pid}
                path.write_text(json.dumps(changed))
                with self.assertRaises(RRCError):
                    self.control.abort(spec.run_id, confirmed=True)
                self.assertIsNone(sentinel.poll())
                path.write_text(json.dumps(original))
                self.assertEqual(
                    self.control.abort(spec.run_id, confirmed=True)["state"], "aborted"
                )
                self.assertIsNone(sentinel.poll())
            finally:
                path.write_text(json.dumps(original))
                sentinel.terminate()
                sentinel.wait(timeout=5)

    def test_first_step_timeout_does_not_stop_workload(self):
        spec = self.spec(delay=30)
        spec = replace(
            spec,
            health=replace(
                spec.health,
                first_step=replace(
                    spec.health.first_step,
                    timeout_seconds=1,
                    progress_path="not-created.json",
                ),
            ),
        )
        with self.assertRaises(RRCError) as caught:
            self.control.launch(spec)
        self.assertEqual(caught.exception.code, "first_step_observer_timeout")
        self.assertTrue(caught.exception.details["remote_workload_preserved"])
        self.assertTrue(owned_processes(spec.run_id, spec.remote.control_root))
        self.control.abort(spec.run_id, confirmed=True)

    def test_nonzero_workload_is_authoritative_failure(self):
        spec = self.spec(delay=0.3, exit_code=7)
        try:
            self.control.launch(spec)
        except RRCError as exc:
            self.assertEqual(exc.code, "first_step_terminal")
        result = self.control.wait(spec.run_id, poll_seconds=0.05, max_wait_seconds=5)
        self.assertEqual(result["status"]["state"], "failed")
        self.assertEqual(result["status"]["detail"]["exit_code"], 7)

    def test_dependency_preflight_fails_before_staging(self):
        spec = self.spec()
        spec = replace(
            spec,
            environment=replace(
                spec.environment, required_modules=("rrctl_missing_test_dependency",)
            ),
        )
        with self.assertRaises(RRCError) as caught:
            self.control.launch(spec)
        self.assertEqual(caught.exception.code, "remote_preflight")
        self.assertFalse(Path(spec.remote.stage_root).exists())

    def test_runtime_options_are_not_confused_with_credential_names(self):
        raw = self.spec().to_dict()
        raw["environment"]["variables"] = {"TOKENIZERS_PARALLELISM": "false", "TOKEN_LIMIT": "100"}
        parsed = RunSpec.from_dict(raw)
        self.assertEqual(parsed.environment.variables["TOKENIZERS_PARALLELISM"], "false")
        raw["environment"]["variables"]["SERVICE_API_KEY"] = "test-only-not-a-credential"
        with self.assertRaises(RRCError):
            RunSpec.from_dict(raw)

    def test_legacy_spec_keeps_digest_fields_but_cannot_launch(self):
        raw = self.spec().to_dict()
        raw["session"]["backend"] = "tmux"
        raw.pop("resources")
        for key in ("required_modules", "variables"):
            raw["environment"].pop(key)
        legacy = RunSpec.from_dict(raw)
        self.assertEqual(legacy.to_dict(), raw)
        result = self.control.ready(legacy, offline=True)
        self.assertFalse(result["ready"])
        self.assertIn("session_backend", [item["code"] for item in result["errors"]])


class ProtocolTests(unittest.TestCase):
    def test_wait_exit_codes_distinguish_failure_and_observation(self):
        for value, expected in (
            ({"status": {"state": "completed"}}, 0),
            ({"status": {"state": "failed"}}, 1),
            ({"status": {"state": "aborted"}}, 1),
            ({"status": {"state": "running"}, "observation": "timeout"}, 124),
        ):
            with (
                self.subTest(expected=expected),
                patch.object(
                    cli,
                    "Controller",
                    return_value=SimpleNamespace(wait=lambda *a, _value=value, **kw: _value),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(cli.main(["--json", "wait", "audit"]), expected)

    def test_environment_inherits_only_declared_runtime_values(self):
        with patch.dict(
            os.environ,
            {
                "LD_LIBRARY_PATH": "old",
                "PYTHONPATH": "old",
                "BASH_ENV": "old",
                "UNRELATED_TEST_VARIABLE": "old",
            },
        ):
            value = make_environment(
                {
                    "name": "x",
                    "conda_sh": "/conda.sh",
                    "variables": {"LD_LIBRARY_PATH": "/explicit/lib"},
                }
            )
        self.assertEqual(value["LD_LIBRARY_PATH"], "/explicit/lib")
        for key in ("PYTHONPATH", "BASH_ENV", "UNRELATED_TEST_VARIABLE"):
            self.assertNotIn(key, value)


if __name__ == "__main__":
    unittest.main()
