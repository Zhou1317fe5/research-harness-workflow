#!/usr/bin/env python3
"""Unit tests for the lean scientific PRERUN packet."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from prerun_ready import validate_packet


class PrerunReadyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name)
        subprocess.run(
            ["git", "init", "-b", "review-branch", str(self.repo)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test"],
            check=True,
        )
        (self.repo / "run.py").write_text("print('baseline')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "run.py"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "baseline"],
            check=True,
            capture_output=True,
        )
        self.base_commit = self.rev_parse()
        (self.repo / "run.py").write_text("print('scientific')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "run.py"], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-m", "implementation"],
            check=True,
            capture_output=True,
        )
        self.commit = self.rev_parse()
        self.packet = self.valid_packet()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def rev_parse(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def smoke(self, **overrides) -> dict:
        value = {
            "schema_version": "prerun.pre-review-smoke.v1",
            "disposition": "passed",
            "candidate_commit": self.commit,
            "run_id": "SMOKE-01",
            "exact_command": "rrctl launch smoke-01.json",
            "exit_code": 0,
            "step_budget": 3,
            "completed_steps": 3,
            "finite_loss": True,
            "production_entrypoint_reached": True,
            "isolated_output": True,
            "official_metrics_disabled": True,
            "artifact_ingest_disabled": True,
            "checkpoint_cleanup_completed": True,
            "checkpoint_paths_remaining": [],
            "retained_evidence_paths": [
                "control/console.log",
                "control/status.json",
                "output/smoke_summary.json",
            ],
            "evidence_paths": ["/tmp/smoke-01/status.json"],
        }
        value.update(overrides)
        return value

    def valid_packet(self) -> dict:
        return {
            "schema_version": "prerun.scientific-review.v1",
            "review_mode": "scientific_review",
            "repo_root": str(self.repo),
            "pre_run_code_commit": self.commit,
            "review_diff_base_commit": self.base_commit,
            "approved_basis": ["docs/spec.md#scientific-contract"],
            "implementation_intent": "route lambda_fg into the training loss",
            "exact_command": "python run.py --fold 0 --shot 1",
            "output_collision_policy": "unique_output",
            "local_validation": [
                {
                    "command": "python -m compileall run.py",
                    "exit_code": 0,
                    "observation": "compiled",
                }
            ],
            "pre_review_smoke": self.smoke(),
            "critical_values": [
                {
                    "name": "lambda_fg",
                    "source": "CLI --lambda-fg",
                    "sink": "training loss foreground term",
                    "evidence": "run.py production trace",
                }
            ],
            "experiment": {
                "benchmark": "bench-a",
                "dataset": "bench-split-a",
                "checkpoint": "baseline.ckpt",
                "fold": 0,
                "shot": 1,
                "seed": 123,
                "metric_policy": "指标 primary; 辅助指标 auxiliary",
                "output_path": "/tmp/exp/scientific-01",
            },
        }

    def errors(self, packet: dict) -> str:
        return "\n".join(validate_packet(packet)["errors"])

    def test_complete_packet_is_ready_and_digest_is_stable(self) -> None:
        first = validate_packet(self.packet)
        second = validate_packet(json.loads(json.dumps(self.packet, sort_keys=True)))
        self.assertTrue(first["ready"], first)
        self.assertEqual(first["packet_sha256"], second["packet_sha256"])
        self.assertEqual(first["review_mode"], "scientific_review")
        self.assertEqual(first["implementation_diff_paths"], ["run.py"])

    def test_schema_and_mode_are_explicit(self) -> None:
        packet = deepcopy(self.packet)
        packet["schema_version"] = "prerun.legacy.v1"
        packet["review_mode"] = "resolution_review"
        errors = self.errors(packet)
        self.assertIn("schema_invalid", errors)
        self.assertIn("review_mode_invalid", errors)

    def test_scientific_review_requires_approved_basis_and_critical_sink(self) -> None:
        packet = deepcopy(self.packet)
        packet["approved_basis"] = []
        packet["critical_values"] = []
        errors = self.errors(packet)
        self.assertIn("approved_basis_missing", errors)
        self.assertIn("critical_values_missing", errors)

    def test_critical_value_requires_source_sink_and_evidence(self) -> None:
        packet = deepcopy(self.packet)
        packet["critical_values"][0].pop("sink")
        packet["critical_values"][0].pop("evidence")
        errors = self.errors(packet)
        self.assertIn("critical_values[0].sink", errors)
        self.assertIn("critical_values[0].evidence", errors)

    def test_experiment_identity_is_required(self) -> None:
        packet = deepcopy(self.packet)
        packet["experiment"].pop("checkpoint")
        packet["experiment"].pop("metric_policy")
        errors = self.errors(packet)
        self.assertIn("experiment.checkpoint", errors)
        self.assertIn("experiment.metric_policy", errors)

    def test_local_validation_must_pass(self) -> None:
        packet = deepcopy(self.packet)
        packet["local_validation"][0]["exit_code"] = 1
        self.assertIn("local_validation_failed", self.errors(packet))

    def test_current_commit_smoke_is_required(self) -> None:
        packet = deepcopy(self.packet)
        packet.pop("pre_review_smoke")
        self.assertIn("pre_review_smoke_missing", self.errors(packet))
        packet["pre_review_smoke"] = self.smoke(candidate_commit="0" * 40)
        self.assertIn("pre_review_smoke_commit_mismatch", self.errors(packet))

    def test_smoke_enforces_budget_runtime_and_no_metrics_boundary(self) -> None:
        packet = deepcopy(self.packet)
        packet["pre_review_smoke"] = self.smoke(
            step_budget=101,
            finite_loss=False,
            official_metrics_disabled=False,
        )
        errors = self.errors(packet)
        self.assertIn("pre_review_smoke_step_budget_invalid", errors)
        self.assertIn("pre_review_smoke_boundary_invalid", errors)

    def test_smoke_requires_completed_cleanup_and_retained_evidence(self) -> None:
        packet = deepcopy(self.packet)
        packet["pre_review_smoke"] = self.smoke(
            checkpoint_cleanup_completed=False,
            checkpoint_paths_remaining=["checkpoint-10"],
            retained_evidence_paths=["console.log"],
        )
        errors = self.errors(packet)
        self.assertIn("pre_review_smoke_boundary_invalid", errors)
        self.assertIn("pre_review_smoke_cleanup_incomplete", errors)
        self.assertIn("pre_review_smoke_retained_evidence_invalid", errors)

    def test_smoke_not_applicable_is_reviewable_with_warning(self) -> None:
        packet = deepcopy(self.packet)
        packet["pre_review_smoke"] = {
            "schema_version": "prerun.pre-review-smoke.v1",
            "disposition": "not_applicable",
            "reason": "no GPU production path",
            "alternative_validation": "CPU production integration passed",
        }
        result = validate_packet(packet)
        self.assertTrue(result["ready"], result)
        self.assertTrue(
            any("pre_review_smoke_not_applicable" in item for item in result["warnings"])
        )

    def test_targeted_review_does_not_require_scientific_smoke_or_experiment(self) -> None:
        packet = deepcopy(self.packet)
        packet["review_mode"] = "targeted_review"
        packet.pop("pre_review_smoke")
        packet.pop("experiment")
        packet.pop("critical_values")
        result = validate_packet(packet)
        self.assertTrue(result["ready"], result)

    def test_output_collision_policy_is_narrow(self) -> None:
        packet = deepcopy(self.packet)
        packet["output_collision_policy"] = "overwrite"
        self.assertIn("output_collision_policy_invalid", self.errors(packet))

    def test_commit_and_diff_base_must_be_resolvable(self) -> None:
        packet = deepcopy(self.packet)
        packet["pre_run_code_commit"] = "0" * 40
        errors = self.errors(packet)
        self.assertIn("unresolvable_commit", errors)

    def test_secret_in_command_is_rejected(self) -> None:
        packet = deepcopy(self.packet)
        packet["exact_command"] += " --api-key=supersecret"
        self.assertIn("secret_in_command", self.errors(packet))

    def test_old_state_machine_fields_are_rejected(self) -> None:
        packet = deepcopy(self.packet)
        packet.update(
            formal_attempt=2,
            gate_lineage_id="legacy-lineage",
            gate_generation=9,
            review_state={"legacy": True},
            coverage_manifest={"legacy": True},
            rrctl_provenance={"legacy": True},
        )
        result = validate_packet(packet)
        self.assertFalse(result["ready"], result)
        for field in (
            "formal_attempt",
            "gate_lineage_id",
            "gate_generation",
            "review_state",
            "coverage_manifest",
            "rrctl_provenance",
        ):
            self.assertTrue(
                any(field in error for error in result["errors"]),
                (field, result),
            )


if __name__ == "__main__":
    unittest.main()
