#!/usr/bin/env python3
"""从 Mission CSV 与 remote_artifacts 投影出 record.json，再派生 EXPERIMENTS.csv。

设计原则：只写证据支持的字段。单 Run 指标和明确一致的协议直接投影；
无法从 CSV 或 artifacts 推出的字段保留待补状态，不猜聚合、baseline 或 outcome。
"""
from __future__ import annotations

# 直接运行脚本和通过 Python 包导入时使用同一实现。
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.records.experiment_records import main
    raise SystemExit(main())

import argparse
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
import math
import os
import re
import sys
import tempfile
from pathlib import Path

from harness.common.project_config import DEFAULT_CONFIG, REPO_ROOT, load_config
from harness.remote.adapters.common import AdapterContractError, dotted_value
from harness.remote.build_rrctl_runspec import RunSpecBuildError, canonical_run_spec, run_spec_digest

ARTIFACTS = REPO_ROOT / "remote_artifacts"
EXPERIMENTS = REPO_ROOT / "research_workspace" / "experiments"
LEDGER = REPO_ROOT / "research_workspace" / "EXPERIMENTS.csv"
GENERATOR = ".agents/harness/records/experiment_records.py"

# 默认待补字段；从机器事实明确投影后移除对应项。
PENDING_FIELDS = ("parent", "metrics.protocol", "metrics.baseline_id", "outcome")
RESULT_ANALYSIS_OUTCOMES = frozenset({
    "hypothesis_supported",
    "hypothesis_not_supported",
    "gate_failed",
    "inconclusive",
    "not_applicable",
})
OUTCOME_META_SCHEMA = "research-result-analysis-outcome.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_pending_outcome(value: object) -> bool:
    return value is None or (isinstance(value, str) and (not value.strip() or value == "pending"))


def _record_run_ids(record: dict) -> list[str] | None:
    runs = record.get("runs")
    if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        return None
    values = [run.get("run_id") for run in runs]
    if any(not isinstance(run_id, str) or not run_id for run_id in values):
        return None
    return sorted(set(values))


