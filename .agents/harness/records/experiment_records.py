#!/usr/bin/env python3
"""从 Mission CSV 与 remote_artifacts 投影出 record.json，再派生 EXPERIMENTS.csv。

设计原则：只写证据支持的字段。无法从 CSV 或 artifacts 推出的（parent、protocol、
baseline 身份、outcome）一律留 null 并列入 `_pending`，不猜、不编。
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
import json
import math
import re
from pathlib import Path

from harness.common.project_config import DEFAULT_CONFIG, REPO_ROOT, load_config
from harness.remote.adapters.common import dotted_value

ARTIFACTS = REPO_ROOT / "remote_artifacts"
EXPERIMENTS = REPO_ROOT / "research_workspace" / "experiments"
LEDGER = REPO_ROOT / "research_workspace" / "EXPERIMENTS.csv"

# 无法从机器可读来源推出、必须由人填写的字段
PENDING_FIELDS = ("parent", "metrics.protocol", "metrics.baseline_id", "outcome")


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError(f"invalid experiment identifier: {value}")
    return value


def csv_projection() -> dict[str, dict]:
    """按 exp_id 汇总 Mission CSV 中可投影的字段。"""
    out: dict[str, dict] = {}
    for path in sorted((REPO_ROOT / "issues").rglob("*.csv")):
        try:
            rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
        except Exception:
            continue
        for row in rows:
            for exp in (row.get("exp_id") or "").split(";"):
                exp = exp.strip()
                if not exp:
                    continue
                identifier(exp)
                acc = out.setdefault(
                    exp,
                    {"spec_id": set(), "branch": set(), "commit": set(),
                     "run_id": set(), "next_action": [], "csv": set()},
                )
                acc["csv"].add(path.relative_to(REPO_ROOT).as_posix())
                for key, col in (("spec_id", "spec_id"), ("branch", "branch"),
                                 ("commit", "commit_hash"), ("run_id", "run_id")):
                    val = (row.get(col) or "").strip()
                    if val:
                        acc[key].add(val)
                na = (row.get("next_action") or "").strip()
                if na and na not in acc["next_action"]:
                    acc["next_action"].append(na)
    return out


def read_runs(exp_id: str, settings: dict | None = None) -> list[dict]:
    """从 remote_artifacts/<ExpID> 的 summary.json 抽取每次评估的事实。"""
    root = ARTIFACTS / identifier(exp_id)
    settings = settings or {}
    runs: list[dict] = []
    if not root.is_dir():
        return runs
    for run_root in sorted(root.iterdir()):
        if not run_root.is_dir() or run_root.is_symlink() or run_root.name.startswith("."):
            continue
        for summary in sorted(run_root.glob(settings.get("summary_glob", "summary.json"))):
            if "diagnostics" in summary.relative_to(run_root).parts:
                continue
            if not summary.resolve().is_relative_to(run_root.resolve()) or summary.is_symlink():
                raise ValueError(f"summary path escapes its run: {summary}")
            data = json.loads(summary.read_text(encoding="utf-8"))
            metric = dotted_value(data, settings.get("primary_metric", "metric"), "summary")
            if isinstance(metric, bool) or not isinstance(metric, (int, float)) or not math.isfinite(metric):
                raise ValueError(f"summary primary metric is not finite: {summary}")
            auxiliary = settings.get("secondary_metric")
            runs.append({
                "run_id": run_root.name,
                "summary_path": summary.relative_to(REPO_ROOT).as_posix(),
                "eval_dir": summary.parent.relative_to(REPO_ROOT).as_posix(),
                **{k: dotted_value(data, k, "summary") for k in settings.get("dimensions", [])},
                "metric": metric,
                "metric_aux": dotted_value(data, auxiliary, "summary") if auxiliary else None,
                "weights_path": data.get("checkpoint_path"),
                "commit": data.get("commit"),
            })
    return runs


def build_record(exp_id: str, proj: dict | None, settings: dict | None = None) -> dict:
    runs = read_runs(exp_id, settings)
    proj = proj or {}
    record = {
        "exp_id": exp_id,
        "parent": None,
        "relation": None,
        "source": {
            "spec_id": sorted(proj.get("spec_id", [])) or None,
            "branch": sorted(proj.get("branch", [])) or None,
            "commit": sorted(proj.get("commit", [])) or None,
            "mission_csv": sorted(proj.get("csv", [])) or None,
        },
        "metrics": {
            "protocol": None,
            "baseline_id": None,
            "baseline_run_id": None,
            "baseline_metric": None,
            "ours_metric": None,
            "delta_metric": None,
        },
        "runs": runs,
        "outcome": "pending",
        "artifact_path": f"remote_artifacts/{exp_id}/" if (ARTIFACTS / exp_id).exists() else None,
        "next_action": proj.get("next_action") or None,
        "_pending": list(PENDING_FIELDS),
        "_generated_by": ".agents/harness/records/experiment_records.py",
    }
    return record


def cmd_build(args) -> int:
    proj = csv_projection()
    config_path = getattr(args, "config", DEFAULT_CONFIG)
    settings = load_config(config_path).get("records", {}) if config_path.is_file() else {}
    known = {p.name for p in ARTIFACTS.iterdir() if p.is_dir() and not p.name.startswith(".")} if ARTIFACTS.is_dir() else set()
    targets = sorted(known | set(proj)) \
        if not args.exp else [args.exp]
    written = skipped = 0
    for exp_id in targets:
        record = build_record(exp_id, proj.get(exp_id), settings)
        if not record["runs"] and not record["source"]["commit"]:
            skipped += 1
            continue
        out = EXPERIMENTS / exp_id / "record.json"
        if out.exists() and not args.force:
            skipped += 1
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1
    print(f"record.json 写入 {written} 个，跳过 {skipped} 个")
    return 0


def cmd_derive(args) -> int:
    rows = []
    for rec_path in sorted(EXPERIMENTS.glob("*/record.json")):
        r = json.loads(rec_path.read_text(encoding="utf-8"))
        m = r.get("metrics", {})
        rows.append({
            "ExpID": r["exp_id"],
            "Parent": r.get("parent") or "",
            "Relation": r.get("relation") or "",
            "SpecID": ";".join(r["source"].get("spec_id") or []),
            "Commit": ";".join((r["source"].get("commit") or [])[:1]),
            "Protocol": m.get("protocol") or "",
            "BaselineID": m.get("baseline_id") or "",
            "OursMetric": m.get("ours_metric") if m.get("ours_metric") is not None else "",
            "DeltaMetric": m.get("delta_metric") if m.get("delta_metric") is not None else "",
            "Runs": len(r.get("runs") or []),
            "Outcome": r.get("outcome") or "",
            "ArtifactPath": r.get("artifact_path") or "",
        })
    if not rows:
        print("没有 record.json，先跑 build")
        return 1
    with LEDGER.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"EXPERIMENTS.csv 派生 {len(rows)} 行 -> {LEDGER.relative_to(REPO_ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="生成 record.json")
    b.add_argument("--exp", help="只处理一个 ExpID")
    b.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="project TOML containing records field mapping")
    b.add_argument("--force", action="store_true", help="覆盖已存在的 record.json")
    b.set_defaults(func=cmd_build)
    d = sub.add_parser("derive", help="从 record.json 派生 EXPERIMENTS.csv")
    d.set_defaults(func=cmd_derive)
    args = ap.parse_args()
    return args.func(args)
