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
    """只投影来源完整的 Run；历史缺口留在 _pending，不混入正式指标。"""
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
                metric = dotted_value(data, settings.get("primary_metric", "metric"), "summary")
                if isinstance(metric, bool) or not isinstance(metric, (int, float)) or not math.isfinite(metric):
                    raise ValueError(f"summary primary metric is not finite: {summary}")
                auxiliary = settings.get("secondary_metric")
                dimensions = {k: dotted_value(data, k, "summary") for k in settings.get("dimensions", [])}
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


def cmd_build(args) -> int:
    proj = csv_projection()
    config_path = getattr(args, "config", DEFAULT_CONFIG)
    settings = load_config(config_path).get("records", {}) if config_path.is_file() else {}
    known = {p.name for p in ARTIFACTS.iterdir() if p.is_dir() and not p.name.startswith(".")} if ARTIFACTS.is_dir() else set()
    targets = sorted(known | set(proj)) \
        if not args.exp else [args.exp]
    written = skipped = 0
    for exp_id in targets:
        out = EXPERIMENTS / identifier(exp_id) / "record.json"
        # 无参模式保持只补建；显式选择实验才自动刷新，--force 保留批量兼容。
        if out.exists() and not args.exp and not args.force:
            skipped += 1
            continue
        record = build_record(exp_id, proj.get(exp_id), settings)
        if not record["runs"] and not record["artifact_path"] and not out.exists():
            skipped += 1
            continue
        if out.exists():
            previous = json.loads(out.read_text(encoding="utf-8"))
            if not isinstance(previous, dict) or previous.get("_generated_by") != GENERATOR or previous.get("exp_id") != exp_id or previous.get("_projection_version", 1) not in (1, 2):
                raise ValueError(f"record requires explicit migration: {out}")
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


def cmd_derive(args) -> int:
    rows = []
    for rec_path in sorted(EXPERIMENTS.glob("*/record.json")):
        r = json.loads(rec_path.read_text(encoding="utf-8"))
        rows.append(record_index_row(r))
    if not rows:
        print("没有 record.json，先跑 build")
        return 1
    output = io.StringIO(newline="")
    w = csv.DictWriter(output, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    _write_if_changed(LEDGER, output.getvalue().encode("utf-8"))
    print(f"EXPERIMENTS.csv 派生 {len(rows)} 行 -> {LEDGER.relative_to(REPO_ROOT)}")
    return 0


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