def _runs_digest(record: dict) -> str | None:
    runs = record.get("runs")
    if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        return None
    ordered = sorted(runs, key=lambda run: json.dumps(run, ensure_ascii=False, sort_keys=True))
    return hashlib.sha256(
        json.dumps(ordered, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError(f"invalid experiment identifier: {value}")
    return value


def optional_summary_value(data: dict, field: str):
    try:
        return dotted_value(data, field, "summary")
    except AdapterContractError:
        return None


def csv_projection(repo_root: Path | None = None) -> dict[str, dict]:
    """按 exp_id 汇总 Mission CSV 中可投影的字段。"""
    project_root = (repo_root or REPO_ROOT).resolve()
    out: dict[str, dict] = {}
    for path in sorted((project_root / "issues").rglob("*.csv")):
        if not path.resolve().is_relative_to(project_root):
            continue
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        except Exception:
            continue
        for row in rows:
            for exp in re.split(r"[;,]", row.get("exp_id") or ""):
                exp = exp.strip()
                if not exp:
                    continue
                identifier(exp)
                acc = out.setdefault(
                    exp,
                    {"spec_id": set(), "branch": set(), "commit": set(),
                     "run_id": set(), "next_action": [], "csv": set()},
                )
                acc["csv"].add(path.relative_to(project_root).as_posix())
                for key, col in (("spec_id", "spec_id"), ("branch", "branch"),
                                 ("commit", "commit_hash")):
                    val = (row.get(col) or "").strip()
                    if val:
                        acc[key].add(val)
                run_id = (row.get("run_id") or "").strip()
                if run_id:
                    # 受限 smoke/probe 的证据在 Mission 内，不属于正式实验应交付的 Run。
                    spec_path = path.parent / "runs" / identifier(run_id) / "runspec.json"
                    purpose = None
                    if spec_path.resolve().is_relative_to(path.parent.resolve()):
                        try:
                            run_spec = json.loads(spec_path.read_text(encoding="utf-8"))
                            metadata = run_spec.get("metadata", {})
                            if run_spec.get("run_id") == run_id and metadata.get("exp_id") == exp:
                                purpose = metadata.get("execution_purpose")
                        except (OSError, ValueError, AttributeError):
                            pass
                    if purpose not in ("pre_review_smoke", "preregistered_read_only_probe"):
                        # Restricted runs are deliberately not formal records.  If
                        # their canonical RunSpec was not retained, the explicit
                        # disabled-ingest marker is the only safe fallback; never
                        # silently drop a missing-spec formal run.
                        restricted_marker = (
                            "artifact_ingest_disabled:true" in (row.get("notes") or "")
                            and "official_metrics_disabled:true" in (row.get("notes") or "")
                        )
                        if not (restricted_marker and row.get("remote_state") == "not_applicable"):
                            acc["run_id"].add(run_id)
                na = (row.get("next_action") or "").strip()
                if na and na not in acc["next_action"]:
                    acc["next_action"].append(na)
    return out


RECOVERY_BINDING_SCHEMA = "research.mission-recovery-binding.v1"
RECOVERY_COVERAGE_SCHEMA = "research.recovery-coverage.v1"
_TERMINAL_STATES = {"completed", "workload_complete", "failed", "stopped", "cancelled", "aborted"}


def _evidence_json(path: Path, root: Path) -> dict:
    # Reject symlinks in every component, not only the final filename.
    root = root.resolve()
    abs_path = path.absolute()
    if not abs_path.is_relative_to(root):
        raise ValueError("ingest_evidence_path_outside_workspace")
    cursor = abs_path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError("ingest_evidence_symlink")
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    if not path.resolve().is_relative_to(root):
        raise ValueError("ingest_evidence_path_outside_workspace")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("ingest_evidence_not_object")
    return value


def _status_payload(payload: dict) -> dict:
    if payload.get("schema_version") == "research.rrctl-observation.v1":
        payload = payload.get("response") or {}
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return {}
    status = result.get("status", result)
    return status if isinstance(status, dict) else {}


def _explicit_failure(data: dict) -> bool:
    # These are operational verdicts, not scientific outcomes (a negative result is valid).
    return (any(data.get(key) is False for key in ("complete", "completed", "healthy", "success", "completion_contract_passed"))
            or any(data.get(key) in {"failed", "failure", "error", "stopped", "cancelled", "aborted"}
                   for key in ("status", "state", "terminal_state") if isinstance(data.get(key), str))
            or any(data.get(key) is not None and data[key] != 0
                   for key in ("exit_code", "workload_exit_code")))


def _terminal_evidence(csv_path: Path, run_root: Path, spec: dict, root: Path,
                       extra_paths: tuple[Path, ...] = ()) -> dict:
    """Read retained rrctl observations; never infer a remote exit from CSV ingest state."""
    root = root.resolve()
    csv_path = csv_path.resolve()
    run_root = run_root.resolve()
    run_id = spec["run_id"]
    # Only controller evidence belonging to this Run, never arbitrary workload or Mission JSON.
    paths = {run_root / ".rrctl/status.json", *extra_paths}
    run_dir = csv_path.parent / "runs" / run_id
    paths.update(run_dir / name for name in ("status.json", "inspect.json", "terminal.json"))
    observations_dir = csv_path.parent / "validation/rrctl-observations" / run_id
    if observations_dir.is_dir():
        for stage in ("launch", "inspect", "wait"):
            paths.update(observations_dir.glob(f"{stage}-*.json"))
    recovery_binding = csv_path.parent / "validation/recovery-binding.json"
    if recovery_binding.is_file():
        payload = _evidence_json(recovery_binding, root)
        for item in payload.get("bindings", []):
            if isinstance(item, dict) and item.get("run_id") == run_id:
                path, _ = _bound_evidence(item["status_evidence"], csv_path.parent, root)
                paths.add(path)
    observations = []
    for path in sorted(paths):
        if not path.exists() and not path.is_symlink():
            continue
        payload = _evidence_json(path, root)
        wrapper = payload if payload.get("schema_version") == "research.rrctl-observation.v1" else None
        if path.parent == observations_dir and wrapper is None:
            raise ValueError("ingest_terminal_observation_schema_invalid")
        if wrapper:
            if (wrapper.get("run_id") != run_id or wrapper.get("run_spec_sha256") != run_spec_digest(spec)
                    or wrapper.get("stage") not in {"launch", "inspect", "wait"}):
                raise ValueError("ingest_terminal_observation_identity_mismatch")
            payload = wrapper.get("response")
            if not isinstance(payload, dict):
                raise ValueError("ingest_terminal_observation_response_invalid")
            response_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")).encode()).hexdigest()
            if path.parent == observations_dir and path.name != f"{wrapper['stage']}-{response_hash}.json":
                raise ValueError("ingest_terminal_observation_hash_mismatch")
        status = _status_payload(payload)
        # Observer errors/timeouts are not remote terminal verdicts. A later inspect remains usable.
        if wrapper and status.get("run_id") is None:
            continue
        if status.get("run_id") != run_id:
            raise ValueError("ingest_terminal_run_id_mismatch")
        digest = status.get("run_spec_sha256", wrapper.get("run_spec_sha256") if wrapper else None)
        if digest is None and (path.parent / "binding.json").is_file():
            binding = _evidence_json(path.parent / "binding.json", root)
            if (binding.get("run_id") != run_id
                    or binding.get("control_root") != spec["remote"]["control_root"]):
                raise ValueError("ingest_terminal_binding_mismatch")
            digest = binding.get("run_spec_sha256")
        if digest != run_spec_digest(spec):
            raise ValueError("ingest_terminal_runspec_mismatch")
        detail = status.get("detail") or {}
        exit_code = status.get("exit_code", detail.get("exit_code"))
        state = status.get("state")
        if state in _TERMINAL_STATES or exit_code is not None:
            observations.append({"state": state, "exit_code": exit_code,
                                 "path": path.resolve().relative_to(root).as_posix()})
    failed = [item for item in observations if _explicit_failure(item)]
    chosen = (failed or [item for item in observations if item["state"] == "completed"] or observations)
    return {"state": chosen[0]["state"] if chosen else None,
            "exit_code": chosen[0]["exit_code"] if chosen else None,
            "evidence": observations}


def _bound_evidence(entry: dict, base: Path, root: Path) -> tuple[Path, dict]:
    root = root.resolve()
    base = base.resolve()
    path = base / entry["path"]
    payload = _evidence_json(path, root)
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
        raise ValueError("recovery_evidence_hash_mismatch")
    return path, payload


def _recovery_projection(csv_path: Path, run_root: Path, spec: dict, loaded: dict,
                         expected: dict, root: Path, terminal: dict, *,
                         proposed: tuple[dict, dict] | None = None) -> dict:
    """Normalize an original rrctl recovery snapshot in memory, never rewrite it."""
    root = root.resolve()
    csv_path = csv_path.resolve()
    run_root = run_root.resolve()
    binding_path = csv_path.parent / "validation/recovery-binding.json"
    payload = proposed[0] if proposed else _evidence_json(binding_path, root)
    if payload.get("schema") != RECOVERY_BINDING_SCHEMA:
        raise ValueError("recovery_binding_schema_invalid")
    matches = [item for item in payload.get("bindings", [])
               if isinstance(item, dict) and item.get("run_id") == spec["run_id"]]
    if len(matches) != 1:
        raise ValueError("recovery_binding_missing_or_duplicate")
    binding = matches[0]
    manifest_hash = hashlib.sha256((run_root / "artifact_manifest.json").read_bytes()).hexdigest()
    if (binding.get("run_spec_sha256") != expected["run_spec_sha256"]
            or loaded.get("run_spec_sha256") != expected["run_spec_sha256"]
            or binding.get("manifest_sha256") != manifest_hash
            or not loaded.get("recovery_id") or binding.get("recovery_id") != loaded["recovery_id"]
            or not isinstance(binding.get("reason"), str) or not binding["reason"].strip()):
        raise ValueError("recovery_binding_identity_mismatch")
    status_path, status_payload = _bound_evidence(binding["status_evidence"], csv_path.parent, root)
    status = _status_payload(status_payload)
    detail = status.get("detail") or {}
    state, exit_code = status.get("state"), status.get("exit_code", detail.get("exit_code"))
    if (status.get("run_id") != spec["run_id"] or state not in _TERMINAL_STATES
            or not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise ValueError("recovery_terminal_evidence_invalid")
    if (loaded.get("terminal_state", state) != state or loaded.get("exit_code", exit_code) != exit_code):
        raise ValueError("recovery_snapshot_terminal_mismatch")
    # A retained failure cannot be replaced by a newly hand-filled clean observation.
    if any(item["state"] != state or (item["exit_code"] is not None and item["exit_code"] != exit_code)
           for item in terminal["evidence"] if (item["state"] in _TERMINAL_STATES - {"workload_complete"}
                                                or _explicit_failure(item))):
        raise ValueError("recovery_terminal_evidence_conflict")
    if proposed:
        report_path = csv_path.parent / binding["coverage_report"]["path"]
        report = proposed[1]
    else:
        report_path, report = _bound_evidence(binding["coverage_report"], csv_path.parent, root)
    required = sorted(item["path"] for item in spec["artifacts"] if item["required"])
    contract = spec["metadata"].get("adapter_contract") or {}
    if (report.get("schema") != RECOVERY_COVERAGE_SCHEMA or report.get("status") != "passed"
            or report.get("run_id") != spec["run_id"]
            or report.get("run_spec_sha256") != expected["run_spec_sha256"]
            or report.get("manifest_sha256") != manifest_hash
            or report.get("required_artifacts") != required
            or report.get("verified_entries") != loaded["entries"]):
        raise ValueError("recovery_coverage_report_mismatch")
    for key in ("completion_min_count", "completion_exact_count"):
        if contract.get(key) is not None:
            count = report.get("verified_count")
            if (report.get("count_field") != contract.get("progress_count_field")
                    or not isinstance(count, int) or isinstance(count, bool)
                    or (count != contract[key] if key == "completion_exact_count" else count < contract[key])):
                raise ValueError("recovery_coverage_count_mismatch")
    entries = []
    for entry in loaded["entries"]:
        if not entry["path"].startswith("output/"):
            raise ValueError("recovery_entry_not_confined")
        entries.append({**entry, "path": entry["path"][len("output/"):]})
    from remote_run_control.artifacts import build_artifact_manifest
    from remote_run_control.models import ArtifactSpec
    # All recovered files, plus every required artifact, are rehashed. No domain-specific gaps.
    current = build_artifact_manifest(run_id=spec["run_id"], output_root=run_root / "output",
        declared=tuple(ArtifactSpec(path) for path in sorted(set(required) | {e["path"] for e in entries})),
        destination=None)
    if sorted(current["entries"], key=lambda e: e["path"]) != sorted(entries, key=lambda e: e["path"]):
        raise ValueError("recovery_artifact_manifest_mismatch")
    if any(contract.get(key) is not None for key in ("completion_min_count", "completion_exact_count")):
        from harness.remote.adapters.common import confined_path, load_structured_object
        progress = load_structured_object(
            confined_path(run_root / "output", contract["progress_path"], "progress_path"),
            "progress", contract.get("progress_format", "json"))
        count = dotted_value(progress, contract["progress_count_field"], "progress")
        if type(count) is not int or count != report["verified_count"]:
            raise ValueError("recovery_coverage_progress_mismatch")
    return {**loaded, "entries": entries, "provenance": expected,
            "evidence_mode": "recovery", "output_base": str(run_root / "output"),
            "completion_contract_passed": False,
            "terminal": {"state": state, "exit_code": exit_code, "evidence": terminal["evidence"]},
            "recovery_binding_path": binding_path.relative_to(root).as_posix(),
            "recovery_reason": binding["reason"],
            "recovery_evidence": {"status_evidence": status_path.relative_to(root).as_posix(),
                                  "coverage_report": report_path.relative_to(root).as_posix()}}


def load_run_provenance(
    csv_path: Path, exp_id: str, run_id: str, *, repo_root: Path,
    artifacts: Path | None = None,
) -> tuple[dict, dict]:
    """共用的 RunSpec → manifest 校验；不依赖 record 或 ingested 状态。"""
    project_root = repo_root.resolve()
    csv_path = csv_path.resolve()
    spec_path = csv_path.parent / "runs" / identifier(run_id) / "runspec.json"
    artifacts_dir = artifacts.resolve() if artifacts else (project_root / "remote_artifacts")
    run_root = artifacts_dir / identifier(exp_id) / run_id
    manifest_path = run_root / "artifact_manifest.json"
    if (not csv_path.is_relative_to(project_root) or not csv_path.is_file()
            or not spec_path.resolve().is_relative_to(csv_path.parent)
            or not run_root.resolve().is_relative_to(project_root)
            or not manifest_path.resolve().is_relative_to(run_root.resolve())):
        raise ValueError("ingest_provenance_path_outside_workspace")
    spec = canonical_run_spec(json.loads(spec_path.read_text(encoding="utf-8")))
    # canonical_run_spec 已加载仓库内的 rrctl 包；复用其路径、manifest 和流式哈希规则。
    from remote_run_control.artifacts import build_artifact_manifest, load_artifact_manifest
    from remote_run_control.errors import RRCError
    from remote_run_control.models import ArtifactSpec

    metadata = spec.get("metadata", {})
    source = spec["source"]
    if (spec["run_id"] != run_id or metadata.get("exp_id") != exp_id
            or not isinstance(metadata.get("spec_id"), str) or not metadata["spec_id"].strip()):
        raise ValueError("ingest_runspec_identity_mismatch")
    mission_csv = metadata.get("mission_csv")
    if mission_csv is not None and (not isinstance(mission_csv, str)
            or (project_root / mission_csv).resolve() != csv_path):
        raise ValueError("ingest_runspec_mission_mismatch")
    purpose = metadata.get("execution_purpose")
    if purpose in {"pre_review_smoke", "preregistered_read_only_probe"}:
        raise ValueError("restricted_run_cannot_be_ingested")
    try:
        manifest = load_artifact_manifest(manifest_path)
    except RRCError as exc:
        raise ValueError(f"ingest_manifest_invalid:{exc.code}: {exc.message}") from exc
    if manifest.get("run_id") != run_id:
        raise ValueError("ingest_manifest_identity_mismatch")
    expected = {"spec_id": metadata["spec_id"], "exp_id": exp_id,
                "commit": source["commit"], "run_spec_sha256": run_spec_digest(spec)}
    terminal = _terminal_evidence(csv_path, run_root, spec, project_root)
    # A recovery_id is never a normal completion, even if local provenance was added.
    if manifest.get("recovery_id"):
        try:
            return spec, _recovery_projection(csv_path, run_root, spec, manifest, expected, project_root, terminal)
        except RRCError as exc:
            raise ValueError(f"ingest_recovery_artifact_invalid:{exc.code}: {exc.message}") from exc
    if _explicit_failure(terminal) or _explicit_failure(manifest):
        raise ValueError("ingest_completion_conflicts_with_terminal_failure")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or any(provenance.get(key) != value for key, value in expected.items()):
        raise ValueError("ingest_manifest_provenance_mismatch")
    required = tuple(ArtifactSpec(item["path"]) for item in spec["artifacts"] if item["required"])
    # 只核合同要求拉取的必需文件/目录；不要求 optional 或 on_demand 产物存在。
    try:
        current = build_artifact_manifest(run_id=run_id, output_root=run_root,
                                          declared=required, destination=None)
    except RRCError as exc:
        raise ValueError(f"ingest_required_artifact_invalid:{exc.code}: {exc.message}") from exc
    expected_entries = {
        entry["path"]: (entry["size"], entry["sha256"]) for entry in manifest["entries"]
        if any(entry["path"] == item.path or entry["path"].startswith(item.path + "/") for item in required)
    }
    current_entries = {entry["path"]: (entry["size"], entry["sha256"]) for entry in current["entries"]}
    if current_entries != expected_entries:
        raise ValueError("ingest_required_artifact_manifest_mismatch")
    contract = metadata.get("adapter_contract") or {}
    if contract.get("summary_path"):
        from harness.remote.adapters.common import confined_path, load_structured_object
        summary_path = confined_path(run_root, contract["summary_path"], "summary_path")
        summary = load_structured_object(summary_path, "summary", contract.get("summary_format", "json"))
        if _explicit_failure(summary):
            raise ValueError("ingest_completion_conflicts_with_summary_failure")
    # Provenance + hashes establish identity, not a passed remote completion contract.
    # Old healthy ingests remain readable, but absence of a receipt stays unknown, never True.
    passed = None
    receipt_path = run_root / ".rrctl/completion.json"
    # Legacy root receipts are only control evidence if not declared workload output.
    legacy_receipt = run_root / "completion.json"
    if not receipt_path.exists() and legacy_receipt.is_file() and not any(
            entry["path"] == "completion.json" for entry in manifest["entries"]):
        receipt_path = legacy_receipt
    if receipt_path.is_file():
        receipt = _evidence_json(receipt_path, project_root)
        binding = _evidence_json(receipt_path.parent / "binding.json", project_root)
        status = _status_payload(_evidence_json(receipt_path.parent / "status.json", project_root))
        from remote_run_control.jsonutil import sha256_json
        identity = sha256_json({"run_id": run_id, "run_spec_sha256": expected["run_spec_sha256"],
                               "control_root": spec["remote"]["control_root"],
                               "worker_sha256": binding.get("worker_sha256"),
                               "created_at": binding.get("created_at")})
        health = receipt.get("health") or {}
        if (binding.get("run_id") != run_id
                or binding.get("run_spec_sha256") != expected["run_spec_sha256"]
                or binding.get("control_root") != spec["remote"]["control_root"]
                or status.get("run_id") != run_id or status.get("state") != "completed"
                or _explicit_failure(status) or _explicit_failure(status.get("detail") or {})
                or (status.get("detail") or {}).get("completion_sha256") != sha256_json(receipt)
                or receipt.get("run_identity") != identity
                or receipt.get("schema_version") != "rrctl.completion.v1"
                or receipt.get("run_id") != run_id
                or receipt.get("run_spec_sha256") != expected["run_spec_sha256"]
                or receipt.get("manifest_sha256") != sha256_json(manifest)
                or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0
                or health.get("schema_version") != "rrctl.health.v2"
                or health.get("phase") != "completion"
                or health.get("healthy") is not True or health.get("complete") is not True):
            raise ValueError("ingest_completion_receipt_invalid")
        passed = True
        terminal = {"state": "completed", "exit_code": 0,
                    "evidence": [{"state": "completed", "exit_code": 0,
                                  "path": (receipt_path.parent / "status.json").relative_to(project_root).as_posix()}]}
    return spec, {**manifest, "evidence_mode": "completion", "terminal": terminal,
                  "completion_contract_passed": passed, "output_base": str(run_root)}


def runs_match_projection(stored: object, projected: list[dict]) -> bool:
    """Read pre-evidence-metadata healthy records without rewriting historical ledgers.

    Omission means unknown, not a positive completion claim. Recovery/failure requires
    explicit metadata; all pre-existing identity, metric and artifact fields stay exact.
    """
    if not isinstance(stored, list) or len(stored) != len(projected):
        return False
    metadata_fields = {"evidence_mode", "terminal", "completion_contract_passed"}
    for previous, current in zip(stored, projected):
        if not isinstance(previous, dict):
            return False
        missing = metadata_fields - previous.keys()
        if missing:
            if (current.get("evidence_mode") != "completion"
                    or current.get("completion_contract_passed") is False
                    or _explicit_failure(current.get("terminal") or {})):
                return False
            current = {key: value for key, value in current.items() if key not in missing}
        if previous != current:
            return False
    return True


def _read_run_projection(
    exp_id: str, settings: dict | None = None, *,
    repo_root: Path | None = None, artifacts: Path | None = None,
    projection: dict | None = None,
) -> tuple[list[dict], dict, list[str]]:
    """只投影来源完整的 Run；历史缺口留在 _pending，不混入正式指标。

    RunSpec 合同未声明标量指标时（矩阵/链式 pipeline 的摘要是控制摘要），
    该 Run 仍以 `metric=None` 入册，标量指标留在 _pending。
    """
    project_root = (repo_root or REPO_ROOT).resolve()
    artifacts_dir = artifacts.resolve() if artifacts else ARTIFACTS.resolve()
    root = artifacts_dir / identifier(exp_id)
    settings = settings or {}
    runs: list[dict] = []
    sources = {key: set() for key in ("spec_id", "branch", "commit", "mission_csv")}
    pending: list[str] = []
    if projection is None or "csv" not in projection:
        projection = csv_projection(project_root).get(exp_id, {})
    csv_paths = {(project_root / path).resolve() for path in projection.get("csv", [])}
    if not root.resolve().is_relative_to(project_root):
        return runs, sources, ["runs.provenance: artifact experiment path outside workspace"]
    run_roots = sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")) if root.is_dir() else []
    missing = set(projection.get("run_id", [])) - {path.name for path in run_roots}
    pending.extend(f"runs.{run_id}.provenance: artifacts missing" for run_id in sorted(missing))
    for run_root in run_roots:
        try:
            if run_root.is_symlink() or not run_root.resolve().is_relative_to(project_root):
                raise ValueError("artifact run path escapes its workspace")
            candidates = [path for path in csv_paths if (path.parent / "runs" / run_root.name / "runspec.json").is_file()]
            if len(candidates) != 1:
                raise ValueError("RunSpec missing or ambiguous for this experiment")
            csv_path = candidates[0]
            spec, manifest = load_run_provenance(csv_path, exp_id, run_root.name,
                                                 repo_root=project_root, artifacts=root.parent)
            provenance = manifest["provenance"]
            artifact_base = Path(manifest["output_base"])
            # 该 Run 自己的 RunSpec 合同才是「摘要该有哪些字段」的权威声明。
            # 合同没有要求标量指标时，摘要里没有它只说明该 pipeline 不产出单一指标
            # （例如 matrix_chain 的链摘要，逐臂结果在 evaluate/ 下），
            # 不足以丢弃这个 Run 的身份与产物证据。
            contract = spec["metadata"].get("adapter_contract") or {}
            declared_metric = settings.get("primary_metric", "metric") in tuple(
                contract.get("summary_required_fields") or ())
            run_results: list[dict] = []
            for summary in sorted(artifact_base.glob(settings.get("summary_glob", "summary.json"))):
                if "diagnostics" in summary.relative_to(artifact_base).parts:
                    continue
                if not summary.resolve().is_relative_to(artifact_base.resolve()) or summary.is_symlink():
                    raise ValueError(f"summary path escapes its run: {summary}")
                summary_bytes = summary.read_bytes()
                relative = summary.relative_to(artifact_base).as_posix()
                matches = [entry for entry in manifest["entries"] if isinstance(entry, dict) and entry.get("path") == relative]
                if (len(matches) != 1 or matches[0].get("sha256") != hashlib.sha256(summary_bytes).hexdigest()
                        or matches[0].get("size") != len(summary_bytes)):
                    raise ValueError(f"summary does not match artifact manifest: {summary}")
                data = json.loads(summary_bytes)
                if not isinstance(data, dict):
                    raise ValueError(f"summary must be an object: {summary}")
                if manifest["evidence_mode"] == "completion" and _explicit_failure(data):
                    raise ValueError(f"ingest_completion_conflicts_with_summary_failure: {summary}")
                identities = {"run_id": run_root.name, **{key: provenance[key] for key in (
                    "exp_id", "spec_id", "commit", "run_spec_sha256")}}
                if any(data.get(key) not in (None, value) for key, value in identities.items()):
                    raise ValueError(f"summary identity mismatch: {summary}")
                try:
                    metric = dotted_value(data, settings.get("primary_metric", "metric"), "summary")
                except AdapterContractError:
                    # 合同声明了该指标时缺失仍是硬错；合同未声明时按「无标量指标」投影。
                    if declared_metric:
                        raise
                    metric = None
                else:
                    # 字段存在但不是有限标量（含 null / NaN / 字符串）仍硬错。
                    if (metric is None or isinstance(metric, bool)
                            or not isinstance(metric, (int, float)) or not math.isfinite(metric)):
                        raise ValueError(f"summary primary metric is not finite: {summary}")
                auxiliary = settings.get("secondary_metric")
                # 辅助指标与主指标遵循同一条合同感知规则：RunSpec 合同未声明必需时，
                # 缺失只说明该 pipeline 不产出该辅助指标（如训练/探针 Run），属于「无值」，
                # 投影为 None；合同声明必需时仍然硬失败。字段存在但值非法（null/NaN/字符串）仍报错。
                declared_auxiliary = bool(auxiliary) and auxiliary in tuple(
                    contract.get("summary_required_fields") or ()
                )
                # 只在合同未声明标量指标时容许维度缺失；指标在册时维度仍必须存在，
                # 避免该实验的协议/维度拼写错误被静默降级成 None。
                dimension_fields = settings.get("dimensions", [])
                dimensions = ({k: dotted_value(data, k, "summary") for k in dimension_fields}
                              if declared_metric
                              else {k: optional_summary_value(data, k) for k in dimension_fields})
                if auxiliary:
                    try:
                        metric_aux_value = dotted_value(data, auxiliary, "summary")
                    except AdapterContractError:
                        if declared_auxiliary:
                            raise
                        metric_aux_value = None
                    else:
                        if metric_aux_value is not None and (
                            isinstance(metric_aux_value, bool)
                            or not isinstance(metric_aux_value, (int, float))
                            or not math.isfinite(metric_aux_value)
                        ):
                            raise ValueError(
                                f"summary auxiliary metric is not finite: {summary}"
                            )
                else:
                    metric_aux_value = None
                protocol = optional_summary_value(data, settings.get("protocol_field", "protocol"))
                if protocol is not None and (not isinstance(protocol, str) or not protocol.strip()):
                    raise ValueError(f"summary protocol must be non-empty text: {summary}")
                run_results.append({
                    **dimensions,
                    "run_id": run_root.name,
                    "summary_path": summary.relative_to(project_root).as_posix(),
                    "eval_dir": summary.parent.relative_to(project_root).as_posix(),
                    "dimensions": dimensions,
                    "protocol": protocol,
                    "metric": metric,
                    "steps": optional_summary_value(data, settings.get("steps_field", "steps")),
                    "metric_aux": metric_aux_value,
                    "weights_path": optional_summary_value(data, settings.get("checkpoint_field", "checkpoint_path")),
                    "commit": spec["source"]["commit"],
                    "run_spec_sha256": provenance["run_spec_sha256"],
                    "evidence_mode": manifest["evidence_mode"],
                    "terminal": manifest["terminal"],
                    "completion_contract_passed": manifest["completion_contract_passed"],
                    **({key: manifest[key] for key in (
                        "recovery_id", "recovery_reason", "recovery_binding_path", "recovery_evidence")}
                       if manifest["evidence_mode"] == "recovery" else {}),
                    **({"pipeline_name": spec["metadata"]["pipeline_name"]} if spec["metadata"].get("pipeline_name") else {}),
                })
            if not run_results:
                raise ValueError("summary missing for this run")
            runs.extend(run_results)
            for key, value in (("spec_id", spec["metadata"]["spec_id"]), ("branch", spec["source"]["branch"]),
                               ("commit", spec["source"]["commit"]), ("mission_csv", csv_path.relative_to(project_root).as_posix())):
                sources[key].add(value)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, AdapterContractError, RunSpecBuildError) as exc:
            pending.append(f"runs.{run_root.name}.provenance: {exc}")
    return runs, sources, pending


def read_runs(
    exp_id: str, settings: dict | None = None, *,
    repo_root: Path | None = None, artifacts: Path | None = None,
) -> list[dict]:
    return _read_run_projection(exp_id, settings, repo_root=repo_root, artifacts=artifacts)[0]


def run_metric_projection(runs: list[dict]) -> dict:
    protocols = {run["protocol"] for run in runs}
    protocol = next(iter(protocols)) if len(protocols) == 1 and None not in protocols else None
    # summary 是结果粒度，多份结果没有聚合合同就不计算总体指标。
    return {"protocol": protocol, "ours_metric": runs[0]["metric"] if len(runs) == 1 else None}


def build_record(
    exp_id: str, proj: dict | None, settings: dict | None = None, *,
    repo_root: Path | None = None, artifacts: Path | None = None,
) -> dict:
    projection = proj if proj is not None and "csv" in proj else csv_projection(repo_root).get(exp_id, {})
    runs, source, gaps = _read_run_projection(exp_id, settings, repo_root=repo_root,
                                             artifacts=artifacts, projection=projection)
    proj = proj or {}
    projected = run_metric_projection(runs)
    protocol, ours_metric = projected["protocol"], projected["ours_metric"]
    if set(projection.get("run_id", [])) - {run["run_id"] for run in runs}:
        # 缺少已登记的正式 Run 时保留单次结果，不把残余一次冒充实验级指标。
        ours_metric = None
    pending = [field for field in PENDING_FIELDS if field != "metrics.protocol" or protocol is None]
    if ours_metric is None:
        pending.append("metrics.ours_metric")
    pending.extend(gaps)
    record = {
        "exp_id": exp_id,
        "parent": None,
        "relation": None,
        "source": {key: sorted(values) or None for key, values in source.items()},
        "metrics": {
            "protocol": protocol,
            "baseline_id": None,
            "baseline_run_id": None,
            "baseline_metric": None,
            "ours_metric": ours_metric,
            "delta_metric": None,
        },
        "runs": runs,
        "outcome": "pending",
        "artifact_path": f"remote_artifacts/{exp_id}/" if ((artifacts or ARTIFACTS) / exp_id).exists() else None,
        "next_action": proj.get("next_action") or None,
        "_pending": pending,
        "_generated_by": GENERATOR,
        "_projection_version": 2,
    }
    return record


def _valid_outcome_metadata(previous: dict, current: dict, *, repo_root: Path | None = None) -> bool:
    outcome = previous.get("outcome")
    meta = previous.get("outcome_meta")
    if outcome not in RESULT_ANALYSIS_OUTCOMES or not isinstance(meta, dict):
        return False
    if meta.get("schema_version") != OUTCOME_META_SCHEMA:
        return False
    run_ids = meta.get("run_ids")
    current_run_ids = _record_run_ids(current)
    if (current_run_ids is None or not isinstance(run_ids, list)
            or any(not isinstance(run_id, str) or not run_id for run_id in run_ids)
            or len(run_ids) != len(set(run_ids))
            or sorted(run_ids) != current_run_ids):
        return False
    if not all(isinstance(meta.get(key), str) and meta[key].strip()
               for key in ("source", "csv_source", "entries_sha256", "runs_sha256", "review_evidence_ref", "review_output_sha256")):
        return False
    if (not _SHA256_RE.fullmatch(meta["entries_sha256"])
            or not _SHA256_RE.fullmatch(meta["runs_sha256"])
            or not _SHA256_RE.fullmatch(meta["review_output_sha256"])
            or meta["runs_sha256"] != _runs_digest(current)):
        return False
    root = (repo_root or REPO_ROOT).resolve()
    try:
        source = (root / meta["source"]).resolve()
        csv_source = (root / meta["csv_source"]).resolve()
        if (not source.is_relative_to(root) or not csv_source.is_relative_to(root)
                or not source.is_file() or not csv_source.is_file()):
            return False
        validator_path = root / ".codex/skills/post-run-result-analysis/scripts/validate_result_analysis.py"
        if not validator_path.is_file():
            validator_path = root / ".agents/skills/post-run-result-analysis/scripts/validate_result_analysis.py"
        if not validator_path.is_file():
            return False
        import importlib.util
        spec = importlib.util.spec_from_file_location("result_analysis_validator_for_records", validator_path)
        if spec is None or spec.loader is None:
            return False
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        if validator.validate_index(source, csv_source, workdir=root):
            return False
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError):
        return False
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return False
    relevant = [entry for entry in entries
                if isinstance(entry, dict) and entry.get("exp_id") == current.get("exp_id")]
    relevant_run_ids = [entry.get("run_id") for entry in relevant]
    if (any(not isinstance(run_id, str) or not run_id for run_id in relevant_run_ids)
            or sorted(relevant_run_ids) != current_run_ids
            or len(relevant_run_ids) != len(set(relevant_run_ids))):
        return False
    formal_run_ids = set(csv_projection(root).get(current.get("exp_id"), {}).get("run_id", set()))
    pending = current.get("_pending")
    if formal_run_ids and set(current_run_ids) != formal_run_ids:
        return False
    if isinstance(pending, list) and any(isinstance(item, str) and item.startswith("runs.") for item in pending):
        return False
    if {entry.get("scientific_outcome") for entry in relevant} != {outcome}:
        return False
    review_refs = {entry.get("review_evidence_ref") for entry in relevant}
    review_hashes = {entry.get("review_output_sha256") for entry in relevant}
    if (len(review_refs) != 1 or meta["review_evidence_ref"] not in review_refs
            or len(review_hashes) != 1 or meta["review_output_sha256"] not in review_hashes):
        return False
    ordered = sorted(relevant, key=lambda entry: entry["run_id"])
    digest = hashlib.sha256(
        json.dumps(ordered, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest == meta["entries_sha256"]


def _write_if_changed(path: Path, content: bytes) -> bool:
    if path.is_file() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def cmd_build(args, *, repo_root: Path | None = None) -> int:
    project_root = (repo_root or REPO_ROOT).resolve()
    artifacts_root = project_root / "remote_artifacts"
    experiments_root = project_root / "research_workspace" / "experiments"
    if (not artifacts_root.resolve().is_relative_to(project_root)
            or not experiments_root.resolve().is_relative_to(project_root)):
        raise ValueError("record projection paths outside project root")
    proj = csv_projection(project_root)
    config_path = Path(getattr(args, "config", DEFAULT_CONFIG))
    if not config_path.is_absolute():
        config_path = project_root / config_path
    settings = load_config(config_path).get("records", {}) if config_path.is_file() else {}
    known = {p.name for p in artifacts_root.iterdir() if p.is_dir() and not p.name.startswith(".")} if artifacts_root.is_dir() else set()
    targets = sorted(known | set(proj)) \
        if not args.exp else [args.exp]
    written = skipped = 0
    for exp_id in targets:
        out = experiments_root / identifier(exp_id) / "record.json"
        if not out.resolve().is_relative_to(project_root):
            raise ValueError(f"record output outside project root: {out}")
        # 无参模式保持只补建；显式选择实验才自动刷新，--force 保留批量兼容。
        if out.exists() and not args.exp and not args.force:
            skipped += 1
            continue
        record = build_record(exp_id, proj.get(exp_id), settings,
                              repo_root=project_root, artifacts=artifacts_root)
        if not record["runs"] and not record["artifact_path"] and not out.exists():
            skipped += 1
            continue
        if out.exists():
            previous = json.loads(out.read_text(encoding="utf-8"))
            if not isinstance(previous, dict) or previous.get("_generated_by") != GENERATOR or previous.get("exp_id") != exp_id or previous.get("_projection_version", 1) not in (1, 2):
                raise ValueError(f"record requires explicit migration: {out}")
            if "outcome_meta" in previous:
                record["outcome_meta"] = None
            for old, new in ((previous, record), (previous.get("source", {}), record["source"]), (previous.get("metrics", {}), record["metrics"])):
                if not isinstance(old, dict) or set(old) - set(new):
                    raise ValueError(f"record contains unknown fields; preserve before migration: {out}")
            dimensions = set(settings.get("dimensions", []))
            run_fields = {"run_id", "summary_path", "eval_dir", "dimensions", "protocol", "metric",
                          "steps", "metric_aux", "weights_path", "commit", "run_spec_sha256", "pipeline_name",
                          "evidence_mode", "terminal", "completion_contract_passed", "recovery_id",
                          "recovery_reason", "recovery_binding_path", "recovery_evidence"} | dimensions
            old_runs = previous.get("runs", [])
            if not isinstance(old_runs, list):
                raise ValueError(f"record runs require explicit migration: {out}")
            for run in old_runs:
                if (not isinstance(run, dict) or set(run) - run_fields
                        or not isinstance(run.get("dimensions", {}), dict)
                        or set(run.get("dimensions", {})) - dimensions):
                    raise ValueError(f"record contains unknown run fields; preserve before migration: {out}")
            if _valid_outcome_metadata(previous, record, repo_root=project_root):
                record["outcome"] = previous["outcome"]
                record["outcome_meta"] = previous["outcome_meta"]
                record["_pending"] = [field for field in record["_pending"] if field != "outcome"]
            if previous == record:
                skipped += 1
                continue
        changed = _write_if_changed(out, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        written += int(changed)
        skipped += int(not changed)
    print(f"record.json 写入 {written} 个，跳过 {skipped} 个")
    return 0


def record_index_row(r: dict) -> dict:
    m = r.get("metrics", {})
    return {
        "ExpID": r["exp_id"],
        "Parent": r.get("parent") or "",
        "Relation": r.get("relation") or "",
        "SpecID": ";".join(r["source"].get("spec_id") or []),
        "Commit": ";".join(r["source"].get("commit") or []),
        "Protocol": m.get("protocol") or "",
        "BaselineID": m.get("baseline_id") or "",
        "OursMetric": m.get("ours_metric") if m.get("ours_metric") is not None else "",
        "DeltaMetric": m.get("delta_metric") if m.get("delta_metric") is not None else "",
        "Runs": len({run["run_id"] for run in r.get("runs", []) if run.get("run_id")}),
        "Outcome": r.get("outcome") or "",
        "ArtifactPath": r.get("artifact_path") or "",
    }


def cmd_derive(args, *, repo_root: Path | None = None) -> int:
    project_root = (repo_root or REPO_ROOT).resolve()
    experiments_root = project_root / "research_workspace" / "experiments"
    ledger_path = project_root / "research_workspace" / "EXPERIMENTS.csv"
    if (not experiments_root.resolve().is_relative_to(project_root)
            or not ledger_path.resolve().is_relative_to(project_root)):
        raise ValueError("records paths outside project root")
    rows = []
    for rec_path in sorted(experiments_root.glob("*/record.json")):
        if not rec_path.resolve().is_relative_to(project_root):
            raise ValueError(f"record path outside project root: {rec_path}")
        r = json.loads(rec_path.read_text(encoding="utf-8"))
        rows.append(record_index_row(r))
    if not rows:
        print("没有 record.json，先跑 build")
        return 1
    output = io.StringIO(newline="")
    w = csv.DictWriter(output, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    _write_if_changed(ledger_path, output.getvalue().encode("utf-8"))
    print(f"EXPERIMENTS.csv 派生 {len(rows)} 行 -> {ledger_path.relative_to(project_root)}")
    return 0


def apply_result_analysis_outcomes(
    index_path: Path,
    *,
    repo_root: Path,
    allowed_outcomes: set[str] | frozenset[str] | None = None,
    csv_path: Path | None = None,
    stderr=None,
) -> tuple[int, int]:
    """将已通过验证的 RESULT-ANALYSIS 按 ExpID 原子回写并派生 ledger。

    只有同一 ExpID 的全部 RunID 都被覆盖且 scientific_outcome 一致时才写入；
    已有非 pending 判定不覆盖。派生始终执行，以便重试此前失败的 ledger 写入。
    """
    import sys as _sys
    stream = stderr or _sys.stderr
    root = Path(repo_root).resolve()
    index = Path(index_path).resolve()
    if not index.is_relative_to(root):
        print(f"result_analysis_sync: skip (index outside repo: {index})", file=stream)
        return (0, 0)
    try:
        payload = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"result_analysis_sync: skip (cannot read index): {exc}", file=stream)
        return (0, 0)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        print("result_analysis_sync: skip (entries missing)", file=stream)
        return (0, 0)
    valid_outcomes = allowed_outcomes or RESULT_ANALYSIS_OUTCOMES
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        exp_id = entry.get("exp_id")
        if not isinstance(exp_id, str) or not exp_id.strip():
            continue
        try:
            exp_id = identifier(exp_id.strip())
        except ValueError:
            print(f"result_analysis_sync: skip ({exp_id!r} invalid ExpID)", file=stream)
            continue
        grouped.setdefault(exp_id, []).append(entry)

    experiments_root = root / "research_workspace" / "experiments"
    patched = skipped = 0
    for exp_id, exp_entries in sorted(grouped.items()):
        run_ids = [entry.get("run_id") for entry in exp_entries]
        if (any(not isinstance(run_id, str) or not run_id.strip() for run_id in run_ids)
                or len(run_ids) != len(set(run_ids))):
            print(f"result_analysis_sync: skip ({exp_id} duplicate or invalid RunID)", file=stream)
            skipped += 1
            continue
        outcomes = {entry.get("scientific_outcome") for entry in exp_entries}
        if len(outcomes) != 1 or next(iter(outcomes), None) not in valid_outcomes:
            print(f"result_analysis_sync: skip ({exp_id} inconsistent outcome)", file=stream)
            skipped += 1
            continue
        record_dir = experiments_root / exp_id
        record_path = record_dir / "record.json"
        try:
            if (not record_dir.resolve().is_relative_to(root)
                    or not record_path.resolve().is_relative_to(root)):
                print(f"result_analysis_sync: skip ({exp_id} record path outside repo)", file=stream)
                skipped += 1
                continue
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"result_analysis_sync: skip ({exp_id} record.json not found)", file=stream)
            skipped += 1
            continue
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(f"result_analysis_sync: skip ({exp_id} record.json unreadable): {exc}", file=stream)
            skipped += 1
            continue
        if not isinstance(record, dict):
            print(f"result_analysis_sync: skip ({exp_id} record.json not a dict)", file=stream)
            skipped += 1
            continue
        if record.get("exp_id") != exp_id:
            print(f"result_analysis_sync: skip ({exp_id} record identity mismatch)", file=stream)
            skipped += 1
            continue
        formal_run_ids = set(csv_projection(root).get(exp_id, {}).get("run_id", set()))
        pending = record.get("_pending")
        if isinstance(pending, list) and any(
                isinstance(item, str) and item.startswith("runs.") for item in pending):
            print(f"result_analysis_sync: skip ({exp_id} formal Run evidence incomplete)", file=stream)
            skipped += 1
            continue
        record_run_ids = _record_run_ids(record)
        if record_run_ids is None:
            print(f"result_analysis_sync: skip ({exp_id} record RunID invalid or duplicated)", file=stream)
            skipped += 1
            continue
        if sorted(run_ids) != record_run_ids or (formal_run_ids and set(record_run_ids) != formal_run_ids):
            print(f"result_analysis_sync: skip ({exp_id} RunID coverage mismatch)", file=stream)
            skipped += 1
            continue
        current = record.get("outcome")
        if not _is_pending_outcome(current):
            print(f"result_analysis_sync: skip ({exp_id} outcome already {current!r}; not overwriting)", file=stream)
            skipped += 1
            continue
        ordered_entries = sorted(exp_entries, key=lambda entry: entry["run_id"])
        entries_sha256 = hashlib.sha256(
            json.dumps(ordered_entries, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        review_refs = {entry.get("review_evidence_ref") for entry in ordered_entries}
        review_hashes = {entry.get("review_output_sha256") for entry in ordered_entries}
        if (len(review_refs) != 1 or not next(iter(review_refs), None)
                or len(review_hashes) != 1 or not next(iter(review_hashes), None)):
            print(f"result_analysis_sync: skip ({exp_id} review evidence mismatch)", file=stream)
            skipped += 1
            continue
        record["outcome"] = next(iter(outcomes))
        csv_source = ""
        if csv_path is not None:
            candidate_csv = Path(csv_path).resolve()
            if not candidate_csv.is_file() or not candidate_csv.is_relative_to(root):
                print(f"result_analysis_sync: skip ({exp_id} CSV outside repo or missing)", file=stream)
                skipped += 1
                continue
            csv_source = candidate_csv.relative_to(root).as_posix()
        record["outcome_meta"] = {
            "schema_version": OUTCOME_META_SCHEMA,
            "source": index.relative_to(root).as_posix(),
            "csv_source": csv_source,
            "entries_sha256": entries_sha256,
            "runs_sha256": _runs_digest(record),
            "run_ids": sorted(set(run_ids)),
            "review_evidence_ref": next(iter(review_refs)),
            "review_output_sha256": next(iter(review_hashes)),
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "applied_by": "validate_result_analysis",
        }
        pending = record.get("_pending")
        if isinstance(pending, list):
            record["_pending"] = [field for field in pending if field != "outcome"]
        try:
            _write_if_changed(record_path, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        except OSError as exc:
            print(f"result_analysis_sync: warn (cannot patch {exp_id}): {exc}", file=stream)
            skipped += 1
            continue
        patched += 1

    try:
        derive_status = cmd_derive(None, repo_root=root)
        if derive_status != 0:
            print(f"result_analysis_sync: warn (derive exit={derive_status})", file=stream)
    except Exception as exc:
        print(f"result_analysis_sync: warn (derive failed): {exc}", file=stream)
    print(f"result_analysis_sync: patched={patched} skipped={skipped}", file=stream)
    return patched, skipped


def cmd_bind_recovery(args, *, repo_root: Path | None = None) -> int:
    """Verify a pulled rrctl recovery snapshot and write derived bindings only.

    First use rrctl recover-artifacts to a fresh remote_artifacts/<ExpID>/<RunID>;
    retain the matching controller status (and sibling binding when status has no digest).
    This command never changes the snapshot, RunSpec, summary or remote state.
    """
    root = (repo_root or REPO_ROOT).resolve()
    csv_path = Path(args.csv).absolute()
    if not csv_path.is_relative_to(root) or not csv_path.is_file():
        raise ValueError("recovery_csv_outside_workspace_or_missing")
    run_id, exp_id = identifier(args.run), identifier(args.exp)
    spec = canonical_run_spec(_evidence_json(csv_path.parent / "runs" / run_id / "runspec.json", root))
    metadata = spec["metadata"]
    if (spec["run_id"] != run_id or metadata.get("exp_id") != exp_id
            or not metadata.get("spec_id")
            or (root / metadata.get("mission_csv", "")).resolve() != csv_path.resolve()
            or metadata.get("execution_purpose") in {"pre_review_smoke", "preregistered_read_only_probe"}):
        raise ValueError("recovery_runspec_identity_mismatch")
    from remote_run_control.artifacts import load_artifact_manifest
    from remote_run_control.errors import RRCError
    run_root = root / "remote_artifacts" / exp_id / run_id
    manifest_path = run_root / "artifact_manifest.json"
    _evidence_json(manifest_path, root)
    loaded = load_artifact_manifest(manifest_path, expected_run_id=run_id)
    status_path = Path(args.status).absolute()
    _evidence_json(status_path, root)
    terminal = _terminal_evidence(csv_path, run_root, spec, root, (status_path,))
    expected = {"spec_id": metadata["spec_id"], "exp_id": exp_id,
                "commit": spec["source"]["commit"], "run_spec_sha256": run_spec_digest(spec)}
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    contract = metadata.get("adapter_contract") or {}
    report = {"schema": RECOVERY_COVERAGE_SCHEMA, "status": "passed", "run_id": run_id,
              "run_spec_sha256": expected["run_spec_sha256"], "manifest_sha256": manifest_hash,
              "required_artifacts": sorted(item["path"] for item in spec["artifacts"] if item["required"]),
              "verified_entries": loaded["entries"]}
    if any(contract.get(key) is not None for key in ("completion_min_count", "completion_exact_count")):
        from harness.remote.adapters.common import confined_path, load_structured_object
        progress = load_structured_object(confined_path(run_root / "output", contract["progress_path"], "progress_path"),
                                          "progress", contract.get("progress_format", "json"))
        report.update(count_field=contract["progress_count_field"],
                      verified_count=dotted_value(progress, contract["progress_count_field"], "progress"))
    encode = lambda value: (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    report_bytes = encode(report)
    report_path = csv_path.parent / "validation" / f"recovery-coverage-{run_id}.json"
    binding_path = csv_path.parent / "validation/recovery-binding.json"
    payload = (_evidence_json(binding_path, root) if binding_path.exists()
               else {"schema": RECOVERY_BINDING_SCHEMA, "bindings": []})
    if any(item.get("run_id") == run_id for item in payload.get("bindings", [])):
        raise ValueError("recovery_binding_already_exists")
    if report_path.exists() or report_path.is_symlink():
        raise ValueError("recovery_coverage_already_exists")
    entry = {"run_id": run_id, "run_spec_sha256": expected["run_spec_sha256"],
             "manifest_sha256": manifest_hash, "recovery_id": loaded.get("recovery_id"), "reason": args.reason,
             "status_evidence": {"path": os.path.relpath(status_path, csv_path.parent),
                                 "sha256": hashlib.sha256(status_path.read_bytes()).hexdigest()},
             "coverage_report": {"path": report_path.relative_to(csv_path.parent).as_posix(),
                                 "sha256": hashlib.sha256(report_bytes).hexdigest()}}
    payload = {**payload, "bindings": [*payload.get("bindings", []), entry]}
    try:
        _recovery_projection(csv_path, run_root, spec, loaded, expected, root, terminal, proposed=(payload, report))
    except RRCError as exc:
        raise ValueError(f"recovery_artifact_invalid:{exc.code}") from exc
    # Validation completed before either derived file is published.
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if report_path.parent.is_symlink() or not report_path.parent.resolve().is_relative_to(root):
        raise ValueError("recovery_derived_path_invalid")
    _write_if_changed(report_path, report_bytes)
    _write_if_changed(binding_path, encode(payload))
    print(json.dumps({"ok": True, "binding": str(binding_path), "coverage": str(report_path),
                      "completion_contract_passed": False}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    recovery = sub.add_parser("bind-recovery", help="验证 rrctl 恢复快照并生成绑定；不更改原始工件或宣称成功")
    recovery.add_argument("--csv", type=Path, required=True)
    recovery.add_argument("--exp", required=True)
    recovery.add_argument("--run", required=True)
    recovery.add_argument("--status", type=Path, required=True, help="保留的控制面终态；须含 RunSpec digest 或同目录 binding.json")
    recovery.add_argument("--reason", required=True)
    recovery.set_defaults(func=cmd_bind_recovery)
    b = sub.add_parser("build", help="生成 record.json")
    b.add_argument("--exp", help="重算并刷新一个 ExpID；省略时只补建缺失 record")
    b.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="project TOML containing records field mapping")
    b.add_argument("--force", action="store_true", help="无参模式也刷新已有的生成器 record；不绕过格式校验")
    b.set_defaults(func=cmd_build)
    d = sub.add_parser("derive", help="从 record.json 派生 EXPERIMENTS.csv")
    d.set_defaults(func=cmd_derive)
    args = ap.parse_args()
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError, AdapterContractError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
