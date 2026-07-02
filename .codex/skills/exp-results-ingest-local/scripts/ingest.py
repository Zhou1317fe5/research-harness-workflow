#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import os
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RECORD_MD = REPO_ROOT / "research_workspace" / "00-实验记录.md"
MODULE_DOCS_ROOT = REPO_ROOT / "research_workspace" / "module_research"

UPDATED_SECTION_TITLE_RE = re.compile(r"^##\s*20251222更新后实验：以SD_init权重进行训练", re.UNICODE)
DETAIL_HEADING_RE = re.compile(r"^#\s*E\d", re.UNICODE)
_TIMESTAMP_DIR_RE = re.compile(r"^\d{8}_\d{6}$")


def experiment_dir(expid: str) -> Path:
    return REPO_ROOT / "research_workspace" / "experiments" / expid


def remote_artifacts_dir_for(expid: str) -> Path:
    return experiment_dir(expid) / "remote_artifacts"


@dataclass(frozen=True)
class Summary:
    path: Path
    benchmark: str
    fold: int
    nshot: int
    miou: float
    fb_iou: float
    created_at: str | None


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
    # Prefer created_at if available; fallback to mtime.
    if s.created_at:
        dt = _parse_iso(s.created_at)
        if dt is not None:
            return (1, dt.timestamp())
    try:
        return (0, s.path.stat().st_mtime)
    except Exception:
        return (0, 0.0)


def find_summaries(remote_artifacts_dir: Path) -> list[Summary]:
    summaries: list[Summary] = []
    for p in sorted(remote_artifacts_dir.glob("eval/**/metrics/summary.json")):
        data = _read_json(p)
        if not data:
            continue
        try:
            summaries.append(
                Summary(
                    path=p,
                    benchmark=str(data.get("benchmark", "")),
                    fold=int(data.get("fold", 0)),
                    nshot=int(data.get("nshot", 1)),
                    miou=float(data.get("mIoU", 0.0)),
                    fb_iou=float(data.get("FB-IoU", 0.0)),
                    created_at=str(data.get("created_at")) if data.get("created_at") else None,
                )
            )
        except Exception:
            continue
    return summaries


def find_latest_for_k(remote_artifacts_dir: Path, k: tuple[str, int, int]) -> Summary | None:
    summaries = find_summaries(remote_artifacts_dir)
    if not summaries:
        return None
    # group by K then pick latest by created_at/mtime
    groups: dict[tuple[str, int, int], list[Summary]] = {}
    for s in summaries:
        kk = (s.benchmark, s.fold, s.nshot)
        groups.setdefault(kk, []).append(s)
    items = groups.get(k) or []
    if not items:
        return None
    return sorted(items, key=_summary_sort_key)[-1]


def pick_latest_summary(summaries: list[Summary]) -> Summary:
    if not summaries:
        raise RuntimeError("No summary.json found under remote_artifacts/eval/**/metrics/summary.json")
    return sorted(summaries, key=_summary_sort_key)[-1]


def format_main_eval(s: Summary) -> str:
    return f"{s.benchmark} / {s.fold} / {s.nshot}"


def format_main_result(s: Summary) -> str:
    return f"{s.miou:.2f}/{s.fb_iou:.2f}"


def format_main_result_mean(summaries: list[Summary], k: tuple[str, int, int]) -> str:
    """Format mean mIoU/FB-IoU for the given K (benchmark, fold, nshot)."""
    checkpoint_rows = _checkpoint_rows_for_k(summaries, k)
    if checkpoint_rows:
        _, primary = checkpoint_rows[-1]
        return f"{primary.miou:.2f}/{primary.fb_iou:.2f}"

    items = _summaries_for_k(summaries, k)
    if not items:
        return "NA/NA"
    miou_vals = [s.miou for s in items]
    fb_vals = [s.fb_iou for s in items]
    miou_mean = sum(miou_vals) / len(miou_vals)
    fb_mean = sum(fb_vals) / len(fb_vals)
    return f"{miou_mean:.2f}/{fb_mean:.2f}"


def _format_code_branch(training_info: dict[str, Any]) -> str:
    branch = str(training_info.get("code_branch") or "").strip()
    commit = str(training_info.get("git_commit") or "").strip()
    if branch and commit:
        return f"{branch} ({commit})"
    return branch or commit


def _infer_experiment_name_from_exp_root(remote_exp_root: str) -> str:
    s = (remote_exp_root or "").strip().rstrip("/")
    if not s:
        return ""
    p = Path(s)
    if _TIMESTAMP_DIR_RE.match(p.name) and p.parent and p.parent.name:
        return p.parent.name
    # Fallback: use last segment (better than empty).
    return p.name or ""


def _is_empty_table_row(line: str) -> bool:
    # A row like: |  |  | |  |  |  |  |
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return len(cells) >= 2 and all(c == "" for c in cells)


def _parse_md_table_row(line: str) -> list[str]:
    # Keep it simple; assumes '|' separated markdown rows.
    if not line.lstrip().startswith("|"):
        return []
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def _format_md_table_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |\n"


