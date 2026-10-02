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
                        acc["run_id"].add(run_id)
                na = (row.get("next_action") or "").strip()
                if na and na not in acc["next_action"]:
                    acc["next_action"].append(na)
    return out


def load_run_provenance(
    csv_path: Path, exp_id: str, run_id: str, *, repo_root: Path,
    artifacts: Path | None = None,
) -> tuple[dict, dict]:
    """共用的 RunSpec → manifest 校验；不依赖 record 或 ingested 状态。"""
    project_root = repo_root.resolve()
    csv_path = csv_path.resolve()
    spec_path = csv_path.parent / "runs" / identifier(run_id) / "runspec.json"
    run_root = (artifacts or project_root / "remote_artifacts") / identifier(exp_id) / run_id
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
    return spec, manifest


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
    root = (artifacts or ARTIFACTS) / identifier(exp_id)
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
            # 该 Run 自己的 RunSpec 合同才是「摘要该有哪些字段」的权威声明。
            # 合同没有要求标量指标时，摘要里没有它只说明该 pipeline 不产出单一指标
            # （例如 matrix_chain 的链摘要，逐臂结果在 evaluate/ 下），
            # 不足以丢弃这个 Run 的身份与产物证据。
            contract = spec["metadata"].get("adapter_contract") or {}
            declared_metric = settings.get("primary_metric", "metric") in tuple(
                contract.get("summary_required_fields") or ())
            run_results: list[dict] = []
            for summary in sorted(run_root.glob(settings.get("summary_glob", "summary.json"))):
                if "diagnostics" in summary.relative_to(run_root).parts:
                    continue
                if not summary.resolve().is_relative_to(run_root.resolve()) or summary.is_symlink():
                    raise ValueError(f"summary path escapes its run: {summary}")
                summary_bytes = summary.read_bytes()
                relative = summary.relative_to(run_root).as_posix()
                matches = [entry for entry in manifest["entries"] if isinstance(entry, dict) and entry.get("path") == relative]
                if (len(matches) != 1 or matches[0].get("sha256") != hashlib.sha256(summary_bytes).hexdigest()
                        or matches[0].get("size") != len(summary_bytes)):
                    raise ValueError(f"summary does not match artifact manifest: {summary}")
                data = json.loads(summary_bytes)
                if not isinstance(data, dict):
                    raise ValueError(f"summary must be an object: {summary}")
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
                # 只在合同未声明标量指标时容许维度缺失；指标在册时维度仍必须存在，
                # 避免该实验的协议/维度拼写错误被静默降级成 None。
                dimension_fields = settings.get("dimensions", [])
                dimensions = ({k: dotted_value(data, k, "summary") for k in dimension_fields}
                              if declared_metric
                              else {k: optional_summary_value(data, k) for k in dimension_fields})
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
                    "metric_aux": dotted_value(data, auxiliary, "summary") if auxiliary else None,
                    "weights_path": optional_summary_value(data, settings.get("checkpoint_field", "checkpoint_path")),
                    "commit": spec["source"]["commit"],
                    "run_spec_sha256": provenance["run_spec_sha256"],
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
               for key in ("source", "entries_sha256", "runs_sha256", "review_evidence_ref", "review_output_sha256")):
        return False
    if (not _SHA256_RE.fullmatch(meta["entries_sha256"])
            or not _SHA256_RE.fullmatch(meta["runs_sha256"])
            or not _SHA256_RE.fullmatch(meta["review_output_sha256"])
            or meta["runs_sha256"] != _runs_digest(current)):
        return False
    root = (repo_root or REPO_ROOT).resolve()
    try:
        source = (root / meta["source"]).resolve()
        if not source.is_relative_to(root):
            return False
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
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
                          "steps", "metric_aux", "weights_path", "commit", "run_spec_sha256", "pipeline_name"} | dimensions
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
        record_run_ids = _record_run_ids(record)
        if record_run_ids is None:
            print(f"result_analysis_sync: skip ({exp_id} record RunID invalid or duplicated)", file=stream)
            skipped += 1
            continue
        if sorted(run_ids) != record_run_ids:
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
        record["outcome_meta"] = {
            "schema_version": OUTCOME_META_SCHEMA,
            "source": index.relative_to(root).as_posix(),
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
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
