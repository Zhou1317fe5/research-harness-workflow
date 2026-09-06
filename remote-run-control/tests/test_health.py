from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from remote_run_control.errors import RRCError
from remote_run_control.health import evaluate_health
from remote_run_control.models import HealthPhaseSpec
from remote_run_control.state import transition


def test_low_gpu_must_exceed_sustained_limit_before_failure(
    monkeypatch, project_factory, spec_factory
):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    periodic = HealthPhaseSpec(
        timeout_seconds=30,
        poll_interval_seconds=1,
        gpu_min_percent=30,
        low_gpu_limit_seconds=10,
        gpu_utilization_policy="required",
    )
    spec = replace(spec, health=replace(spec.health, periodic=periodic))
    control = __import__("pathlib").Path(spec.remote.control_root)
    transition(control, run_id=spec.run_id, next_state="prepared", reason="test")
    clock = [100.0]
    monkeypatch.setattr("remote_run_control.health.time.time", lambda: clock[0])
    monkeypatch.setattr("remote_run_control.health._sample_gpu", lambda: 12)

    first = evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )
    assert first.healthy
    assert first.status == "degraded"
    assert first.observations["low_gpu_duration_seconds"] == 0

    clock[0] = 111.0
    sustained = evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )
    assert not sustained.healthy
    assert sustained.status == "unhealthy"
    assert sustained.observations["low_gpu_duration_seconds"] == 11

    monkeypatch.setattr("remote_run_control.health._sample_gpu", lambda: 70)
    recovered = evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )
    assert recovered.healthy
    assert recovered.status == "healthy"
    assert recovered.observations["low_gpu_duration_seconds"] == 0


def test_concurrent_health_samples_remain_parseable(project_factory, spec_factory):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    periodic = HealthPhaseSpec(timeout_seconds=30, poll_interval_seconds=1)
    spec = replace(spec, health=replace(spec.health, periodic=periodic))
    control = __import__("pathlib").Path(spec.remote.control_root)
    transition(control, run_id=spec.run_id, next_state="prepared", reason="test")

    def sample(_):
        return evaluate_health(
            spec, control, phase="periodic", process_required=False, transition_lifecycle=False
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(sample, range(16)))

    assert all(result.healthy for result in results)
    lines = (control / "health.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 16
    assert all(json.loads(line)["schema_version"] == "rrctl.health.v2" for line in lines)


def test_concurrent_first_step_checks_record_one_gate(project_factory, spec_factory):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    first_step = HealthPhaseSpec(timeout_seconds=30, poll_interval_seconds=1)
    spec = replace(spec, health=replace(spec.health, first_step=first_step))
    control = Path(spec.remote.control_root)
    for state in ("prepared", "staged", "launched"):
        transition(control, run_id=spec.run_id, next_state=state, reason="setup")

    def sample(_):
        return evaluate_health(spec, control, phase="first_step", process_required=False)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(sample, range(16)))

    assert all(result.healthy for result in results)
    events = [json.loads(line) for line in (control / "events.jsonl").read_text().splitlines()]
    assert [event["state"] for event in events] == [
        "prepared", "staged", "launched", "first_step_passed", "running"
    ]


def test_missing_gpu_sample_is_degraded_and_resets_required_window(
    monkeypatch, project_factory, spec_factory
):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    periodic = HealthPhaseSpec(
        timeout_seconds=30,
        poll_interval_seconds=1,
        gpu_min_percent=30,
        low_gpu_limit_seconds=10,
        gpu_utilization_policy="required",
    )
    spec = replace(spec, health=replace(spec.health, periodic=periodic))
    control = __import__("pathlib").Path(spec.remote.control_root)
    transition(control, run_id=spec.run_id, next_state="prepared", reason="test")
    clock = [100.0]
    samples = iter((12, None, 12))
    monkeypatch.setattr("remote_run_control.health.time.time", lambda: clock[0])
    monkeypatch.setattr("remote_run_control.health._sample_gpu", lambda: next(samples))

    first = evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )
    clock[0] = 111.0
    missing = evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )
    clock[0] = 122.0
    restarted = evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )

    assert first.status == "degraded"
    assert missing.status == "degraded"
    assert missing.observations["low_gpu_duration_seconds"] == 0
    assert restarted.status == "degraded"
    assert restarted.observations["low_gpu_duration_seconds"] == 0


def test_advisory_low_gpu_never_becomes_unhealthy(monkeypatch, project_factory, spec_factory):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    periodic = HealthPhaseSpec(
        timeout_seconds=30,
        poll_interval_seconds=1,
        gpu_min_percent=30,
        low_gpu_limit_seconds=10,
        gpu_utilization_policy="advisory",
    )
    spec = replace(spec, health=replace(spec.health, periodic=periodic))
    control = __import__("pathlib").Path(spec.remote.control_root)
    transition(control, run_id=spec.run_id, next_state="prepared", reason="test")
    clock = [100.0]
    monkeypatch.setattr("remote_run_control.health.time.time", lambda: clock[0])
    monkeypatch.setattr("remote_run_control.health._sample_gpu", lambda: 1)

    evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )
    clock[0] = 1000.0
    result = evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )

    assert result.status == "degraded"
    assert result.healthy


def test_disabled_gpu_policy_does_not_sample(monkeypatch, project_factory, spec_factory):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    periodic = HealthPhaseSpec(
        timeout_seconds=30,
        gpu_min_percent=30,
        low_gpu_limit_seconds=10,
        gpu_utilization_policy="disabled",
    )
    spec = replace(spec, health=replace(spec.health, periodic=periodic))
    control = __import__("pathlib").Path(spec.remote.control_root)
    transition(control, run_id=spec.run_id, next_state="prepared", reason="test")
    monkeypatch.setattr(
        "remote_run_control.health._sample_gpu",
        lambda: (_ for _ in ()).throw(AssertionError("GPU must not be sampled")),
    )

    result = evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )

    assert result.status == "healthy"


def test_adapter_protocol_error_is_authoritative_unhealthy(
    monkeypatch, project_factory, spec_factory
):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    periodic = replace(spec.health.periodic, adapter_argv=("adapter",))
    spec = replace(spec, health=replace(spec.health, periodic=periodic))
    control = __import__("pathlib").Path(spec.remote.control_root)
    transition(control, run_id=spec.run_id, next_state="prepared", reason="test")

    def fail_adapter(*_args, **_kwargs):
        raise RRCError("adapter_protocol", "invalid adapter output", "adapter")

    monkeypatch.setattr("remote_run_control.health.run_adapter", fail_adapter)
    result = evaluate_health(
        spec, control, phase="periodic", process_required=False, transition_lifecycle=False
    )

    assert result.status == "unhealthy"
    assert result.observations["adapter_error"]["code"] == "adapter_protocol"
