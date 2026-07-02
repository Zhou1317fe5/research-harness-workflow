#!/usr/bin/env python3
"""
Utility functions for experiment analysis.

Extracted from the original analyze.py for reuse across the 3-layer system.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Import shared configuration
try:
    from ...common.config import DEFAULT_BASELINE_EXPID, DEFAULT_BASELINE_K
except ImportError:
    # Fallback if common config is not available
    DEFAULT_BASELINE_EXPID = "E20260113-01"
    DEFAULT_BASELINE_K = ("coco", 0, 1)

# Eval arg keys (for comparability diagnostics)
MODEL_ID_KEYS: tuple[str, ...] = ("checkpoint", "unet_ckpt_path")
COMPARABILITY_KEYS: tuple[str, ...] = ("benchmark", "fold", "nshot", "seed", "threshold", "r_threshold")


@dataclass(frozen=True)
class Summary:
    path: Path
    run_dir: Path
    benchmark: str
    fold: int
    nshot: int
    miou: float
    fb_iou: float | None
    created_at: str | None

    @property
    def k(self) -> tuple[str, int, int]:
        return (self.benchmark, self.fold, self.nshot)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_iso(dt_str: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(dt_str)
    except Exception:
        return None


def _summary_sort_key(s: Summary) -> tuple[int, float]:
    if s.created_at:
        dt = _parse_iso(s.created_at)
        if dt is not None:
            return (1, dt.timestamp())
    try:
        return (0, s.path.stat().st_mtime)
    except Exception:
        return (0, 0.0)


def _safe_relpath(p: Path, start: Path) -> str:
    try:
        return os.path.relpath(p, start)
    except Exception:
        return p.as_posix()


_SEED_RE = re.compile(r"(?:^|[_-])seed(?P<seed>\d+)(?:$|[_-])", re.IGNORECASE)
_CHECKPOINT_RE = re.compile(
    r"(?:^|[_-])(?:iter|checkpoint)[_-]?(?P<checkpoint>\d+)(?:$|[_-])",
    re.IGNORECASE,
)


def _infer_seed_from_run_dir(run_dir: Path) -> int | None:
    """Best-effort seed inference from eval run directory name."""
    m = _SEED_RE.search(run_dir.name)
    if not m:
        return None
    try:
        return int(m.group("seed"))
    except Exception:
        return None


def _infer_checkpoint_from_run_dir(run_dir: Path) -> int | None:
    """Best-effort checkpoint inference from eval run directory name."""
    for part in reversed(run_dir.parts):
        m = _CHECKPOINT_RE.search(part)
        if not m:
            continue
        try:
            return int(m.group("checkpoint"))
        except Exception:
            return None
    return None


def _format_mean_std(values: list[float]) -> tuple[str, float | None]:
    if not values:
        return ("NA", None)
    mean = float(sum(values) / len(values))
    if len(values) >= 2:
        try:
            std = float(statistics.stdev(values))
        except Exception:
            std = None
        if std is not None:
            return (f"{mean:.2f}±{std:.2f}", std)
    return (f"{mean:.2f}", None)


def _summaries_for_k(summaries: list[Summary], k: tuple[str, int, int]) -> list[Summary]:
    b, f, s = k
    return [x for x in summaries if (x.benchmark, x.fold, x.nshot) == (b, f, s)]


def _summarize_runs_for_k(
    *,
    summaries: list[Summary],
    k: tuple[str, int, int],
    rel_to: Path,
) -> dict[str, Any]:
    items = _summaries_for_k(summaries, k)
    n = len(items)
    best = max(items, key=lambda s: (s.miou, _summary_sort_key(s))) if items else None
    best_seed = _infer_seed_from_run_dir(best.run_dir) if best is not None else None
    best_run_rel = _safe_relpath(best.run_dir, rel_to) if best is not None else ""

    miou_vals = [s.miou for s in items]
    fb_vals = [s.fb_iou for s in items if s.fb_iou is not None]
    miou_mean = float(sum(miou_vals) / len(miou_vals)) if miou_vals else None
    fb_mean = float(sum(float(x) for x in fb_vals) / len(fb_vals)) if fb_vals else None
    miou_ms, miou_std = _format_mean_std(miou_vals)
    fb_ms, fb_std = _format_mean_std([float(x) for x in fb_vals]) if fb_vals else ("NA", None)

    return {
        "n": n,
        "miou_mean": miou_mean,
        "miou_mean_std": miou_ms,
        "miou_std": miou_std,
        "fb_mean": fb_mean,
        "fb_mean_std": fb_ms,
        "fb_std": fb_std,
        "best_miou": (best.miou if best is not None else None),
        "best_fb": (best.fb_iou if best is not None else None),
        "best_seed": best_seed,
        "best_run_rel": best_run_rel,
        "items": items,
    }


def load_summaries(remote_artifacts_dir: Path) -> list[Summary]:
    """Load all summary.json files from remote_artifacts/eval."""
    out: list[Summary] = []
    for p in sorted(remote_artifacts_dir.glob("eval/**/metrics/summary.json")):
        data = _read_json(p)
        if not isinstance(data, dict):
            continue
        run_dir = p.parent.parent
        try:
            fb_iou = data.get("FB-IoU")
            out.append(
                Summary(
                    path=p,
                    run_dir=run_dir,
                    benchmark=str(data.get("benchmark", "")),
                    fold=int(data.get("fold", 0)),
                    nshot=int(data.get("nshot", 1)),
                    miou=float(data.get("mIoU", 0.0)),
                    fb_iou=float(fb_iou) if fb_iou is not None else None,
                    created_at=str(data.get("created_at")) if data.get("created_at") else None,
                )
            )
        except Exception:
            continue
    return out


def load_results_json(remote_artifacts_dir: Path) -> dict[str, Any] | None:
    """Load remote_artifacts/results.json (if present)."""
    p = remote_artifacts_dir / "results.json"
    data = _read_json(p)
    return data if isinstance(data, dict) else None


def _summarize_results_json_for_k(
    *,
    results: dict[str, Any],
    k: tuple[str, int, int],
    rel_to: Path,
) -> dict[str, Any]:
    """
    Summarize metrics for a given K=(benchmark, fold, nshot) from results.json.

    Expected schema (best-effort):
      results["evaluation_metrics"][i]:
        - benchmark/fold/nshot
        - mIoU_mean/mIoU_std (or mIoU)
        - FB-IoU_mean/FB-IoU_std (or FB-IoU)
        - runs[*].mIoU, runs[*].FB-IoU, runs[*].log_path
    """
    b, f, s = k
    metrics = results.get("evaluation_metrics")
    if not isinstance(metrics, list):
        return {"n": 0, "miou_mean": None, "miou_std": None, "fb_mean": None, "best_run_rel": "", "items": []}

    candidates: list[dict[str, Any]] = []
    for item in metrics:
        if not isinstance(item, dict):
            continue
        try:
            if str(item.get("benchmark", "")) == b and int(item.get("fold", -1)) == f and int(item.get("nshot", -1)) == s:
                candidates.append(item)
        except Exception:
            continue

    if not candidates:
        return {"n": 0, "miou_mean": None, "miou_std": None, "fb_mean": None, "best_run_rel": "", "items": []}

    # Prefer the most recently updated metrics if timestamps exist
    def _cand_key(d: dict[str, Any]) -> tuple[int, float]:
        for ts_key in ("updated_at", "created_at"):
            ts = d.get(ts_key)
            if isinstance(ts, str):
                dt = _parse_iso(ts)
                if dt is not None:
                    return (1, dt.timestamp())
        return (0, 0.0)

    chosen = sorted(candidates, key=_cand_key)[-1]

    miou_mean = chosen.get("mIoU_mean", chosen.get("mIoU"))
    miou_std = chosen.get("mIoU_std")
    fb_mean = chosen.get("FB-IoU_mean", chosen.get("FB-IoU"))

    try:
        miou_mean = float(miou_mean) if miou_mean is not None else None
    except Exception:
        miou_mean = None
    try:
        miou_std = float(miou_std) if miou_std is not None else None
    except Exception:
        miou_std = None
    try:
        fb_mean = float(fb_mean) if fb_mean is not None else None
    except Exception:
        fb_mean = None

    runs = chosen.get("runs")
    runs_list: list[dict[str, Any]] = [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []

    # Best run dir (relative to remote_artifacts) inferred from best run's log_path
    best_run_rel = ""
    if runs_list:
        def _run_key(r: dict[str, Any]) -> float:
            v = r.get("mIoU")
            try:
                return float(v)
            except Exception:
                return float("-inf")

        best_run = max(runs_list, key=_run_key)
        lp = best_run.get("log_path")
        if isinstance(lp, str) and lp:
            try:
                run_dir = Path(lp).parents[2]
                best_run_rel = _safe_relpath(run_dir, rel_to)
            except Exception:
                best_run_rel = ""

    n = chosen.get("num_runs")
    try:
        n_val = int(n) if n is not None else len(runs_list)
    except Exception:
        n_val = len(runs_list)

    return {
        "n": n_val,
        "miou_mean": miou_mean,
        "miou_std": miou_std,
        "fb_mean": fb_mean,
        "best_run_rel": best_run_rel,
        "items": runs_list,
    }


def pick_latest_per_k(summaries: list[Summary]) -> dict[tuple[str, int, int], Summary]:
    """Pick the latest summary for each (benchmark, fold, nshot) combination."""
    groups: dict[tuple[str, int, int], list[Summary]] = {}
    for s in summaries:
        groups.setdefault(s.k, []).append(s)
    latest: dict[tuple[str, int, int], Summary] = {}
    for k, items in groups.items():
        latest[k] = sorted(items, key=_summary_sort_key)[-1]
    return latest


def _extract_record_section(record_md: Path, expid: str) -> list[str]:
    """Extract experiment section from 00-实验记录.md."""
    if not record_md.is_file():
        return []
    text = _read_text(record_md)
    if not text:
        return []

    lines = text.splitlines()
    section_lines = []
    in_section = False

    for line in lines:
        if line.strip().startswith(f"# {expid}"):
            in_section = True
            continue
        if in_section:
            if line.strip().startswith("# E") and line.strip() != f"# {expid}":
                break
            section_lines.append(line)

    return section_lines


def _extract_index_row(record_md: Path, expid: str) -> dict[str, str]:
    """Extract index table row for expid from 00-实验记录.md."""
    if not record_md.is_file():
        return {}

    text = _read_text(record_md)
    if not text:
        return {}

    # Find table and extract row for expid
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if expid in line and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            # Table format: | ExpID | 主评估 | 主结果 | experiment_name | 任务说明 | 对应模块 | 代码支 | EXP_ROOT |
            # Index:         0       1        2        3                 4          5         6        7         8
            if len(parts) >= 7 and expid in parts[1]:
                return {
                    "module_dir": parts[6] if len(parts) > 6 else "",  # Column 6: 对应模块
                    "experiment_name": parts[4] if len(parts) > 4 else "",  # Column 4: experiment_name
                }

    return {}


def _extract_module_dir_from_record(record_md: Path, expid: str) -> str:
    """Extract module directory from 00-实验记录.md index table."""
    row = _extract_index_row(record_md, expid)
    return row.get("module_dir", "").strip()


def _infer_compare_mode(
    *,
    module_dir: str,
    experiment_name: str | None,
) -> str:
    """Infer comparison mode from module directory and experiment name."""
    if not module_dir:
        return "e2e"

    name = (experiment_name or "").lower()
    if "ablation" in name or "abl" in name:
        return "infer_ablation"

    return "e2e"


def _find_train_logs(remote_artifacts_dir: Path) -> list[Path]:
    """Find training log files."""
    return sorted(remote_artifacts_dir.glob("train/logs/**/*.log"))


def _extract_train_snippets(log_path: Path, max_lines: int = 50) -> str:
    """Extract training log snippets."""
    text = _read_text(log_path)
    if not text:
        return ""

    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text

    return "\n".join(lines[-max_lines:])


def _find_eval_log_txt(run_dir: Path) -> Path | None:
    """Find eval log.txt in run directory."""
    candidates = list(run_dir.glob("logs/**/log.txt"))
    return candidates[0] if candidates else None


def _parse_eval_args_from_log(log_path: Path) -> dict[str, Any]:
    """Parse evaluation arguments from log.txt."""
    text = _read_text(log_path)
    if not text:
        return {}

    args = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            args[key.strip()] = value.strip()

    return args


def _parse_per_class_from_log(log_path: Path | str) -> list[tuple[int, float]]:
    """Parse per-class IoU from log.txt."""
    if isinstance(log_path, str):
        text = log_path
    else:
        text = _read_text(log_path)

    if not text:
        return []

    results = []
    for line in text.splitlines():
        # Look for patterns like "Class 0: 0.85" or "|  0:  0.00    |  1:  0.00    |"
        # First try the "Class N:" format
        match = re.search(r"Class\s+(\d+):\s+([\d.]+)", line)
        if match:
            try:
                class_id = int(match.group(1))
                iou = float(match.group(2))
                results.append((class_id, iou))
            except Exception:
                continue
        else:
            # Try the batch format: "|  0:  0.00    |  1:  0.00    |"
            # Extract all "N: value" pairs from the line
            matches = re.findall(r"\|\s+(\d+):\s+([\d.]+)\s+", line)
            for match in matches:
                try:
                    class_id = int(match[0])
                    iou = float(match[1])
                    results.append((class_id, iou))
                except Exception:
                    continue

    return results


def _diff_eval_args_selected(
    exp_args: dict[str, Any],
    base_args: dict[str, Any],
    keys: tuple[str, ...],
) -> list[tuple[str, Any, Any]]:
    """Compare selected evaluation arguments."""
    diffs = []
    for key in keys:
        exp_val = exp_args.get(key)
        base_val = base_args.get(key)
        if exp_val != base_val:
            diffs.append((key, exp_val, base_val))

    return diffs
