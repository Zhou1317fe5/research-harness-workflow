"""单一 worker 的监控调度、健康缓存和只读有界观察协议。"""

from __future__ import annotations

import math
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import RRCError
from .finalization import read_completion, run_identity
from .health import HealthResult, persist_health, sample_health
from .jsonutil import append_jsonl, atomic_write_json, load_json, sha256_json, utc_now
from .models import RunSpec
from .processes import probe_bound_process
from .state import TERMINAL_STATES, read_status

PROTOCOL = "rrctl.monitor.v1"
HEARTBEAT_SECONDS = 30.0
OBSERVE_MAX_SECONDS = 120.0
CHECKER_FAILURE_LIMIT = 3
PHASES = ("first_step", "periodic", "completion")


def declaration(spec: RunSpec) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "owner": "worker",
        "policy": {
            "health": asdict(spec.health),
            "heartbeat_seconds": HEARTBEAT_SECONDS,
            "heartbeat_stale_seconds": 3 * HEARTBEAT_SECONDS
            + 10
            + max(getattr(spec.health, phase).adapter_timeout_seconds for phase in PHASES),
            "checker_failure_limit": CHECKER_FAILURE_LIMIT,
        },
    }


def protocol(binding: dict[str, Any]) -> str | None:
    if not isinstance(binding, dict):
        raise RRCError("monitor_binding_invalid", "run binding must be an object", "observer")
    value = binding.get("monitoring")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("protocol") != PROTOCOL
        or value.get("owner") != "worker"
    ):
        raise RRCError("monitor_protocol", "unsupported monitoring declaration", "observer")
    return PROTOCOL


