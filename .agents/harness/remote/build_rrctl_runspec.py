#!/usr/bin/env python3
"""Build a deterministic rrctl.run.v1 document from an explicit <PROJECT> request."""

from __future__ import annotations

# 直接运行脚本和通过 Python 包导入时使用同一实现。
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.remote.build_rrctl_runspec import main
    raise SystemExit(main())

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

REQUEST_SCHEMA = "mission.rrctl-request.v1"
RUN_SCHEMA = "rrctl.run.v1"
GATE_PROVENANCE_SCHEMA = "prerun.gate-provenance.v2"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SECRET_KEY_RE = re.compile(
    r"(?i)^(?:password|passwd|pwd|api[_-]?key|access[_-]?token|secret[_-]?key|auth[_-]?token)$"
)
DEFAULT_ADAPTER_ARGV = ["python", ".agents/harness/remote/adapters/generic_json.py"]
EXECUTION_PURPOSES = {
    "official",
    "pilot",
    "pre_review_smoke",
    "preregistered_read_only_probe",
}
ARTIFACT_PULL_MODES = {"minimal", "diagnostic"}
RAW_DIAGNOSTIC_TOKENS = (
    "episode_trace",
    "rank_trace",
    "per_episode",
    "episode_metrics",
    "condition_metrics",
    "path_ablation_metrics",
)
SMOKE_SUMMARY_PATH = "smoke_summary.json"
SMOKE_DELETE_GLOBS = [
    "checkpoint*",
    "**/checkpoint*",
    "*.pt",
    "**/*.pt",
    "*.pth",
    "**/*.pth",
    "*.ckpt",
    "**/*.ckpt",
    "*.safetensors",
    "**/*.safetensors",
    "pytorch_model*.bin",
    "**/pytorch_model*.bin",
    "optimizer*",
    "**/optimizer*",
    "scheduler*",
    "**/scheduler*",
]
ADAPTER_CONTRACT_FIELDS = {
    "progress_path",
    "progress_format",
    "summary_path",
    "summary_format",
    "progress_count_field",
    "first_step_min_count",
    "completion_min_count",
    "completion_exact_count",
    "progress_finite_fields",
    "summary_finite_fields",
    "summary_required_fields",
    "artifacts",
    "progress_identity_fields",
    "summary_identity_fields",
}
GATE_PROVENANCE_FIELDS = {
    "schema_version",
    "pre_run_code_commit",
    "review_mode",
    "review_result",
    "reviewer_id",
    "blocker_closure_evidence",
}
GATE_PROVENANCE_METADATA_KEYS = GATE_PROVENANCE_FIELDS - {"schema_version"}


def _load_project_adapters() -> dict[tuple[str, ...], dict[str, Any]]:
    """加载本项目自定义 adapter 的契约校验；缺失时返回空注册表。

    通用工作流只需 generic adapter；项目 adapter 登记在
    ``.agents/harness/remote/project_adapters.py``，模板化时删除该文件即可。
    """
    try:
        from harness.remote.project_adapters import PROJECT_ADAPTERS
    except Exception:
        return {}
    return dict(PROJECT_ADAPTERS)


class RunSpecBuildError(Exception):
    """A stable, user-facing RunSpec request failure."""


