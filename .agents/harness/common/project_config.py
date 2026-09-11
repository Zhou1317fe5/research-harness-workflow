"""读取项目命令、监控字段和产物配置，不依赖训练框架。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("Python 3.10 requires tomli; install .agents/harness/requirements.txt in the selected environment") from exc
from pathlib import Path, PurePosixPath

from harness.common.paths import CONFIG_DIR, REPO_ROOT as REPO_ROOT

DEFAULT_CONFIG = CONFIG_DIR / "project.toml"


def relative_path(value: str, label: str, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label}: expected a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or (str(path) == "." and not allow_dot):
        raise ValueError(f"{label}: path must stay within its root")
    return path.as_posix()


def _pipeline_options(config: dict) -> tuple[dict, dict]:
    legacy = config.get("pipeline", {})
    named = config.get("pipelines", {})
    if not isinstance(legacy, dict) or not isinstance(named, dict):
        raise ValueError("pipeline and pipelines must be tables")
    if set(legacy) - {"default", "stages"}:
        raise ValueError("pipeline accepts default or legacy stages")
    for name, definition in named.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) or not isinstance(definition, dict) or set(definition) - {"stages"}:
            raise ValueError(f"invalid named pipeline: {name}")
    if legacy.get("stages") and "default" in named:
        raise ValueError("named pipeline 'default' conflicts with legacy stages")
    return legacy, named


def list_pipelines(path: Path = DEFAULT_CONFIG) -> dict:
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    legacy, named = _pipeline_options(config)
    names = (["default"] if legacy.get("stages") else []) + sorted(named)
    default = legacy.get("default") or ("default" if legacy.get("stages") else names[0] if len(names) == 1 else None)
    if default is not None and default not in names:
        raise ValueError(f"pipeline.default is not registered: {default}")
    return {"default": default, "pipelines": names}


def pipeline_digest(config: dict) -> str:
    data = json.dumps(config.get("pipeline", {}).get("stages", []), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def load_config(path: Path = DEFAULT_CONFIG, *, pipeline: str | None = None) -> dict:
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    legacy, named = _pipeline_options(config)
    selected = pipeline if pipeline is not None else legacy.get("default")
    if selected is None:
        if legacy.get("stages"):
            selected = "default"
        elif len(named) == 1:
            selected = next(iter(named))
        elif named:
            raise ValueError("multiple pipelines are available; choose --pipeline or set pipeline.default")
    if selected is not None and (not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", selected)):
        raise ValueError("pipeline selector must be an identifier")
    if selected == "default" and legacy.get("stages"):
        stages, registered = legacy["stages"], False
    elif selected in named:
        stages, registered = named[selected].get("stages", []), True
    elif selected is None:
        stages, registered = [], False
    else:
        raise ValueError(f"unknown pipeline: {selected}; available: {', '.join(sorted(named))}")
    config.pop("pipelines", None)
    config["pipeline"] = {"name": selected, "named": registered, "stages": stages}
    if config.get("version") != 1:
        raise ValueError("project config version must be 1")
    unknown = set(config) - {"version", "pipeline", "adapter", "artifacts", "records", "environment", "resources", "health"}
    if unknown:
        raise ValueError(f"unknown project config sections: {sorted(unknown)}")
    for section in ("pipeline", "adapter", "records", "environment", "resources", "health"):
        if not isinstance(config.get(section, {}), dict):
            raise ValueError(f"{section} must be a table")
    stages = config.get("pipeline", {}).get("stages", [])
    if not isinstance(stages, list):
        raise ValueError("pipeline.stages must be an array of tables")
    names, logs = set(), set()
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) - {"name", "argv", "check_argv", "cwd", "log", "requires", "outputs"}:
            raise ValueError("invalid pipeline stage fields")
        name = stage.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) or name in names:
            raise ValueError("stage names must be unique identifiers")
        names.add(name)
        argv = stage.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v or "\0" in v for v in argv):
            raise ValueError(f"stage {name}: argv must be a non-empty string array")
        check_argv = stage.get("check_argv", [])
        if not isinstance(check_argv, list) or any(not isinstance(v, str) or not v or "\0" in v for v in check_argv):
            raise ValueError(f"stage {name}: check_argv must be a string array")
        relative_path(stage.get("cwd", "."), "stage.cwd", allow_dot=True)
        log = relative_path(stage.get("log", f"{name}.log"), "stage.log")
        if log in logs:
            raise ValueError("stage log paths must be unique")
        logs.add(log)
        for field in ("requires", "outputs"):
            values = stage.get(field, [])
            if not isinstance(values, list):
                raise ValueError(f"stage.{field} must be an array")
            for value in values:
                relative_path(value, f"stage.{field}")
    artifacts = config.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ValueError("artifacts must be an array of tables")
    for item in artifacts:
        if not isinstance(item, dict) or set(item) - {"path", "required"}:
            raise ValueError("artifact entries accept path and required")
        relative_path(item.get("path"), "artifact.path")
        if not isinstance(item.get("required", True), bool):
            raise ValueError("artifact.required must be a boolean")
    records = config.get("records", {})
    if set(records) - {"summary_glob", "primary_metric", "secondary_metric", "protocol_field", "checkpoint_field", "steps_field", "dimensions"}:
        raise ValueError("unknown records configuration field")
    relative_path(records.get("summary_glob", "summary.json"), "records.summary_glob")
    for key in ("primary_metric", "secondary_metric", "protocol_field", "checkpoint_field", "steps_field"):
        if key in records and (not isinstance(records[key], str) or not records[key]):
            raise ValueError(f"records.{key} must be non-empty text")
    dimensions = records.get("dimensions", [])
    if not isinstance(dimensions, list) or any(not isinstance(v, str) or not v for v in dimensions):
        raise ValueError("records.dimensions must be a string array")
    environment = config.get("environment", {})
    if set(environment) - {"variables", "required_modules", "preflight_argv"}:
        raise ValueError("project environment accepts variables, required_modules and preflight_argv")
    resources = config.get("resources", {})
    if set(resources) - {"device", "gpu_ids", "minimum_free_mib"}:
        raise ValueError("unknown project resources field")
    if set(config.get("health", {})) - {"first_step", "periodic", "completion"}:
        raise ValueError("unknown project health phase")
    return config


def apply_project_config(request: dict, path: Path, *, pipeline: str | None = None) -> dict:
    requested = request.get("pipeline")
    if pipeline is not None and requested is not None and pipeline != requested:
        raise ValueError("--pipeline differs from the request pipeline")
    selector = pipeline if pipeline is not None else requested
    config = load_config(path, pipeline=selector)
    result = copy.deepcopy(request)
    source = result.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("repo_root"), str):
        raise ValueError("source.repo_root is required before applying project config")
    repo_root = Path(source["repo_root"]).resolve()
    config_relative = path.resolve().relative_to(repo_root).as_posix()
    if not config.get("pipeline", {}).get("stages"):
        raise ValueError("configure at least one pipeline stage")
    name = config["pipeline"]["name"]
    digest = pipeline_digest(config)
    runner = ["python", ".agents/harness/pipeline/run_pipeline.py", "--config", config_relative]
    selection = ["--pipeline", name, "--pipeline-sha256", digest]
    workload = {"argv": runner + selection, "cwd": "."}
    explicit = result.get("workload")
    if (selector is not None or config["pipeline"]["named"]) and explicit is not None and explicit != workload:
        raise ValueError("named pipeline selection conflicts with an explicit workload; let project config generate the workload")
    if explicit is None or explicit == {"argv": runner, "cwd": "."} or explicit == workload:
        result["workload"] = workload
        result["pipeline"] = name
        metadata = result.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        metadata.update({"pipeline_config": config_relative, "pipeline_stages_sha256": digest,
                         "pipeline_stages": copy.deepcopy(config["pipeline"]["stages"])})
    result.setdefault("adapter_contract", config.get("adapter", {}))
    result.setdefault("artifacts", config.get("artifacts", []))
    requested_resources = result.get("resources", {})
    if not isinstance(requested_resources, dict):
        raise ValueError("resources must be an object")
    result["resources"] = {"device": "gpu", **config.get("resources", {}), **requested_resources}
    if requested_resources.get("device") == "cpu" and "gpu_ids" not in requested_resources:
        result["resources"]["gpu_ids"] = []
    environment = result.setdefault("environment", {})
    if not isinstance(environment, dict):
        raise ValueError("environment must be an object")
    for key, value in config.get("environment", {}).items():
        if key == "variables":
            if not isinstance(value, dict) or not isinstance(environment.get(key, {}), dict):
                raise ValueError("environment.variables must be a table/object")
            environment[key] = {**copy.deepcopy(value), **environment.get(key, {})}
        else:
            environment.setdefault(key, copy.deepcopy(value))
    preflight = [*runner, *selection, "--check"]
    if (selector is not None or config["pipeline"]["named"]) and environment.get("preflight_argv", preflight) != preflight:
        raise ValueError("named pipeline preflight must match its selection; put extra input checks in stage.check_argv")
    environment.setdefault("preflight_argv", preflight)
    health = result.setdefault("health", {})
    if not isinstance(health, dict):
        raise ValueError("health must be an object")
    for phase, defaults in config.get("health", {}).items():
        if not isinstance(defaults, dict):
            raise ValueError(f"health.{phase} must be a table")
        current = health.setdefault(phase, {})
        if not isinstance(current, dict):
            raise ValueError(f"health.{phase} must be an object")
        for key, value in defaults.items():
            current.setdefault(key, copy.deepcopy(value))
    return result
