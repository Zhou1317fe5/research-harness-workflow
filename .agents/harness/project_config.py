"""读取项目命令、监控字段和产物配置，不依赖训练框架。"""
from __future__ import annotations

import copy
import re
import tomllib
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / ".agents/harness/project.toml"


def relative_path(value: str, label: str, *, allow_dot: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label}: expected a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or (str(path) == "." and not allow_dot):
        raise ValueError(f"{label}: path must stay within its root")
    return path.as_posix()


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if config.get("version") != 1:
        raise ValueError("project config version must be 1")
    unknown = set(config) - {"version", "pipeline", "adapter", "artifacts", "records"}
    if unknown:
        raise ValueError(f"unknown project config sections: {sorted(unknown)}")
    for section in ("pipeline", "adapter", "records"):
        if not isinstance(config.get(section, {}), dict):
            raise ValueError(f"{section} must be a table")
    stages = config.get("pipeline", {}).get("stages", [])
    if not isinstance(stages, list):
        raise ValueError("pipeline.stages must be an array of tables")
    names, logs = set(), set()
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) - {"name", "argv", "cwd", "log", "requires", "outputs"}:
            raise ValueError("invalid pipeline stage fields")
        name = stage.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) or name in names:
            raise ValueError("stage names must be unique identifiers")
        names.add(name)
        argv = stage.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v or "\0" in v for v in argv):
            raise ValueError(f"stage {name}: argv must be a non-empty string array")
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
    if set(records) - {"summary_glob", "primary_metric", "secondary_metric", "dimensions"}:
        raise ValueError("unknown records configuration field")
    relative_path(records.get("summary_glob", "summary.json"), "records.summary_glob")
    for key in ("primary_metric", "secondary_metric"):
        if key in records and (not isinstance(records[key], str) or not records[key]):
            raise ValueError(f"records.{key} must be non-empty text")
    dimensions = records.get("dimensions", [])
    if not isinstance(dimensions, list) or any(not isinstance(v, str) or not v for v in dimensions):
        raise ValueError("records.dimensions must be a string array")
    return config


def apply_project_config(request: dict, path: Path) -> dict:
    config = load_config(path)
    result = copy.deepcopy(request)
    repo_root = Path(result["source"]["repo_root"]).resolve()
    config_relative = path.resolve().relative_to(repo_root).as_posix()
    if not config.get("pipeline", {}).get("stages"):
        raise ValueError("configure at least one pipeline stage")
    result.setdefault("workload", {
        "argv": ["python", ".agents/harness/run_pipeline.py", "--config", config_relative],
        "cwd": ".",
    })
    result.setdefault("adapter_contract", config.get("adapter", {}))
    result.setdefault("artifacts", config.get("artifacts", []))
    return result
