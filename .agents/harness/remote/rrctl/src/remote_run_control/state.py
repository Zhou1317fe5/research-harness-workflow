"""Locked lifecycle transitions for the remote authoritative status."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import RRCError
from .jsonutil import append_jsonl, atomic_write_json, load_json, utc_now

SCHEMA_VERSION = "rrctl.status.v1"
TERMINAL_STATES = {"completed", "failed", "aborted"}
TRANSITIONS = {
    None: {"prepared"},
    "prepared": {"staged", "failed", "aborted"},
    "staged": {"launched", "failed", "aborted"},
    "launched": {"first_step_passed", "failed", "aborted"},
    "first_step_passed": {"running", "failed", "aborted"},
    # Direct completed remains readable for v0.1 recovery; new workers use
    # workload_complete so controller-side cleanup is verified first.
    "running": {"workload_complete", "completed", "failed", "aborted"},
    "workload_complete": {"completed", "failed", "aborted"},
    "completed": set(),
    "failed": set(),
    "aborted": set(),
}


@contextmanager
def control_lock(control_root: Path) -> Iterator[None]:
    control_root.mkdir(parents=True, exist_ok=True)
    lock_path = control_root / ".state.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_status(control_root: Path) -> dict[str, Any]:
    path = control_root / "status.json"
    if not path.is_file():
        raise RRCError(
            code="status_missing",
            message=f"status file does not exist: {path}",
            phase="recovery",
        )
    try:
        value = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise RRCError(
            code="status_invalid",
            message=f"invalid status document: {path}",
            phase="recovery",
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or not isinstance(value.get("run_id"), str)
        or value.get("state") not in TRANSITIONS
        or not isinstance(value.get("sequence"), int)
        or value.get("sequence", 0) < 1
    ):
        raise RRCError(
            code="status_invalid",
            message=f"invalid status document: {path}",
            phase="recovery",
        )
    return value


def _status_from_events(control_root: Path) -> dict[str, Any]:
    path = control_root / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RRCError(
            "status_recovery_events", f"cannot read recovery event log: {path}", "recovery"
        ) from exc
    if not lines:
        raise RRCError("status_recovery_empty", "recovery event log is empty", "recovery")

    previous_state: str | None = None
    run_id: str | None = None
    latest: dict[str, Any] | None = None
    for expected_sequence, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RRCError(
                "status_recovery_event_invalid",
                f"event {expected_sequence} is not valid JSON",
                "recovery",
            ) from exc
        event_run_id = event.get("run_id") if isinstance(event, dict) else None
        state = event.get("state") if isinstance(event, dict) else None
        if (
            not isinstance(event, dict)
            or event.get("schema_version") != "rrctl.event.v1"
            or not isinstance(event_run_id, str)
            or not event_run_id
            or event.get("sequence") != expected_sequence
            or event.get("previous_state") != previous_state
            or state not in TRANSITIONS.get(previous_state, set())
            or not isinstance(event.get("at"), str)
            or not isinstance(event.get("reason"), str)
            or not isinstance(event.get("detail"), dict)
        ):
            raise RRCError(
                "status_recovery_event_invalid",
                f"event {expected_sequence} breaks the lifecycle event chain",
                "recovery",
            )
        if run_id is None:
            run_id = event_run_id
        elif event_run_id != run_id:
            raise RRCError(
                "status_recovery_run_mismatch",
                f"event {expected_sequence} belongs to a different run",
                "recovery",
            )
        previous_state = state
        latest = event

    assert latest is not None and run_id is not None
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "state": latest["state"],
        "sequence": latest["sequence"],
        "updated_at": latest["at"],
        "reason": latest["reason"],
        "detail": latest["detail"],
    }


def recover_status(control_root: Path) -> tuple[dict[str, Any], bool]:
    try:
        return read_status(control_root), False
    except RRCError as exc:
        if exc.code not in {"status_missing", "status_invalid"}:
            raise
    with control_lock(control_root):
        try:
            return read_status(control_root), False
        except RRCError as exc:
            if exc.code not in {"status_missing", "status_invalid"}:
                raise
        recovered = _status_from_events(control_root)
        atomic_write_json(control_root / "status.json", recovered)
        return recovered, True


def _transition_locked(
    control_root: Path,
    *,
    run_id: str,
    next_state: str,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if next_state not in TRANSITIONS:
        raise RRCError(
            code="state_unknown",
            message=f"unknown lifecycle state: {next_state}",
            phase="state",
        )
    status_path = control_root / "status.json"
    previous = load_json(status_path) if status_path.is_file() else None
    previous_state = previous.get("state") if isinstance(previous, dict) else None
    if next_state not in TRANSITIONS.get(previous_state, set()):
        raise RRCError(
            code="state_transition_invalid",
            message=f"cannot transition from {previous_state!r} to {next_state!r}",
            phase="state",
            details={"previous": previous_state, "next": next_state},
        )
    if previous is not None and previous.get("run_id") != run_id:
        raise RRCError(
            code="state_run_mismatch",
            message="status belongs to a different run",
            phase="state",
        )
    now = utc_now()
    sequence = int(previous.get("sequence", 0)) + 1 if previous else 1
    status = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "state": next_state,
        "sequence": sequence,
        "updated_at": now,
        "reason": reason,
        "detail": detail or {},
    }
    event = {
        "schema_version": "rrctl.event.v1",
        "run_id": run_id,
        "sequence": sequence,
        "at": now,
        "previous_state": previous_state,
        "state": next_state,
        "reason": reason,
        "detail": detail or {},
    }
    atomic_write_json(status_path, status)
    append_jsonl(control_root / "events.jsonl", event)
    return status


def transition(
    control_root: Path,
    *,
    run_id: str,
    next_state: str,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with control_lock(control_root):
        return _transition_locked(
            control_root,
            run_id=run_id,
            next_state=next_state,
            reason=reason,
            detail=detail,
        )


def mark_first_step_passed(
    control_root: Path, *, run_id: str, health: dict[str, Any]
) -> dict[str, Any]:
    """在同一把锁内记录首步通过与运行态，避免短任务结束时插入转换。"""
    with control_lock(control_root):
        status = read_status(control_root)
        if status["run_id"] != run_id:
            raise RRCError("state_run_mismatch", "status belongs to a different run", "state")
        if status["state"] not in {"launched", "first_step_passed"}:
            return status
        if status["state"] == "launched":
            _transition_locked(
                control_root,
                run_id=run_id,
                next_state="first_step_passed",
                reason="first_step_health_passed",
                detail={"health": health},
            )
        return _transition_locked(
            control_root,
            run_id=run_id,
            next_state="running",
            reason="periodic_monitoring_enabled",
        )


def mark_launched(control_root: Path, *, run_id: str) -> dict[str, Any]:
    """启动器与已分离 worker 均可确认启动，SSH 中断不会留下孤立的 staged 状态。"""
    with control_lock(control_root):
        status = read_status(control_root)
        if status["run_id"] != run_id:
            raise RRCError("state_run_mismatch", "status belongs to a different run", "state")
        if status["state"] != "staged":
            return status
        return _transition_locked(
            control_root, run_id=run_id, next_state="launched", reason="process_worker_started",
        )


def update_status_detail(
    control_root: Path,
    *,
    run_id: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    with control_lock(control_root):
        status = read_status(control_root)
        if status.get("run_id") != run_id:
            raise RRCError("state_run_mismatch", "status belongs to a different run", "state")
        detail = dict(status.get("detail") or {})
        detail.update(values)
        status["detail"] = detail
        status["updated_at"] = utc_now()
        atomic_write_json(control_root / "status.json", status)
        return status
