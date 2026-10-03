"""Focused coverage for completion receipt validation and finalize_exit ordering."""

from __future__ import annotations

import time
import unittest
from dataclasses import replace
from pathlib import Path

import test_process_backend as fixtures

from remote_run_control import state
from remote_run_control.errors import RRCError
from remote_run_control.finalization import (
    complete_existing,
    finalize_exit,
    read_completion,
    run_identity,
)
from remote_run_control.health import HealthResult
from remote_run_control.jsonutil import atomic_write_json, load_json, sha256_json, utc_now
from remote_run_control.models import RunSpec


def completed_report(phase="completion"):
    return HealthResult(
        healthy=True,
        complete=True,
        phase=phase,
        observations={"state": "workload_complete"},
        errors=(),
        status="healthy",
        gate_passed=True,
    )


def passing_check(_phase):
    return completed_report(phase=_phase)


class FinalizationHelpers:
    """在临时 control_root 中装出可验收的 workload_complete 状态。"""

    def settled_root(
        self: fixtures.ProcessBackendTests, spec: RunSpec, *, up_to: str = "launched"
    ):
        # finalize_exit 的结构：first_step 阶段只在 state == launched 时运行，
        # completion 阶段在 state == running 时先推进 workload_complete 再检查。
        root = Path(spec.remote.control_root)
        root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(root / "run_spec.json", spec.to_dict())
        binding = {
            "run_id": spec.run_id,
            "run_spec_sha256": spec.digest,
            "worker_sha256": "a" * 64,
            "created_at": utc_now(),
        }
        atomic_write_json(root / "binding.json", binding)
        ladder = {
            "launched": ("prepared", "staged", "launched"),
            "running": ("prepared", "staged", "launched", "first_step_passed", "running"),
        }[up_to]
        for value in ladder:
            state.transition(root, run_id=spec.run_id, next_state=value, reason="fixture")
        return root, binding


class ReadCompletionTests(fixtures.ProcessBackendTests, FinalizationHelpers):
    def sealed_root(self, spec, binding):
        root = Path(spec.remote.control_root)
        report = completed_report()
        manifest = {
            "schema_version": "rrctl.artifacts.v1",
            "run_id": spec.run_id,
            "generated_at": utc_now(),
            "output_root": spec.remote.output_root,
            "entries": [],
        }
        receipt = {
            "schema_version": "rrctl.completion.v1",
            "run_id": spec.run_id,
            "run_identity": run_identity(spec, binding),
            "run_spec_sha256": spec.digest,
            "manifest_sha256": sha256_json(manifest),
            "exit_code": 0,
            "accepted_at": utc_now(),
            "health": report.to_dict(),
        }
        atomic_write_json(root / "artifact_manifest.json", manifest)
        atomic_write_json(root / "completion.json", receipt)
        for value in ("first_step_passed", "running", "workload_complete"):
            state.transition(root, run_id=spec.run_id, next_state=value, reason="fixture")
        state.transition(
            root,
            run_id=spec.run_id,
            next_state="completed",
            reason="fixture",
            detail={"exit_code": 0, "completion_sha256": sha256_json(receipt)},
        )
        return root

    def test_valid_sealed_completion_is_accepted(self):
        spec = self.spec()
        root, binding = self.settled_root(spec)
        self.sealed_root(spec, binding)
        receipt = read_completion(spec, root, load_json(root / "binding.json"))
        self.assertEqual(receipt["schema_version"], "rrctl.completion.v1")
        self.assertEqual(receipt["run_id"], spec.run_id)

    def test_tampered_receipt_is_rejected_fail_closed(self):
        spec = self.spec()
        root, binding = self.settled_root(spec)
        self.sealed_root(spec, binding)
        receipt = load_json(root / "completion.json")
        receipt["accepted_at"] = "2000-01-01T00:00:00Z"  # 破坏 completion_sha256 链
        atomic_write_json(root / "completion.json", receipt)
        with self.assertRaises(RRCError) as caught:
            read_completion(spec, root, load_json(root / "binding.json"))
        self.assertEqual(caught.exception.code, "completion_receipt_invalid")

    def test_completion_idempotent_via_complete_existing(self):
        spec = self.spec()
        root, binding = self.settled_root(spec)
        sealed = self.sealed_root(spec, binding)
        status = complete_existing(spec, sealed)
        self.assertEqual(status["state"], "completed")

    def test_complete_existing_rejects_wrong_state(self):
        spec = self.spec()
        root, _binding = self.settled_root(spec)
        with self.assertRaises(RRCError) as caught:
            complete_existing(spec, root)
        self.assertEqual(caught.exception.code, "complete_state")


