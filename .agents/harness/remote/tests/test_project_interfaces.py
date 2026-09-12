from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HARNESS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HARNESS.parent))
sys.path.insert(0, str(HARNESS / "remote/rrctl/src"))

from harness.common.project_config import apply_project_config, list_pipelines, load_config, pipeline_digest
from harness.pipeline import run_pipeline as pipeline_module
from harness.pipeline.run_pipeline import check_pipeline, run_pipeline
from harness.records import experiment_records
from harness.remote import remote_run
from harness.remote.adapters.generic_json import evaluate
from harness.remote.build_rrctl_runspec import RunSpecBuildError, build_runspec, run_spec_digest
from remote_run_control.models import RunSpec


class ProjectInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="harness-interfaces-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def request(self):
        return {
            "schema_version": "mission.rrctl-request.v1", "spec_id": "SPEC-A", "exp_id": "EXP-A", "run_id": "RUN-A",
            "project": "audit", "source": {"repo_root": str(self.root), "branch": "main", "commit": "a" * 40},
            "remote": {"profile": "local-test", **{key + "_root": str(self.root / "run" / key) for key in ("stage", "repo", "control", "output")}},
            "environment": {"kind": "conda", "name": "test", "conda_sh": "/test/conda.sh"},
            "workload": {"argv": ["bash", "train.sh"]},
            "adapter_contract": {"progress_path": "progress.jsonl", "progress_format": "jsonl_last", "progress_count_field": "step",
                                 "first_step_min_count": 1, "completion_min_count": 1, "summary_path": "summary.json",
                                 "summary_required_fields": ["metric"], "summary_finite_fields": ["metric"]},
            "artifacts": [{"path": "summary.json", "required": True}],
        }

    def test_generator_binds_process_resources_budgets_and_completion_evidence(self):
        value = build_runspec(self.request())
        self.assertEqual(value["session"], {"backend": "process", "name": "RUN-A"})
        self.assertEqual(value["remote"]["python"], "/usr/bin/python3")
        self.assertEqual(value["resources"]["device"], "gpu")
        self.assertEqual(value["health"]["first_step"]["timeout_seconds"], 600)
        self.assertEqual(value["health"]["periodic"]["poll_interval_seconds"], 600)
        self.assertEqual({item["path"] for item in value["artifacts"]}, {"progress.jsonl", "summary.json"})
        self.assertEqual(run_spec_digest(value), RunSpec.from_dict(value).digest)

    def test_unrelated_raw_diagnostics_still_require_explicit_pull_policy(self):
        request = self.request()
        request["artifacts"].append({"path": "unrelated_episode_trace.jsonl", "required": True})
        with self.assertRaises(RunSpecBuildError):
            build_runspec(request)

    def test_project_defaults_preserve_explicit_request_values(self):
        config = self.root / "project.toml"
        config.write_text('''version = 1
[[pipeline.stages]]
name = "train"
argv = ["bash", "train.sh"]
[resources]
device = "gpu"
gpu_ids = ["0"]
[environment]
required_modules = ["json"]
[health.periodic]
poll_interval_seconds = 600
[records]
protocol_field = "evaluation.mode"
''')
        request = self.request()
        request["resources"] = {"device": "cpu"}
        value = apply_project_config(request, config)
        self.assertEqual(value["resources"]["device"], "cpu")
        self.assertEqual(value["environment"]["required_modules"], ["json"])
        self.assertIn("--check", value["environment"]["preflight_argv"])
        self.assertEqual(load_config(config)["records"]["protocol_field"], "evaluation.mode")
        request["source"].pop("repo_root")
        with self.assertRaises(ValueError):
            apply_project_config(request, config)

    def test_completion_adapter_returns_every_required_file(self):
        (self.root / "progress.jsonl").write_text('{"step":1}\n')
        (self.root / "summary.json").write_text('{"metric":1.25}')
        context = {"protocol": "rrctl.adapter.context.v1", "phase": "completion", "run_id": "RUN-A", "project": "audit",
                   "output_root": str(self.root), "metadata": {"adapter_contract": self.request()["adapter_contract"]}}
        result = evaluate(context)
        self.assertTrue(result["complete"])
        self.assertEqual(set(result["artifacts"]), {"progress.jsonl", "summary.json"})

    def test_pipeline_check_validates_script_without_running_it(self):
        script = self.root / "train.sh"
        marker = self.root / "must-not-run"
        script.write_text(f'#!/bin/bash\ntouch "{marker}"\n')
        config = {"pipeline": {"stages": [{"name": "train", "argv": ["bash", "train.sh"]}]}}
        self.assertEqual(check_pipeline(config, self.root), ["train"])
        self.assertFalse(marker.exists())
        script.write_text("if true; then\n")
        with self.assertRaises(ValueError):
            check_pipeline(config, self.root)

    def test_projection_supports_both_exp_id_separators(self):
        issues = self.root / "issues"
        issues.mkdir()
        with (issues / "tasks.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["exp_id", "commit_hash"])
            writer.writeheader()
            writer.writerow({"exp_id": "EXP-A,EXP-B;EXP-C", "commit_hash": "a" * 40})
        with patch.object(experiment_records, "REPO_ROOT", self.root):
            self.assertEqual(set(experiment_records.csv_projection()), {"EXP-A", "EXP-B", "EXP-C"})

    def test_record_uses_manifest_identity_and_nested_protocol(self):
        artifacts = self.root / "artifacts"
        run = artifacts / "EXP-A" / "RUN-A"
        run.mkdir(parents=True)
        (run / "summary.json").write_text(json.dumps({"metric": 1.25, "evaluation": {"mode": "protocol-a"}, "training": {"updates": 3},
                                                       "files": {"weights": "weights/model.bin"}}))
        mission = self.root / "issues/T"
        spec_path = mission / "runs/RUN-A/runspec.json"
        spec_path.parent.mkdir(parents=True)
        (mission / "tasks.csv").write_text("exp_id\nEXP-A\n")
        request = self.request()
        request["source"]["commit"] = "b" * 40
        spec = build_runspec(request)
        spec_path.write_text(json.dumps(spec))
        progress_name = request["adapter_contract"]["progress_path"]
        (run / progress_name).write_text('{"step": 3}\n')
        (run / "artifact_manifest.json").write_text(json.dumps({
            "schema_version": "rrctl.artifacts.v1", "run_id": "RUN-A",
            "provenance": {"spec_id": "SPEC-A", "exp_id": "EXP-A", "commit": "b" * 40,
                           "run_spec_sha256": run_spec_digest(spec)},
            "entries": [{"path": name, "size": (run / name).stat().st_size,
                         "sha256": hashlib.sha256((run / name).read_bytes()).hexdigest()}
                        for name in ("summary.json", progress_name)],
        }))
        with patch.object(experiment_records, "ARTIFACTS", artifacts), patch.object(experiment_records, "REPO_ROOT", self.root):
            record = experiment_records.build_record("EXP-A", {"commit": {"a" * 40}}, {
                "protocol_field": "evaluation.mode", "steps_field": "training.updates", "checkpoint_field": "files.weights",
            })
        self.assertEqual(record["runs"][0]["commit"], "b" * 40)
        self.assertEqual(record["runs"][0]["steps"], 3)
        self.assertEqual(record["runs"][0]["weights_path"], "weights/model.bin")
        self.assertEqual(record["metrics"]["protocol"], "protocol-a")

    def test_observer_timeout_gives_resume_without_diagnostic_or_relaunch(self):
        spec = build_runspec(self.request())
        path = self.root / "runspec.json"
        path.write_text(json.dumps(spec))
        calls = []
        def call(argv, _root):
            stage = argv[2]
            calls.append(stage)
            if stage == "wait":
                return subprocess.CompletedProcess(argv, 124, json.dumps({"ok": True, "result": {"status": {"state": "running"}, "observation": "timeout"}}), "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ready": True} if stage == "ready" else {"ok": True, "result": {}}), "")
        output = io.StringIO()
        with patch.object(remote_run, "validate_mission_launch"), patch.object(remote_run, "resolve_rrctl", return_value="rrctl"), patch.object(remote_run, "rrctl_call", side_effect=call), patch.object(remote_run, "_emit_stage"), contextlib.redirect_stdout(output):
            code = remote_run.execute(path, profiles=None, poll_seconds=600)
        self.assertEqual(code, 124)
        self.assertEqual(calls, ["ready", "launch", "wait"])
        self.assertIn("--resume", json.loads(output.getvalue())["resume_argv"])

    def test_resume_checks_identity_and_skips_launch(self):
        spec = build_runspec(self.request())
        path = self.root / "runspec.json"
        path.write_text(json.dumps(spec))
        calls = []
        def call(argv, _root):
            stage = argv[2]
            calls.append(stage)
            result = {"binding": {"run_spec_sha256": run_spec_digest(spec)}} if stage == "inspect" else {"status": {"state": "completed"}}
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": result}), "")
        # 此处只测 rrctl 传输序列；真实 CSV/生命周期准入由 ResearchBindingTests 覆盖。
        with patch.object(remote_run, "validate_mission_launch"), patch.object(remote_run, "resolve_rrctl", return_value="rrctl"), patch.object(remote_run, "rrctl_call", side_effect=call), patch.object(remote_run, "_emit_stage"):
            self.assertEqual(remote_run.execute(path, profiles=None, poll_seconds=600, resume=True), 0)
        self.assertEqual(calls, ["inspect", "wait", "pull"])

    def test_capability_check_rejects_old_installation(self):
        old = subprocess.CompletedProcess([], 2, "", "unknown doctor")
        with patch.object(remote_run.shutil, "which", return_value="/old/rrctl"), patch.object(remote_run.subprocess, "run", return_value=old):
            with self.assertRaises(ValueError):
                remote_run.resolve_rrctl()

    def test_capability_check_requires_every_process_contract_capability(self):
        report = {"backends": ["process"], "capabilities": [
            "observer-deadline", "worker-monitoring", "unknown-operation-outcome",
        ]}
        for missing in (None, "process", *report["capabilities"]):
            with self.subTest(missing=missing):
                value = {key: [item for item in values if item != missing] for key, values in report.items()}
                result = subprocess.CompletedProcess([], 0, json.dumps(value), "")
                with patch.object(remote_run.shutil, "which", return_value="/fixture/rrctl"), patch.object(remote_run.subprocess, "run", return_value=result):
                    if missing is None:
                        self.assertEqual(remote_run.resolve_rrctl(), "/fixture/rrctl")
                    else:
                        with self.assertRaisesRegex(ValueError, missing):
                            remote_run.resolve_rrctl()

    def named_config(self, default=True):
        path = self.root / "named.toml"
        path.write_text('version = 1\n' + ('[pipeline]\ndefault = "baseline"\n' if default else '') + '''
[[pipelines.baseline.stages]]
name = "train"
argv = ["bash", "a.sh"]
outputs = ["a.txt"]
[[pipelines.module_b.stages]]
name = "train_eval"
argv = ["bash", "b.sh"]
outputs = ["b.txt"]
''')
        (self.root / "a.sh").write_text('printf A > "$RRCTL_OUTPUT_ROOT/a.txt"\n')
        (self.root / "b.sh").write_text('printf B > "$RRCTL_OUTPUT_ROOT/b.txt"\n')
        return path

    def test_named_pipeline_runs_only_the_selected_scripts(self):
        path = self.named_config()
        output = self.root / "selected-output"
        config = load_config(path, pipeline="module_b")
        self.assertEqual(run_pipeline(config, self.root, output), 0)
        self.assertEqual((output / "b.txt").read_text(), "B")
        self.assertFalse((output / "a.txt").exists())
        self.assertEqual(list_pipelines(path), {"default": "baseline", "pipelines": ["baseline", "module_b"]})

    def test_named_selection_is_frozen_in_workload_preflight_and_metadata(self):
        path = self.named_config()
        request = self.request()
        request.pop("workload")
        request = apply_project_config(request, path, pipeline="module_b")
        value = build_runspec(request)
        self.assertEqual(value["metadata"]["pipeline_name"], "module_b")
        self.assertEqual(value["metadata"]["pipeline_stages"][0]["argv"], ["bash", "b.sh"])
        for argv in (value["workload"]["argv"], value["environment"]["preflight_argv"]):
            self.assertEqual(argv[argv.index("--pipeline") + 1], "module_b")
            self.assertEqual(argv[argv.index("--pipeline-sha256") + 1], value["metadata"]["pipeline_stages_sha256"])
        self.assertEqual(pipeline_digest(load_config(path, pipeline="module_b")), value["metadata"]["pipeline_stages_sha256"])

    def test_unknown_or_ambiguous_pipeline_never_falls_back(self):
        path = self.named_config(default=False)
        self.assertIsNone(list_pipelines(path)["default"])
        with self.assertRaises(ValueError):
            load_config(path)
        with self.assertRaises(ValueError):
            load_config(path, pipeline="unknown")

    def test_named_selection_rejects_conflicting_low_level_workload(self):
        path = self.named_config()
        with self.assertRaises(ValueError):
            apply_project_config(self.request(), path, pipeline="module_b")
        request = self.request()
        request.pop("workload")
        request["pipeline"] = "baseline"
        with self.assertRaises(ValueError):
            apply_project_config(request, path, pipeline="module_b")
        request.pop("pipeline")
        request["environment"]["preflight_argv"] = ["python", "check_another_module.py"]
        with self.assertRaises(ValueError):
            apply_project_config(request, path, pipeline="module_b")

    def test_changed_pipeline_digest_stops_before_execution(self):
        path = self.named_config()
        args = ["run_pipeline.py", "--config", str(path), "--pipeline", "module_b", "--pipeline-sha256", "0" * 64, "--check"]
        with patch.object(sys, "argv", args), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pipeline_module.main(), 2)
        self.assertFalse((self.root / "b.txt").exists())


if __name__ == "__main__":
    unittest.main()
