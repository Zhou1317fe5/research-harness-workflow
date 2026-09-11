"""Experiment-independent health probes and adapter composition."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapter import AdapterResult, run_adapter
from .environment import activation_argv, make_environment
from .errors import RRCError
from .jsonutil import append_jsonl, atomic_write_json, load_json, utc_now
from .models import HealthPhaseSpec, RunSpec
from .processes import owned_processes, probe_bound_process
from .state import control_lock, mark_first_step_passed, read_status


@dataclass(frozen=True, slots=True)
class HealthResult:
    healthy: bool
    complete: bool
    phase: str
    observations: dict[str, Any]
    errors: tuple[str, ...]
    adapter: AdapterResult | None = None
    status: str = "healthy"
    degraded_reasons: tuple[str, ...] = ()
    gate_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rrctl.health.v2",
            "status": self.status,
            "healthy": self.healthy,
            "complete": self.complete,
            "phase": self.phase,
            "observations": self.observations,
            "errors": list(self.errors),
            "degraded_reasons": list(self.degraded_reasons),
            "adapter": self.adapter.to_dict() if self.adapter else None,
            "gate_passed": self.gate_passed,
        }


def _file_age(path: Path, now: float) -> float | None:
    if not path.is_file():
        return None
    return max(0.0, now - path.stat().st_mtime)


def _sample_gpu(gpu_ids: list[str]) -> int | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                *(["-i", ",".join(gpu_ids)] if gpu_ids else []),
                "--query-gpu=utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    values: list[int] = []
    for line in result.stdout.splitlines():
        try:
            values.append(int(line.strip()))
        except ValueError:
            continue
    return max(values) if values else None


def _track_low_gpu(control_root: Path, *, phase: str, now: float, low: bool) -> float:
    path = control_root / "health_state.json"
    with control_lock(control_root):
        value = load_json(path) if path.is_file() else {"schema_version": "rrctl.health-state.v1"}
        since_by_phase = value.get("low_gpu_since", {})
        if not isinstance(since_by_phase, dict):
            since_by_phase = {}
        if low:
            since = since_by_phase.get(phase)
            if not isinstance(since, int | float) or isinstance(since, bool):
                since = now
            since_by_phase[phase] = since
            duration = max(0.0, now - float(since))
        else:
            since_by_phase.pop(phase, None)
            duration = 0.0
        value["low_gpu_since"] = since_by_phase
        atomic_write_json(path, value)
    return duration


def _phase_spec(spec: RunSpec, phase: str) -> HealthPhaseSpec:
    if phase not in {"first_step", "periodic", "completion"}:
        raise RRCError("health_phase", f"unknown health phase: {phase}", "health")
    return getattr(spec.health, phase)


def environment_prefix(spec: RunSpec, gpu_ids: list[str]) -> tuple[str, ...]:
    return tuple(
        activation_argv(
            [], overrides={**spec.environment.variables, "CUDA_VISIBLE_DEVICES": ",".join(gpu_ids)}
        )
    )


def evaluate_health(
    spec: RunSpec,
    control_root: Path,
    *,
    phase: str,
    process_required: bool = True,
    transition_lifecycle: bool = True,
) -> HealthResult:
    phase_spec = _phase_spec(spec, phase)
    now = time.time()
    status = read_status(control_root)
    binding_path = control_root / "binding.json"
    binding = load_json(binding_path) if binding_path.is_file() else {}
    errors: list[str] = []
    degraded_reasons: list[str] = []
    observations: dict[str, Any] = {"state": status.get("state")}

    workload_pid = binding.get("workload_pid")
    executor_pid = binding.get("executor_pid")
    workload_probe = probe_bound_process(binding, "workload", spec.run_id, control_root)
    executor_probe = probe_bound_process(binding, "executor", spec.run_id, control_root)
    workload_alive = workload_probe["state"] == "alive"
    executor_alive = executor_probe["state"] == "alive"
    process_alive = workload_alive or executor_alive
    first_step_ready = not process_required or workload_alive
    observations.update(
        {
            "process_pid": workload_pid or executor_pid,
            "process_alive": process_alive,
            "workload_pid": workload_pid,
            "workload_alive": workload_alive,
            "executor_pid": executor_pid,
            "executor_alive": executor_alive,
            "workload_identity": workload_probe,
            "executor_identity": executor_probe,
            "backend": spec.session.backend,
        }
    )
    if process_required and status.get("state") not in {"completed", "failed", "aborted"}:
        if not process_alive:
            if any(
                probe["state"] in {"pending", "unavailable", "mismatch"}
                for probe in (workload_probe, executor_probe)
            ):
                degraded_reasons.append("process identity is not yet authoritative")
            elif owned_processes(spec.run_id, control_root):
                degraded_reasons.append("supervisor exited while owned descendants remain")
            else:
                errors.append("owned process is not alive")
        elif not workload_alive:
            degraded_reasons.append("workload is starting or finishing")
        elif not executor_alive:
            degraded_reasons.append("workload is alive but supervisor identity is unavailable")

    console = control_root / "console.log"
    console_age = _file_age(console, now)
    observations["console_age_seconds"] = console_age
    if phase_spec.console_stale_seconds is not None:
        if console_age is None:
            degraded_reasons.append("console log is missing")
            first_step_ready = False
        elif console_age > phase_spec.console_stale_seconds:
            degraded_reasons.append("console log is stale")
            first_step_ready = False
    if console.is_file():
        with console.open("rb") as stream:
            stream.seek(max(0, console.stat().st_size - 65536))
            text = stream.read().decode("utf-8", errors="replace")
        for pattern in phase_spec.fatal_patterns:
            if re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
                errors.append(f"fatal pattern matched: {pattern}")

    if phase_spec.progress_path:
        progress = Path(spec.remote.output_root) / phase_spec.progress_path
        progress_age = _file_age(progress, now)
        observations["progress_path"] = str(progress)
        observations["progress_age_seconds"] = progress_age
        if progress_age is None:
            degraded_reasons.append("progress file is missing")
            first_step_ready = False
        elif (
            phase_spec.progress_stale_seconds is not None
            and progress_age > phase_spec.progress_stale_seconds
        ):
            degraded_reasons.append("progress file is stale")
            first_step_ready = False

    gpu_policy = phase_spec.gpu_utilization_policy
    observations["gpu_utilization_policy"] = gpu_policy
    if gpu_policy != "disabled" and phase_spec.gpu_min_percent is not None:
        gpu = _sample_gpu(binding.get("resources", {}).get("gpu_ids", []))
        observations["gpu_utilization_percent"] = gpu
        if gpu is None:
            observations["low_gpu_duration_seconds"] = _track_low_gpu(
                control_root, phase=phase, now=now, low=False
            )
            degraded_reasons.append("GPU utilization is unavailable")
        elif gpu < phase_spec.gpu_min_percent:
            low_duration = _track_low_gpu(control_root, phase=phase, now=now, low=True)
            observations["low_gpu_duration_seconds"] = low_duration
            reached_required_window = (
                gpu_policy == "required"
                and phase_spec.low_gpu_limit_seconds is not None
                and low_duration >= phase_spec.low_gpu_limit_seconds
            )
            if reached_required_window:
                errors.append(f"GPU utilization {gpu}% is below {phase_spec.gpu_min_percent}%")
            else:
                degraded_reasons.append(
                    f"GPU utilization {gpu}% is below {phase_spec.gpu_min_percent}%"
                )
        else:
            observations["low_gpu_duration_seconds"] = _track_low_gpu(
                control_root, phase=phase, now=now, low=False
            )

    context = {
        "protocol": "rrctl.adapter.context.v1",
        "phase": phase,
        "run_id": spec.run_id,
        "project": spec.project,
        "repo_root": spec.remote.repo_root,
        "output_root": spec.remote.output_root,
        "control_root": str(control_root),
        "status": status,
        "binding": binding,
        "metadata": spec.metadata,
    }
    adapter: AdapterResult | None = None
    if phase_spec.adapter_argv:
        adapter_env = make_environment(
            asdict(spec.environment),
            {
                "CUDA_VISIBLE_DEVICES": ",".join(binding.get("resources", {}).get("gpu_ids", [])),
            },
        )
        try:
            adapter = run_adapter(
                phase_spec.adapter_argv,
                context,
                timeout_seconds=phase_spec.adapter_timeout_seconds,
                cwd=Path(spec.remote.repo_root) / spec.workload.cwd,
                environment_command=environment_prefix(
                    spec, binding.get("resources", {}).get("gpu_ids", [])
                ),
                env=adapter_env,
            )
            if adapter and not adapter.healthy:
                errors.append("adapter reported unhealthy")
        except RRCError as exc:
            observations["adapter_error"] = {"code": exc.code, "phase": exc.phase}
            errors.append(f"adapter health contract failed: {exc.code}")

    complete = status.get("state") in {"workload_complete", "completed"}
    if phase == "completion" and adapter is not None:
        complete = bool(adapter.complete) and status.get("state") in {
            "workload_complete",
            "completed",
        }
        if not adapter.complete:
            errors.append("completion adapter reported incomplete")
    elif (
        phase == "completion"
        and spec.output_cleanup is not None
        and spec.output_cleanup.mode == "pre_review_smoke"
    ):
        summary_path = Path(spec.remote.output_root) / "smoke_summary.json"
        try:
            summary = load_json(summary_path)
        except (OSError, ValueError):
            summary = None
        observations["smoke_summary_path"] = str(summary_path)
        if not isinstance(summary, dict):
            errors.append("smoke summary is missing or invalid")
        elif summary.get("schema_version") != "rrctl.smoke-summary.v1":
            errors.append("smoke summary schema is invalid")
        elif summary.get("run_id") != spec.run_id:
            errors.append("smoke summary run_id does not match")
        elif summary.get("checkpoint_cleanup_completed") is not True:
            errors.append("smoke checkpoint cleanup is incomplete")
        elif summary.get("checkpoint_paths_remaining") != []:
            errors.append("smoke checkpoint paths remain")
        else:
            complete = status.get("state") in {"workload_complete", "completed"}
    if status.get("state") in {"failed", "aborted"}:
        errors.append(f"run is terminal: {status.get('state')}")
    health_status = "unhealthy" if errors else "degraded" if degraded_reasons else "healthy"
    healthy = health_status != "unhealthy"
    result = HealthResult(
        healthy=healthy,
        complete=complete,
        phase=phase,
        observations=observations,
        errors=tuple(errors),
        adapter=adapter,
        status=health_status,
        degraded_reasons=tuple(degraded_reasons),
        gate_passed=healthy and first_step_ready,
    )
    event = {**result.to_dict(), "at": utc_now(), "run_id": spec.run_id}
    append_jsonl(control_root / "health.jsonl", event)

    if result.gate_passed and transition_lifecycle and phase == "first_step":
        mark_first_step_passed(control_root, run_id=spec.run_id, health=result.to_dict())
    return result