def _merge_cell(old: str, new: str) -> str:
    new = (new or "").strip()
    if new:
        return new
    return (old or "").strip()


def _norm_header_cell(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def _guess_table_columns(header_cells: list[str]) -> dict[str, int]:
    """
    Supported columns (by semantic key):
      - expid
      - experiment_name
      - module (对应模块)
      - code_branch (代码支)
      - task_desc (任务说明)
      - main_eval (主评估)
      - main_result (主结果)
      - exp_root (EXP_ROOT/运行目录)
    """
    idx: dict[str, int] = {}
    for i, h in enumerate(header_cells):
        nh = _norm_header_cell(h)
        if nh.startswith("expid"):
            idx["expid"] = i
            continue
        if "experiment_name" in nh:
            idx["experiment_name"] = i
            continue
        if "对应模块" in h:
            idx["module"] = i
            continue
        if "代码支" in h:
            idx["code_branch"] = i
            continue
        if "任务说明" in h:
            idx["task_desc"] = i
            continue
        if "主评估" in h:
            idx["main_eval"] = i
            continue
        if "主结果" in h:
            idx["main_result"] = i
            continue
        if "exp_root" in nh or "运行目录" in h:
            idx["exp_root"] = i
            continue
    return idx


def _build_table_row_cells(
    header_cells: list[str],
    *,
    expid: str,
    experiment_name: str,
    module_name: str,
    code_branch: str,
    task_desc: str,
    main_eval: str,
    main_result: str,
    remote_exp_root: str,
) -> list[str]:
    if not header_cells:
        # Legacy fallback (8 columns): ExpID | 主评估 | 主结果 | experiment_name | 任务说明 | 对应模块 | 代码支 | EXP_ROOT
        return [expid, main_eval, main_result, experiment_name, task_desc, module_name, code_branch, remote_exp_root]

    n = len(header_cells)
    cells = [""] * n
    col = _guess_table_columns(header_cells)

    def _set(idx_key: str, value: str) -> None:
        i = col.get(idx_key)
        if i is None or i < 0 or i >= n:
            return
        cells[i] = (value or "").strip()

    _set("expid", expid)
    _set("experiment_name", experiment_name)
    _set("module", module_name)
    _set("code_branch", code_branch)
    _set("task_desc", task_desc)
    _set("main_eval", main_eval)
    _set("main_result", main_result)
    _set("exp_root", remote_exp_root)

    # Safety fallbacks if headers are unusual.
    if (cells[0] or "").strip() == "":
        cells[0] = expid
    if n >= 2 and (cells[1] or "").strip() == "":
        cells[1] = experiment_name
    if (cells[-1] or "").strip() == "" and remote_exp_root:
        cells[-1] = remote_exp_root

    return cells


def _infer_module_name(code_branch_raw: str) -> str:
    """
    Try to map code branch into an existing module doc directory name under MODULE_DOCS_ROOT.
    This is best-effort and safe: if no directory matches, return empty.
    """
    s = (code_branch_raw or "").strip()
    if not s:
        return ""
    if s.startswith("Module/"):
        s = s[len("Module/") :]
    candidates: list[str] = []
    candidates.append(s)
    # Remove inline variants like "(Bank)" while keeping the core module name.
    candidates.append(re.sub(r"\([^)]*\)", "", s))
    candidates.append(s.replace("_", "-"))
    candidates.append(re.sub(r"\([^)]*\)", "", s).replace("_", "-"))
    candidates = [re.sub(r"-{2,}", "-", c).strip("-").strip() for c in candidates if c and c.strip()]

    for c in candidates:
        if (MODULE_DOCS_ROOT / c).is_dir():
            return c
    return ""


def _find_index_end(lines: list[str]) -> int:
    # Top-level index tables are before the first "# E..." details heading.
    for i, line in enumerate(lines):
        if DETAIL_HEADING_RE.match(line.rstrip("\n")):
            return i
    return len(lines)


def _upsert_row_in_first_matching_index_table(
    lines: list[str],
    *,
    expid: str,
    experiment_name: str,
    module_name: str,
    code_branch: str,
    task_desc: str,
    main_eval: str,
    main_result: str,
    remote_exp_root: str,
) -> int:
    """
    Update existing ExpID rows across all "top-level index tables" (tables whose header begins with "| ExpID").
    Returns number of updated rows. Does NOT insert new rows.
    """
    index_end = _find_index_end(lines)
    row_re = re.compile(rf"^\|\s*{re.escape(expid)}\s*\|")

    updated = 0
    i = 0
    while i < index_end:
        if not lines[i].lstrip().startswith("| ExpID"):
            i += 1
            continue
        table_header_idx = i
        table_end_idx = index_end
        for j in range(table_header_idx + 1, index_end):
            if not lines[j].lstrip().startswith("|"):
                table_end_idx = j
                break

        for j in range(table_header_idx + 2, table_end_idx):
            if not row_re.match(lines[j]):
                continue
            old_cells = _parse_md_table_row(lines[j])
            header_cells = _parse_md_table_row(lines[table_header_idx])
            target_len = len(header_cells) if header_cells else max(len(old_cells), 7)
            cur = old_cells[:target_len] + [""] * max(0, target_len - len(old_cells))

            if header_cells:
                col = _guess_table_columns(header_cells)

                def _merge_into(idx_key: str, new_value: str) -> None:
                    idx = col.get(idx_key)
                    if idx is None or idx < 0 or idx >= target_len:
                        return
                    cur[idx] = _merge_cell(cur[idx], new_value)

                expid_idx = col.get("expid", 0)
                if 0 <= expid_idx < target_len:
                    cur[expid_idx] = expid
                else:
                    cur[0] = expid

                _merge_into("experiment_name", experiment_name)
                _merge_into("module", module_name)
                _merge_into("code_branch", code_branch)
                _merge_into("task_desc", task_desc)
                _merge_into("main_eval", main_eval)
                _merge_into("main_result", main_result)
                _merge_into("exp_root", remote_exp_root)

                # Avoid keeping obviously shifted values in "任务说明" when we don't have a real task_desc.
                td_idx = col.get("task_desc")
                me_idx = col.get("main_eval")
                if td_idx is not None and me_idx is not None and not (task_desc or "").strip():
                    td = (cur[td_idx] or "").strip()
                    me = (cur[me_idx] or "").strip()
                    # A common misalignment pattern: task_desc accidentally equals main_eval (e.g. "coco / 0 / 1").
                    if td and me and td == me and td == (main_eval or "").strip():
                        if re.fullmatch(r"[A-Za-z0-9_-]+\s*/\s*\d+\s*/\s*\d+", td):
                            cur[td_idx] = ""
                lines[j] = _format_md_table_row(cur[:target_len])
            else:
                # Legacy fallback (8 columns): ExpID | 主评估 | 主结果 | experiment_name | 任务说明 | 对应模块 | 代码支 | EXP_ROOT
                while len(cur) < 8:
                    cur.append("")
                cur[0] = expid
                cur[1] = _merge_cell(cur[1], main_eval)
                cur[2] = _merge_cell(cur[2], main_result)
                cur[3] = _merge_cell(cur[3], experiment_name)
                cur[4] = _merge_cell(cur[4], task_desc)
                cur[5] = _merge_cell(cur[5], module_name)
                cur[6] = _merge_cell(cur[6], code_branch)
                cur[7] = _merge_cell(cur[7], remote_exp_root)
                lines[j] = _format_md_table_row(cur[:8])
            updated += 1
        i = table_end_idx

    return updated


def upsert_row_in_updated_index_table(
    md_text: str,
    *,
    expid: str,
    experiment_name: str,
    module_name: str,
    code_branch: str,
    task_desc: str,
    main_eval: str,
    main_result: str,
    remote_exp_root: str,
) -> str:
    lines = md_text.splitlines(keepends=True)

    # 0) If ExpID already exists in any top-level index table, update those rows in-place and DO NOT create new rows.
    updated_rows = _upsert_row_in_first_matching_index_table(
        lines,
        expid=expid,
        experiment_name=experiment_name,
        module_name=module_name,
        code_branch=code_branch,
        task_desc=task_desc,
        main_eval=main_eval,
        main_result=main_result,
        remote_exp_root=remote_exp_root,
    )
    if updated_rows > 0:
        return "".join(lines)

    # 1) Find the first table with "| ExpID" header (no section title matching required).
    index_end = _find_index_end(lines)
    table_header_idx = None
    for i in range(index_end):
        if lines[i].lstrip().startswith("| ExpID"):
            table_header_idx = i
            break
    if table_header_idx is None:
        raise RuntimeError("Cannot find any index table with '| ExpID' header in 00-实验记录.md")

    # 2) Find table end: first non '|' line after separator.
    table_end_idx = None
    for i in range(table_header_idx + 1, index_end):
        if not lines[i].lstrip().startswith("|"):
            table_end_idx = i
            break
    if table_end_idx is None:
        table_end_idx = index_end

    header_cells = _parse_md_table_row(lines[table_header_idx])
    row_cells = _build_table_row_cells(
        header_cells,
        expid=expid,
        experiment_name=experiment_name,
        module_name=module_name,
        code_branch=code_branch,
        task_desc=task_desc,
        main_eval=main_eval,
        main_result=main_result,
        remote_exp_root=remote_exp_root,
    )
    new_row = _format_md_table_row(row_cells)

    # 3) Insert at the end of table (Option A: always append to the last position).
    lines.insert(table_end_idx, new_row)
    return "".join(lines)


_EXP_ROOT_LINE_RE = re.compile(r"^\s*-\s*EXP_ROOT\s*[:：]\s*")
_AUTO_INGEST_START_RE = re.compile(r"<!--\s*AUTO-INGEST\s+START\s*-->")
_AUTO_INGEST_END_RE = re.compile(r"<!--\s*AUTO-INGEST\s+END\s*-->")
_AUTO_HEN_START_RE = re.compile(r"<!--\s*AUTO-HEN\s+START\s*-->")
_HEN_REPORT_LINE_RE = re.compile(r"^\s*-\s*分析报告\s*[:：]\s*`?.+`?\s*$")
_SEED_RE = re.compile(r"(?:^|[_-])seed(?P<seed>\d+)(?:$|[_-])", re.IGNORECASE)
_CHECKPOINT_RE = re.compile(
    r"(?:^|[_-])(?:iter|checkpoint)[_-]?(?P<checkpoint>\d+)(?:$|[_-])",
    re.IGNORECASE,
)

# Import shared configuration
try:
    from ...common.config import DEFAULT_BASELINE_EXPID, DEFAULT_BASELINE_K
except ImportError:
    # Fallback if common config is not available
    DEFAULT_BASELINE_EXPID = "E20260113-01"
    DEFAULT_BASELINE_K = ("coco", 0, 1)

def _infer_seed_from_run_dir(run_dir: Path) -> int | None:
    """
    Best-effort seed inference from eval run directory name.
    Expected pattern (from scripts): ..._seed{SEED}_...
    """
    m = _SEED_RE.search(run_dir.name)
    if not m:
        return None
    try:
        return int(m.group("seed"))
    except Exception:
        return None


def _infer_checkpoint_from_run_dir(run_dir: Path) -> int | None:
    """Best-effort checkpoint inference from eval run directory path."""
    for part in reversed(run_dir.parts):
        m = _CHECKPOINT_RE.search(part)
        if not m:
            continue
        try:
            return int(m.group("checkpoint"))
        except Exception:
            return None
    return None


def _run_dir_for_summary(s: Summary) -> Path:
    return s.path.parent.parent


def _checkpoint_rows_for_items(items: list[Summary]) -> list[tuple[int, Summary]]:
    rows: list[tuple[int, Summary]] = []
    seen: set[int] = set()
    for item in items:
        checkpoint = _infer_checkpoint_from_run_dir(_run_dir_for_summary(item))
        if checkpoint is None or checkpoint in seen:
            return []
        seen.add(checkpoint)
        rows.append((checkpoint, item))

    if len(rows) < 2 or len(rows) != len(items):
        return []
    return sorted(rows, key=lambda row: row[0])


def _checkpoint_rows_for_k(summaries: list[Summary], k: tuple[str, int, int]) -> list[tuple[int, Summary]]:
    return _checkpoint_rows_for_items(_summaries_for_k(summaries, k))


def _primary_summary_for_k(summaries: list[Summary], k: tuple[str, int, int]) -> Summary | None:
    items = _summaries_for_k(summaries, k)
    if not items:
        return None
    checkpoint_rows = _checkpoint_rows_for_items(items)
    if checkpoint_rows:
        return checkpoint_rows[-1][1]
    return sorted(items, key=_summary_sort_key)[-1]


def pick_primary_summary(summaries: list[Summary]) -> Summary:
    latest = pick_latest_summary(summaries)
    k = (latest.benchmark, latest.fold, latest.nshot)
    return _primary_summary_for_k(summaries, k) or latest


def _summaries_for_k(summaries: list[Summary], k: tuple[str, int, int]) -> list[Summary]:
    b, f, s = k
    return [x for x in summaries if (x.benchmark, x.fold, x.nshot) == (b, f, s)]


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


def _summarize_runs_for_k(
    *,
    summaries: list[Summary],
    local_remote_artifacts: Path,
    k: tuple[str, int, int],
) -> dict[str, Any]:
    items = _summaries_for_k(summaries, k)
    n = len(items)
    checkpoint_rows = _checkpoint_rows_for_items(items)
    # Deterministic "best": higher mIoU, then latest by created_at/mtime.
    best = max(items, key=lambda s: (s.miou, _summary_sort_key(s))) if items else None
    best_run_dir = _run_dir_for_summary(best) if best is not None else None
    best_seed = _infer_seed_from_run_dir(best_run_dir) if best_run_dir is not None else None
    best_run_rel = os.path.relpath(best_run_dir, local_remote_artifacts) if best_run_dir is not None else ""
    primary_checkpoint = None
    primary = None
    if checkpoint_rows:
        primary_checkpoint, primary = checkpoint_rows[-1]
    primary_run_dir = _run_dir_for_summary(primary) if primary is not None else None
    primary_run_rel = os.path.relpath(primary_run_dir, local_remote_artifacts) if primary_run_dir is not None else ""

    miou_vals = [s.miou for s in items]
    fb_vals = [s.fb_iou for s in items]
    miou_ms, miou_std = _format_mean_std(miou_vals)
    fb_ms, fb_std = _format_mean_std(fb_vals)

    return {
        "n": n,
        "checkpoint_rows": checkpoint_rows,
        "primary_checkpoint": primary_checkpoint,
        "primary_miou": (primary.miou if primary is not None else None),
        "primary_fb": (primary.fb_iou if primary is not None else None),
        "primary_run_rel": primary_run_rel,
        "miou_mean_std": miou_ms,
        "miou_std": miou_std,
        "fb_mean_std": fb_ms,
        "fb_std": fb_std,
        "best_miou": (best.miou if best is not None else None),
        "best_fb": (best.fb_iou if best is not None else None),
        "best_seed": best_seed,
        "best_run_rel": best_run_rel,
    }


def _infer_remote_eval_dir(remote_exp_root: str, local_remote_artifacts: Path, summary_path: Path) -> str:
    remote_exp_root = (remote_exp_root or "").strip().rstrip("/")
    if not remote_exp_root:
        return ""
    try:
        run_dir = summary_path.parent.parent  # .../eval/<eval_name>/<run_id>
        rel = os.path.relpath(run_dir, local_remote_artifacts)
        # rel should look like: eval/<eval_name>/<run_id>
        return str((Path(remote_exp_root) / rel).as_posix())
    except Exception:
        return ""


def _build_detail_block(
    *,
    expid: str,
    experiment_name: str,
    code_branch: str,
    remote_exp_root: str,
    local_remote_artifacts: Path,
    latest: Summary,
    summaries: list[Summary],
    baseline_expid: str | None = None,
) -> str:
    remote_artifacts_posix = local_remote_artifacts.as_posix()
    train_console = local_remote_artifacts / "train_console.log"
    eval_console = local_remote_artifacts / "eval_console.log"

    lines: list[str] = []
    if remote_exp_root:
        lines.append(f"- EXP_ROOT：`{remote_exp_root}`")
    else:
        lines.append("- EXP_ROOT：（缺失；请补充）")

    if code_branch:
        lines.append(f"- 代码分支 / commit：{code_branch}")
    else:
        lines.append("- 代码分支 / commit：（待补充）")

    baseline_text = "（待补充）"
    if (latest.benchmark, latest.fold, latest.nshot) == DEFAULT_BASELINE_K:
        base_dir = remote_artifacts_dir_for(DEFAULT_BASELINE_EXPID)
        base_latest = find_latest_for_k(base_dir, DEFAULT_BASELINE_K) if base_dir.is_dir() else None
        if base_latest is not None:
            baseline_text = f"{DEFAULT_BASELINE_EXPID}（{base_latest.miou:.2f}/{base_latest.fb_iou:.2f}）"
        else:
            baseline_text = f"{DEFAULT_BASELINE_EXPID}（未找到 baseline remote_artifacts）"
    lines.append(f"- 主评估（benchmark/fold/nshot）：{format_main_eval(latest)}；基线：{baseline_text}")

    # Multi-run summary (same K as `latest`).
    k = (latest.benchmark, latest.fold, latest.nshot)
    exp_sum = _summarize_runs_for_k(summaries=summaries, local_remote_artifacts=local_remote_artifacts, k=k)
    exp_n = int(exp_sum.get("n") or 0)
    if exp_n > 0 and exp_sum.get("best_miou") is not None:
        checkpoint_rows = exp_sum.get("checkpoint_rows") or []
        if checkpoint_rows:
            row_text = "，".join(
                f"step{checkpoint} `{summary.miou:.2f}/{summary.fb_iou:.2f}`"
                for checkpoint, summary in checkpoint_rows
            )
            lines.append(f"- 评估汇总（checkpoint 口径，非 multi-seed）：{row_text}")
        else:
            best_m = float(exp_sum["best_miou"])
            mean_std = str(exp_sum.get("miou_mean_std") or "NA")
            best_seed = exp_sum.get("best_seed")
            best_run = str(exp_sum.get("best_run_rel") or "").strip()
            extras: list[str] = []
            if best_seed is not None:
                extras.append(f"seed={best_seed}")
            if best_run:
                extras.append(f"run={best_run}")
            if exp_n < 3:
                extras.append("建议补跑seeds=0/1/2")
            extras_s = ("；".join(extras)) if extras else ""
            if "±" in mean_std:
                lines.append(f"- 评估汇总（K={format_main_eval(latest)}）：n={exp_n}，mIoU(mean±std)={mean_std}，best={best_m:.2f}（{extras_s}）")
            else:
                lines.append(f"- 评估汇总（K={format_main_eval(latest)}）：n={exp_n}，mIoU(mean)={mean_std}，best={best_m:.2f}（{extras_s}）")

    # Optional: compare against a user-chosen baseline ExpID for this K.
    baseline_expid = (baseline_expid or "").strip() or None
    if baseline_expid:
        base_dir = remote_artifacts_dir_for(baseline_expid)
        if base_dir.is_dir():
            base_summaries = find_summaries(base_dir)
            base_sum = _summarize_runs_for_k(summaries=base_summaries, local_remote_artifacts=base_dir, k=k)
            base_n = int(base_sum.get("n") or 0)
            if base_n > 0:
                def _parse_mean(ms: str) -> float | None:
                    s = (ms or "").strip()
                    if not s or s == "NA":
                        return None
                    try:
                        return float(s.split("±", 1)[0])
                    except Exception:
                        return None

                exp_checkpoint = exp_sum.get("primary_checkpoint")
                base_checkpoint = base_sum.get("primary_checkpoint")
                exp_value = exp_sum.get("primary_miou") if exp_checkpoint is not None else None
                base_value = base_sum.get("primary_miou") if base_checkpoint is not None else None
                exp_mean = float(exp_value) if exp_value is not None else _parse_mean(str(exp_sum.get("miou_mean_std") or ""))
                base_mean = float(base_value) if base_value is not None else _parse_mean(str(base_sum.get("miou_mean_std") or ""))
                d_mean = (exp_mean - base_mean) if (exp_mean is not None and base_mean is not None) else None
                delta_s = f"{d_mean:+.2f}" if d_mean is not None else "NA"
                base_best = base_sum.get("best_miou")
                base_best_s = f"{float(base_best):.2f}" if base_best is not None else "NA"
                base_checkpoint_rows = base_sum.get("checkpoint_rows") or []
                hint = "；建议补跑seeds=0/1/2" if (not base_checkpoint_rows and base_n < 3) else ""
                if exp_checkpoint is not None or base_checkpoint is not None:
                    exp_label = f"checkpoint{exp_checkpoint}" if exp_checkpoint is not None else "mean"
                    base_label = f"checkpoint{base_checkpoint}" if base_checkpoint is not None else "mean"
                    base_metric_s = f"{base_mean:.2f}" if base_mean is not None else "NA"
                    lines.append(
                        f"- vs baseline({baseline_expid})：Δ{exp_label}_vs_{base_label}={delta_s}；base_n={base_n}；base_{base_label}={base_metric_s}；base_best={base_best_s}{hint}"
                    )
                else:
                    base_ms = str(base_sum.get("miou_mean_std") or "NA")
                    lines.append(
                        f"- vs baseline({baseline_expid})：Δmean={delta_s}；base_n={base_n}；base_mean±std={base_ms}；base_best={base_best_s}{hint}"
                    )
            else:
                lines.append(f"- vs baseline({baseline_expid})：baseline 缺该 K（K={format_main_eval(latest)}）")
        else:
            lines.append(f"- vs baseline({baseline_expid})：baseline remote_artifacts 缺失（本机未找到：`{base_dir.as_posix()}`）")

    if train_console.is_file():
        lines.append(f"- Train：见 `{train_console.as_posix()}`（待补充具体脚本与关键参数）")
    else:
        lines.append(f"- Train：见 `{remote_artifacts_posix}/train_console.log`（如有；待补充具体脚本与关键参数）")

    if eval_console.is_file():
        lines.append(f"- Eval：见 `{eval_console.as_posix()}`（待补充具体脚本与关键参数）")
    else:
        lines.append(f"- Eval：见 `{remote_artifacts_posix}/eval_console.log`（如有；待补充具体脚本与关键参数）")

    remote_eval_dir = _infer_remote_eval_dir(remote_exp_root, local_remote_artifacts, latest.path)
    if remote_eval_dir:
        lines.append(f"- eval_dir：`{remote_eval_dir}`")
    else:
        # At least provide the local relative path for the user to find it quickly.
        eval_dir = latest.path.parent.parent  # .../eval/<eval_name>/<run_id>
        rel = os.path.relpath(eval_dir, local_remote_artifacts)
        lines.append(f"- eval_dir：`{rel}`（本机相对 remote_artifacts；请补充远端路径）")

    # Calculate primary result values for this K. Repeated checkpoints are not
    # statistical repeats; use the highest checkpoint as the main result.
    k = (latest.benchmark, latest.fold, latest.nshot)
    items = _summaries_for_k(summaries, k)
    checkpoint_rows = _checkpoint_rows_for_items(items)
    primary_checkpoint = None
    if checkpoint_rows:
        primary_checkpoint, primary_summary = checkpoint_rows[-1]
        miou_result = primary_summary.miou
        fb_result = primary_summary.fb_iou
    else:
        miou_vals = [s.miou for s in items]
        fb_vals = [s.fb_iou for s in items]
        miou_result = sum(miou_vals) / len(miou_vals) if miou_vals else 0.0
        fb_result = sum(fb_vals) / len(fb_vals) if fb_vals else 0.0

    vs_text = "（待补充）"
    if k == DEFAULT_BASELINE_K:
        base_dir = remote_artifacts_dir_for(DEFAULT_BASELINE_EXPID)
        if base_dir.is_dir():
            base_summaries = find_summaries(base_dir)
            base_items = _summaries_for_k(base_summaries, k)
            if base_items:
                base_checkpoint_rows = _checkpoint_rows_for_items(base_items)
                if base_checkpoint_rows:
                    _, base_primary = base_checkpoint_rows[-1]
                    base_miou = base_primary.miou
                    base_fb = base_primary.fb_iou
                else:
                    base_miou_vals = [s.miou for s in base_items]
                    base_fb_vals = [s.fb_iou for s in base_items]
                    base_miou = sum(base_miou_vals) / len(base_miou_vals)
                    base_fb = sum(base_fb_vals) / len(base_fb_vals)
                d_m = miou_result - base_miou
                d_f = fb_result - base_fb
                vs_text = f"{d_m:+.2f}/{d_f:+.2f}"
    result_label = f"（checkpoint{primary_checkpoint}）" if primary_checkpoint is not None else ""
    lines.append(f"- 结果{result_label}：mIoU/FB-IoU = {miou_result:.2f}/{fb_result:.2f}（vs 基线：{vs_text}）")
    lines.append("- 结论：（待补充）")
    lines.append("- 下一步：（待补充）")

    # Keep a compact multi-eval table for auditability (optional for the user).
    lines.append("")
    lines.append("### 指标汇总（summary.json）")
    has_checkpoint_column = any(_infer_checkpoint_from_run_dir(_run_dir_for_summary(s)) is not None for s in summaries)
    if has_checkpoint_column:
        lines.append("| checkpoint | benchmark | fold | nshot | mIoU | FB-IoU | created_at | eval_dir |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    else:
        lines.append("| benchmark | fold | nshot | mIoU | FB-IoU | created_at | eval_dir |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    def _table_sort_key(s: Summary) -> tuple[int, int, tuple[int, float]]:
        checkpoint = _infer_checkpoint_from_run_dir(_run_dir_for_summary(s))
        return (0, checkpoint, _summary_sort_key(s)) if checkpoint is not None else (1, 0, _summary_sort_key(s))

    for s in sorted(summaries, key=_table_sort_key if has_checkpoint_column else _summary_sort_key):
        eval_dir = _run_dir_for_summary(s)
        rel = os.path.relpath(eval_dir, local_remote_artifacts)
        if has_checkpoint_column:
            checkpoint = _infer_checkpoint_from_run_dir(eval_dir)
            checkpoint_s = str(checkpoint) if checkpoint is not None else ""
            lines.append(
                f"| {checkpoint_s} | {s.benchmark} | {s.fold} | {s.nshot} | {s.miou:.2f} | {s.fb_iou:.2f} | {s.created_at or ''} | `{rel}` |"
            )
        else:
            lines.append(
                f"| {s.benchmark} | {s.fold} | {s.nshot} | {s.miou:.2f} | {s.fb_iou:.2f} | {s.created_at or ''} | `{rel}` |"
            )

    lines.append("")
    return "\n".join(lines) + "\n"


def upsert_detail_section(md_text: str, *, expid: str, section_title: str, block: str) -> str:
    lines = md_text.splitlines(keepends=True)
    heading_re = re.compile(rf"^#\s*{re.escape(expid)}\b")

    # Find existing ExpID heading
    start_idx = None
    for i, line in enumerate(lines):
        if heading_re.match(line.rstrip("\n")):
            start_idx = i
            break

    if start_idx is None:
        # Insert a new section right after the top-level index tables (before the first existing detail section).
        insert_at = _find_index_end(lines)

        # Example: "# E20251225-01: E1-DA（...）"
        safe_title = (section_title or "").strip()
        if not safe_title:
            safe_title = "自动入库"
        new_heading = f"# {expid}: {safe_title}\n"

        new_section_parts: list[str] = [new_heading, "\n", block]
        new_section = "".join(new_section_parts)

        # Ensure separation with surrounding text.
        if insert_at > 0 and lines[insert_at - 1].strip() != "":
            new_section = "\n" + new_section
        if insert_at < len(lines) and lines[insert_at].strip() != "":
            new_section = new_section + "\n"

        lines[insert_at:insert_at] = new_section.splitlines(keepends=True)
        return "".join(lines)

    # Determine section end (next top-level heading or EOF)
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if lines[i].startswith("# "):
            end_idx = i
            break

    def _find_ingest_block_range(section_lines: list[str]) -> tuple[int, int] | None:
        # 1) Legacy marker-based range (inclusive of markers).
        start = None
        end = None
        for i, ln in enumerate(section_lines):
            if start is None and _AUTO_INGEST_START_RE.search(ln):
                start = i
            if start is not None and _AUTO_INGEST_END_RE.search(ln):
                end = i + 1
                break
        if start is not None and end is not None and end > start:
            return (start, end)

        # 2) Signature-based range: from "- EXP_ROOT：" through the end of the "指标汇总" table,
        # stopping before AUTO-HEN if present.
        start = None
        for i, ln in enumerate(section_lines):
            if _EXP_ROOT_LINE_RE.match(ln):
                start = i
                break
        if start is None:
            return None

        # Stop before analysis blocks if present (marker-based or signature-based).
        hen_idx = None
        for i in range(start + 1, len(section_lines)):
            if _AUTO_HEN_START_RE.search(section_lines[i]) or _HEN_REPORT_LINE_RE.match(section_lines[i]):
                hen_idx = i
                break

        # Prefer ending at the end of the metrics table.
        metrics_idx = None
        for i in range(start + 1, len(section_lines)):
            if section_lines[i].startswith("### 指标汇总（summary.json）"):
                metrics_idx = i
                break

        if metrics_idx is None:
            end = hen_idx if hen_idx is not None else len(section_lines)
            return (start, end)

        # Find end of markdown table (first non '|' line after the header+separator).
        table_start = None
        for i in range(metrics_idx + 1, len(section_lines)):
            if section_lines[i].lstrip().startswith("|"):
                table_start = i
                break
            if section_lines[i].startswith("# "):
                break
        if table_start is None:
            end = hen_idx if hen_idx is not None else len(section_lines)
            return (start, end)

        table_end = len(section_lines)
        for i in range(table_start + 1, len(section_lines)):
            if not section_lines[i].lstrip().startswith("|"):
                table_end = i
                break
        end = table_end
        if hen_idx is not None:
            end = min(end, hen_idx)
        return (start, end)

    section_lines = lines[start_idx:end_idx]
    rng = _find_ingest_block_range(section_lines)
    if rng is not None:
        a, b = rng
        replacement = (block if block.endswith("\n") else (block + "\n")).splitlines(keepends=True)
        section_lines[a:b] = replacement
        lines[start_idx:end_idx] = section_lines
        return "".join(lines)

    # No existing ingest block found: insert right after the ExpID heading (before other content).
    insert_at = start_idx + 1
    while insert_at < end_idx and lines[insert_at].strip() == "":
        insert_at += 1
    # Ensure a blank line between heading and the block.
    if insert_at == start_idx + 1 and (insert_at >= len(lines) or lines[insert_at].strip() != ""):
        lines.insert(insert_at, "\n")
        insert_at += 1
        end_idx += 1
    lines[insert_at:insert_at] = (block if block.endswith("\n") else (block + "\n")).splitlines(keepends=True)
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest remote artifacts and update 00-实验记录.md.")
    parser.add_argument("--expid", required=True, help="ExpID, e.g. E20251225-01")
    parser.add_argument("--record-md", default=str(DEFAULT_RECORD_MD), help="Path to 00-实验记录.md")
    parser.add_argument(
        "--remote-artifacts",
        default=None,
        help="Path to <ExpID>/remote_artifacts (default: research_workspace/experiments/<ExpID>/remote_artifacts).",
    )
    parser.add_argument("--baseline-expid", default="", help="Optional target baseline ExpID for mean/std comparison on the primary K.")
    parser.add_argument("--dry-run", action="store_true", help="Preview the diff; do not write 00-实验记录.md.")
    parser.add_argument("--backup", action="store_true", help="Create a timestamped backup of 00-实验记录.md before writing.")
    args = parser.parse_args()

    expid = args.expid.strip()
    record_md = Path(args.record_md)
    if not record_md.is_file():
        raise RuntimeError(f"Record md not found: {record_md}")

    if args.remote_artifacts:
        remote_artifacts_dir = Path(args.remote_artifacts)
    else:
        remote_artifacts_dir = remote_artifacts_dir_for(expid)

    if not remote_artifacts_dir.is_dir():
        raise RuntimeError(f"remote_artifacts dir not found: {remote_artifacts_dir}")

    summaries = find_summaries(remote_artifacts_dir)
    latest = pick_primary_summary(summaries)

    results_json = _read_json(remote_artifacts_dir / "results.json") or {}
    training_info = (results_json.get("training_info") or {}) if isinstance(results_json, dict) else {}
    code_branch_raw = str(training_info.get("code_branch") or "").strip() if isinstance(training_info, dict) else ""
    code_branch = _format_code_branch(training_info if isinstance(training_info, dict) else {})
    module_name = _infer_module_name(code_branch_raw)

    remote_meta = _read_json(remote_artifacts_dir / "remote_meta.json") or {}
    remote_exp_root = ""
    if isinstance(remote_meta, dict):
        remote_exp_root = str(remote_meta.get("remote_exp_root") or "").strip()
    experiment_name_from_root = _infer_experiment_name_from_exp_root(remote_exp_root)
    experiment_name_from_results = ""
    if isinstance(training_info, dict):
        experiment_name_from_results = str(training_info.get("experiment_name") or "").strip()
    experiment_name = experiment_name_from_root or experiment_name_from_results

    main_eval = format_main_eval(latest)
    # Use mean values for the top-level index table
    k = (latest.benchmark, latest.fold, latest.nshot)
    main_result = format_main_result_mean(summaries, k)

    md_text = record_md.read_text(encoding="utf-8", errors="replace")
    before = md_text
    md_text = upsert_row_in_updated_index_table(
        md_text,
        expid=expid,
        experiment_name=experiment_name,
        module_name=module_name,
        code_branch=code_branch,
        task_desc="",
        main_eval=main_eval,
        main_result=main_result,
        remote_exp_root=remote_exp_root,
    )

    block = _build_detail_block(
        expid=expid,
        experiment_name=experiment_name,
        code_branch=code_branch,
        remote_exp_root=remote_exp_root,
        local_remote_artifacts=remote_artifacts_dir,
        latest=latest,
        summaries=summaries,
        baseline_expid=(args.baseline_expid or "").strip() or None,
    )
    md_text = upsert_detail_section(md_text, expid=expid, section_title=experiment_name, block=block)

    if args.dry_run:
        diff = difflib.unified_diff(
            before.splitlines(True),
            md_text.splitlines(True),
            fromfile=str(record_md),
            tofile=str(record_md),
        )
        print("".join(diff))
        return 0

    if args.backup:
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = record_md.with_suffix(record_md.suffix + f".bak.{ts}")
        backup_path.write_text(before, encoding="utf-8")

    record_md.write_text(md_text, encoding="utf-8")
    print(record_md.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