class Monitor:
    """调用者持有整个 execute 的 monitor 锁；任何读取者都不创建本对象。"""

    def __init__(
        self,
        spec: RunSpec,
        control_root: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.spec = spec
        self.root = control_root
        self.clock = clock
        self.first_deadline = clock() + spec.health.first_step.timeout_seconds
        self.tracking: dict[str, Any] = {}
        self.pending: dict[str, int] = {}
        binding = load_json(control_root / "binding.json")
        self.data: dict[str, Any] = {
            "schema_version": PROTOCOL,
            "run_id": spec.run_id,
            "run_identity": run_identity(spec, binding),
            "policy": declaration(spec)["policy"],
            "owner": {
                key: binding.get(key)
                for key in (
                    "boot_id",
                    "executor_pid",
                    "executor_start_ticks",
                    "executor_session_id",
                )
            },
            "monitor_status": "starting",
            "phase": "first_step",
            "last_check_at": None,
            "last_check_epoch": None,
            "event_sequence": 0,
            "active_alerts": {},
            "checks": dict.fromkeys(PHASES, 0),
            "adapter_calls": dict.fromkeys(PHASES, 0),
        }
        self.heartbeat()

    def heartbeat(self) -> None:
        self.data["heartbeat_at"] = utc_now()
        self.data["heartbeat_epoch"] = time.time()
        atomic_write_json(self.root / "monitor.json", self.data)

    def _event(self, kind: str, key: str, issue: dict[str, Any], phase: str) -> dict[str, Any]:
        self.data["event_sequence"] += 1
        event = {
            "schema_version": "rrctl.monitor-event.v1",
            "run_id": self.spec.run_id,
            "run_identity": self.data["run_identity"],
            "sequence": self.data["event_sequence"],
            "at": utc_now(),
            "kind": kind,
            "fingerprint": key,
            "phase": phase,
            "code": issue["code"],
            "subject": issue.get("subject", ""),
            "required": True,
        }
        append_jsonl(self.root / "monitor-events.jsonl", event)
        return {**event, "count": 1, "last_seen_at": event["at"]}

    def _alerts(self, phase: str, issues: list[dict[str, Any]]) -> None:
        seen = set()
        active = self.data["active_alerts"]
        for issue in issues:
            if not issue["required"] and not issue["retryable"]:
                continue
            key = sha256_json(
                [self.data["run_identity"], phase, issue["code"], issue.get("subject", "")]
            )
            seen.add(key)
            self.pending[key] = self.pending.get(key, 0) + 1
            if issue["retryable"] and self.pending[key] < CHECKER_FAILURE_LIMIT:
                continue
            if key not in active:
                active[key] = self._event("opened", key, issue, phase)
            else:
                active[key]["count"] += 1
                active[key]["last_seen_at"] = utc_now()
        for key, issue in list(active.items()):
            if issue["phase"] == phase and key not in seen:
                self._event("recovered", key, issue, phase)
                del active[key]
        # 只保留本轮仍出现的重试计数，短暂故障不会跨健康样本累积。
        for key in list(self.pending):
            if key not in seen and (key not in active or active[key]["phase"] == phase):
                del self.pending[key]

    def check(self, phase: str, *, process_required: bool = True) -> HealthResult:
        self.data["phase"] = phase
        self.heartbeat()
        try:
            result = sample_health(
                self.spec,
                self.root,
                phase=phase,
                process_required=process_required,
                tracking=self.tracking,
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, RRCError) else "checker_exception"
            result = HealthResult(
                healthy=False,
                complete=False,
                phase=phase,
                observations={"checker_error": code},
                errors=("health sampling is unavailable",),
                status="unavailable",
                issues=({"code": code, "subject": "", "required": True, "retryable": True},),
                tracking=self.tracking,
            )
        self.tracking = result.tracking
        persist_health(self.spec, self.root, result)
        self.data["checks"][phase] += 1
        if getattr(self.spec.health, phase).adapter_argv:
            self.data["adapter_calls"][phase] += 1
        at = utc_now()
        now = time.time()
        atomic_write_json(
            self.root / "health-latest" / f"{phase}.json",
            {
                "schema_version": "rrctl.health-cache.v1",
                "run_id": self.spec.run_id,
                "run_identity": self.data["run_identity"],
                "at": at,
                "observed_epoch": now,
                "check_sequence": self.data["checks"][phase],
                "result": result.to_dict(),
            },
        )
        self.data["last_check_at"] = at
        self.data["last_check_epoch"] = now
        issues = list(result.issues)
        if phase == "first_step" and not result.gate_passed and self.clock() >= self.first_deadline:
            issues.append(
                {
                    "code": "first_step_timeout",
                    "subject": "",
                    "required": True,
                    "retryable": False,
                }
            )
        self._alerts(phase, issues)
        self.heartbeat()
        return result

    def supervise(self, process: subprocess.Popen) -> int:
        self.data["monitor_status"] = "running"
        next_check = self.clock()
        next_heartbeat = self.clock()
        phase = "first_step"
        while process.poll() is None:
            now = self.clock()
            if now >= next_check:
                report = self.check(phase)
                if phase == "first_step" and report.gate_passed:
                    phase = "periodic"
                    self.data["phase"] = phase
                    self.heartbeat()
                next_check = self.clock() + getattr(self.spec.health, phase).poll_interval_seconds
                next_heartbeat = self.clock() + HEARTBEAT_SECONDS
            if self.clock() >= next_heartbeat:
                self.heartbeat()
                next_heartbeat = self.clock() + HEARTBEAT_SECONDS
            try:
                return process.wait(
                    timeout=max(0.001, min(next_check, next_heartbeat) - self.clock())
                )
            except subprocess.TimeoutExpired:
                pass
        return process.wait()

    def finish(
        self, status: dict[str, Any], *, error_code: str = "finalization_unavailable"
    ) -> None:
        self.data["monitor_status"] = (
            "finished" if status["state"] in TERMINAL_STATES else "attention"
        )
        if status["state"] not in TERMINAL_STATES:
            self._alerts(
                self.data["phase"],
                [
                    {
                        "code": error_code,
                        "subject": "",
                        "required": True,
                        "retryable": False,
                    }
                ],
            )
        self.heartbeat()


def _cache(spec: RunSpec, root: Path, binding: dict[str, Any], phase: str) -> dict[str, Any] | None:
    path = root / "health-latest" / f"{phase}.json"
    if not path.exists():
        return None
    value = load_json(path)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "rrctl.health-cache.v1"
        or value.get("run_identity") != run_identity(spec, binding)
        or value.get("run_id") != spec.run_id
        or not isinstance(value.get("observed_epoch"), int | float)
        or not math.isfinite(value["observed_epoch"])
        or not isinstance(value.get("result"), dict)
        or value["result"].get("phase") != phase
        or value["result"].get("schema_version") != "rrctl.health.v2"
        or value["result"].get("status") not in {"healthy", "degraded", "unhealthy", "unavailable"}
        or any(
            not isinstance(value["result"].get(key), bool)
            for key in ("healthy", "complete", "gate_passed")
        )
        or not isinstance(value.get("at"), str)
        or not isinstance(value.get("check_sequence"), int)
        or value["check_sequence"] < 1
        or value["result"]["healthy"] != (value["result"]["status"] in {"healthy", "degraded"})
        or (value["result"]["complete"] and not value["result"]["healthy"])
    ):
        raise ValueError("invalid health cache")
    return value


