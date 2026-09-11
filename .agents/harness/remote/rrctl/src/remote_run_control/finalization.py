"""远端完成验收及封存；观察者不重复执行科学检查。"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .artifacts import build_artifact_manifest, load_artifact_manifest
from .cleanup import cleanup_output
from .errors import RRCError
from .health import HealthResult, evaluate_health
from .jsonutil import atomic_write_json, load_json, sha256_json, utc_now
from .models import RunSpec
from .state import (
    TERMINAL_STATES,
    operation_lock,
    publish_completed,
    read_status,
    transition_if_open,
)


def run_identity(spec: RunSpec, binding: dict[str, Any]) -> str:
    return sha256_json(
        {
            "run_id": spec.run_id,
            "run_spec_sha256": spec.digest,
            "control_root": spec.remote.control_root,
            "worker_sha256": binding.get("worker_sha256"),
            "created_at": binding.get("created_at"),
        }
    )


def read_completion(spec: RunSpec, control_root: Path, binding: dict[str, Any]) -> dict[str, Any]:
    """只验证封存身份与小型 manifest；拉取时另行校验全部文件内容。"""
    try:
        receipt = load_json(control_root / "completion.json")
        manifest = load_artifact_manifest(
            control_root / "artifact_manifest.json", expected_run_id=spec.run_id
        )
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != "rrctl.completion.v1"
            or receipt.get("run_identity") != run_identity(spec, binding)
            or receipt.get("run_spec_sha256") != spec.digest
            or receipt.get("manifest_sha256") != sha256_json(manifest)
            or read_status(control_root).get("detail", {}).get("completion_sha256")
            != sha256_json(receipt)
            or receipt.get("exit_code") != 0
            or isinstance(receipt.get("exit_code"), bool)
            or not isinstance(receipt.get("health"), dict)
            or receipt["health"].get("schema_version") != "rrctl.health.v2"
            or receipt["health"].get("phase") != "completion"
            or receipt["health"].get("healthy") is not True
            or receipt["health"].get("complete") is not True
        ):
            raise ValueError("completion receipt identity or verdict is invalid")
    except (OSError, ValueError, TypeError, RRCError) as exc:
        raise RRCError(
            "completion_receipt_invalid",
            "sealed completion evidence is unavailable or invalid",
            "observer",
        ) from exc
    return receipt


def _publish(spec: RunSpec, control_root: Path, report: HealthResult) -> dict[str, Any]:
    if not report.healthy or not report.complete:
        raise RRCError("completion_health_failed", "completion contract did not pass", "health")
    manifest = build_artifact_manifest(
        run_id=spec.run_id,
        output_root=Path(spec.remote.output_root),
        declared=spec.artifacts,
        adapter_paths=report.adapter.artifacts if report.adapter else (),
        destination=None,
    )
    manifest["provenance"] = {
        "run_spec_sha256": spec.digest,
        "commit": spec.source.commit,
        "branch": spec.source.branch,
        "spec_id": spec.metadata.get("spec_id"),
        "exp_id": spec.metadata.get("exp_id"),
        "pipeline_name": spec.metadata.get("pipeline_name"),
        "pipeline_stages_sha256": spec.metadata.get("pipeline_stages_sha256"),
    }
    binding = load_json(control_root / "binding.json")
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
    return publish_completed(control_root, run_id=spec.run_id, manifest=manifest, receipt=receipt)


def complete_existing(spec: RunSpec, control_root: Path) -> dict[str, Any]:
    """兼容未声明自主监控能力的调用；验收与发布仍只有一份实现。"""
    with operation_lock(control_root, "finalization"):
        status = read_status(control_root)
        if status["state"] == "completed":
            read_completion(spec, control_root, load_json(control_root / "binding.json"))
            return status
        if status["state"] != "workload_complete":
            raise RRCError("complete_state", "completion requires workload_complete", "health")
        report = evaluate_health(
            spec,
            control_root,
            phase="completion",
            process_required=False,
            transition_lifecycle=False,
        )
        return _publish(spec, control_root, report)


def finalize_exit(
    spec: RunSpec,
    control_root: Path,
    return_code: int,
    *,
    check: Callable[[str], HealthResult],
    heartbeat: Callable[[], None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """真实退出、清理、快任务首步、完成契约及发布的 canonical 顺序。"""
    atomic_write_json(
        control_root / "workload-exit.json",
        {
            "run_id": spec.run_id,
            "run_spec_sha256": spec.digest,
            "exit_code": return_code,
            "at": utc_now(),
        },
    )

    def stopped() -> bool:
        return (
            read_status(control_root)["state"] in TERMINAL_STATES
            or (control_root / "stop_request.json").exists()
        )

    def fail(reason: str, detail: dict[str, Any]) -> dict[str, Any]:
        return transition_if_open(
            control_root,
            run_id=spec.run_id,
            next_state="failed",
            reason=reason,
            detail={"exit_code": return_code, **detail},
        )

    def unavailable(code: str, phase: str) -> dict[str, Any]:
        atomic_write_json(
            control_root / "finalization-error.json",
            {
                "run_id": spec.run_id,
                "kind": "checker_error",
                "code": code,
                "phase": phase,
                "exit_code": return_code,
                "at": utc_now(),
            },
        )
        return read_status(control_root)

    with operation_lock(control_root, "finalization"):
        if stopped():
            return read_status(control_root)
        if return_code != 0:
            status = fail("workload_exit_nonzero", {"failure_kind": "workload_exit"})
            try:
                cleanup_output(spec, terminal_state="failed")
            except (OSError, RRCError) as exc:
                unavailable(getattr(exc, "code", "cleanup_io"), "cleanup")
            return status
        try:
            cleanup_output(spec, terminal_state="workload_exit_zero")
        except RRCError as exc:
            return fail("cleanup_contract_failed", {"error": exc.to_dict()})
        except OSError:
            return unavailable("cleanup_io", "cleanup")

        for phase in ("first_step", "completion"):
            if stopped():
                return read_status(control_root)
            state = read_status(control_root)["state"]
            if phase == "first_step" and state != "launched":
                continue
            if phase == "completion" and state == "running":
                transition_if_open(
                    control_root,
                    run_id=spec.run_id,
                    next_state="workload_complete",
                    reason="workload_exit_zero_awaiting_completion",
                    detail={"exit_code": 0},
                )
            policy = getattr(spec.health, phase)
            deadline = clock() + policy.timeout_seconds
            while not stopped():
                heartbeat()
                report = check(phase)
                heartbeat()
                hard = any(item["required"] and not item["retryable"] for item in report.issues)
                retryable = report.status == "unavailable"
                passed = (
                    report.gate_passed
                    if phase == "first_step"
                    else (report.healthy and report.complete)
                )
                if hard or (not passed and not retryable):
                    return fail(
                        f"{phase}_contract_failed",
                        {
                            "failure_kind": "result_contract",
                            "health": report.to_dict(),
                        },
                    )
                if passed:
                    if phase == "first_step":
                        break
                    try:
                        return _publish(spec, control_root, report)
                    except RRCError as exc:
                        return fail(
                            "artifact_contract_failed",
                            {
                                "failure_kind": "result_contract",
                                "error": exc.to_dict(),
                            },
                        )
                    except OSError:
                        return unavailable("completion_publication_io", phase)
                if clock() >= deadline:
                    return unavailable("completion_check_unavailable", phase)
                sleep(min(policy.poll_interval_seconds, max(0, deadline - clock()), 30))
        return read_status(control_root)