def _reject_unknown(value: dict[str, Any], field: str, allowed: set[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RunSpecBuildError(f"{field}.unknown_fields: {','.join(unknown)}")


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunSpecBuildError(f"{field}_type_invalid: expected an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunSpecBuildError(f"{field}_type_invalid: expected non-empty text")
    return value.strip()


def _string_list(value: Any, field: str, *, nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
    ):
        requirement = "non-empty " if nonempty else ""
        raise RunSpecBuildError(
            f"{field}_type_invalid: expected a {requirement}string array"
        )
    return list(value)


def _relative(value: Any, field: str) -> str:
    text = _text(value, field)
    path = PurePosixPath(text)
    if path.is_absolute() or text in {"", "."} or ".." in path.parts:
        raise RunSpecBuildError(f"{field}_not_confined: {text}")
    return path.as_posix()


def _absolute(value: Any, field: str) -> PurePosixPath:
    text = _text(value, field)
    path = PurePosixPath(text)
    if not path.is_absolute() or text == "/" or ".." in path.parts:
        raise RunSpecBuildError(f"{field}_not_specific_absolute_path: {text}")
    return path


def _local_absolute(value: Any, field: str) -> Path:
    path = Path(_text(value, field)).expanduser()
    if not path.is_absolute() or path == Path(path.anchor):
        raise RunSpecBuildError(f"{field}_not_specific_absolute_path: {path}")
    return path


def _official_local_pull_root(repo_root: Path, exp_id: str, run_id: str) -> Path:
    """Return the canonical local destination for official remote artifacts.

    证据区在项目根 ``remote_artifacts/<ExpID>/<RunID>``，不在 research_workspace 内：
    后者只保存科研认知（record.json / analysis），原始证据不进 Git、不作默认上下文。
    """
    # rrctl pull 自行追加 RunID，此处只提供 ExpID 目录。
    return repo_root / "remote_artifacts" / exp_id


def _contains_secret_data(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and SECRET_KEY_RE.fullmatch(key))
            or _contains_secret_data(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_data(item) for item in value)
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    markers = (
        "--password",
        "--passwd",
        "--api-key",
        "--access-token",
        "private key-----",
    )
    return any(marker in lowered for marker in markers)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_generic_contract(contract: dict[str, Any]) -> None:
    _reject_unknown(contract, "adapter_contract", ADAPTER_CONTRACT_FIELDS)
    for field in ("progress_path", "summary_path"):
        _relative(contract.get(field), f"adapter_contract.{field}")
    for field in ("progress_format", "summary_format"):
        value = contract.get(field, "json")
        if value not in {"json", "jsonl_last"}:
            raise RunSpecBuildError(f"adapter_contract.{field}_invalid")
    count_field = contract.get("progress_count_field")
    if count_field is not None:
        _text(count_field, "adapter_contract.progress_count_field")
    for field in (
        "progress_finite_fields",
        "summary_finite_fields",
        "summary_required_fields",
        "artifacts",
    ):
        values = _string_list(contract.get(field, []), f"adapter_contract.{field}")
        if field == "artifacts":
            for index, item in enumerate(values):
                _relative(item, f"adapter_contract.artifacts[{index}]")
    for field in ("progress_identity_fields", "summary_identity_fields"):
        _mapping(contract.get(field, {}), f"adapter_contract.{field}")
    for field in (
        "first_step_min_count",
        "completion_min_count",
        "completion_exact_count",
    ):
        value = contract.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise RunSpecBuildError(f"adapter_contract.{field}_type_invalid")
    if (
        contract.get("completion_min_count") is not None
        and contract.get("completion_exact_count") is not None
    ):
        raise RunSpecBuildError("adapter_contract_completion_count_ambiguous")
    if (
        any(
            contract.get(field) is not None
            for field in (
                "first_step_min_count",
                "completion_min_count",
                "completion_exact_count",
            )
        )
        and count_field is None
    ):
        raise RunSpecBuildError("adapter_contract.progress_count_field_required")






def _gate_provenance(raw: Any, *, source_commit: str) -> dict[str, Any]:
    provenance = _mapping(raw, "gate_provenance")
    _reject_unknown(
        provenance,
        "gate_provenance",
        GATE_PROVENANCE_FIELDS,
    )
    missing = sorted(GATE_PROVENANCE_FIELDS - set(provenance))
    if missing:
        raise RunSpecBuildError(
            f"gate_provenance.missing_fields: {','.join(missing)}"
        )
    if provenance["schema_version"] != GATE_PROVENANCE_SCHEMA:
        raise RunSpecBuildError("gate_provenance.schema_version_invalid")
    commit = _text(
        provenance["pre_run_code_commit"],
        "gate_provenance.pre_run_code_commit",
    )
    if not COMMIT_RE.fullmatch(commit):
        raise RunSpecBuildError(
            "gate_provenance.pre_run_code_commit_invalid"
        )
    if commit != source_commit:
        raise RunSpecBuildError(
            "gate_provenance.pre_run_code_commit_mismatch_with_source"
        )
    review_mode = provenance["review_mode"]
    if review_mode not in {"scientific_review", "targeted_review"}:
        raise RunSpecBuildError("gate_provenance.review_mode_invalid")
    review_result = provenance["review_result"]
    if review_result not in {
        "scientifically_correct",
        "scientifically_incorrect",
        "targeted_correct",
        "targeted_incorrect",
    }:
        raise RunSpecBuildError("gate_provenance.review_result_invalid")
    if review_mode == "scientific_review" and review_result not in {
        "scientifically_correct",
        "scientifically_incorrect",
    }:
        raise RunSpecBuildError("gate_provenance.review_mode_result_mismatch")
    if review_mode == "targeted_review" and review_result not in {
        "targeted_correct",
        "targeted_incorrect",
    }:
        raise RunSpecBuildError("gate_provenance.review_mode_result_mismatch")
    reviewer_id = _text(
        provenance["reviewer_id"],
        "gate_provenance.reviewer_id",
    )
    closure = _string_list(
        provenance["blocker_closure_evidence"],
        "gate_provenance.blocker_closure_evidence",
    )
    direct_pass = review_result in {
        "scientifically_correct",
        "targeted_correct",
    }
    repaired_pass = review_result in {
        "scientifically_incorrect",
        "targeted_incorrect",
    } and bool(closure)
    if not (direct_pass or repaired_pass):
        raise RunSpecBuildError("gate_provenance.correctness_not_closed")
    return {
        "schema_version": GATE_PROVENANCE_SCHEMA,
        "pre_run_code_commit": commit,
        "review_mode": review_mode,
        "review_result": review_result,
        "reviewer_id": reviewer_id,
        "blocker_closure_evidence": closure,
    }


def _validate_contract(
    contract: dict[str, Any], adapter_argv: list[str]
) -> None:
    spec = _load_project_adapters().get(tuple(adapter_argv))
    if spec is None:
        _validate_generic_contract(contract)
        return
    spec["validate"](contract)


def _adapter_forces_gate_provenance(
    adapter_argv: list[str], contract: dict[str, Any]
) -> bool:
    """注册表可声明某些 adapter 契约版本强制携带 gate_provenance。

    模板默认注册表为空，此函数恒返回 False，走通用分支。
    """
    spec = _load_project_adapters().get(tuple(adapter_argv))
    if not spec:
        return False
    forced = spec.get("forces_gate_provenance")
    if forced is True:
        return True
    if isinstance(forced, (set, frozenset, tuple, list)):
        return contract.get("schema_version") in set(forced)
    return False


def _validate_roots(remote: dict[str, Any]) -> dict[str, PurePosixPath]:
    roots = {
        field: _absolute(remote.get(field), f"remote.{field}")
        for field in ("stage_root", "repo_root", "control_root", "output_root")
    }
    items = sorted(roots.items())
    for index, (left_name, left) in enumerate(items):
        for right_name, right in items[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise RunSpecBuildError(
                    f"remote_roots_overlap: {left_name}={left} {right_name}={right}"
                )
    return roots


def _anchors(
    raw: Any, lifecycle_roots: dict[str, PurePosixPath]
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise RunSpecBuildError("anchors_type_invalid: expected an array")
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    destinations: set[str] = set()
    for index, value in enumerate(raw):
        item = _mapping(value, f"anchors[{index}]")
        _reject_unknown(
            item,
            f"anchors[{index}]",
            {"name", "local_path", "remote_path", "sha256"},
        )
        name = _text(item.get("name"), f"anchors[{index}].name")
        if not IDENTIFIER_RE.fullmatch(name) or name in names:
            raise RunSpecBuildError(f"anchors[{index}].name_invalid_or_duplicate")
        names.add(name)
        local_path = _local_absolute(
            item.get("local_path"), f"anchors[{index}].local_path"
        )
        if not local_path.is_file() or local_path.is_symlink():
            raise RunSpecBuildError(f"anchors[{index}].local_path_not_regular_file")
        expected = _text(item.get("sha256"), f"anchors[{index}].sha256")
        if not SHA256_RE.fullmatch(expected):
            raise RunSpecBuildError(f"anchors[{index}].sha256_invalid")
        actual = hashlib.sha256(local_path.read_bytes()).hexdigest()
        if actual != expected:
            raise RunSpecBuildError(
                f"anchors[{index}].sha256_mismatch: expected={expected} actual={actual}"
            )
        remote_path = _absolute(
            item.get("remote_path"), f"anchors[{index}].remote_path"
        )
        if any(
            remote_path == root or root in remote_path.parents
            for root in lifecycle_roots.values()
        ):
            raise RunSpecBuildError(
                f"anchors[{index}].remote_path_overlaps_lifecycle_root"
            )
        if remote_path.as_posix() in destinations:
            raise RunSpecBuildError(f"anchors[{index}].remote_path_duplicate")
        destinations.add(remote_path.as_posix())
        result.append(
            {
                "name": name,
                "local_path": str(local_path),
                "remote_path": remote_path.as_posix(),
                "sha256": expected,
            }
        )
    return result


def _artifacts(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise RunSpecBuildError("artifacts_type_invalid: expected an array")
    result: list[dict[str, Any]] = []
    paths: set[str] = set()
    for index, value in enumerate(raw):
        item = _mapping(value, f"artifacts[{index}]")
        _reject_unknown(item, f"artifacts[{index}]", {"path", "required"})
        path = _relative(item.get("path"), f"artifacts[{index}].path")
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise RunSpecBuildError(f"artifacts[{index}].required_type_invalid")
        if path in paths:
            raise RunSpecBuildError(f"artifacts[{index}].path_duplicate")
        paths.add(path)
        result.append({"path": path, "required": required})
    return result


def _artifact_pull_policy(
    raw: Any,
    artifacts: list[dict[str, Any]],
    *,
    thin_smoke: bool,
) -> dict[str, Any]:
    """Validate what is pulled now versus retained remotely for on-demand debug."""
    if thin_smoke:
        if raw not in (None, {}):
            raise RunSpecBuildError(
                "pre_review_smoke.artifact_pull_policy_is_implicit_minimal"
            )
        return {"mode": "minimal", "on_demand": []}

    policy = _mapping(raw if raw is not None else {}, "artifact_pull_policy")
    _reject_unknown(policy, "artifact_pull_policy", {"mode", "on_demand"})
    mode = policy.get("mode", "minimal")
    if mode not in ARTIFACT_PULL_MODES:
        raise RunSpecBuildError("artifact_pull_policy.mode_invalid")
    on_demand = [
        _relative(value, f"artifact_pull_policy.on_demand[{index}]")
        for index, value in enumerate(
            _string_list(
                policy.get("on_demand", []),
                "artifact_pull_policy.on_demand",
            )
        )
    ]
    if len(set(on_demand)) != len(on_demand):
        raise RunSpecBuildError("artifact_pull_policy.on_demand_duplicate")

    pulled_paths = {item["path"] for item in artifacts}
    overlap = sorted(pulled_paths.intersection(on_demand))
    if overlap:
        raise RunSpecBuildError(
            "artifact_pull_policy.on_demand_already_pulled: " + ",".join(overlap)
        )
    if mode == "minimal":
        raw_paths = sorted(
            path
            for path in pulled_paths
            if path.lower().endswith(".jsonl")
            or any(token in path.lower() for token in RAW_DIAGNOSTIC_TOKENS)
        )
        if raw_paths:
            raise RunSpecBuildError(
                "artifact_pull_policy.minimal_contains_raw_diagnostics: "
                + ",".join(raw_paths)
            )
    return {"mode": mode, "on_demand": on_demand}


def _health(
    raw: Any,
    adapter_argv: list[str],
    adapter_progress_path: str,
) -> dict[str, Any]:
    health = _mapping(raw, "health")
    _reject_unknown(health, "health", {"first_step", "periodic", "completion"})
    result: dict[str, Any] = {}
    allowed_fields = {
        "timeout_seconds",
        "poll_interval_seconds",
        "console_stale_seconds",
        "progress_path",
        "progress_stale_seconds",
        "gpu_min_percent",
        "gpu_utilization_policy",
        "low_gpu_limit_seconds",
        "fatal_patterns",
        "adapter_timeout_seconds",
    }
    for phase in ("first_step", "periodic", "completion"):
        item = dict(_mapping(health.get(phase), f"health.{phase}"))
        unknown = sorted(set(item) - allowed_fields)
        if unknown:
            raise RunSpecBuildError(
                f"health.{phase}.unknown_fields: {','.join(unknown)}"
            )
        timeout = item.get("timeout_seconds")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
            raise RunSpecBuildError(f"health.{phase}.timeout_seconds_invalid")
        poll_interval = item.get("poll_interval_seconds", 5.0)
        if (
            not isinstance(poll_interval, int | float)
            or isinstance(poll_interval, bool)
            or poll_interval <= 0
        ):
            raise RunSpecBuildError(f"health.{phase}.poll_interval_seconds_invalid")
        adapter_timeout = item.get("adapter_timeout_seconds", 30)
        if (
            not isinstance(adapter_timeout, int)
            or isinstance(adapter_timeout, bool)
            or adapter_timeout < 1
        ):
            raise RunSpecBuildError(f"health.{phase}.adapter_timeout_seconds_invalid")
        gpu_policy = item.get("gpu_utilization_policy", "advisory")
        if not isinstance(gpu_policy, str) or gpu_policy not in {"required", "advisory", "disabled"}:
            raise RunSpecBuildError(f"health.{phase}.gpu_utilization_policy_invalid")
        fatal_patterns = _string_list(
            item.get("fatal_patterns", []),
            f"health.{phase}.fatal_patterns",
        )
        optional_integer_limits = {
            "console_stale_seconds": (1, None),
            "progress_stale_seconds": (1, None),
            "gpu_min_percent": (0, 100),
            "low_gpu_limit_seconds": (1, None),
        }
        for field, (minimum, maximum) in optional_integer_limits.items():
            value = item.get(field)
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < minimum
                or (maximum is not None and value > maximum)
            ):
                raise RunSpecBuildError(f"health.{phase}.{field}_invalid")
        if phase in {"first_step", "periodic"}:
            configured_progress = item.get("progress_path", adapter_progress_path)
            if configured_progress != adapter_progress_path:
                raise RunSpecBuildError(
                    f"health.{phase}.progress_path_mismatch_with_adapter_contract"
                )
            item["progress_path"] = adapter_progress_path
        elif item.get("progress_path") is not None:
            item["progress_path"] = _relative(
                item["progress_path"],
                f"health.{phase}.progress_path",
            )
        result[phase] = {
            "timeout_seconds": timeout,
            "poll_interval_seconds": float(poll_interval),
            "console_stale_seconds": item.get("console_stale_seconds"),
            "progress_path": item.get("progress_path"),
            "progress_stale_seconds": item.get("progress_stale_seconds"),
            "gpu_min_percent": item.get("gpu_min_percent"),
            "gpu_utilization_policy": gpu_policy,
            "low_gpu_limit_seconds": item.get("low_gpu_limit_seconds"),
            "fatal_patterns": fatal_patterns,
            "adapter_argv": list(adapter_argv),
            "adapter_timeout_seconds": adapter_timeout,
        }
    return result


def build_runspec(request: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(
        request,
        "request",
        {
            "schema_version",
            "spec_id",
            "exp_id",
            "run_id",
            "project",
            "source",
            "remote",
            "session_name",
            "environment",
            "workload",
            "health",
            "anchors",
            "artifacts",
            "artifact_pull_policy",
            "local_pull_root",
            "adapter_argv",
            "adapter_contract",
            "gate_provenance",
            "metadata",
            "execution_purpose",
        },
    )
    if request.get("schema_version") != REQUEST_SCHEMA:
        raise RunSpecBuildError(f"schema_version_must_equal_{REQUEST_SCHEMA}")
    if _contains_secret_data(request):
        raise RunSpecBuildError("request_contains_secret_like_data")
    spec_id = _text(request.get("spec_id"), "spec_id")
    exp_id = _text(request.get("exp_id"), "exp_id")
    run_id = _text(request.get("run_id"), "run_id")
    if not IDENTIFIER_RE.fullmatch(exp_id):
        raise RunSpecBuildError("exp_id_invalid")
    if not IDENTIFIER_RE.fullmatch(run_id):
        raise RunSpecBuildError("run_id_invalid")
    execution_purpose = request.get("execution_purpose", "official")
    if execution_purpose not in EXECUTION_PURPOSES:
        raise RunSpecBuildError("execution_purpose_invalid")
    thin_smoke = execution_purpose == "pre_review_smoke"

    source = _mapping(request.get("source"), "source")
    _reject_unknown(
        source,
        "source",
        {
            "repo_root",
            "branch",
            "commit",
            "bundle_sha256",
            "allowed_post_commit_paths",
        },
    )
    commit = _text(source.get("commit"), "source.commit")
    if not COMMIT_RE.fullmatch(commit):
        raise RunSpecBuildError("source.commit_must_be_full_lowercase_sha")
    bundle_sha256 = str(source.get("bundle_sha256", "")).strip()
    if bundle_sha256 and not SHA256_RE.fullmatch(bundle_sha256):
        raise RunSpecBuildError("source.bundle_sha256_invalid")
    repo_root = _local_absolute(source.get("repo_root"), "source.repo_root")
    if not repo_root.is_dir():
        raise RunSpecBuildError("source.repo_root_missing")
    branch = _text(source.get("branch"), "source.branch")
    allowed_paths = [
        _relative(value, f"source.allowed_post_commit_paths[{index}]")
        for index, value in enumerate(
            _string_list(
                source.get("allowed_post_commit_paths", []),
                "source.allowed_post_commit_paths",
            )
        )
    ]

    remote = _mapping(request.get("remote"), "remote")
    _reject_unknown(
        remote,
        "remote",
        {
            "profile",
            "stage_root",
            "repo_root",
            "control_root",
            "output_root",
            "python",
        },
    )
    lifecycle_roots = _validate_roots(remote)
    if thin_smoke and run_id not in lifecycle_roots["output_root"].as_posix():
        raise RunSpecBuildError("pre_review_smoke.output_root_not_bound_to_run_id")
    profile = _text(remote.get("profile"), "remote.profile")
    remote_python = _text(remote.get("python", "python3"), "remote.python")

    environment = _mapping(request.get("environment"), "environment")
    _reject_unknown(environment, "environment", {"kind", "name", "conda_sh"})
    if environment.get("kind", "conda") != "conda":
        raise RunSpecBuildError("environment.kind_must_equal_conda")
    conda_name = _text(environment.get("name"), "environment.name")
    conda_sh = _absolute(
        environment.get("conda_sh", "<REMOTE_CONDA_ROOT>/etc/profile.d/conda.sh"),
        "environment.conda_sh",
    ).as_posix()

    workload = _mapping(request.get("workload"), "workload")
    _reject_unknown(workload, "workload", {"argv", "cwd"})
    workload_argv = _string_list(workload.get("argv"), "workload.argv", nonempty=True)
    workload_cwd = _text(workload.get("cwd", "."), "workload.cwd")
    cwd_path = PurePosixPath(workload_cwd)
    if cwd_path.is_absolute() or ".." in cwd_path.parts:
        raise RunSpecBuildError("workload.cwd_not_confined")

    session_name = _text(request.get("session_name"), "session_name")
    if not IDENTIFIER_RE.fullmatch(session_name):
        raise RunSpecBuildError("session_name_invalid")
    requested_local_pull_root = request.get("local_pull_root")
    if requested_local_pull_root is None:
        if thin_smoke:
            raise RunSpecBuildError("pre_review_smoke.local_pull_root_required")
        local_pull_root = str(_official_local_pull_root(repo_root, exp_id, run_id))
    else:
        local_pull_root = str(
            _local_absolute(requested_local_pull_root, "local_pull_root")
        )
    requested_adapter_argv = _string_list(
        request.get("adapter_argv", DEFAULT_ADAPTER_ARGV),
        "adapter_argv",
        nonempty=True,
    )
    requested_contract = _mapping(request.get("adapter_contract"), "adapter_contract")
    if thin_smoke:
        adapter_argv = list(DEFAULT_ADAPTER_ARGV)
        adapter_progress_path = requested_contract.get(
            "progress_path", requested_contract.get("run_progress_path")
        )
        if adapter_progress_path is None:
            progress_paths = _mapping(
                requested_contract.get("progress_paths"),
                "adapter_contract.progress_paths",
            )
            adapter_progress_path = progress_paths.get("observed")
        adapter_progress_path = _relative(
            adapter_progress_path,
            "adapter_contract.progress_path",
        )
        progress_count_field = requested_contract.get("progress_count_field")
        if progress_count_field is not None:
            progress_count_field = _text(
                progress_count_field,
                "adapter_contract.progress_count_field",
            )
        contract = {
            "progress_path": adapter_progress_path,
            "progress_format": requested_contract.get("progress_format", "json"),
            "summary_path": SMOKE_SUMMARY_PATH,
            "progress_finite_fields": _string_list(
                requested_contract.get("progress_finite_fields", []),
                "adapter_contract.progress_finite_fields",
            ),
            "summary_finite_fields": [],
            "summary_required_fields": [
                "checkpoint_cleanup_completed",
                "checkpoint_paths_remaining",
                "retained_evidence_paths",
            ],
            "artifacts": [SMOKE_SUMMARY_PATH],
            "progress_identity_fields": _mapping(
                requested_contract.get("progress_identity_fields", {}),
                "adapter_contract.progress_identity_fields",
            ),
            "summary_identity_fields": {
                "schema_version": "rrctl.smoke-summary.v1",
                "run_id": run_id,
                "checkpoint_cleanup_completed": True,
                "checkpoint_paths_remaining": [],
            },
            **(
                {"progress_count_field": progress_count_field}
                if progress_count_field is not None
                else {}
            ),
            **(
                {
                    "first_step_min_count": requested_contract.get(
                        "first_step_min_count", 1
                    )
                }
                if progress_count_field is not None
                else {}
            ),
        }
        _validate_generic_contract(contract)
    else:
        adapter_argv = requested_adapter_argv
        contract = requested_contract
        _validate_contract(contract, adapter_argv)
        adapter_progress_path = contract.get(
            "progress_path", contract.get("run_progress_path")
        )
        if adapter_progress_path is None:
            adapter_progress_path = contract["progress_paths"]["observed"]
    adapter_spec = _load_project_adapters().get(tuple(adapter_argv), {})
    if (
        not thin_smoke
        and adapter_spec.get("requires_bundle_sha256")
        and not bundle_sha256
    ):
        raise RunSpecBuildError("source.bundle_sha256_required_for_adapter")
    selected_artifacts = (
        [{"path": SMOKE_SUMMARY_PATH, "required": True}]
        if thin_smoke
        else _artifacts(request.get("artifacts", []))
    )
    artifact_pull_policy = _artifact_pull_policy(
        request.get("artifact_pull_policy"),
        selected_artifacts,
        thin_smoke=thin_smoke,
    )
    extra_metadata = _mapping(request.get("metadata", {}), "metadata")
    reserved = {
        "spec_id",
        "exp_id",
        "adapter_contract",
        "gate_provenance",
        "execution_purpose",
        "thin_smoke",
        "artifact_pull_policy",
        *GATE_PROVENANCE_METADATA_KEYS,
    }
    if reserved.intersection(extra_metadata):
        raise RunSpecBuildError("metadata_uses_reserved_key")
    gate_provenance = None
    if thin_smoke:
        gate_provenance = None
    elif _adapter_forces_gate_provenance(adapter_argv, contract):
        gate_provenance = _gate_provenance(
            request.get("gate_provenance"),
            source_commit=commit,
        )
    elif request.get("gate_provenance") is not None:
        gate_provenance = _gate_provenance(
            request["gate_provenance"],
            source_commit=commit,
        )
    return {
        "schema_version": RUN_SCHEMA,
        "run_id": run_id,
        "project": _text(request.get("project", "<PROJECT>"), "project"),
        "source": {
            "repo_root": str(repo_root),
            "branch": branch,
            "commit": commit,
            **({"bundle_sha256": bundle_sha256} if bundle_sha256 else {}),
            "allowed_post_commit_paths": allowed_paths,
        },
        "remote": {
            "profile": profile,
            "stage_root": lifecycle_roots["stage_root"].as_posix(),
            "repo_root": lifecycle_roots["repo_root"].as_posix(),
            "control_root": lifecycle_roots["control_root"].as_posix(),
            "output_root": lifecycle_roots["output_root"].as_posix(),
            "python": remote_python,
        },
        "session": {"backend": "tmux", "name": session_name},
        "environment": {"kind": "conda", "name": conda_name, "conda_sh": conda_sh},
        "workload": {"argv": workload_argv, "cwd": workload_cwd},
        "health": _health(
            request.get("health"),
            adapter_argv,
            adapter_progress_path,
        ),
        "anchors": (
            []
            if thin_smoke
            else _anchors(request.get("anchors", []), lifecycle_roots)
        ),
        "artifacts": selected_artifacts,
        **(
            {
                "output_cleanup": {
                    "mode": "pre_review_smoke",
                    "retain": [SMOKE_SUMMARY_PATH],
                    "delete_globs": list(SMOKE_DELETE_GLOBS),
                    "delete_files_larger_than_bytes": 67108864,
                }
            }
            if thin_smoke
            else {}
        ),
        "local_pull_root": local_pull_root,
        "metadata": {
            "spec_id": spec_id,
            "exp_id": exp_id,
            "adapter_contract": contract,
            "artifact_pull_policy": artifact_pull_policy,
            **(
                {"execution_purpose": execution_purpose, "thin_smoke": True}
                if thin_smoke
                else (
                    {"execution_purpose": execution_purpose}
                    if execution_purpose != "official"
                    else {}
                )
            ),
            **(
                {"gate_provenance": gate_provenance}
                if gate_provenance is not None
                else {}
            ),
            **extra_metadata,
        },
    }


def write_exclusive(path: Path, value: dict[str, Any]) -> str:
    if path.exists() or path.is_symlink():
        raise RunSpecBuildError(f"output_collision: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical_bytes(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def require_rrctl(executable: str) -> str:
    resolved = shutil.which(executable)
    if resolved is None:
        raise RunSpecBuildError(
            f"rrctl_unavailable: {executable}; install with "
            "python -m pip install -e .agents/harness/remote/rrctl"
        )
    return resolved


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument(
        "request",
        help=f"{REQUEST_SCHEMA} request JSON path, or - to read stdin",
    )
    root.add_argument(
        "--output", type=Path, required=True, help="exclusive RunSpec output path"
    )
    root.add_argument(
        "--check-rrctl",
        action="store_true",
        help="fail before generation when the rrctl executable is unavailable",
    )
    root.add_argument("--rrctl-executable", default="rrctl")
    root.add_argument("--project-config", type=Path, help="project TOML defaults for workload, adapter and artifacts")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.check_rrctl:
            require_rrctl(args.rrctl_executable)
        raw_request = (
            sys.stdin.read()
            if args.request == "-"
            else Path(args.request).read_text(encoding="utf-8")
        )
        value = json.loads(raw_request)
        request = _mapping(value, "request")
        if args.project_config:
            from harness.common.project_config import apply_project_config
            try:
                request = apply_project_config(request, args.project_config)
            except (ValueError, KeyError) as exc:
                raise RunSpecBuildError(f"project_config: {exc}") from exc
        runspec = build_runspec(request)
        digest = write_exclusive(args.output, runspec)
        print(
            json.dumps(
                {
                    "ok": True,
                    "output": str(args.output),
                    "run_id": runspec["run_id"],
                    "run_spec_sha256": digest,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, json.JSONDecodeError, RunSpecBuildError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "runspec_build_failed",
                        "message": str(exc),
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
