"""Accept/reject matrix coverage for local RunSpec readiness validation."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remote_run_control import readiness
from remote_run_control.models import RunSpec
from remote_run_control.profiles import ProfileStore


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rrctl-readiness-test-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        (self.repo / "work.py").write_text("print('noop')\n")
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
        self.conda = self.root / "conda.sh"
        self.conda.write_text(f"conda() {{ export PATH={shlex.quote(os.defpath)}; }}\n")
        self.profiles_path = self.root / "profiles.json"
        self.profiles_path.write_text(
            json.dumps({"profiles": {"local-test": {"kind": "local"}}})
        )
        self.profile_store = ProfileStore(self.profiles_path)

    def tearDown(self):
        self.temporary.cleanup()

    def base_dict(self, **overrides):
        run_id = "ready-" + uuid.uuid4().hex[:10]
        remote = self.root / run_id
        document = {
            "schema_version": "rrctl.run.v1",
            "run_id": run_id,
            "project": "readiness",
            "source": {"repo_root": str(self.repo), "branch": "main", "commit": self.commit},
            "remote": {
                "profile": "local-test",
                "python": sys.executable,
                **{
                    key + "_root": str(remote / key)
                    for key in ("stage", "repo", "control", "output")
                },
            },
            "session": {"backend": "process", "name": "readiness"},
            "environment": {
                "kind": "conda",
                "name": "test",
                "conda_sh": str(self.conda),
                "required_modules": ["json"],
                "variables": {},
            },
            "resources": {"device": "cpu"},
            "workload": {"argv": ["python", "work.py"]},
            "health": {
                name: {"timeout_seconds": 10, "poll_interval_seconds": 0.05}
                for name in ("first_step", "periodic", "completion")
            },
            "artifacts": [{"path": "summary.json"}],
            "local_pull_root": str(self.root / "pulled"),
        }
        for dotted, value in overrides.items():
            parts = dotted.split(".")
            target = document
            for part in parts[:-1]:
                target = target[part]
            if value is ...:
                target.pop(parts[-1], None)
            else:
                target[parts[-1]] = value
        return document

    def validate(self, **overrides):
        spec = RunSpec.from_dict(self.base_dict(**overrides))
        return readiness.validate_run_spec(spec, profile_store=self.profile_store)

    def codes(self, result):
        return {item.code for item in result.errors}

    # ---- accept cases ----

    def test_clean_spec_is_accepted(self):
        doc = self.base_dict()
        spec = RunSpec.from_dict(doc)
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertTrue(result.ready, [item.to_dict() for item in result.errors])
        self.assertEqual(result.errors, ())
        self.assertEqual(result.run_spec_sha256, spec.digest)
        self.assertIsNotNone(result.source_content_sha256)
        self.assertEqual(len(result.source_content_sha256), 64)
        self.assertIsNotNone(result.profile)

    def test_matching_source_content_sha_is_accepted(self):
        spec = RunSpec.from_dict(self.base_dict())
        first = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertTrue(first.ready, [item.to_dict() for item in first.errors])
        pinned = RunSpec.from_dict(
            self.base_dict(
                source={
                    "repo_root": str(self.repo),
                    "branch": "main",
                    "commit": self.commit,
                    "source_content_sha256": first.source_content_sha256,
                }
            )
        )
        second = readiness.validate_run_spec(pinned, profile_store=self.profile_store)
        self.assertTrue(second.ready, [item.to_dict() for item in second.errors])
        self.assertEqual(second.source_content_sha256, first.source_content_sha256)

    def test_long_first_step_budget_is_warning_not_error(self):
        doc = self.base_dict()
        doc["health"]["first_step"]["timeout_seconds"] = 3700
        spec = RunSpec.from_dict(doc)
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertTrue(result.ready, [item.to_dict() for item in result.errors])
        self.assertIn("first_step_budget_long", {item.code for item in result.warnings})

    def test_skip_profile_load_warns_but_accepts(self):
        spec = RunSpec.from_dict(self.base_dict())
        result = readiness.validate_run_spec(
            spec, profile_store=self.profile_store, load_profile=False
        )
        self.assertTrue(result.ready, [item.to_dict() for item in result.errors])
        self.assertIsNone(result.profile)
        self.assertIn("profile_skipped", {item.code for item in result.warnings})

    # ---- schema / identity rejects ----

    def test_wrong_schema_version_is_rejected(self):
        result = self.validate(schema_version="rrctl.run.v0")
        self.assertFalse(result.ready)
        self.assertIn("schema_version", self.codes(result))

    def test_invalid_identifier_is_rejected(self):
        result = self.validate(run_id=".bad start")
        self.assertFalse(result.ready)
        self.assertIn("identifier_invalid", self.codes(result))

    def test_legacy_backend_is_rejected(self):
        result = self.validate(session={"backend": "legacy", "name": "readiness"})
        self.assertFalse(result.ready)
        self.assertIn("session_backend", self.codes(result))

    def test_missing_resources_is_rejected(self):
        result = self.validate(resources=...)
        self.assertFalse(result.ready)
        self.assertIn("resources_missing", self.codes(result))

    # ---- environment rejects ----

    def test_relative_remote_python_is_rejected(self):
        result = self.validate(
            remote={
                "profile": "local-test",
                "python": "python3",
                **{
                    key + "_root": str(self.root / "r" / key)
                    for key in ("stage", "repo", "control", "output")
                },
            }
        )
        self.assertFalse(result.ready)
        self.assertIn("remote_python", self.codes(result))

    def test_non_conda_environment_is_rejected(self):
        result = self.validate(
            environment={
                "kind": "venv",
                "name": "test",
                "conda_sh": str(self.conda),
                "required_modules": [],
                "variables": {},
            }
        )
        self.assertFalse(result.ready)
        self.assertIn("environment_kind", self.codes(result))

    def test_secret_in_spec_is_rejected(self):
        from dataclasses import replace

        spec = RunSpec.from_dict(self.base_dict())
        environment = replace(spec.environment, variables={"AUDIT": "password=hunter2"})
        spec = replace(spec, environment=environment)
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertFalse(result.ready)
        self.assertIn("secret_in_spec", self.codes(result))

    # ---- path / workload rejects ----

    def test_workload_cwd_escape_is_rejected(self):
        result = self.validate(workload={"argv": ["python", "work.py"], "cwd": ".."})
        self.assertFalse(result.ready)
        self.assertIn("workload_cwd", self.codes(result))

    def test_conda_sh_must_be_specific_absolute_path(self):
        doc = self.base_dict()
        doc["environment"]["conda_sh"] = "conda.sh"
        spec = RunSpec.from_dict(doc)
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertFalse(result.ready)
        self.assertIn("conda_sh", self.codes(result))

    def test_secret_in_workload_argv_is_rejected(self):
        result = self.validate(workload={"argv": ["python", "work.py", "--token", "abc123"]})
        self.assertFalse(result.ready)
        self.assertIn("secret_in_argv", self.codes(result))

    def test_required_gpu_window_needs_thresholds(self):
        doc = self.base_dict()
        doc["health"]["periodic"] = {
            "timeout_seconds": 10,
            "poll_interval_seconds": 5,
            "gpu_utilization_policy": "required",
        }
        spec = RunSpec.from_dict(doc)
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertFalse(result.ready)
        self.assertIn("required_gpu_window", self.codes(result))

    def test_low_gpu_limit_without_threshold_is_rejected(self):
        doc = self.base_dict()
        doc["health"]["periodic"] = {
            "timeout_seconds": 10,
            "poll_interval_seconds": 5,
            "low_gpu_limit_seconds": 60,
        }
        spec = RunSpec.from_dict(doc)
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertFalse(result.ready)
        self.assertIn("low_gpu_without_threshold", self.codes(result))

    def test_remote_root_overlap_is_rejected(self):
        base = self.root / "overlap"
        result = self.validate(
            remote={
                "profile": "local-test",
                "python": sys.executable,
                "stage_root": str(base),
                "repo_root": str(base / "repo"),
                "control_root": str(self.root / "overlap-control"),
                "output_root": str(self.root / "overlap-output"),
            }
        )
        self.assertFalse(result.ready)
        self.assertIn("remote_path_overlap", self.codes(result))

    def test_non_absolute_remote_root_is_rejected(self):
        result = self.validate(
            remote={
                "profile": "local-test",
                "python": sys.executable,
                "stage_root": "relative/stage",
                "repo_root": str(self.root / "repo"),
                "control_root": str(self.root / "control"),
                "output_root": str(self.root / "output"),
            }
        )
        self.assertFalse(result.ready)
        self.assertIn("remote_path", self.codes(result))

    # ---- artifacts / source ----

    def test_duplicate_artifact_is_rejected(self):
        result = self.validate(artifacts=[{"path": "a.json"}, {"path": "a.json"}])
        self.assertFalse(result.ready)
        self.assertIn("artifact_duplicate", self.codes(result))

    def test_absolute_artifact_path_is_rejected(self):
        result = self.validate(artifacts=[{"path": "/abs/x.json"}])
        self.assertFalse(result.ready)
        self.assertIn("artifact_path", self.codes(result))

    def test_malformed_commit_is_rejected(self):
        result = self.validate(
            source={"repo_root": str(self.repo), "branch": "main", "commit": "not-a-commit"}
        )
        self.assertFalse(result.ready)
        self.assertIn("commit_format", self.codes(result))

    def test_source_content_sha_mismatch_is_rejected(self):
        result = self.validate(
            source={
                "repo_root": str(self.repo),
                "branch": "main",
                "commit": self.commit,
                "source_content_sha256": "0" * 64,
            }
        )
        self.assertFalse(result.ready)
        self.assertIn("source_content_sha_mismatch", self.codes(result))

    def test_malformed_anchor_sha_is_rejected(self):
        result = self.validate(
            anchors=[
                {"name": "dataset", "local_path": ".", "remote_path": "ds", "sha256": "zz" * 32}
            ]
        )
        self.assertFalse(result.ready)
        self.assertIn("anchor_sha", self.codes(result))

    def test_source_drift_is_rejected_but_allowlisted_change_is_accepted(self):
        (self.repo / "notes.txt").write_text("uncommitted change\n")
        result = self.validate()
        self.assertFalse(result.ready)
        self.assertIn("source_drift", self.codes(result))

        allowed = self.validate(
            source={
                "repo_root": str(self.repo),
                "branch": "main",
                "commit": self.commit,
                "allowed_post_commit_paths": ["notes.txt"],
            }
        )
        self.assertTrue(allowed.ready, [item.to_dict() for item in allowed.errors])

    # ---- health ----

    def test_zero_health_interval_is_rejected(self):
        from dataclasses import replace

        spec = RunSpec.from_dict(self.base_dict())
        phase = replace(spec.health.periodic, poll_interval_seconds=0)
        spec = replace(spec, health=replace(spec.health, periodic=phase))
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertFalse(result.ready)
        self.assertIn("health_interval", self.codes(result))

    def test_adapter_argv_with_secret_is_rejected(self):
        doc = self.base_dict()
        doc["health"]["first_step"] = {
            "timeout_seconds": 10,
            "poll_interval_seconds": 5,
            "adapter_argv": ["python", "check.py", "password=s3cret"],
        }
        result = self.validate(**{})
        result = readiness.validate_run_spec(
            RunSpec.from_dict(doc), profile_store=self.profile_store
        )
        self.assertFalse(result.ready)
        self.assertIn("secret_in_argv", self.codes(result))

    def test_invalid_progress_path_and_fatal_pattern_are_rejected(self):
        doc = self.base_dict()
        doc["health"]["periodic"] = {
            "timeout_seconds": 10,
            "poll_interval_seconds": 5,
            "progress_path": "../escape",
            "fatal_patterns": ["([invalid"],
        }
        result = readiness.validate_run_spec(
            RunSpec.from_dict(doc), profile_store=self.profile_store
        )
        self.assertFalse(result.ready)
        self.assertIn("progress_path", self.codes(result))
        self.assertIn("fatal_pattern", self.codes(result))

    # ---- output cleanup policy ----

    def cleanup_spec(self, **cleanup):
        doc = self.base_dict()
        doc["output_cleanup"] = cleanup
        doc["metadata"] = {"execution_purpose": "pre_review_smoke"}
        doc["remote"]["output_root"] = str(self.root / "smoke" / doc["run_id"])
        return RunSpec.from_dict(doc)

    def test_cleanup_bad_mode_is_rejected(self):
        spec = self.cleanup_spec(
            mode="other", retain=["smoke_summary.json"], delete_globs=["*.ckpt"]
        )
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertIn("cleanup_mode", self.codes(result))

    def test_cleanup_without_matching_purpose_is_rejected(self):
        doc = self.base_dict()
        doc["output_cleanup"] = {
            "mode": "pre_review_smoke",
            "retain": ["smoke_summary.json"],
            "delete_globs": ["*.ckpt"],
        }
        result = readiness.validate_run_spec(
            RunSpec.from_dict(doc), profile_store=self.profile_store
        )
        self.assertIn("cleanup_purpose", self.codes(result))

    def test_cleanup_glob_escape_is_rejected(self):
        spec = self.cleanup_spec(
            mode="pre_review_smoke", retain=["smoke_summary.json"], delete_globs=["../x"]
        )
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertIn("cleanup_path", self.codes(result))

    def test_cleanup_output_root_must_bind_run_id(self):
        doc = self.base_dict()
        doc["output_cleanup"] = {
            "mode": "pre_review_smoke",
            "retain": ["smoke_summary.json"],
            "delete_globs": ["*.ckpt"],
        }
        doc["metadata"] = {"execution_purpose": "pre_review_smoke"}
        doc["remote"]["output_root"] = str(self.root / "no-run-id-here")
        result = readiness.validate_run_spec(
            RunSpec.from_dict(doc), profile_store=self.profile_store
        )
        self.assertIn("cleanup_run_binding", self.codes(result))

    def test_cleanup_missing_summary_retention_and_globs_are_rejected(self):
        spec = self.cleanup_spec(mode="pre_review_smoke", retain=[], delete_globs=[])
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertIn("cleanup_summary_retention", self.codes(result))
        self.assertIn("cleanup_patterns_missing", self.codes(result))

    def test_valid_cleanup_policy_is_accepted(self):
        spec = self.cleanup_spec(
            mode="pre_review_smoke", retain=["smoke_summary.json"], delete_globs=["*.ckpt"]
        )
        result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        self.assertTrue(result.ready, [item.to_dict() for item in result.errors])

    # ---- tools / profile ----

    def test_missing_tool_is_rejected_via_which_stub(self):
        spec = RunSpec.from_dict(self.base_dict())
        original = readiness.shutil.which

        def fake_which(name):
            return None if name == "git" else original(name)

        readiness.shutil.which = fake_which
        try:
            result = readiness.validate_run_spec(spec, profile_store=self.profile_store)
        finally:
            readiness.shutil.which = original
        self.assertFalse(result.ready)
        self.assertIn("tool_missing", self.codes(result))

    def test_unknown_profile_is_rejected(self):
        result = self.validate(
            remote={
                "profile": "missing-profile",
                "python": sys.executable,
                **{
                    key + "_root": str(self.root / "np" / key)
                    for key in ("stage", "repo", "control", "output")
                },
            }
        )
        self.assertFalse(result.ready)
        self.assertIn("profile", self.codes(result))


if __name__ == "__main__":
    unittest.main()
