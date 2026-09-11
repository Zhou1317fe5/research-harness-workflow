"""Local rrctl orchestration and recovery index."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import load_artifact_manifest
from .errors import RRCError
from .jsonutil import atomic_write_json, load_json, sha256_file, sha256_json
from .models import RunSpec
from .monitor import OBSERVE_MAX_SECONDS
from .monitor import protocol as monitoring_protocol
from .profiles import DEFAULT_PROFILE_PATH, ProfileStore
from .readiness import ReadinessResult, validate_run_spec
from .security import redact, redact_data
from .source_identity import source_content_sha256
from .transport import CommandResult, Transport, transport_for
from .zipapp_builder import build_worker_zipapp

DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "rrctl" / "runs"


@dataclass(frozen=True, slots=True)
class RunRef:
    run_id: str
    profile: str
    stage_root: str
    control_root: str
    worker_path: str
    remote_python: str
    run_spec_sha256: str
    source_content_sha256: str = ""
    transport_bundle_sha256: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RunRef:
        required = {
            key: value[key]
            for key in (
                "run_id",
                "profile",
                "stage_root",
                "control_root",
                "worker_path",
                "remote_python",
                "run_spec_sha256",
            )
        }
        return cls(
            **required,
            source_content_sha256=str(value.get("source_content_sha256", "")),
            transport_bundle_sha256=str(value.get("transport_bundle_sha256", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "profile": self.profile,
            "stage_root": self.stage_root,
            "control_root": self.control_root,
            "worker_path": self.worker_path,
            "remote_python": self.remote_python,
            "run_spec_sha256": self.run_spec_sha256,
            "source_content_sha256": self.source_content_sha256,
            "transport_bundle_sha256": self.transport_bundle_sha256,
        }


class RunIndex:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(os.environ.get("RRCTL_STATE_ROOT", DEFAULT_STATE_ROOT))

    def save(self, ref: RunRef, spec: RunSpec, status: dict[str, Any] | None = None) -> None:
        destination = self.root / ref.run_id
        destination.mkdir(parents=True, exist_ok=True)
        atomic_write_json(destination / "run_ref.json", ref.to_dict())
        atomic_write_json(destination / "run_spec.json", spec.to_dict())
        if status is not None:
            atomic_write_json(destination / "last_status.json", status)

    def load_ref(self, run_id: str) -> RunRef:
        path = self.root / run_id / "run_ref.json"
        if not path.is_file():
            raise RRCError(
                "run_ref_missing", f"local run index does not contain {run_id}", "recovery"
            )
        value = load_json(path)
        if not isinstance(value, dict):
            raise RRCError("run_ref_invalid", f"invalid local run reference: {path}", "recovery")
        return RunRef.from_dict(value)

    def load_spec(self, run_id: str) -> RunSpec:
        return RunSpec.from_path(self.root / run_id / "run_spec.json")


def _parse_worker_result(
    result: CommandResult, *, phase: str, secret_values: tuple[str, ...] = ()
) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RRCError(
            "worker_output",
            f"worker emitted invalid JSON during {phase}",
            phase,
            details={
                "stdout": redact(
                    result.stdout.decode("utf-8", errors="replace")[-4000:], secret_values
                ),
                "stderr": redact(
                    result.stderr.decode("utf-8", errors="replace")[-4000:], secret_values
                ),
            },
        ) from exc
    value = redact_data(value, secret_values)
    if result.returncode != 0 or (isinstance(value, dict) and value.get("ok") is False):
        raise RRCError(
            "worker_command",
            f"worker command failed during {phase}",
            phase,
            details={"returncode": result.returncode, "worker": value},
        )
    if not isinstance(value, dict):
        raise RRCError("worker_output", "worker output must be an object", phase)
    return value


class Controller:
    def __init__(
        self,
        *,
        profiles_path: Path | None = None,
        state_root: Path | None = None,
    ):
        self.profile_store = ProfileStore(profiles_path or DEFAULT_PROFILE_PATH)
        self.index = RunIndex(state_root)

    def ready(self, spec: RunSpec, *, offline: bool = False) -> dict[str, Any]:
        result = validate_run_spec(spec, profile_store=self.profile_store, load_profile=True)
        output = result.to_dict()
        if result.ready and not offline and result.profile:
            transport = transport_for(result.profile)
            output["remote_preflight"] = transport.preflight(
                python=spec.remote.python,
                environment=spec.environment,
            )
        elif offline:
            output["remote_preflight"] = "skipped"
        return output

    def _require_ready(self, spec: RunSpec) -> tuple[ReadinessResult, Transport]:
        result = validate_run_spec(spec, profile_store=self.profile_store, load_profile=True)
        if not result.ready or result.profile is None:
            raise RRCError(
                "readiness_failed",
                "RunSpec did not pass readiness",
                "readiness",
                details=result.to_dict(),
            )
        transport = transport_for(result.profile)
        transport.preflight(python=spec.remote.python, environment=spec.environment)
        return result, transport

    def _create_bundle(self, spec: RunSpec, destination: Path) -> str:
        repo = Path(spec.source.repo_root)
        temporary_ref = f"refs/heads/rrctl-{sha256_json(spec.run_id)[:24]}"
        try:
            subprocess.run(
                ["git", "-C", str(repo), "update-ref", temporary_ref, spec.source.commit],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "pack.threads=1",
                    "-C",
                    str(repo),
                    "bundle",
                    "create",
                    str(destination),
                    temporary_ref,
                ],
                check=True,
            )
            subprocess.run(
                ["git", "bundle", "verify", str(destination)], check=True, capture_output=True
            )
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "update-ref", "-d", temporary_ref],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        heads = subprocess.run(
            ["git", "bundle", "list-heads", str(destination)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if not any(line.split()[0] == spec.source.commit for line in heads if line.split()):
            raise RRCError(
                "bundle_commit", "source bundle does not contain reviewed commit", "staging"
            )
        actual_sha256 = sha256_file(destination)
        expected_transport_sha = spec.source.transport_bundle_sha256
        if expected_transport_sha and actual_sha256 != expected_transport_sha:
            raise RRCError(
                "bundle_sha_mismatch",
                "generated source bundle does not match the frozen RunSpec SHA",
                "staging",
                details={
                    "expected": expected_transport_sha,
                    "actual": actual_sha256,
                },
            )
        return actual_sha256

    def _stage(self, spec: RunSpec, readiness: ReadinessResult, transport: Transport) -> RunRef:
        with tempfile.TemporaryDirectory(prefix="rrctl-stage-") as temporary_name:
            temporary = Path(temporary_name)
            bundle = temporary / "source.bundle"
            worker = temporary / "rrctl-worker.pyz"
            spec_path = temporary / "run_spec.json"
            manifest_path = temporary / "stage_manifest.json"
            bundle_sha = self._create_bundle(spec, bundle)
            source_sha = source_content_sha256(Path(spec.source.repo_root), spec.source)
            if readiness.source_content_sha256 != source_sha:
                raise RRCError(
                    "source_content_changed",
                    "source content changed after readiness validation",
                    "staging",
                    details={
                        "readiness": readiness.source_content_sha256,
                        "staging": source_sha,
                    },
                )
            package_source = Path(__file__).resolve().parent.parent
            worker_sha = build_worker_zipapp(package_source, worker)
            atomic_write_json(spec_path, spec.to_dict())
            anchor_files: list[tuple[Path, str, str]] = []
            for anchor in spec.anchors:
                target = temporary / "anchors" / anchor.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(anchor.local_path).expanduser(), target)
                anchor_files.append(
                    (target, f"{spec.remote.stage_root}/anchors/{anchor.name}", anchor.sha256)
                )
            manifest = {
                "schema_version": "rrctl.stage.v1",
                "run_id": spec.run_id,
                "run_spec_sha256": spec.digest,
                "run_spec_file_sha256": sha256_file(spec_path),
                "source_content_sha256": source_sha,
                "transport_bundle_sha256": bundle_sha,
                "bundle_sha256": bundle_sha,
                "worker_sha256": worker_sha,
                "anchors": {item.name: item.sha256 for item in spec.anchors},
            }
            atomic_write_json(manifest_path, manifest)
            transport.mkdir_exclusive(spec.remote.stage_root)
            uploads = [
                (bundle, f"{spec.remote.stage_root}/source.bundle", bundle_sha),
                (worker, f"{spec.remote.stage_root}/rrctl-worker.pyz", worker_sha),
                (spec_path, f"{spec.remote.stage_root}/run_spec.json", sha256_file(spec_path)),
                (
                    manifest_path,
                    f"{spec.remote.stage_root}/stage_manifest.json",
                    sha256_file(manifest_path),
                ),
                *anchor_files,
            ]
            if anchor_files:
                mkdir = transport.run(["mkdir", "-m", "700", f"{spec.remote.stage_root}/anchors"])
                if mkdir.returncode != 0:
                    raise RRCError(
                        "anchor_stage", "failed to create anchor staging directory", "staging"
                    )
            for local_path, remote_path, expected_sha in uploads:
                transport.upload(local_path, remote_path, expected_sha)
        return RunRef(
            run_id=spec.run_id,
            profile=spec.remote.profile,
            stage_root=spec.remote.stage_root,
            control_root=spec.remote.control_root,
            worker_path=f"{spec.remote.stage_root}/rrctl-worker.pyz",
            remote_python=spec.remote.python,
            run_spec_sha256=readiness.run_spec_sha256,
            source_content_sha256=source_sha,
            transport_bundle_sha256=bundle_sha,
        )

    def launch(self, spec: RunSpec, *, max_wait_seconds: float = 900) -> dict[str, Any]:
        if not math.isfinite(max_wait_seconds) or max_wait_seconds < 0:
            raise RRCError(
                "launch_budget", "max-wait must be nonnegative finite seconds", "observer"
            )
        readiness, transport = self._require_ready(spec)
        ref = self._stage(spec, readiness, transport)
        # 启动失败也保留恢复定位，允许通过同一控制面拉取诊断。
        self.index.save(ref, spec)
        result = transport.run(
            [
                ref.remote_python,
                ref.worker_path,
                "launch",
                "--stage",
                ref.stage_root,
            ]
        )
        launched = _parse_worker_result(
            result, phase="launch", secret_values=transport.secret_values
        )
        self.index.save(ref, spec, launched)
        try:
            if monitoring_protocol({"monitoring": launched.get("monitoring")}):
                budget = (
                    min(spec.health.first_step.timeout_seconds, max_wait_seconds)
                    if max_wait_seconds
                    else spec.health.first_step.timeout_seconds
                )
                observed = self._observe_remote(
                    spec.run_id,
                    deadline=time.monotonic() + budget,
                    interval=spec.health.first_step.poll_interval_seconds,
                    until="first_step",
                )
                state = observed.get("status", {}).get("state")
                if state in {"failed", "aborted"}:
                    raise RRCError(
                        "first_step_terminal", f"run entered {state}", "health", details=observed
                    )
                if observed["observation"] == "timeout":
                    raise RRCError(
                        "first_step_observer_timeout",
                        "first-step observation expired; workload preserved",
                        "observer",
                        details={"last": observed, "remote_workload_preserved": True},
                    )
                if observed["observation"] == "attention":
                    raise RRCError(
                        "first_step_attention",
                        "first-step monitoring needs attention",
                        "observer",
                        details=observed,
                    )
                return {
                    "launch": launched,
                    "first_step": observed.get("first_step", "completed_fast"),
                    "inspect": observed,
                }
            budget = (
                min(spec.health.first_step.timeout_seconds, max_wait_seconds)
                if max_wait_seconds
                else spec.health.first_step.timeout_seconds
            )
            deadline = time.monotonic() + budget
            last: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                inspected = self.inspect(spec.run_id)
                state = inspected["status"]["state"]
                if state == "workload_complete":
                    terminal = self._finalize_completed_workload(spec.run_id)
                    return {
                        "launch": launched,
                        "first_step": "completed_fast",
                        "inspect": terminal,
                    }
                if state == "completed":
                    return {
                        "launch": launched,
                        "first_step": "completed_fast",
                        "inspect": inspected,
                    }
                if state in {"failed", "aborted"}:
                    raise RRCError(
                        "first_step_terminal",
                        f"run entered terminal state before first-step pass: {state}",
                        "health",
                        details=inspected,
                    )
                last = self.health(spec.run_id, phase="first_step")
                if self._health_status(last) != "unhealthy" and last.get("gate_passed") is True:
                    return {"launch": launched, "first_step": last}
                time.sleep(spec.health.first_step.poll_interval_seconds)
            raise RRCError(
                "first_step_observer_timeout",
                "first-step observation ended without an authoritative pass; "
                "remote workload was preserved",
                "observer",
                details={
                    "last": last,
                    "run_ref": ref.to_dict(),
                    "remote_workload_preserved": True,
                },
            )
        except BaseException as exc:
            self._raise_observer_error(spec.run_id, exc)

    def _runtime(self, run_id: str) -> tuple[RunRef, RunSpec, Transport]:
        ref = self.index.load_ref(run_id)
        spec = self.index.load_spec(run_id)
        profile = self.profile_store.load(ref.profile)
        return ref, spec, transport_for(profile)

    def inspect(self, run_id: str, *, timeout_seconds: float = 180) -> dict[str, Any]:
        ref, spec, transport = self._runtime(run_id)
        result = transport.run(
            [ref.remote_python, ref.worker_path, "inspect", "--control", ref.control_root],
            timeout_seconds=timeout_seconds,
        )
        value = _parse_worker_result(result, phase="inspect", secret_values=transport.secret_values)
        self.index.save(ref, spec, value.get("status"))
        return value

    def health(self, run_id: str, *, phase: str) -> dict[str, Any]:
        ref, spec, transport = self._runtime(run_id)
        if spec.session.backend != "process":
            raise RRCError(
                "legacy_backend_read_only", "use inspect/pull for frozen legacy runs", "observer"
            )
        result = transport.run(
            [
                ref.remote_python,
                ref.worker_path,
                "health",
                "--control",
                ref.control_root,
                "--phase",
                phase,
            ]
        )
        return _parse_worker_result(result, phase="health", secret_values=transport.secret_values)

    @staticmethod
    def _health_status(value: dict[str, Any]) -> str:
        status = value.get("status")
        if status in {"healthy", "degraded", "unhealthy"}:
            return status
        return "healthy" if value.get("healthy") else "unhealthy"

    @staticmethod
    def _raise_observer_error(run_id: str, exc: BaseException) -> None:
        if isinstance(exc, RRCError):
            if exc.code == "transport_timeout":
                exc.details.update({"run_id": run_id, "remote_workload_preserved": True})
            raise exc
        raise RRCError(
            "observer_detached",
            "local observer detached; remote workload ownership was not changed",
            "observer",
            details={"run_id": run_id, "remote_workload_preserved": True},
        ) from exc

    def _worker_lifecycle_command(
        self, run_id: str, argv: list[str], *, phase: str
    ) -> dict[str, Any]:
        ref, _, transport = self._runtime(run_id)
        result = transport.run(
            [ref.remote_python, ref.worker_path, *argv, "--control", ref.control_root]
        )
        return _parse_worker_result(result, phase=phase, secret_values=transport.secret_values)

    def _fail_completed_workload(self, run_id: str, reason: str) -> dict[str, Any]:
        ref, _, transport = self._runtime(run_id)
        inspected: dict[str, Any] | None = None
        with suppress(RRCError):
            inspected = self.inspect(run_id)
        state = inspected.get("status", {}).get("state") if isinstance(inspected, dict) else None
        if state in {"failed", "aborted", "completed"}:
            return inspected or {"state": state}
        if state != "workload_complete":
            raise RRCError(
                "completion_state_unknown",
                "completion failure cannot mutate an active or unobserved run",
                "observer",
                details={"remote_workload_preserved": True, "run_id": run_id},
            )
        command = [
            ref.remote_python,
            ref.worker_path,
            "fail-completed",
            "--control",
            ref.control_root,
            "--reason",
            reason,
        ]
        result = transport.run(command)
        return _parse_worker_result(
            result, phase="monitor_cleanup", secret_values=transport.secret_values
        )

    def _finalize_completed_workload(self, run_id: str) -> dict[str, Any]:
        _, spec, _ = self._runtime(run_id)
        deadline = time.monotonic() + spec.health.completion.timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.health(run_id, phase="completion")
            status = self._health_status(last)
            if status != "unhealthy" and last.get("complete"):
                self._worker_lifecycle_command(run_id, ["complete"], phase="complete")
                return self.inspect(run_id)
            if status == "unhealthy":
                self._fail_completed_workload(run_id, "authoritative_completion_unhealthy")
                raise RRCError(
                    "completion_health",
                    "remote authoritative completion health failed",
                    "health",
                    details=last,
                )
            time.sleep(spec.health.completion.poll_interval_seconds)
        raise RRCError(
            "completion_observer_timeout",
            "completion observation ended without an authoritative result; "
            "remote state was preserved",
            "observer",
            details={"last": last, "remote_state_preserved": True},
        )

    @staticmethod
    def _timeout(run_id: str, last: dict[str, Any] | None) -> dict[str, Any]:
        return {
            **(last or {"run_id": run_id}),
            "observation": "timeout",
            "remote_workload_preserved": True,
            "resume_argv": ["rrctl", "wait", run_id],
        }

    def _observe_remote(
        self,
        run_id: str,
        *,
        deadline: float | None,
        interval: float,
        until: str = "event",
        after_event: str | None = None,
    ) -> dict[str, Any]:
        ref, spec, transport = self._runtime(run_id)
        last = None
        failures = 0
        requests = 0
        while True:
            remaining = deadline - time.monotonic() if deadline is not None else 180.0
            if remaining <= 0:
                value = self._timeout(run_id, last)
                value["observer_requests"] = requests
                return value
            transport_budget = min(180.0, remaining)
            margin = min(10.0, transport_budget / 4)
            remote_budget = min(OBSERVE_MAX_SECONDS, interval, transport_budget - margin)
            argv = [
                ref.remote_python,
                ref.worker_path,
                "observe",
                "--control",
                ref.control_root,
                "--timeout-seconds",
                str(remote_budget),
                "--until",
                until,
            ]
            if after_event is not None:
                argv += ["--after-event", after_event]
            try:
                requests += 1
                result = transport.run(argv, timeout_seconds=transport_budget)
                if result.returncode == 255:
                    raise RRCError(
                        "transport_connection", "observation connection failed", "observer"
                    )
                last = _parse_worker_result(
                    result, phase="observe", secret_values=transport.secret_values
                )
            except RRCError as exc:
                if exc.code not in {"transport_timeout", "transport_connection"}:
                    raise
                failures += 1
                if deadline is not None and time.monotonic() >= deadline:
                    value = self._timeout(run_id, last)
                    value["remote_state_unknown"] = True
                    return value
                if failures >= 3:
                    raise RRCError(
                        "observation_unavailable",
                        "bounded observation retries exhausted",
                        "observer",
                        details={"run_id": run_id, "remote_workload_preserved": True},
                    ) from exc
                delay = min(2 ** (failures - 1), 5)
                if deadline is not None:
                    delay = min(delay, max(0, deadline - time.monotonic()))
                time.sleep(delay)
                continue
            failures = 0
            if monitoring_protocol(last.get("binding", {})) is None:
                raise RRCError(
                    "monitor_protocol", "observe response lost its capability", "observer"
                )
            if (
                last.get("run_id") != run_id
                or last["binding"].get("run_spec_sha256") != spec.digest
                or not isinstance(last.get("status"), dict)
            ):
                raise RRCError(
                    "observe_identity", "observation belongs to a different run", "observer"
                )
            if last.get("observation") not in {
                "no_event",
                "terminal",
                "attention",
                "unavailable",
                "first_step",
            }:
                raise RRCError("observe_response", "invalid observation response", "observer")
            if last.get("status", {}).get("state") in {"failed", "aborted"}:
                last["observation"] = "terminal"
            if (
                last["observation"] == "terminal"
                and last["status"].get("state") not in {"completed", "failed", "aborted"}
            ) or (last["observation"] == "first_step" and until != "first_step"):
                raise RRCError(
                    "observe_response", "observation is not an authoritative result", "observer"
                )
            if last["observation"] == "unavailable":
                raise RRCError(
                    "monitor_unavailable",
                    "monitor snapshot is stale, lost or unavailable",
                    "observer",
                    details={"snapshot": last, "remote_workload_preserved": True},
                )
            if last["observation"] != "no_event":
                self.index.save(ref, spec, last.get("status"))
                last["observer_requests"] = requests
                return last
            # 无事件响应只在当前客户端内续等，不向 CLI 或模型输出。

    def wait(
        self,
        run_id: str,
        *,
        poll_seconds: float | None = None,
        max_wait_seconds: float = 900,
        after_event: str | None = None,
    ) -> dict[str, Any]:
        interval = 600 if poll_seconds is None else poll_seconds
        if (
            not math.isfinite(interval)
            or interval <= 0
            or not math.isfinite(max_wait_seconds)
            or max_wait_seconds < 0
        ):
            raise RRCError(
                "wait_budget",
                "poll must be positive and max-wait nonnegative finite seconds",
                "observer",
            )
        deadline = time.monotonic() + max_wait_seconds if max_wait_seconds else None
        try:
            try:
                initial = self.inspect(
                    run_id, timeout_seconds=min(180, max_wait_seconds) if max_wait_seconds else 180
                )
            except RRCError as exc:
                if (
                    exc.code == "transport_timeout"
                    and deadline is not None
                    and time.monotonic() >= deadline
                ):
                    return self._timeout(run_id, None)
                raise
            if monitoring_protocol(initial.get("binding", {})):
                return self._observe_remote(
                    run_id,
                    deadline=deadline,
                    interval=interval,
                    after_event=after_event,
                )
            if after_event is not None:
                raise RRCError(
                    "event_cursor_unsupported",
                    "legacy worker has no monitor event cursor",
                    "observer",
                )
            remaining = max(0, deadline - time.monotonic()) if deadline is not None else 0
            if deadline is not None and remaining <= 0:
                return self._timeout(run_id, initial)
            value = self._wait_legacy(run_id, poll_seconds=interval, max_wait_seconds=remaining)
            return {**value, "monitoring_mode": "client_compatibility"}
        except BaseException as exc:
            self._raise_observer_error(run_id, exc)

    def _wait_legacy(
        self,
        run_id: str,
        *,
        poll_seconds: float | None = None,
        max_wait_seconds: float = 900,
    ) -> dict[str, Any]:
        _, spec, _ = self._runtime(run_id)
        interval = 600 if poll_seconds is None else poll_seconds
        if (
            not math.isfinite(interval)
            or interval <= 0
            or not math.isfinite(max_wait_seconds)
            or max_wait_seconds < 0
        ):
            raise RRCError(
                "wait_budget",
                "poll must be positive and max-wait must be nonnegative finite seconds",
                "observer",
            )
        deadline = time.monotonic() + max_wait_seconds if max_wait_seconds else None
        try:
            while True:
                value = self.inspect(run_id)
                state = value["status"]["state"]
                if spec.session.backend != "process":
                    if state in {"completed", "failed", "aborted"}:
                        return value
                    raise RRCError(
                        "legacy_backend_read_only",
                        "legacy run preserved; inspect its frozen control state",
                        "observer",
                        details={"run_id": run_id, "remote_workload_preserved": True},
                    )
                if state == "workload_complete":
                    return self._finalize_completed_workload(run_id)
                if state == "completed":
                    completion = self.health(run_id, phase="completion")
                    if self._health_status(completion) == "unhealthy" or not completion.get(
                        "complete"
                    ):
                        raise RRCError(
                            "completion_recheck",
                            "terminal completion health recheck failed",
                            "health",
                            details=completion,
                        )
                    return value
                if state in {"failed", "aborted"}:
                    return value
                if deadline is not None and time.monotonic() >= deadline:
                    return {
                        **value,
                        "observation": "timeout",
                        "remote_workload_preserved": True,
                        "resume_argv": ["rrctl", "wait", run_id],
                    }
                phase = "first_step" if state == "launched" else "periodic"
                periodic = self.health(run_id, phase=phase)
                if self._health_status(periodic) == "unhealthy":
                    rechecked = self.inspect(run_id)
                    rechecked_state = rechecked["status"]["state"]
                    if rechecked_state == "workload_complete":
                        return self._finalize_completed_workload(run_id)
                    if rechecked_state in {"failed", "aborted"}:
                        return rechecked
                    raise RRCError(
                        "periodic_health",
                        "periodic health check needs attention; remote workload was preserved",
                        "observer",
                        details={
                            "health": periodic,
                            "recheck": rechecked,
                            "remote_workload_preserved": True,
                            "explicit_abort_required": True,
                        },
                    )
                delay = (
                    min(interval, max(0, deadline - time.monotonic()))
                    if deadline is not None
                    else interval
                )
                time.sleep(delay)
        except BaseException as exc:
            self._raise_observer_error(run_id, exc)

    def pull(self, run_id: str, *, diagnostic: bool = False) -> dict[str, Any]:
        ref, spec, transport = self._runtime(run_id)
        source_root = spec.remote.output_root
        manifest_root = ref.control_root
        destination = Path(spec.local_pull_root).expanduser() / run_id
        if diagnostic:
            snapshot = self._worker_lifecycle_command(run_id, ["diagnostics"], phase="diagnostics")
            snapshot_id = snapshot.get("snapshot_id")
            if not isinstance(snapshot_id, str) or not re.fullmatch(r"[0-9a-f]{32}", snapshot_id):
                raise RRCError(
                    "diagnostic_snapshot_invalid", "invalid snapshot identifier", "artifact"
                )
            source_root = f"{ref.control_root}/diagnostics/{snapshot_id}"
            manifest_root = source_root
            destination = destination / "diagnostics" / snapshot_id
        manifest_bytes = transport.download(f"{manifest_root}/artifact_manifest.json")
        with tempfile.TemporaryDirectory(prefix="rrctl-manifest-") as temporary_name:
            manifest_path = Path(temporary_name) / "artifact_manifest.json"
            manifest_path.write_bytes(manifest_bytes)
            manifest = load_artifact_manifest(manifest_path, expected_run_id=run_id)
        diagnostic_only = (
            not diagnostic
            and destination.is_dir()
            and not destination.is_symlink()
            and {path.name for path in destination.iterdir()} == {"diagnostics"}
            and (destination / "diagnostics").is_dir()
            and not (destination / "diagnostics").is_symlink()
        )
        if destination.exists() and not diagnostic_only:
            existing_manifest = destination / "artifact_manifest.json"
            if (
                not destination.is_symlink()
                and existing_manifest.is_file()
                and not existing_manifest.is_symlink()
            ):
                existing = load_artifact_manifest(existing_manifest, expected_run_id=run_id)
                if sha256_json(existing) == sha256_json(manifest) and all(
                    (destination / item["path"]).is_file()
                    and not (destination / item["path"]).is_symlink()
                    and (destination / item["path"]).resolve().is_relative_to(destination.resolve())
                    and (destination / item["path"]).stat().st_size == item["size"]
                    and sha256_file(destination / item["path"]) == item["sha256"]
                    for item in manifest["entries"]
                ):
                    return {
                        "run_id": run_id,
                        "destination": str(destination),
                        "entries": len(manifest["entries"]),
                        "diagnostic": diagnostic,
                        "reused": True,
                        "layout": "diagnostic_snapshot" if diagnostic else "artifacts",
                    }
            raise RRCError(
                "pull_collision",
                f"local artifact destination already exists: {destination}",
                "artifact",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=destination.parent))
        try:
            for entry in manifest["entries"]:
                relative = Path(entry["path"])
                local_path = temporary / relative
                local_path.parent.mkdir(parents=True, exist_ok=True)
                data = transport.download(f"{source_root}/{relative.as_posix()}")
                local_path.write_bytes(data)
                if (
                    local_path.stat().st_size != entry["size"]
                    or sha256_file(local_path) != entry["sha256"]
                ):
                    raise RRCError(
                        "pull_integrity",
                        f"downloaded artifact failed size/SHA verification: {relative}",
                        "artifact",
                    )
            atomic_write_json(temporary / "artifact_manifest.json", manifest)
            if diagnostic_only:
                if {path.name for path in destination.iterdir()} != {"diagnostics"} or (
                    temporary / "diagnostics"
                ).exists():
                    raise RRCError("pull_collision", "destination changed during pull", "artifact")
                os.replace(destination / "diagnostics", temporary / "diagnostics")
                try:
                    destination.rmdir()
                    os.replace(temporary, destination)
                except BaseException:
                    destination.mkdir(exist_ok=True)
                    os.replace(temporary / "diagnostics", destination / "diagnostics")
                    raise
            else:
                os.replace(temporary, destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return {
            "run_id": run_id,
            "destination": str(destination),
            "entries": len(manifest["entries"]),
            "diagnostic": diagnostic,
            "reused": False,
            "layout": "diagnostic_snapshot" if diagnostic else "artifacts",
        }

    def resume(self, *, profile_name: str, control_root: str) -> dict[str, Any]:
        profile = self.profile_store.load(profile_name)
        transport = transport_for(profile)
        spec_bytes = transport.download(f"{control_root}/run_spec.json")
        binding_bytes = transport.download(f"{control_root}/binding.json")
        spec_value = json.loads(spec_bytes)
        binding = json.loads(binding_bytes)
        spec = RunSpec.from_dict(spec_value)
        if not isinstance(binding, dict) or binding.get("schema_version") != "rrctl.binding.v1":
            raise RRCError("resume_binding_invalid", "remote binding is invalid", "recovery")
        expected_binding = {
            "run_id": spec.run_id,
            "project": spec.project,
            "commit": spec.source.commit,
            "run_spec_sha256": spec.digest,
            "stage_root": spec.remote.stage_root,
            "repo_root": spec.remote.repo_root,
            "control_root": control_root,
            "output_root": spec.remote.output_root,
            "session": spec.session.name,
        }
        mismatches = {
            key: {"expected": expected, "actual": binding.get(key)}
            for key, expected in expected_binding.items()
            if binding.get(key) != expected
        }
        if profile_name != spec.remote.profile:
            mismatches["profile"] = {
                "expected": spec.remote.profile,
                "actual": profile_name,
            }
        if mismatches:
            raise RRCError(
                "resume_binding_mismatch",
                "remote binding does not match the authoritative RunSpec/control path",
                "recovery",
                details=mismatches,
            )
        worker_sha = binding.get("worker_sha256")
        if (
            not isinstance(worker_sha, str)
            or len(worker_sha) != 64
            or any(character not in "0123456789abcdef" for character in worker_sha)
        ):
            raise RRCError("resume_worker_sha", "bound worker SHA is invalid", "recovery")
        ref = RunRef(
            run_id=spec.run_id,
            profile=profile_name,
            stage_root=binding["stage_root"],
            control_root=control_root,
            worker_path=f"{binding['stage_root']}/rrctl-worker.pyz",
            remote_python=spec.remote.python,
            run_spec_sha256=spec.digest,
            source_content_sha256=str(binding.get("source_content_sha256", "")),
            transport_bundle_sha256=str(
                binding.get("transport_bundle_sha256", binding.get("bundle_sha256", ""))
            ),
        )
        hash_result = transport.run(["sha256sum", ref.worker_path])
        actual_worker_sha = hash_result.stdout.decode("utf-8", errors="replace").split(maxsplit=1)
        if (
            hash_result.returncode != 0
            or not actual_worker_sha
            or actual_worker_sha[0] != worker_sha
        ):
            raise RRCError(
                "resume_worker_sha_mismatch",
                "remote worker does not match the bound SHA",
                "recovery",
                details={
                    "stderr": redact(
                        hash_result.stderr.decode("utf-8", errors="replace")[-4000:],
                        transport.secret_values,
                    )
                },
            )
        result = transport.run(
            [ref.remote_python, ref.worker_path, "inspect", "--control", ref.control_root]
        )
        inspected = _parse_worker_result(
            result, phase="resume", secret_values=transport.secret_values
        )
        self.index.save(ref, spec, inspected["status"])
        return {"run_ref": ref.to_dict(), **inspected}

    def abort(self, run_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise RRCError("abort_confirmation", "abort requires explicit --yes", "ownership")
        ref, spec, transport = self._runtime(run_id)
        if spec.session.backend != "process":
            raise RRCError(
                "legacy_backend_read_only",
                "legacy runs can be inspected and pulled; "
                "process ownership is required for new lifecycle mutations",
                "ownership",
            )
        result = transport.run(
            [ref.remote_python, ref.worker_path, "abort", "--control", ref.control_root]
        )
        return _parse_worker_result(result, phase="abort", secret_values=transport.secret_values)