class FinalizeExitTests(fixtures.ProcessBackendTests, FinalizationHelpers):
    def test_nonzero_exit_fails_authoritatively(self):
        spec = self.spec()
        root, _binding = self.settled_root(spec)
        status = finalize_exit(
            spec, root, 3, check=passing_check, heartbeat=lambda: None
        )
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["reason"], "workload_exit_nonzero")
        self.assertEqual(
            load_json(root / "workload-exit.json")["exit_code"], 3
        )

    def test_zero_exit_publishes_completed_with_real_artifacts(self):
        spec = self.spec()
        root, _binding = self.settled_root(spec, up_to="running")
        Path(spec.remote.output_root).mkdir(parents=True, exist_ok=True)
        Path(spec.remote.output_root, "summary.json").write_text('{"complete": true}\n')
        Path(spec.remote.output_root, "started.json").write_text("started\n")
        status = finalize_exit(
            spec, root, 0, check=passing_check, heartbeat=lambda: None
        )
        self.assertEqual(status["state"], "completed")
        receipt = load_json(root / "completion.json")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["run_spec_sha256"], spec.digest)
        manifest_entry_paths = {
            entry["path"] for entry in load_json(root / "artifact_manifest.json")["entries"]
        }
        self.assertIn("summary.json", manifest_entry_paths)

    def test_zero_exit_missing_required_artifact_fails_as_result_contract(self):
        spec = self.spec()
        root, _binding = self.settled_root(spec, up_to="running")
        Path(spec.remote.output_root).mkdir(parents=True, exist_ok=True)
        # 只写一个工件，summary.json + started.json 均为 required
        status = finalize_exit(
            spec, root, 0, check=passing_check, heartbeat=lambda: None
        )
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["reason"], "artifact_contract_failed")

    def test_terminal_state_short_circuits_before_any_check(self):
        spec = self.spec()
        root, _binding = self.settled_root(spec)
        state.transition(
            root, run_id=spec.run_id, next_state="failed", reason="fixture"
        )
        calls = []

        def check(phase):
            calls.append(phase)
            return passing_check(phase)

        status = finalize_exit(spec, root, 0, check=check, heartbeat=lambda: None)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(calls, [])

    def test_hard_health_failure_is_result_contract_failure(self):
        spec = self.spec()
        root, _binding = self.settled_root(spec, up_to="running")
        Path(spec.remote.output_root).mkdir(parents=True, exist_ok=True)
        failing = HealthResult(
            healthy=False,
            complete=False,
            phase="completion",
            observations={},
            errors=("boom",),
            status="unhealthy",
            issues=(
                {"code": "adapter_unhealthy", "subject": "", "required": True,
                 "retryable": False},
            ),
        )
        status = finalize_exit(
            spec, root, 0, check=lambda _phase: failing, heartbeat=lambda: None
        )
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["reason"], "completion_contract_failed")

    def test_unavailable_check_until_deadline_yields_pending_state(self):
        spec = self.spec()
        root, _binding = self.settled_root(spec, up_to="running")
        Path(spec.remote.output_root).mkdir(parents=True, exist_ok=True)
        unavailable = HealthResult(
            healthy=True,
            complete=False,
            phase="completion",
            observations={},
            errors=(),
            status="unavailable",
            issues=(
                {"code": "worker_probe_unavailable", "subject": "", "required": True,
                 "retryable": True},
            ),
        )
        virtual = {"now": time.monotonic()}
        sleep_calls = []

        def clock():
            return virtual["now"]

        def sleep(seconds):
            sleep_calls.append(seconds)
            virtual["now"] = virtual["now"] + seconds

        status = finalize_exit(
            spec,
            root,
            0,
            check=lambda _phase: unavailable,
            heartbeat=lambda: None,
            clock=clock,
            sleep=sleep,
        )
        # 观察者不可用不得伪造完成；保持 workload_complete 并记录事实。
        self.assertEqual(status["state"], "workload_complete")
        self.assertTrue((root / "finalization-error.json").is_file())
        self.assertEqual(
            load_json(root / "finalization-error.json")["code"],
            "completion_check_unavailable",
        )
        self.assertTrue(sleep_calls)

    def test_first_step_gate_runs_before_completion_for_fast_workload(self):
        spec = self.spec()
        root, _binding = self.settled_root(spec, up_to="running")
        Path(spec.remote.output_root).mkdir(parents=True, exist_ok=True)
        Path(spec.remote.output_root, "summary.json").write_text("summary\n")
        Path(spec.remote.output_root, "started.json").write_text("started\n")
        phases = []

        def check(phase):
            phases.append(phase)
            return completed_report(phase=phase)

        status = finalize_exit(spec, root, 0, check=check, heartbeat=lambda: None)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(phases, ["completion"])

    def test_first_step_failure_from_launched_is_result_contract(self):
        # finalize_exit 的 fast-exit 路径（launched 状态退出为 0）先跑 first_step
        # 门；门的拒绝不得静默落到 completion。成功路径由
        # test_monitoring.test_fast_exit_still_requires_first_step_and_artifacts 覆盖。
        spec = self.spec()
        root, _binding = self.settled_root(spec)  # state == launched
        Path(spec.remote.output_root).mkdir(parents=True, exist_ok=True)
        phases = []

        def check(phase):
            phases.append(phase)
            return HealthResult(
                healthy=False,
                complete=False,
                phase=phase,
                observations={},
                errors=("boom",),
                status="unhealthy",
                issues=(
                    {"code": "fatal_pattern", "subject": "", "required": True,
                     "retryable": False},
                ),
            )

        status = finalize_exit(spec, root, 0, check=check, heartbeat=lambda: None)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["reason"], "first_step_contract_failed")
        self.assertEqual(phases, ["first_step"])

    def _progress_contract_spec(self):
        spec = self.spec()
        contract = {
            "progress_path": "progress.jsonl",
            "progress_format": "jsonl_last",
            "first_step_min_count": 1,
            "progress_finite_fields": ["finite"],
        }
        return replace(spec, metadata={"adapter_contract": contract})

    def test_fast_exit_with_real_progress_skips_first_step_gate(self):
        # smoke 场景：workload 真实跑完并 exit 0，只是首步 progress 读取过早；
        # 已有达到 min_count 且有限性合格的 progress 记录时，跳过 first_step 门，
        # 由 completion 契约唯一判终到 completed。
        spec = self._progress_contract_spec()
        root, _binding = self.settled_root(spec)  # state == launched
        output = Path(spec.remote.output_root)
        output.mkdir(parents=True, exist_ok=True)
        (output / "progress.jsonl").write_text(
            '{"step": 1, "loss": 1.0, "finite": true}\n'
            '{"step": 20, "loss": 0.4, "finite": true}\n'
        )
        # completion 契约所需声明 artifacts（沿用 fixture 声明）。
        Path(output, "summary.json").write_text("summary\n")
        Path(output, "started.json").write_text("started\n")
        phases = []

        def check(phase):
            phases.append(phase)
            return completed_report(phase=phase)

        status = finalize_exit(spec, root, 0, check=check, heartbeat=lambda: None)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(phases, ["completion"])
        self.assertNotIn("first_step", phases)

    def test_fast_exit_without_real_progress_keeps_first_step_gate(self):
        # 启动即退(0)、无有效 progress：不命中跳过分支，仍走 first_step 门并按
        # 现行保守语义判 failed；这是 test_first_step_failure_from_launched_is_result_contract
        # 在新 helper 路径下的回归保证。
        spec = self._progress_contract_spec()
        root, _binding = self.settled_root(spec)  # state == launched
        Path(spec.remote.output_root).mkdir(parents=True, exist_ok=True)
        # progress 不存在或不足 min_count -> 无真实进展。
        phases = []

        def check(phase):
            phases.append(phase)
            return HealthResult(
                healthy=False,
                complete=False,
                phase=phase,
                observations={},
                errors=("boom",),
                status="unhealthy",
                issues=(
                    {"code": "fatal_pattern", "subject": "", "required": True,
                     "retryable": False},
                ),
            )

        status = finalize_exit(spec, root, 0, check=check, heartbeat=lambda: None)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["reason"], "first_step_contract_failed")
        self.assertEqual(phases, ["first_step"])


if __name__ == "__main__":
    unittest.main()
