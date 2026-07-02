"""
Layer 2: Visual Failure Mode Analyzer

Inputs:
- vis/index.jsonl metadata from remote_artifacts/eval/{run_dir}/vis/
- Per-class findings from PerClassAnalyzer (optional cross-reference)

Responsibilities:
1) Parse vis metadata (IoU distribution, failure patterns)
2) Identify systematic failure modes (complete failures, boundary errors, small objects)
3) Cross-reference with per-class deltas if available
4) Generate evidence-based hypotheses about visual failure patterns
5) Produce structured AnalyzerOutput for Layer 3 consumption

Note: This analyzer uses heuristic analysis on metadata only (no image processing).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .coco_meta import (
    internal_class_id_to_name,
    internal_class_id_to_supercategory,
    is_small_object_class_name,
)
from .data_models import AnalyzerOutput, EvidenceRef, ExpStats


def _mean(values: list[float]) -> float | None:
    vals = [float(x) for x in values if x == x]  # drop NaN
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _load_vis_index(vis_dir: Path) -> list[dict[str, Any]]:
    """Load vis/index.jsonl metadata."""
    index_path = vis_dir / "index.jsonl"
    if not index_path.exists():
        return []

    records = []
    with open(index_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def _classify_failure_mode(iou: float) -> str:
    """Classify failure mode based on IoU threshold."""
    if iou < 0.01:
        return "complete_failure"  # FN or complete miss
    elif iou < 0.3:
        return "severe_error"      # Major FN/FP or boundary issues
    elif iou < 0.6:
        return "moderate_error"    # Moderate boundary/detail errors
    else:
        return "minor_error"       # Small refinement issues


@dataclass
class VisualFailureModeAnalyzer:
    """
    Layer 2 analyzer: visual failure mode patterns from vis metadata.

    This analyzer is intentionally lightweight and uses only metadata (no image processing).
    """

    topn: int = 10

    def run(
        self,
        *,
        exp_stats: ExpStats,
        exp_vis_dir: Path,
        base_vis_dir: Path | None = None,
        per_class_findings: dict[str, Any] | None = None,
    ) -> AnalyzerOutput:
        benchmark, fold, nshot = exp_stats.k

        # Load vis metadata
        exp_records = _load_vis_index(exp_vis_dir)
        base_records = _load_vis_index(base_vis_dir) if base_vis_dir else []

        baseline_present = bool(base_records)

        # Classify failure modes
        exp_failures: dict[str, list[dict[str, Any]]] = {
            "complete_failure": [],
            "severe_error": [],
            "moderate_error": [],
            "minor_error": [],
        }

        for rec in exp_records:
            iou = rec.get("iou")
            if iou is None:
                continue
            mode = _classify_failure_mode(float(iou))

            class_id = rec.get("class_id")
            class_name = internal_class_id_to_name(class_id) if class_id is not None else None
            supercategory = internal_class_id_to_supercategory(class_id) if class_id is not None else None

            exp_failures[mode].append({
                "iou": float(iou),
                "class_id": class_id,
                "class_name": class_name,
                "supercategory": supercategory,
                "is_small_object": is_small_object_class_name(class_name),
                "episode_hash": rec.get("episode_hash8"),
                "policy": rec.get("policy"),
            })

        # Aggregate statistics
        total_samples = len(exp_records)
        failure_stats = {
            mode: {
                "count": len(samples),
                "percentage": 100.0 * len(samples) / total_samples if total_samples > 0 else 0.0,
                "mean_iou": _mean([s["iou"] for s in samples]),
            }
            for mode, samples in exp_failures.items()
        }

        # Small object analysis
        small_obj_samples = [
            s for samples in exp_failures.values() for s in samples if s.get("is_small_object")
        ]
        small_obj_stats = {
            "count": len(small_obj_samples),
            "percentage": 100.0 * len(small_obj_samples) / total_samples if total_samples > 0 else 0.0,
            "mean_iou": _mean([s["iou"] for s in small_obj_samples]),
        }

        # Semantic group analysis
        by_group: dict[str, list[float]] = {}
        for samples in exp_failures.values():
            for s in samples:
                group = s.get("supercategory") or "unknown"
                by_group.setdefault(group, []).append(s["iou"])

        group_stats = [
            {
                "group": group,
                "count": len(ious),
                "mean_iou": _mean(ious),
            }
            for group, ious in sorted(by_group.items(), key=lambda kv: _mean(kv[1]) or 0.0)
        ]

        # Top worst samples
        worst_samples = sorted(
            [s for samples in exp_failures.values() for s in samples],
            key=lambda s: s["iou"]
        )[:self.topn]

        # Evidence refs
        evidence: list[EvidenceRef] = []
        if exp_vis_dir:
            evidence.append(
                EvidenceRef(
                    kind="file",
                    path=str(exp_vis_dir / "index.jsonl"),
                    meta={"role": "exp_vis_metadata", "n_samples": total_samples},
                )
            )
        if base_vis_dir:
            evidence.append(
                EvidenceRef(
                    kind="file",
                    path=str(base_vis_dir / "index.jsonl"),
                    meta={"role": "baseline_vis_metadata", "n_samples": len(base_records)},
                )
            )

        # Hypotheses: evidence-based, testable
        hypotheses: list[dict[str, Any]] = []

        # H1: Complete failure rate
        complete_failure_rate = failure_stats["complete_failure"]["percentage"]
        if complete_failure_rate > 5.0:  # >5% complete failures
            hypotheses.append({
                "id": "H-VIS-COMPLETE-FAILURE",
                "hypothesis": (
                    f"模型表现出{complete_failure_rate:.1f}%的完全失败率（IoU < 0.01），"
                    "表明某些类别或episode存在系统性问题（例如：support-query不匹配、"
                    "极端视角/尺度变化、或K/V bank损坏）。"
                ),
                "evidence": {
                    "complete_failure_count": failure_stats["complete_failure"]["count"],
                    "complete_failure_rate": complete_failure_rate,
                    "worst_samples": worst_samples[:5],
                },
                "tests": [
                    {
                        "purpose": "识别完全失败的根本原因",
                        "action": "手动检查前10个完全失败的episode；检查support-query相似度、mask质量和K/V bank状态。",
                        "expected_result": "如果support-query不匹配很常见，考虑更严格的episode采样；如果mask质量差，重新审视mask注入。",
                    },
                    {
                        "purpose": "检查失败是否与类别相关",
                        "action": "将完全失败的class_ids与PerClassAnalyzer的top drops交叉引用；验证是否出现相同类别。",
                        "expected_result": "如果重叠度高，失败与类别相关；重点关注这些类别。",
                    },
                ],
                "confidence": "medium",
                "tags": ["visual", "complete_failure", "post_hoc"],
            })

        # H2: Small object degradation (cross-reference with per-class)
        if small_obj_stats["count"] >= 5 and small_obj_stats["mean_iou"] is not None:
            small_obj_mean_iou = small_obj_stats["mean_iou"]
            overall_mean_iou = exp_stats.miou_mean or 0.0

            if small_obj_mean_iou < overall_mean_iou - 5.0:  # >5 point gap
                hypotheses.append({
                    "id": "H-VIS-SMALL-OBJECTS",
                    "hypothesis": (
                        f"小物体显示出不成比例的低IoU（均值={small_obj_mean_iou:.2f} vs 总体={overall_mean_iou:.2f}），"
                        "与扩散过程中的边界侵蚀或细节保留不足一致。"
                    ),
                    "evidence": {
                        "small_obj_stats": small_obj_stats,
                        "overall_mean_iou": overall_mean_iou,
                        "gap": overall_mean_iou - small_obj_mean_iou,
                    },
                    "tests": [
                        {
                            "purpose": "验证边界侵蚀假设",
                            "action": "检查小物体最差样本；测量边界精度（边缘的FN vs 背景的FP）。",
                            "expected_result": "如果边界侵蚀为真，FN在物体边缘占主导；考虑增加denoise_steps或调整guidance_scale。",
                        },
                        {
                            "purpose": "与逐类别分析交叉引用",
                            "action": "检查PerClassAnalyzer是否在top drops中识别出小物体类别；验证一致性。",
                            "expected_result": "如果一致，小物体问题是系统性的；优先修复此类别。",
                        },
                    ],
                    "confidence": "medium",
                    "tags": ["visual", "small_objects", "post_hoc"],
                })

        # H3: Semantic group disparity (if group stats show large variance)
        if len(group_stats) >= 3:
            worst_group = group_stats[0]
            best_group = group_stats[-1]
            worst_mean = worst_group.get("mean_iou") or 0.0
            best_mean = best_group.get("mean_iou") or 0.0

            if best_mean - worst_mean > 10.0:  # >10 point gap
                hypotheses.append({
                    "id": "H-VIS-GROUP-DISPARITY",
                    "hypothesis": (
                        f"语义组显示出较大的IoU差异（最差={worst_group['group']} at {worst_mean:.2f}，"
                        f"最好={best_group['group']} at {best_mean:.2f}），表明存在组依赖的失败模式"
                        "（例如：纹理丰富 vs 边界依赖的类别）。"
                    ),
                    "evidence": {
                        "worst_group": worst_group,
                        "best_group": best_group,
                        "gap": best_mean - worst_mean,
                        "group_stats": group_stats,
                    },
                    "tests": [
                        {
                            "purpose": "验证组依赖的失败模式",
                            "action": "从最差组中采样10个episode；分析失败模式（FN/FP/边界）。",
                            "expected_result": "如果出现一致的模式（例如：全是边界错误），为针对性修复提供信息。",
                        },
                        {
                            "purpose": "与逐类别语义组交叉引用",
                            "action": "检查PerClassAnalyzer是否识别出相同的最差组；验证一致性。",
                            "expected_result": "如果一致，组级问题是系统性的。",
                        },
                    ],
                    "confidence": "low",
                    "tags": ["visual", "semantic_group", "post_hoc"],
                })

        # Findings
        findings: dict[str, Any] = {
            "k": {"benchmark": benchmark, "fold": fold, "nshot": nshot},
            "baseline_present": baseline_present,
            "total_samples": total_samples,
            "failure_stats": failure_stats,
            "small_obj_stats": small_obj_stats,
            "group_stats": group_stats,
            "worst_samples": worst_samples,
        }

        summary_bits: list[str] = []
        summary_bits.append(f"K=({benchmark},{fold},{nshot})")
        summary_bits.append(f"{total_samples} vis samples analyzed")
        summary_bits.append(f"complete_failure={complete_failure_rate:.1f}%")

        return AnalyzerOutput(
            name="VisualFailureModeAnalyzer",
            summary="; ".join(summary_bits),
            findings=findings,
            evidence=evidence,
            hypotheses=hypotheses,
        )
