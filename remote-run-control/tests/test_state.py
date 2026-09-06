from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import remote_run_control.state as state_module
from remote_run_control.errors import RRCError
from remote_run_control.state import (
    mark_first_step_passed,
    read_status,
    recover_status,
    transition,
)


def test_legal_state_sequence_is_atomic_and_auditable(tmp_path):
    control = tmp_path / "control"
    states = [
        "prepared",
        "staged",
        "launched",
        "first_step_passed",
        "running",
        "workload_complete",
        "completed",
    ]
    for state in states:
        transition(control, run_id="run-1", next_state=state, reason=f"to-{state}")
    status = read_status(control)
    assert status["state"] == "completed"
    assert status["sequence"] == len(states)
    events = [json.loads(line) for line in (control / "events.jsonl").read_text().splitlines()]
    assert [item["state"] for item in events] == states
    assert [item["sequence"] for item in events] == list(range(1, len(states) + 1))


def test_illegal_transition_and_run_mismatch_fail_closed(tmp_path):
    control = tmp_path / "control"
    transition(control, run_id="run-1", next_state="prepared", reason="start")
    with pytest.raises(RRCError, match="state_transition_invalid"):
        transition(control, run_id="run-1", next_state="completed", reason="skip")
    with pytest.raises(RRCError, match="state_run_mismatch"):
        transition(control, run_id="run-2", next_state="staged", reason="wrong")


def test_first_step_gate_serializes_with_workload_completion(tmp_path, monkeypatch):
    control = tmp_path / "control"
    for state in ("prepared", "staged", "launched"):
        transition(control, run_id="run-1", next_state=state, reason="setup")
    gate_written = Event()
    release_gate = Event()
    completion_started = Event()
    original_append = state_module.append_jsonl

    def pause_after_gate(path, value):
        original_append(path, value)
        if value["state"] == "first_step_passed":
            gate_written.set()
            assert release_gate.wait(5), "首步检查未按时继续"

    def finish_workload():
        completion_started.set()
        return transition(
            control,
            run_id="run-1",
            next_state="workload_complete",
            reason="workload_exit_zero",
        )

    monkeypatch.setattr(state_module, "append_jsonl", pause_after_gate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        gate = pool.submit(mark_first_step_passed, control, run_id="run-1", health={})
        try:
            assert gate_written.wait(5)
            completion = pool.submit(finish_workload)
            assert completion_started.wait(5)
            assert not completion.done()
        finally:
            release_gate.set()
        assert gate.result(timeout=5)["state"] == "running"
        assert completion.result(timeout=5)["state"] == "workload_complete"

    status = read_status(control)
    assert status["sequence"] == 6
    assert mark_first_step_passed(control, run_id="run-1", health={}) == status
    events = [json.loads(line) for line in (control / "events.jsonl").read_text().splitlines()]
    assert [event["state"] for event in events][-3:] == [
        "first_step_passed", "running", "workload_complete"
    ]


def test_corrupt_status_is_rebuilt_from_valid_event_chain(tmp_path):
    control = tmp_path / "control"
    transition(control, run_id="run-1", next_state="prepared", reason="start")
    transition(control, run_id="run-1", next_state="staged", reason="verified")
    (control / "status.json").write_text("{broken", encoding="utf-8")

    status, recovered = recover_status(control)

    assert recovered is True
    assert status["state"] == "staged"
    assert status["sequence"] == 2
    assert read_status(control) == status


def test_corrupt_status_recovery_rejects_tampered_event_chain(tmp_path):
    control = tmp_path / "control"
    transition(control, run_id="run-1", next_state="prepared", reason="start")
    event = json.loads((control / "events.jsonl").read_text(encoding="utf-8"))
    event["sequence"] = 9
    (control / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    (control / "status.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(RRCError, match="status_recovery_event_invalid"):
        recover_status(control)