def read_observation(control_root: Path, *, spec: RunSpec | None = None) -> dict[str, Any]:
    spec = spec or RunSpec.from_path(control_root / "run_spec.json")
    binding = load_json(control_root / "binding.json")
    if protocol(binding) is None:
        raise RRCError(
            "monitor_unsupported", "run uses client compatibility monitoring", "observer"
        )
    # JSON 元组序列化为数组；比较规范摘要避免容器类型引起误判。
    if sha256_json(binding["monitoring"]) != sha256_json(declaration(spec)):
        raise RRCError("monitor_policy_mismatch", "monitor policy differs from RunSpec", "observer")
    status = read_status(control_root)
    if status["run_id"] != spec.run_id or binding.get("run_spec_sha256") != spec.digest:
        raise RRCError("monitor_identity_mismatch", "monitor run identity differs", "observer")
    result: dict[str, Any] = {
        "run_id": spec.run_id,
        "status": status,
        "binding": binding,
        "control_root": str(control_root),
        "status_recovered": False,
        "monitoring_mode": "worker",
        "observation": "no_event",
    }
    identity = run_identity(spec, binding)
    state = status["state"]
    now = time.time()
    observations = {
        prefix + "_alive": probe_bound_process(binding, prefix, spec.run_id, control_root)["state"]
        == "alive"
        for prefix in ("executor", "workload")
    }
    observations["process_alive"] = observations["executor_alive"] or observations["workload_alive"]
    summary: dict[str, Any] = {
        "protocol": PROTOCOL,
        "run_identity": identity,
        "monitor_status": "unavailable",
        "health_status": "unavailable",
        "active_alerts": [],
        "event_cursor": identity + ":0",
        "observations": observations,
        "evidence": {
            name: str(control_root / name)
            for name in (
                "monitor.json",
                "monitor-events.jsonl",
                "health-latest",
                "completion.json",
            )
        },
    }
    result["monitor"] = summary
    try:
        data = load_json(control_root / "monitor.json")
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != PROTOCOL
            or data.get("run_identity") != identity
            or data.get("run_id") != spec.run_id
            or not isinstance(data.get("heartbeat_epoch"), int | float)
            or not math.isfinite(data["heartbeat_epoch"])
            or not isinstance(data.get("event_sequence"), int)
            or data["event_sequence"] < 0
            or not isinstance(data.get("active_alerts"), dict)
            or data.get("phase") not in PHASES
            or data.get("monitor_status")
            not in {"starting", "running", "finalizing", "finished", "attention"}
            or data.get("owner")
            != {
                key: binding.get(key)
                for key in (
                    "boot_id",
                    "executor_pid",
                    "executor_start_ticks",
                    "executor_session_id",
                )
            }
            or sha256_json(data.get("policy")) != sha256_json(declaration(spec)["policy"])
            or any(
                not isinstance(data.get(key), dict)
                or any(
                    not isinstance(data[key].get(phase), int) or data[key][phase] < 0
                    for phase in PHASES
                )
                for key in ("checks", "adapter_calls")
            )
        ):
            raise ValueError("invalid monitor snapshot")
        for key, event in data["active_alerts"].items():
            if (
                not isinstance(event, dict)
                or event.get("fingerprint") != key
                or event.get("run_identity") != identity
                or event.get("phase") not in PHASES
                or not isinstance(event.get("sequence"), int)
                or not 1 <= event["sequence"] <= data["event_sequence"]
                or event.get("required") is not True
            ):
                raise ValueError("invalid monitor alert")
        age = now - data["heartbeat_epoch"]
        limit = declaration(spec)["policy"]["heartbeat_stale_seconds"]
        monitor_status = data["monitor_status"]
        if state not in TERMINAL_STATES and monitor_status == "finished":
            raise ValueError("monitor finished without a terminal run")
        if state not in TERMINAL_STATES and monitor_status != "attention":
            if not observations["executor_alive"]:
                monitor_status = "lost"
            elif age > limit or age < -5:
                monitor_status = "stale"
        summary.update(
            {
                "monitor_status": monitor_status,
                "heartbeat_age_seconds": max(0, age),
                "last_check_at": data.get("last_check_at"),
                "checks": data["checks"],
                "adapter_calls": data["adapter_calls"],
                "event_sequence": data["event_sequence"],
                "event_cursor": f"{identity}:{data['event_sequence']}",
                "active_alerts": list(data["active_alerts"].values()),
            }
        )
        cached = _cache(spec, control_root, binding, data["phase"])
        if cached is None and data["phase"] == "periodic":
            cached = _cache(spec, control_root, binding, "first_step")
        if cached:
            policy = getattr(spec.health, data["phase"])
            cache_age = now - cached["observed_epoch"]
            if state not in TERMINAL_STATES and (
                cache_age > policy.poll_interval_seconds + limit or cache_age < -5
            ):
                summary["monitor_status"] = "stale"
            summary["health_status"] = cached["result"]["status"]
        else:
            summary["health_status"] = "starting"
        first = _cache(spec, control_root, binding, "first_step")
        if first:
            result["first_step"] = {
                "status": first["result"]["status"],
                "gate_passed": first["result"].get("gate_passed") is True,
                "observed_at": first["at"],
            }
        if state == "completed":
            read_completion(spec, control_root, binding)
            summary["monitor_status"] = "finished"
            summary["health_status"] = "healthy"
    except FileNotFoundError:
        try:
            started = datetime.fromisoformat(binding["created_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            started = float("-inf")
        if (
            binding.get("executor_pid")
            and not observations["executor_alive"]
            and state not in TERMINAL_STATES
        ):
            summary["monitor_status"] = "lost"
        elif (
            state not in TERMINAL_STATES
            and now - started < declaration(spec)["policy"]["heartbeat_stale_seconds"]
        ):
            summary["monitor_status"] = "starting"
        else:
            summary["monitor_status"] = "unavailable"
    except (OSError, ValueError, KeyError, TypeError, RRCError):
        summary["monitor_status"] = "unavailable"
    if summary["monitor_status"] in {"unavailable", "stale", "lost", "attention"}:
        summary["last_health_status"] = summary["health_status"]
        summary["health_status"] = "unavailable"
        result["observation"] = "unavailable"
    elif state in TERMINAL_STATES:
        result["observation"] = "terminal"
    return result


def read_health(control_root: Path, phase: str) -> dict[str, Any]:
    if phase not in PHASES:
        raise RRCError("health_phase", "unknown health phase", "health")
    spec = RunSpec.from_path(control_root / "run_spec.json")
    observed = read_observation(control_root, spec=spec)
    monitor = observed["monitor"]
    result: dict[str, Any] = {
        "schema_version": "rrctl.health.v2",
        "phase": phase,
        "status": "starting",
        "healthy": False,
        "complete": False,
        "gate_passed": False,
        "errors": [],
    }
    try:
        cached = _cache(spec, control_root, observed["binding"], phase)
        if cached:
            result.update(cached["result"])
            result["observed_at"] = cached["at"]
            immutable_first_pass = (
                phase == "first_step" and cached["result"].get("gate_passed") is True
            )
            if observed["status"]["state"] not in TERMINAL_STATES and not immutable_first_pass:
                policy = getattr(spec.health, phase)
                if (
                    time.time() - cached["observed_epoch"]
                    > policy.poll_interval_seconds
                    + declaration(spec)["policy"]["heartbeat_stale_seconds"]
                ):
                    monitor = {**monitor, "monitor_status": "stale"}
    except (OSError, ValueError, TypeError, KeyError):
        monitor = {**monitor, "monitor_status": "unavailable"}
    if monitor["monitor_status"] in {"unavailable", "stale", "lost", "attention"}:
        result.update(status="unavailable", healthy=False, complete=False, gate_passed=False)
    if observed["status"]["state"] in {"failed", "aborted"}:
        result.update(status="unhealthy", healthy=False, complete=False, gate_passed=False)
        result["errors"] = [
            *result.get("errors", []),
            f"run is terminal: {observed['status']['state']}",
        ]
    result.update(
        {
            "run_state": observed["status"]["state"],
            "monitor_status": monitor["monitor_status"],
            "health_status": result["status"],
            "cached": True,
            "observations": {**result.get("observations", {}), **monitor["observations"]},
        }
    )
    return result


def observe(
    control_root: Path,
    *,
    timeout_seconds: float,
    after_event: str | None = None,
    until: str = "event",
) -> dict[str, Any]:
    if not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= OBSERVE_MAX_SECONDS:
        raise RRCError(
            "observe_budget", "observe timeout must be between 0 and 120 seconds", "observer"
        )
    spec = RunSpec.from_path(control_root / "run_spec.json")
    deadline = time.monotonic() + timeout_seconds
    while True:
        value = read_observation(control_root, spec=spec)
        monitor = value["monitor"]
        after = 0
        if after_event is not None:
            matched = re.fullmatch(r"([0-9a-f]{64}):(0|[1-9][0-9]*)", after_event)
            if not matched or matched[1] != monitor["run_identity"]:
                raise RRCError(
                    "event_cursor_identity", "event cursor does not belong to this run", "observer"
                )
            after = int(matched[2])
            if after > monitor.get("event_sequence", 0):
                raise RRCError(
                    "event_cursor_future", "event cursor exceeds published events", "observer"
                )
        if value["observation"] != "no_event":
            return value
        if until == "first_step" and value.get("first_step", {}).get("gate_passed") is True:
            value["observation"] = "first_step"
            return value
        alerts = [
            event
            for event in monitor["active_alerts"]
            if event["required"] and event["sequence"] > after
        ]
        if alerts:
            value["observation"] = "attention"
            return value
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return value
        time.sleep(min(0.5, remaining))
