#!/usr/bin/env python3
"""
Experiment Analysis - 3-Layer Analysis System

Generates structured experiment analysis reports using a 3-layer architecture:
- Layer 1: Quick Diagnosis (≤5s) - Rapid triage
- Layer 2: Root Cause Analysis - Per-class, visual, training dynamics
- Layer 3: Action Recommendations - Prioritized next steps

Usage:
    python3 analyze.py --expid E20260117-02
    python3 analyze.py --expid E20260117-02 --baseline-expid E20260113-01
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add analysis_core to path
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from analysis_core import (
    ExperimentContext,
    ExpStats,
    QuickDiagnosisEngine,
    PerClassAnalyzer,
    VisualFailureModeAnalyzer,
    TrainingDynamicsAnalyzer,
    RecommendationGenerator,
    ExperimentType,
    Status,
    AnalysisPath,
)

# Import utility functions
from analysis_core.utils import (
    load_summaries,
    load_results_json,
    pick_latest_per_k,
    DEFAULT_BASELINE_EXPID,
    DEFAULT_BASELINE_K,
    _summarize_runs_for_k,
    _summarize_results_json_for_k,
    _infer_checkpoint_from_run_dir,
    _infer_seed_from_run_dir,
    _extract_record_section,
    _extract_index_row,
    _extract_module_dir_from_record,
    _infer_compare_mode,
    _find_train_logs,
    _extract_train_snippets,
    _find_eval_log_txt,
    _parse_eval_args_from_log,
    _parse_per_class_from_log,
    _diff_eval_args_selected,
    MODEL_ID_KEYS,
    COMPARABILITY_KEYS,
)


def _metric_float(value) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _checkpoint_from_metric_item(item) -> int | None:
    if hasattr(item, "run_dir"):
        return _infer_checkpoint_from_run_dir(item.run_dir)

    if isinstance(item, dict):
        for key in ("checkpoint", "checkpoint_step", "step", "iter", "iteration"):
            value = item.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except Exception:
                pass

        for key in ("run_dir", "log_path", "summary_path"):
            value = item.get(key)
            if isinstance(value, str) and value:
                inferred = _infer_checkpoint_from_run_dir(Path(value).parent)
                if inferred is not None:
                    return inferred
                for part in Path(value).parts:
                    inferred = _infer_checkpoint_from_run_dir(Path(part))
                    if inferred is not None:
                        return inferred

    return None


def _metric_item_values(item) -> tuple[float | None, float | None]:
    if hasattr(item, "miou"):
        return _metric_float(item.miou), _metric_float(getattr(item, "fb_iou", None))

    if isinstance(item, dict):
        miou = _metric_float(item.get("mIoU", item.get("miou")))
        fb_iou = _metric_float(
            item.get("FB-IoU", item.get("FB_IoU", item.get("fb_iou")))
        )
        return miou, fb_iou

    return None, None


def _checkpoint_metric_rows(runs: list) -> list[tuple[int, float, float | None]]:
    rows: list[tuple[int, float, float | None]] = []
    seen: set[int] = set()
    for item in runs:
        checkpoint = _checkpoint_from_metric_item(item)
        miou, fb_iou = _metric_item_values(item)
        if checkpoint is None or miou is None:
            continue
        if checkpoint in seen:
            return []
        seen.add(checkpoint)
        rows.append((checkpoint, miou, fb_iou))

    if len(rows) < 2 or len(rows) != len(runs):
        return []

    return sorted(rows, key=lambda row: row[0])


def _has_multiple_seed_evidence(runs: list) -> bool:
    if _checkpoint_metric_rows(runs):
        return False

    seeds: set[int] = set()
    for item in runs:
        seed = None
        if hasattr(item, "run_dir"):
            seed = _infer_seed_from_run_dir(item.run_dir)
        elif isinstance(item, dict):
            raw_seed = item.get("seed", item.get("eval_seed"))
            try:
                seed = int(raw_seed) if raw_seed is not None else None
            except Exception:
                seed = None
        if seed is not None:
            seeds.add(seed)

    if seeds:
        return len(seeds) >= 3

    return len(runs) >= 3


def _fmt_metric(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "NA"


def _load_module_docs(module_dir: str, repo_root: Path) -> dict[str, str]:
    """
    Load module documentation files from module_research/<ModuleDir>/.

    Returns:
        dict mapping filename to content
    """
    if not module_dir:
        return {}

    docs = {}

    module_path = repo_root / "实验方案" / "module_research" / module_dir
    if not module_path.is_dir():
        return docs

    # Load all markdown files
    for md_file in module_path.glob("**/*.md"):
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            # Use relative path as key for clarity
            rel_path = md_file.relative_to(module_path)
            docs[str(rel_path)] = content
        except Exception:
            continue

    return docs


def _experiment_dir(expid: str) -> Path:
    return REPO_ROOT / "research_workspace" / "experiments" / expid


def _remote_artifacts_dir(expid: str) -> Path:
    return _experiment_dir(expid) / "remote_artifacts"


def build_experiment_context(
    *,
    expid: str,
    remote_artifacts_dir: Path,
    baseline_expid: str | None,
    baseline_remote_artifacts_dir: Path | None,
    record_md: Path,
) -> ExperimentContext:
    """Build ExperimentContext from experiment artifacts."""

    # Extract info from 00-实验记录.md
    record_lines = []
    if record_md.exists():
        record_lines = _extract_record_section(record_md, expid)

    module_dir = _extract_module_dir_from_record(record_md, expid) if record_md.exists() else ""
    experiment_name = None

    # Extract experiment name from record lines
    for line in record_lines:
        if "实验目的" in line or "experiment_name" in line:
            parts = line.split(":", 1)
            if len(parts) > 1:
                experiment_name = parts[1].strip()
                break

    # Infer compare_mode
    compare_mode = _infer_compare_mode(
        module_dir=module_dir,
        experiment_name=experiment_name,
    )

    return ExperimentContext(
        expid=expid,
        experiment_name=experiment_name,
        module_dir=module_dir or "",
        compare_mode=compare_mode,
        delta_mean=None,  # Will be filled after loading stats
        changed_flags=[],  # TODO: extract from record
        changed_keys=[],
        remote_artifacts_dir=remote_artifacts_dir,
        baseline_expid=baseline_expid,
        baseline_remote_artifacts_dir=baseline_remote_artifacts_dir,
    )


def load_eval_args_diff(
    *,
    exp_best_run_dir: Path | None,
    base_best_run_dir: Path | None,
    changed_keys: list[str],
) -> dict:
    """Load and diff eval args between exp and baseline."""

    exp_args = {}
    base_args = {}

    if exp_best_run_dir and exp_best_run_dir.is_dir():
        exp_log_path = _find_eval_log_txt(exp_best_run_dir)
        if exp_log_path:
            with open(exp_log_path, 'r', encoding='utf-8', errors='replace') as f:
                exp_log_txt = f.read()
            exp_args = _parse_eval_args_from_log(exp_log_txt)

    if base_best_run_dir and base_best_run_dir.is_dir():
        base_log_path = _find_eval_log_txt(base_best_run_dir)
        if base_log_path:
            with open(base_log_path, 'r', encoding='utf-8', errors='replace') as f:
                base_log_txt = f.read()
            base_args = _parse_eval_args_from_log(base_log_txt)

    model_diffs = _diff_eval_args_selected(exp_args, base_args, keys=list(MODEL_ID_KEYS)) if (exp_args and base_args) else []

    comparability_keys = [k for k in COMPARABILITY_KEYS if k not in set(changed_keys)]
    comparability_diffs = _diff_eval_args_selected(exp_args, base_args, keys=comparability_keys) if (exp_args and base_args) else []

    return {
        "model_id_diffs": model_diffs,
        "intended_diffs": [],
        "comparability_diffs": comparability_diffs,
    }


def format_per_class_report(output) -> str:
    """Format PerClassAnalyzer output as markdown report."""

    lines = []
    lines.append("# Per-Class 分析报告 (Layer 2)")
    lines.append("")
    lines.append(f"**分析器**: {output.name}")
    lines.append(f"**摘要**: {output.summary}")
    lines.append("")

    findings = output.findings
    baseline_present = findings.get("baseline_present", False)

    # Semantic groups summary
    groups = findings.get("groups", [])
    if groups:
        lines.append("## 语义组分析")
        lines.append("")
        lines.append("| 语义组 | 类别数 | Exp mIoU | Base mIoU | Δ |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for g in groups:
            group_name = g.get("group", "unknown")
            n_classes = g.get("n_classes", 0)
            exp_mean = g.get("exp_mean")
            base_mean = g.get("base_mean")
            delta_mean = g.get("delta_mean")

            exp_str = f"{exp_mean:.2f}" if exp_mean is not None else "NA"
            base_str = f"{base_mean:.2f}" if base_mean is not None else "NA"
            delta_str = f"{delta_mean:+.2f}" if delta_mean is not None else "NA"

            lines.append(f"| {group_name} | {n_classes} | {exp_str} | {base_str} | {delta_str} |")
        lines.append("")

    # Top drops
    top_drops = findings.get("top_drops", [])
    if top_drops and baseline_present:
        lines.append("## 退化最严重的类别 (Top Drops)")
        lines.append("")
        lines.append("| cat_idx | 类别名 | 语义组 | Exp IoU | Base IoU | Δ |")
        lines.append("| ---: | --- | --- | ---: | ---: | ---: |")
        for r in top_drops:
            cat_idx = r.get("cat_idx", "?")
            class_name = r.get("class_name", "unknown")
            semantic_group = r.get("semantic_group", "unknown")
            exp_iou = r.get("exp_iou")
            base_iou = r.get("base_iou")
            delta = r.get("delta")

            exp_str = f"{exp_iou:.2f}" if exp_iou is not None else "NA"
            base_str = f"{base_iou:.2f}" if base_iou is not None else "NA"
            delta_str = f"{delta:+.2f}" if delta is not None else "NA"

            lines.append(f"| {cat_idx} | {class_name} | {semantic_group} | {exp_str} | {base_str} | {delta_str} |")
        lines.append("")

    # Top gains
    top_gains = findings.get("top_gains", [])
    if top_gains and baseline_present:
        lines.append("## 改善最明显的类别 (Top Gains)")
        lines.append("")
        lines.append("| cat_idx | 类别名 | 语义组 | Exp IoU | Base IoU | Δ |")
        lines.append("| ---: | --- | --- | ---: | ---: | ---: |")
        for r in top_gains:
            cat_idx = r.get("cat_idx", "?")
            class_name = r.get("class_name", "unknown")
            semantic_group = r.get("semantic_group", "unknown")
            exp_iou = r.get("exp_iou")
            base_iou = r.get("base_iou")
            delta = r.get("delta")

            exp_str = f"{exp_iou:.2f}" if exp_iou is not None else "NA"
            base_str = f"{base_iou:.2f}" if base_iou is not None else "NA"
            delta_str = f"{delta:+.2f}" if delta is not None else "NA"

            lines.append(f"| {cat_idx} | {class_name} | {semantic_group} | {exp_str} | {base_str} | {delta_str} |")
        lines.append("")

    # Hypotheses
    if output.hypotheses:
        lines.append("## 假设 (Hypotheses)")
        lines.append("")
        for h in output.hypotheses:
            h_id = h.get("id", "H-UNKNOWN")
            hypothesis = h.get("hypothesis", "")
            confidence = h.get("confidence", "unknown")
            tags = h.get("tags", [])

            lines.append(f"### {h_id} [{confidence.upper()}]")
            lines.append("")
            lines.append(hypothesis)
            lines.append("")

            tests = h.get("tests", [])
            if tests:
                lines.append("**验证方法**:")
                lines.append("")
                for i, test in enumerate(tests, 1):
                    purpose = test.get("purpose", "")
                    action = test.get("action", "")
                    expected = test.get("expected_result", "")
                    lines.append(f"{i}. **目的**: {purpose}")
                    lines.append(f"   - **行动**: {action}")
                    lines.append(f"   - **预期结果**: {expected}")
                    lines.append("")

            if tags:
                lines.append(f"**标签**: {', '.join(tags)}")
                lines.append("")

    return "\n".join(lines)


def format_visual_report(output) -> str:
    """Format VisualFailureModeAnalyzer output as markdown report."""

    lines = []
    lines.append("# 可视化失败模式分析 (Layer 2)")
    lines.append("")
    lines.append(f"**分析器**: {output.name}")
    lines.append(f"**摘要**: {output.summary}")
    lines.append("")

    findings = output.findings
    total_samples = findings.get("total_samples", 0)
    failure_stats = findings.get("failure_stats", {})

    # Failure mode distribution
    lines.append("## 失败模式分布")
    lines.append("")
    lines.append("| 失败模式 | 样本数 | 占比 | 平均 IoU |")
    lines.append("| --- | ---: | ---: | ---: |")
    for mode in ["complete_failure", "severe_error", "moderate_error", "minor_error"]:
        stats = failure_stats.get(mode, {})
        count = stats.get("count", 0)
        percentage = stats.get("percentage", 0.0)
        mean_iou = stats.get("mean_iou")
        mean_str = f"{mean_iou:.2f}" if mean_iou is not None else "NA"

        mode_label = {
            "complete_failure": "完全失败 (IoU<0.01)",
            "severe_error": "严重错误 (0.01≤IoU<0.3)",
            "moderate_error": "中等错误 (0.3≤IoU<0.6)",
            "minor_error": "轻微错误 (IoU≥0.6)",
        }.get(mode, mode)

        lines.append(f"| {mode_label} | {count} | {percentage:.1f}% | {mean_str} |")
    lines.append("")

    # Small object stats
    small_obj_stats = findings.get("small_obj_stats", {})
    if small_obj_stats.get("count", 0) > 0:
        lines.append("## 小物体分析")
        lines.append("")
        lines.append(f"- **小物体样本数**: {small_obj_stats.get('count', 0)} ({small_obj_stats.get('percentage', 0.0):.1f}%)")
        mean_iou = small_obj_stats.get("mean_iou")
        if mean_iou is not None:
            lines.append(f"- **小物体平均 IoU**: {mean_iou:.2f}")
        lines.append("")

    # Semantic group stats
    group_stats = findings.get("group_stats", [])
    if group_stats:
        lines.append("## 语义组 IoU 分布")
        lines.append("")
        lines.append("| 语义组 | 样本数 | 平均 IoU |")
        lines.append("| --- | ---: | ---: |")
        for g in group_stats[:10]:  # Top 10
            group_name = g.get("group", "unknown")
            count = g.get("count", 0)
            mean_iou = g.get("mean_iou")
            mean_str = f"{mean_iou:.2f}" if mean_iou is not None else "NA"
            lines.append(f"| {group_name} | {count} | {mean_str} |")
        lines.append("")

    # Hypotheses
    if output.hypotheses:
        lines.append("## 假设 (Hypotheses)")
        lines.append("")
        for h in output.hypotheses:
            h_id = h.get("id", "H-UNKNOWN")
            hypothesis = h.get("hypothesis", "")
            confidence = h.get("confidence", "unknown")
            tags = h.get("tags", [])

            lines.append(f"### {h_id} [{confidence.upper()}]")
            lines.append("")
            lines.append(hypothesis)
            lines.append("")

            tests = h.get("tests", [])
            if tests:
                lines.append("**验证方法**:")
                lines.append("")
                for i, test in enumerate(tests, 1):
                    purpose = test.get("purpose", "")
                    action = test.get("action", "")
                    expected = test.get("expected_result", "")
                    lines.append(f"{i}. **目的**: {purpose}")
                    lines.append(f"   - **行动**: {action}")
                    lines.append(f"   - **预期结果**: {expected}")
                    lines.append("")

            if tags:
                lines.append(f"**标签**: {', '.join(tags)}")
                lines.append("")

    return "\n".join(lines)


def format_training_report(output) -> str:
    """Format TrainingDynamicsAnalyzer output as markdown report."""

    lines = []
    lines.append("# 训练动态分析 (Layer 2)")
    lines.append("")
    lines.append(f"**分析器**: {output.name}")
    lines.append(f"**摘要**: {output.summary}")
    lines.append("")

    findings = output.findings
    train_loss_stats = findings.get("train_loss_stats", {})
    lr_stats = findings.get("lr_stats", {})
    loss_component_stats = findings.get("loss_component_stats", {})

    # Training loss statistics
    lines.append("## 训练损失统计")
    lines.append("")
    n_steps = train_loss_stats.get("n_steps", 0)
    lines.append(f"- **训练步数**: {n_steps}")

    initial = train_loss_stats.get("initial")
    final = train_loss_stats.get("final")
    if initial is not None:
        lines.append(f"- **初始损失**: {initial:.4f}")
    if final is not None:
        lines.append(f"- **最终损失**: {final:.4f}")

    mean = train_loss_stats.get("mean")
    std = train_loss_stats.get("std")
    if mean is not None:
        lines.append(f"- **平均损失**: {mean:.4f}")
    if std is not None:
        lines.append(f"- **损失标准差**: {std:.4f}")

    convergence_rate = train_loss_stats.get("convergence_rate")
    if convergence_rate is not None:
        lines.append(f"- **收敛速率**: {convergence_rate:.6f} per 1000 steps")
    lines.append("")

    # Learning rate
    if lr_stats:
        lines.append("## 学习率")
        lines.append("")
        lr_initial = lr_stats.get("initial")
        lr_final = lr_stats.get("final")
        if lr_initial is not None:
            lines.append(f"- **初始学习率**: {lr_initial:.6f}")
        if lr_final is not None:
            lines.append(f"- **最终学习率**: {lr_final:.6f}")
        lines.append("")

    # Loss components
    if loss_component_stats:
        lines.append("## 损失组件")
        lines.append("")
        lines.append("| 组件 | 平均值 | 最终值 |")
        lines.append("| --- | ---: | ---: |")
        for comp_name, comp_stats in loss_component_stats.items():
            mean_val = comp_stats.get("mean")
            final_val = comp_stats.get("final")
            mean_str = f"{mean_val:.4f}" if mean_val is not None else "NA"
            final_str = f"{final_val:.4f}" if final_val is not None else "NA"
            lines.append(f"| {comp_name} | {mean_str} | {final_str} |")
        lines.append("")

    # Red flags
    red_flags = findings.get("red_flags", [])
    if red_flags:
        lines.append("## 🚩 训练警告")
        lines.append("")
        for flag in red_flags:
            code = flag.get("code", "UNKNOWN")
            severity = flag.get("severity", "unknown")
            message = flag.get("message", "")
            lines.append(f"### [{severity.upper()}] {code}")
            lines.append("")
            lines.append(message)
            lines.append("")

    # Hypotheses
    if output.hypotheses:
        lines.append("## 假设 (Hypotheses)")
        lines.append("")
        for h in output.hypotheses:
            h_id = h.get("id", "H-UNKNOWN")
            hypothesis = h.get("hypothesis", "")
            confidence = h.get("confidence", "unknown")
            tags = h.get("tags", [])

            lines.append(f"### {h_id} [{confidence.upper()}]")
            lines.append("")
            lines.append(hypothesis)
            lines.append("")

            tests = h.get("tests", [])
            if tests:
                lines.append("**验证方法**:")
                lines.append("")
                for i, test in enumerate(tests, 1):
                    purpose = test.get("purpose", "")
                    action = test.get("action", "")
                    expected = test.get("expected_result", "")
                    lines.append(f"{i}. **目的**: {purpose}")
                    lines.append(f"   - **行动**: {action}")
                    lines.append(f"   - **预期结果**: {expected}")
                    lines.append("")

            if tags:
                lines.append(f"**标签**: {', '.join(tags)}")
                lines.append("")

    return "\n".join(lines)


def format_recommendations_report(recommendations, diagnosis) -> str:
    """Format Layer 3 recommendations as markdown report."""

    lines = []
    lines.append("# 行动建议 (Layer 3)")
    lines.append("")
    lines.append(f"**分析路径**: {diagnosis.analysis_path.value}")
    lines.append(f"**建议数量**: {len(recommendations)}")
    lines.append("")

    if not recommendations:
        lines.append("暂无建议。")
        return "\n".join(lines)

    # Group by priority
    by_priority = {"P0": [], "P1": [], "P2": []}
    for rec in recommendations:
        by_priority[rec.priority.value].append(rec)

    # Render by priority
    for priority in ["P0", "P1", "P2"]:
        recs = by_priority[priority]
        if not recs:
            continue

        priority_label = {
            "P0": "🔴 P0 - 关键优先级",
            "P1": "🟡 P1 - 高优先级",
            "P2": "🟢 P2 - 中优先级",
        }.get(priority, priority)

        lines.append(f"## {priority_label}")
        lines.append("")

        for i, rec in enumerate(recs, 1):
            lines.append(f"### {priority}-{i}: {rec.purpose}")
            lines.append("")
            lines.append(f"**置信度**: {rec.confidence.value.upper()}")
            lines.append("")
            lines.append(f"**行动**: {rec.action}")
            lines.append("")
            lines.append(f"**预期结果**: {rec.expected_result}")
            lines.append("")
            lines.append(f"**成本**: {rec.cost}")
            lines.append("")

    return "\n".join(lines)


def format_diagnosis_report(diagnosis, ctx: ExperimentContext, exp_stats: ExpStats, base_stats: ExpStats | None, required_evidence: dict[str, bool]) -> str:
    """Format quick diagnosis as markdown report."""

    lines = []
    lines.append("# 快速诊断报告 (Layer 1)")
    lines.append("")
    lines.append(f"**实验ID**: {ctx.expid}")
    lines.append(f"**实验类型**: {diagnosis.experiment_type.value}")
    lines.append(f"**状态**: {diagnosis.status.value}")
    lines.append(f"**分析路径**: {diagnosis.analysis_path.value}")
    lines.append("")

    # Scorecard
    lines.append("## 指标概览")
    lines.append("")
    checkpoint_rows = _checkpoint_metric_rows(exp_stats.runs)
    if checkpoint_rows:
        lines.append("**当前实验按 checkpoint 记录**:")
        lines.append("")
        lines.append("| checkpoint | mIoU | FB-IoU |")
        lines.append("|---|---:|---:|")
        for checkpoint, miou, fb_iou in checkpoint_rows:
            lines.append(f"| {checkpoint} | {_fmt_metric(miou)} | {_fmt_metric(fb_iou)} |")
    elif exp_stats.miou_mean is not None:
        if exp_stats.miou_std is not None:
            lines.append(f"- **当前实验**: mIoU = {exp_stats.miou_mean:.2f} ± {exp_stats.miou_std:.2f}")
        else:
            lines.append(f"- **当前实验**: mIoU = {exp_stats.miou_mean:.2f}")
    else:
        lines.append("- **当前实验**: mIoU = NA")

    if base_stats and base_stats.miou_mean is not None:
        if base_stats.miou_std is not None:
            lines.append(f"- **基线**: mIoU = {base_stats.miou_mean:.2f} ± {base_stats.miou_std:.2f}")
        else:
            lines.append(f"- **基线**: mIoU = {base_stats.miou_mean:.2f}")
        delta = exp_stats.miou_mean - base_stats.miou_mean if (exp_stats.miou_mean is not None and base_stats.miou_mean is not None) else None
        if delta is not None:
            lines.append(f"- **Δmean**: {delta:+.2f}")
    lines.append("")

    # Notes
    lines.append("## 诊断说明")
    lines.append("")
    for note in diagnosis.notes:
        lines.append(f"- {note}")
    lines.append("")

    # Red flags
    if diagnosis.red_flags:
        lines.append("## 🚩 红旗警告")
        lines.append("")
        for flag in diagnosis.red_flags:
            lines.append(f"### [{flag.severity.value.upper()}] {flag.code}")
            lines.append("")
            lines.append(flag.message)
            lines.append("")
    else:
        lines.append("## ✅ 未发现明显问题")
        lines.append("")

    # Next steps based on analysis path
    lines.append("## 建议的下一步")
    lines.append("")

    if diagnosis.analysis_path == AnalysisPath.abort:
        lines.append("⚠️ **立即中止并修复严重问题**")
        lines.append("")
        lines.append("在解决以下严重问题之前，不建议继续分析指标：")
        for flag in diagnosis.red_flags:
            if flag.severity.value == "critical":
                lines.append(f"- {flag.message}")

    elif diagnosis.analysis_path == AnalysisPath.diagnose:
        lines.append("🔍 **深入诊断根因**")
        lines.append("")
        lines.append("建议执行以下诊断步骤：")
        lines.append("1. 检查可比性：确保 baseline 和当前实验的评估参数一致")
        lines.append("2. Per-class 分析：定位哪些类别退化/提升")
        lines.append("3. 可视化分析：检查 worst 样例的失败模式")
        lines.append("4. 模块机制分析：理解模块如何影响 baseline")

    elif diagnosis.analysis_path == AnalysisPath.investigate:
        lines.append("🔬 **补齐证据**")
        lines.append("")

        # Dynamically generate recommendations based on missing evidence
        missing_items = []
        if not required_evidence.get("has_multiple_seeds", False):
            missing_items.append("增加 seed 数量（建议至少 3 个：0/42/3407）")
        if not required_evidence.get("has_vis", False):
            missing_items.append("开启可视化（visualize=1）")
        if not required_evidence.get("has_per_class_data", False):
            missing_items.append("确保 eval log 包含 per-class 信息")

        if missing_items:
            lines.append("当前证据不足以做出明确判断，建议：")
            for i, item in enumerate(missing_items, 1):
                lines.append(f"{i}. {item}")
        else:
            lines.append("证据已充分，但结果不确定（方差大或 delta 接近 0）。建议：")
            lines.append("1. 检查训练日志是否有异常")
            lines.append("2. 分析 per-class 和可视化结果以定位问题")
            lines.append("3. 考虑调整超参或模块配置")

    elif diagnosis.analysis_path == AnalysisPath.optimize:
        lines.append("🚀 **优化与扩展验证**")
        lines.append("")
        lines.append("实验显示提升，建议：")
        lines.append("1. 扩展验证：在其他 fold/shot 上测试")
        lines.append("2. 低成本优化：调整推理超参（denoise_steps 等）")
        lines.append("3. 稳健性检查：确保提升不是偶然（增加 seed）")

    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="3-Layer Experiment Analysis System")
    parser.add_argument("--expid", required=True, help="Experiment ID (e.g., E20260117-02)")
    parser.add_argument("--baseline-expid", default=DEFAULT_BASELINE_EXPID, help=f"Baseline ExpID (default: {DEFAULT_BASELINE_EXPID})")
    parser.add_argument("--record-md", default=str(REPO_ROOT / "research_workspace" / "00-实验记录.md"), help="Path to 00-实验记录.md")
    parser.add_argument("--output", help="Output markdown file (default: <expid>/analysis.md)")
    args = parser.parse_args()

    expid = args.expid.strip()
    baseline_expid = args.baseline_expid.strip() if args.baseline_expid else None
    record_md = Path(args.record_md)

    # Locate the canonical experiment directory.
    exp_dir = _experiment_dir(expid)
    remote_artifacts_dir = _remote_artifacts_dir(expid)

    if not remote_artifacts_dir.is_dir():
        print(f"ERROR: remote_artifacts not found: {remote_artifacts_dir}")
        return 1

    baseline_remote_artifacts_dir = None
    if baseline_expid:
        baseline_remote_artifacts_dir = _remote_artifacts_dir(baseline_expid)
        if not baseline_remote_artifacts_dir.is_dir():
            print(f"WARNING: Baseline remote_artifacts not found: {baseline_remote_artifacts_dir}")
            baseline_remote_artifacts_dir = None

    print(f"Loading artifacts for {expid}...")

    # Load summaries (preferred) or fallback to results.json
    summaries = load_summaries(remote_artifacts_dir)
    results = None

    if summaries:
        latest_per_k = pick_latest_per_k(summaries)
        primary_k = DEFAULT_BASELINE_K if DEFAULT_BASELINE_K in latest_per_k else sorted(latest_per_k.keys())[0]
    else:
        results = load_results_json(remote_artifacts_dir)
        if not results:
            print("ERROR: No summary.json files found in remote_artifacts/eval and results.json is missing")
            return 1

        primary_k = None
        training_info = results.get("training_info")
        if isinstance(training_info, dict):
            try:
                b = str(training_info.get("benchmark", "")).strip()
                f = int(training_info.get("fold", 0))
                s = int(training_info.get("nshot", 1))
                primary_k = (b, f, s) if b else None
            except Exception:
                primary_k = None

        if primary_k is None:
            metrics = results.get("evaluation_metrics")
            if isinstance(metrics, list) and metrics and isinstance(metrics[0], dict):
                try:
                    primary_k = (str(metrics[0].get("benchmark", "")).strip(), int(metrics[0].get("fold", 0)), int(metrics[0].get("nshot", 1)))
                except Exception:
                    primary_k = None

        if primary_k is None:
            print("ERROR: Unable to infer (benchmark, fold, nshot) from results.json")
            return 1

    # Load baseline summaries
    baseline_summaries = []
    baseline_latest_per_k = {}
    baseline_results = None
    if baseline_remote_artifacts_dir:
        baseline_summaries = load_summaries(baseline_remote_artifacts_dir)
        baseline_latest_per_k = pick_latest_per_k(baseline_summaries)
        if not baseline_summaries:
            baseline_results = load_results_json(baseline_remote_artifacts_dir)

    # Build stats
    if summaries:
        exp_stats_raw = _summarize_runs_for_k(summaries=summaries, k=primary_k, rel_to=remote_artifacts_dir)
    else:
        assert results is not None
        exp_stats_raw = _summarize_results_json_for_k(results=results, k=primary_k, rel_to=remote_artifacts_dir)

    if baseline_summaries:
        base_stats_raw = _summarize_runs_for_k(summaries=baseline_summaries, k=primary_k, rel_to=baseline_remote_artifacts_dir)
    elif baseline_results and baseline_remote_artifacts_dir:
        base_stats_raw = _summarize_results_json_for_k(results=baseline_results, k=primary_k, rel_to=baseline_remote_artifacts_dir)
    else:
        base_stats_raw = None

    exp_stats = ExpStats(
        k=primary_k,
        miou_mean=exp_stats_raw.get("miou_mean"),
        miou_std=exp_stats_raw.get("miou_std"),
        fb_iou_mean=exp_stats_raw.get("fb_mean"),
        runs=exp_stats_raw.get("items", []),
        best_run_rel=exp_stats_raw.get("best_run_rel"),
    )

    base_stats = None
    if base_stats_raw:
        base_stats = ExpStats(
            k=primary_k,
            miou_mean=base_stats_raw.get("miou_mean"),
            miou_std=base_stats_raw.get("miou_std"),
            fb_iou_mean=base_stats_raw.get("fb_mean"),
            runs=base_stats_raw.get("items", []),
            best_run_rel=base_stats_raw.get("best_run_rel"),
        )

    # Build context
    ctx = build_experiment_context(
        expid=expid,
        remote_artifacts_dir=remote_artifacts_dir,
        baseline_expid=baseline_expid,
        baseline_remote_artifacts_dir=baseline_remote_artifacts_dir,
        record_md=record_md,
    )

    # Load module documentation if available
    module_docs = _load_module_docs(ctx.module_dir, REPO_ROOT)
    if module_docs:
        print(f"Loaded {len(module_docs)} module documentation file(s) from {ctx.module_dir}")

    # Update delta_mean
    if exp_stats.miou_mean is not None and base_stats and base_stats.miou_mean is not None:
        ctx.delta_mean = exp_stats.miou_mean - base_stats.miou_mean

    # Load eval args diff
    exp_best_run_dir = remote_artifacts_dir / exp_stats.best_run_rel if exp_stats.best_run_rel else None
    base_best_run_dir = baseline_remote_artifacts_dir / base_stats.best_run_rel if (base_stats and base_stats.best_run_rel and baseline_remote_artifacts_dir) else None

    eval_args_diff = load_eval_args_diff(
        exp_best_run_dir=exp_best_run_dir,
        base_best_run_dir=base_best_run_dir,
        changed_keys=ctx.changed_keys,
    )

    # Load train snippets
    train_logs = _find_train_logs(remote_artifacts_dir)
    train_snippets = []
    if train_logs:
        for log_path in train_logs[:3]:  # Limit to first 3 logs
            snippet = _extract_train_snippets(log_path, max_lines=50)
            if snippet:
                train_snippets.append(snippet)

    # Required evidence - Enhanced detection
    has_eval_log = exp_best_run_dir is not None and _find_eval_log_txt(exp_best_run_dir) is not None if exp_best_run_dir else False

    # Check for actual visualization files
    has_vis_files = False
    if exp_best_run_dir:
        vis_dir = exp_best_run_dir / "vis"
        if vis_dir.exists():
            # Check for index.jsonl or any image files
            has_vis_files = (vis_dir / "index.jsonl").exists() or any(vis_dir.glob("*.png")) or any(vis_dir.glob("*.jpg"))

    # Check for per-class data in eval log
    has_per_class_data = False
    if has_eval_log:
        exp_log_path = _find_eval_log_txt(exp_best_run_dir)
        with open(exp_log_path, 'r', encoding='utf-8', errors='replace') as f:
            exp_log_txt = f.read()
        exp_per_class_check = _parse_per_class_from_log(exp_log_txt)
        has_per_class_data = len(exp_per_class_check) > 0

    # Check for multiple seeds; multiple checkpoint evals are not seed evidence.
    has_multiple_seeds = _has_multiple_seed_evidence(exp_stats.runs)

    required_evidence = {
        "has_eval_log": has_eval_log,
        "has_vis": has_vis_files,
        "has_per_class_data": has_per_class_data,
        "has_multiple_seeds": has_multiple_seeds,
    }

    print("\n" + "="*80)
    print("Running Layer 1 Quick Diagnosis...")
    print("="*80 + "\n")

    # Run Layer 1
    engine = QuickDiagnosisEngine()
    diagnosis = engine.run(
        ctx=ctx,
        exp_stats=exp_stats,
        base_stats=base_stats,
        train_snippets=train_snippets,
        eval_args_diff=eval_args_diff,
        required_evidence=required_evidence,
    )

    # Format report
    report = format_diagnosis_report(diagnosis, ctx, exp_stats, base_stats, required_evidence)

    # Print to console
    print(report)

    # Run Layer 2 analyzers
    layer2_reports = []

    # Layer 2a: PerClassAnalyzer (if eval logs available)
    if exp_best_run_dir and _find_eval_log_txt(exp_best_run_dir):
        print("\n" + "="*80)
        print("Running Layer 2a: PerClassAnalyzer...")
        print("="*80 + "\n")

        # Parse per-class data
        exp_log_path = _find_eval_log_txt(exp_best_run_dir)
        with open(exp_log_path, 'r', encoding='utf-8', errors='replace') as f:
            exp_log_txt = f.read()
        exp_per_class = _parse_per_class_from_log(exp_log_txt)

        base_per_class = []
        base_eval_log_rel = None
        if base_best_run_dir and _find_eval_log_txt(base_best_run_dir):
            base_log_path = _find_eval_log_txt(base_best_run_dir)
            with open(base_log_path, 'r', encoding='utf-8', errors='replace') as f:
                base_log_txt = f.read()
            base_per_class = _parse_per_class_from_log(base_log_txt)
            base_eval_log_rel = str(base_log_path.relative_to(baseline_remote_artifacts_dir)) if baseline_remote_artifacts_dir else None

        exp_eval_log_rel = str(exp_log_path.relative_to(remote_artifacts_dir))

        # Run PerClassAnalyzer
        analyzer = PerClassAnalyzer(topn=6, split="val")
        per_class_output = analyzer.run(
            exp_stats=exp_stats,
            exp_per_class=exp_per_class,
            base_stats=base_stats,
            base_per_class=base_per_class,
            exp_eval_log_rel=exp_eval_log_rel,
            base_eval_log_rel=base_eval_log_rel,
        )

        # Format per-class report
        per_class_report = format_per_class_report(per_class_output)
        print(per_class_report)
        layer2_reports.append(per_class_report)

    # Layer 2b: VisualFailureModeAnalyzer (if vis available)
    exp_vis_dir = None
    if exp_best_run_dir:
        exp_vis_dir = exp_best_run_dir / "vis"
        if exp_vis_dir.is_dir() and (exp_vis_dir / "index.jsonl").exists():
            print("\n" + "="*80)
            print("Running Layer 2b: VisualFailureModeAnalyzer...")
            print("="*80 + "\n")

            base_vis_dir = None
            if base_best_run_dir:
                base_vis_dir = base_best_run_dir / "vis"
                if not (base_vis_dir.is_dir() and (base_vis_dir / "index.jsonl").exists()):
                    base_vis_dir = None

            # Run VisualFailureModeAnalyzer
            visual_analyzer = VisualFailureModeAnalyzer(topn=10)
            visual_output = visual_analyzer.run(
                exp_stats=exp_stats,
                exp_vis_dir=exp_vis_dir,
                base_vis_dir=base_vis_dir,
                per_class_findings=per_class_output.findings if 'per_class_output' in locals() else None,
            )

            # Format visual report
            visual_report = format_visual_report(visual_output)
            print(visual_report)
            layer2_reports.append(visual_report)

    # Layer 2c: TrainingDynamicsAnalyzer (if TensorBoard logs available)
    exp_tb_dir = remote_artifacts_dir / "train" / "logs" / "tensorboard"
    if exp_tb_dir.is_dir():
        # Find the actual tensorboard directory (may have subdirectory)
        tb_subdirs = [d for d in exp_tb_dir.iterdir() if d.is_dir()]
        if tb_subdirs:
            exp_tb_dir = tb_subdirs[0]  # Use first subdirectory

        # Check if event files exist
        event_files = list(exp_tb_dir.glob("events.out.tfevents.*"))
        if event_files:
            print("\n" + "="*80)
            print("Running Layer 2c: TrainingDynamicsAnalyzer...")
            print("="*80 + "\n")

            base_tb_dir = None
            if baseline_remote_artifacts_dir:
                base_tb_dir = baseline_remote_artifacts_dir / "train" / "logs" / "tensorboard"
                if base_tb_dir.is_dir():
                    base_tb_subdirs = [d for d in base_tb_dir.iterdir() if d.is_dir()]
                    if base_tb_subdirs:
                        base_tb_dir = base_tb_subdirs[0]
                    if not list(base_tb_dir.glob("events.out.tfevents.*")):
                        base_tb_dir = None
                else:
                    base_tb_dir = None

            # Run TrainingDynamicsAnalyzer
            training_analyzer = TrainingDynamicsAnalyzer()
            training_output = training_analyzer.run(
                exp_stats=exp_stats,
                exp_tb_dir=exp_tb_dir,
                base_tb_dir=base_tb_dir,
            )

            # Format training report
            training_report = format_training_report(training_output)
            print(training_report)
            layer2_reports.append(training_report)

    # Run Layer 3: RecommendationGenerator
    print("\n" + "="*80)
    print("Running Layer 3: RecommendationGenerator...")
    print("="*80 + "\n")

    # Collect all Layer 2 outputs
    layer2_analyzer_outputs = []
    if 'per_class_output' in locals():
        layer2_analyzer_outputs.append(per_class_output)
    if 'visual_output' in locals():
        layer2_analyzer_outputs.append(visual_output)
    if 'training_output' in locals():
        layer2_analyzer_outputs.append(training_output)

    # Generate recommendations
    rec_generator = RecommendationGenerator()
    recommendations = rec_generator.run(
        diagnosis=diagnosis,
        layer2_outputs=layer2_analyzer_outputs,
        required_evidence=required_evidence,
        module_docs=module_docs,
        module_dir=ctx.module_dir,
    )

    # Format recommendations report
    rec_report = format_recommendations_report(recommendations, diagnosis)
    print(rec_report)

    # Write to file
    output_path = Path(args.output) if args.output else (exp_dir / "analysis.md")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    full_report = report
    for layer2_report in layer2_reports:
        full_report += "\n\n" + layer2_report
    full_report += "\n\n" + rec_report

    output_path.write_text(full_report, encoding='utf-8')

    print("\n" + "="*80)
    print(f"Report saved to: {output_path}")
    print("="*80)

    return 0


if __name__ == "__main__":
    sys.exit(main())
