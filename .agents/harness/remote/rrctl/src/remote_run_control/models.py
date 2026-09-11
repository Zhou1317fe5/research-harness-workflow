"""RunSpec protocol models with explicit, dependency-free parsing."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import RRCError
from .jsonutil import sha256_json


def _json_value(value: Any) -> Any:
    """Return dataclass output using only JSON-native container types."""
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RRCError("spec_type", f"{path} must be an object", "schema")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise RRCError("spec_type", f"{path} must be non-empty text", "schema")
    return value.strip()


def _argv(value: Any, path: str, *, optional: bool = False) -> tuple[str, ...]:
    if optional and (value is None or value == []):
        return ()
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(v, str) or not v or "\0" in v for v in value)
    ):
        raise RRCError("spec_argv", f"{path} must be a non-empty string array", "schema")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class SourceSpec:
    repo_root: str
    branch: str
    commit: str
    bundle_sha256: str = ""
    allowed_post_commit_paths: tuple[str, ...] = ()
    source_content_sha256: str = ""

    @property
    def transport_bundle_sha256(self) -> str:
        """Preferred name for the legacy bundle byte digest field."""

        return self.bundle_sha256


@dataclass(frozen=True, slots=True)
class RemoteSpec:
    profile: str
    stage_root: str
    repo_root: str
    control_root: str
    output_root: str
    python: str = "/usr/bin/python3"


@dataclass(frozen=True, slots=True)
class SessionSpec:
    backend: str
    name: str


@dataclass(frozen=True, slots=True)
class EnvironmentSpec:
    kind: str
    name: str
    conda_sh: str = "/root/miniconda3/etc/profile.d/conda.sh"
    variables: dict[str, str] = field(default_factory=dict)
    required_modules: tuple[str, ...] = ()
    preflight_argv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourcesSpec:
    device: str = "gpu"
    gpu_ids: tuple[str, ...] = ()
    minimum_free_mib: int = 1024


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    argv: tuple[str, ...]
    cwd: str = "."


@dataclass(frozen=True, slots=True)
class AnchorSpec:
    name: str
    local_path: str
    remote_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class HealthPhaseSpec:
    timeout_seconds: int
    poll_interval_seconds: float = 5.0
    console_stale_seconds: int | None = None
    progress_path: str | None = None
    progress_stale_seconds: int | None = None
    gpu_min_percent: int | None = None
    low_gpu_limit_seconds: int | None = None
    fatal_patterns: tuple[str, ...] = ()
    adapter_argv: tuple[str, ...] = ()
    adapter_timeout_seconds: int = 30
    gpu_utilization_policy: str = "advisory"


@dataclass(frozen=True, slots=True)
class HealthSpec:
    first_step: HealthPhaseSpec
    periodic: HealthPhaseSpec
    completion: HealthPhaseSpec


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    path: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class OutputCleanupSpec:
    mode: str
    retain: tuple[str, ...] = ("smoke_summary.json",)
    delete_globs: tuple[str, ...] = ()
    delete_files_larger_than_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class RunSpec:
    schema_version: str
    run_id: str
    project: str
    source: SourceSpec
    remote: RemoteSpec
    session: SessionSpec
    environment: EnvironmentSpec
    workload: WorkloadSpec
    health: HealthSpec
    anchors: tuple[AnchorSpec, ...] = ()
    artifacts: tuple[ArtifactSpec, ...] = ()
    output_cleanup: OutputCleanupSpec | None = None
    local_pull_root: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    resources: ResourcesSpec | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RunSpec:
        root = _mapping(raw, "run_spec")
        source = _mapping(root.get("source"), "source")
        remote = _mapping(root.get("remote"), "remote")
        session = _mapping(
            root.get("session", {"backend": "process", "name": root.get("run_id")}), "session"
        )
        environment = _mapping(root.get("environment"), "environment")
        workload = _mapping(root.get("workload"), "workload")
        health = _mapping(root.get("health"), "health")

        def phase(name: str) -> HealthPhaseSpec:
            value = _mapping(health.get(name), f"health.{name}")
            timeout = value.get("timeout_seconds")
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
                raise RRCError(
                    "spec_type", f"health.{name}.timeout_seconds must be an integer >= 1", "schema"
                )
            default_poll = (
                600.0 if name == "periodic" and session.get("backend") == "process" else 5.0
            )
            poll_interval = value.get("poll_interval_seconds", default_poll)
            adapter_timeout = value.get("adapter_timeout_seconds", 30)
            fatal_patterns = value.get("fatal_patterns", [])
            gpu_policy = value.get("gpu_utilization_policy", "advisory")
            if (
                not isinstance(poll_interval, int | float)
                or isinstance(poll_interval, bool)
                or poll_interval <= 0
                or not math.isfinite(poll_interval)
            ):
                raise RRCError(
                    "spec_type",
                    f"health.{name}.poll_interval_seconds must be a number > 0",
                    "schema",
                )
            if (
                not isinstance(adapter_timeout, int)
                or isinstance(adapter_timeout, bool)
                or adapter_timeout < 1
            ):
                raise RRCError(
                    "spec_type",
                    f"health.{name}.adapter_timeout_seconds must be an integer >= 1",
                    "schema",
                )
            if not isinstance(fatal_patterns, list) or any(
                not isinstance(pattern, str) or not pattern for pattern in fatal_patterns
            ):
                raise RRCError(
                    "spec_type",
                    f"health.{name}.fatal_patterns must be a string array",
                    "schema",
                )
            if gpu_policy not in {"required", "advisory", "disabled"}:
                raise RRCError(
                    "spec_type",
                    f"health.{name}.gpu_utilization_policy must be required, advisory, or disabled",
                    "schema",
                )
            optional_integer_fields = {
                "console_stale_seconds": (1, None),
                "progress_stale_seconds": (1, None),
                "gpu_min_percent": (0, 100),
                "low_gpu_limit_seconds": (1, None),
            }
            for field_name, (minimum, maximum) in optional_integer_fields.items():
                field_value = value.get(field_name)
                if field_value is not None and (
                    not isinstance(field_value, int)
                    or isinstance(field_value, bool)
                    or field_value < minimum
                    or (maximum is not None and field_value > maximum)
                ):
                    maximum_text = f" and <= {maximum}" if maximum is not None else ""
                    raise RRCError(
                        "spec_type",
                        f"health.{name}.{field_name} must be an integer >= {minimum}{maximum_text}",
                        "schema",
                    )
            return HealthPhaseSpec(
                timeout_seconds=timeout,
                poll_interval_seconds=float(poll_interval),
                console_stale_seconds=value.get("console_stale_seconds"),
                progress_path=value.get("progress_path"),
                progress_stale_seconds=value.get("progress_stale_seconds"),
                gpu_min_percent=value.get("gpu_min_percent"),
                low_gpu_limit_seconds=value.get("low_gpu_limit_seconds"),
                gpu_utilization_policy=gpu_policy,
                fatal_patterns=tuple(fatal_patterns),
                adapter_argv=_argv(
                    value.get("adapter_argv"), f"health.{name}.adapter_argv", optional=True
                ),
                adapter_timeout_seconds=adapter_timeout,
            )

        anchors_raw = root.get("anchors", [])
        if not isinstance(anchors_raw, list):
            raise RRCError("spec_type", "anchors must be an array", "schema")
        anchors = tuple(
            AnchorSpec(
                name=_text(
                    _mapping(item, f"anchors[{index}]").get("name"), f"anchors[{index}].name"
                ),
                local_path=_text(item.get("local_path"), f"anchors[{index}].local_path"),
                remote_path=_text(item.get("remote_path"), f"anchors[{index}].remote_path"),
                sha256=_text(item.get("sha256"), f"anchors[{index}].sha256"),
            )
            for index, item in enumerate(anchors_raw)
        )

        artifacts_raw = root.get("artifacts", [])
        if not isinstance(artifacts_raw, list):
            raise RRCError("spec_type", "artifacts must be an array", "schema")
        artifacts = tuple(
            ArtifactSpec(
                path=_text(
                    _mapping(item, f"artifacts[{index}]").get("path"), f"artifacts[{index}].path"
                ),
                required=item.get("required", True),
            )
            for index, item in enumerate(artifacts_raw)
        )

        allowed = source.get("allowed_post_commit_paths", [])
        if not isinstance(allowed, list) or any(
            not isinstance(item, str) or not item for item in allowed
        ):
            raise RRCError(
                "spec_type", "source.allowed_post_commit_paths must be a string array", "schema"
            )
        legacy_bundle_sha = str(source.get("bundle_sha256", "")).strip()
        transport_bundle_sha = str(source.get("transport_bundle_sha256", legacy_bundle_sha)).strip()
        if legacy_bundle_sha and transport_bundle_sha != legacy_bundle_sha:
            raise RRCError(
                "spec_type",
                "source bundle_sha256 and transport_bundle_sha256 disagree",
                "schema",
            )
        metadata = root.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RRCError("spec_type", "metadata must be an object", "schema")
        cleanup_raw = root.get("output_cleanup")
        output_cleanup = None
        if cleanup_raw is not None:
            cleanup = _mapping(cleanup_raw, "output_cleanup")
            retain = cleanup.get("retain", ["smoke_summary.json"])
            delete_globs = cleanup.get("delete_globs", [])
            threshold = cleanup.get("delete_files_larger_than_bytes")
            if not isinstance(retain, list) or any(
                not isinstance(item, str) or not item for item in retain
            ):
                raise RRCError(
                    "spec_type", "output_cleanup.retain must be a string array", "schema"
                )
            if not isinstance(delete_globs, list) or any(
                not isinstance(item, str) or not item for item in delete_globs
            ):
                raise RRCError(
                    "spec_type", "output_cleanup.delete_globs must be a string array", "schema"
                )
            if threshold is not None and (
                not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1
            ):
                raise RRCError(
                    "spec_type",
                    "output_cleanup.delete_files_larger_than_bytes must be an integer >= 1",
                    "schema",
                )
            output_cleanup = OutputCleanupSpec(
                mode=_text(cleanup.get("mode"), "output_cleanup.mode"),
                retain=tuple(retain),
                delete_globs=tuple(delete_globs),
                delete_files_larger_than_bytes=threshold,
            )
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact.required, bool):
                raise RRCError(
                    "spec_type", f"artifacts[{index}].required must be a boolean", "schema"
                )

        variables = _mapping(environment.get("variables", {}), "environment.variables")
        for key, value in variables.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)
                or not isinstance(value, str)
                or "\0" in value
                or key.startswith(("RRCTL_", "CONDA_"))
                or key in {"BASH_ENV", "ENV", "CUDA_VISIBLE_DEVICES"}
                or re.search(
                    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|"
                    r"SECRET_ACCESS_KEY|ACCESS_KEY_ID)$",
                    key,
                )
            ):
                raise RRCError(
                    "environment_variable",
                    f"invalid or reserved environment variable name: {key}",
                    "schema",
                )
        modules = environment.get("required_modules", [])
        if not isinstance(modules, list) or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", item)
            for item in modules
        ):
            raise RRCError(
                "environment_modules",
                "environment.required_modules must contain module names",
                "schema",
            )
        resources = None
        if root.get("resources") is not None:
            values = _mapping(root["resources"], "resources")
            if set(values) - {"device", "gpu_ids", "minimum_free_mib"}:
                raise RRCError("resources_fields", "unknown resources field", "schema")
            device = values.get("device", "gpu")
            ids = values.get("gpu_ids", [])
            minimum = values.get("minimum_free_mib", 1024)
            if (
                device not in {"gpu", "cpu"}
                or not isinstance(ids, list)
                or any(
                    not isinstance(item, str)
                    or not re.fullmatch(r"(?:\d+|GPU-[A-Za-z0-9-]+)", item)
                    for item in ids
                )
                or len(set(ids)) != len(ids)
                or (device == "cpu" and ids)
            ):
                raise RRCError(
                    "resources_devices", "declare cpu or unique GPU indices/UUIDs", "schema"
                )
            if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
                raise RRCError(
                    "resources_memory",
                    "resources.minimum_free_mib must be an integer >= 0",
                    "schema",
                )
            resources = ResourcesSpec(device=device, gpu_ids=tuple(ids), minimum_free_mib=minimum)

        return cls(
            schema_version=_text(root.get("schema_version"), "schema_version"),
            run_id=_text(root.get("run_id"), "run_id"),
            project=_text(root.get("project"), "project"),
            source=SourceSpec(
                repo_root=_text(source.get("repo_root"), "source.repo_root"),
                branch=_text(source.get("branch"), "source.branch"),
                commit=_text(source.get("commit"), "source.commit"),
                source_content_sha256=str(source.get("source_content_sha256", "")).strip(),
                bundle_sha256=transport_bundle_sha,
                allowed_post_commit_paths=tuple(allowed),
            ),
            remote=RemoteSpec(
                profile=_text(remote.get("profile"), "remote.profile"),
                stage_root=_text(remote.get("stage_root"), "remote.stage_root"),
                repo_root=_text(remote.get("repo_root"), "remote.repo_root"),
                control_root=_text(remote.get("control_root"), "remote.control_root"),
                output_root=_text(remote.get("output_root"), "remote.output_root"),
                python=_text(
                    remote.get(
                        "python",
                        "/usr/bin/python3" if session.get("backend") == "process" else "python3",
                    ),
                    "remote.python",
                ),
            ),
            session=SessionSpec(
                backend=_text(session.get("backend"), "session.backend"),
                name=_text(session.get("name"), "session.name"),
            ),
            environment=EnvironmentSpec(
                kind=_text(environment.get("kind"), "environment.kind"),
                name=_text(environment.get("name"), "environment.name"),
                conda_sh=_text(
                    environment.get("conda_sh", "/root/miniconda3/etc/profile.d/conda.sh"),
                    "environment.conda_sh",
                ),
                variables=dict(variables),
                required_modules=tuple(modules),
                preflight_argv=_argv(
                    environment.get("preflight_argv"), "environment.preflight_argv", optional=True
                ),
            ),
            workload=WorkloadSpec(
                argv=_argv(workload.get("argv"), "workload.argv"),
                cwd=_text(workload.get("cwd", "."), "workload.cwd"),
            ),
            health=HealthSpec(
                first_step=phase("first_step"),
                periodic=phase("periodic"),
                completion=phase("completion"),
            ),
            anchors=anchors,
            artifacts=artifacts,
            output_cleanup=output_cleanup,
            local_pull_root=_text(root.get("local_pull_root"), "local_pull_root"),
            metadata=metadata,
            resources=resources,
        )

    @classmethod
    def from_path(cls, path: Path) -> RunSpec:
        from .jsonutil import load_json

        raw = load_json(path)
        if not isinstance(raw, dict):
            raise RRCError("spec_type", "RunSpec top level must be an object", "schema")
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        value = _json_value(asdict(self))
        if not self.source.bundle_sha256:
            value["source"].pop("bundle_sha256", None)
        if not self.source.source_content_sha256:
            value["source"].pop("source_content_sha256", None)
        if self.output_cleanup is None:
            value.pop("output_cleanup", None)
        if self.resources is None:
            value.pop("resources", None)
        # 旧 RunSpec 的规范摘要保持不变，以便读取冻结的历史运行。
        for key in ("variables", "required_modules", "preflight_argv"):
            if not value["environment"][key]:
                value["environment"].pop(key)
        for phase in value["health"].values():
            if phase.get("gpu_utilization_policy") == "advisory":
                phase.pop("gpu_utilization_policy")
        return value

    @property
    def digest(self) -> str:
        return sha256_json(self.to_dict())
