"""Process-isolated project adapter protocol."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RRCError
from .jsonutil import canonical_bytes
from .security import redact


@dataclass(frozen=True, slots=True)
class AdapterResult:
    healthy: bool
    complete: bool
    progress: dict[str, Any]
    observations: dict[str, Any]
    artifacts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "rrctl.adapter.v1",
            "healthy": self.healthy,
            "complete": self.complete,
            "progress": self.progress,
            "observations": self.observations,
            "artifacts": list(self.artifacts),
        }


def parse_adapter_result(value: Any) -> AdapterResult:
    if not isinstance(value, dict):
        raise RRCError("adapter_json_type", "adapter output must be a JSON object", "adapter")
    if value.get("protocol") != "rrctl.adapter.v1":
        raise RRCError(
            "adapter_protocol",
            "adapter protocol must equal rrctl.adapter.v1",
            "adapter",
        )
    healthy = value.get("healthy")
    complete = value.get("complete")
    if not isinstance(healthy, bool) or not isinstance(complete, bool):
        raise RRCError(
            "adapter_verdict_type",
            "adapter healthy and complete must be booleans",
            "adapter",
        )
    if complete and not healthy:
        raise RRCError(
            "adapter_verdict_inconsistent",
            "adapter cannot report complete=true with healthy=false",
            "adapter",
        )
    progress = value.get("progress", {})
    observations = value.get("observations", {})
    artifacts = value.get("artifacts", [])
    if not isinstance(progress, dict) or not isinstance(observations, dict):
        raise RRCError(
            "adapter_payload_type",
            "adapter progress and observations must be objects",
            "adapter",
        )
    if not isinstance(artifacts, list) or any(
        not isinstance(item, str) or not item for item in artifacts
    ):
        raise RRCError(
            "adapter_artifacts_type",
            "adapter artifacts must be a string array",
            "adapter",
        )
    return AdapterResult(healthy, complete, progress, observations, tuple(artifacts))


def run_adapter(
    argv: tuple[str, ...],
    context: dict[str, Any],
    *,
    timeout_seconds: int,
    cwd: Path,
    environment_command: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> AdapterResult | None:
    if not argv:
        return None
    command = [*environment_command, *argv]
    try:
        result = subprocess.run(
            command,
            input=canonical_bytes(context) + b"\n",
            check=False,
            capture_output=True,
            cwd=cwd,
            env=env or os.environ.copy(),
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RRCError(
            "adapter_timeout",
            f"adapter exceeded {timeout_seconds}s timeout",
            "adapter",
        ) from exc
    if result.returncode != 0:
        raise RRCError(
            "adapter_exit",
            f"adapter exited with code {result.returncode}",
            "adapter",
            details={"stderr": redact(result.stderr.decode("utf-8", errors="replace")[-4000:])},
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RRCError(
            "adapter_json",
            f"adapter emitted invalid JSON: {exc}",
            "adapter",
            details={"stdout": redact(result.stdout.decode("utf-8", errors="replace")[-4000:])},
        ) from exc
    return parse_adapter_result(value)
