"""
Layer 2: Per-class Analyzer

Inputs:
- Per-class data parsed from eval logs: list of (cat_idx, iou)
- cat_idx is episode-local index (for COCO folds: 0..19 for val split)

Responsibilities:
1) Map cat_idx -> internal class_id (0..79) -> COCO category_id -> class_name
2) Semantic grouping (animals, vehicles, furniture, ...)
3) Delta analysis vs baseline (largest drops/gains), handle missing baseline gracefully
4) Evidence-based hypothesis generation (testable, not direct conclusions)
5) Produce structured `AnalyzerOutput.findings` for Layer 3 consumption
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .coco_meta import (
    cat_idx_to_internal_class_id,
    internal_class_id_to_coco_id,
    internal_class_id_to_name,
    internal_class_id_to_supercategory,
    is_small_object_class_name,
)
from .data_models import AnalyzerOutput, EvidenceRef, ExpStats


def _pairs_to_map(pairs: list[tuple[int, float]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for k, v in pairs or []:
        try:
            out[int(k)] = float(v)
        except Exception:
            continue
    return out


def _mean(values: list[float]) -> float | None:
    vals = [float(x) for x in values if x == x]  # drop NaN
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _top_per_class_deltas(
    *,
    exp_pairs: list[tuple[int, float]],
    base_pairs: list[tuple[int, float]],
    topn: int = 6,
) -> tuple[list[tuple[int, float, float, float]], list[tuple[int, float, float, float]]]:
    """
    Returns (worst_drops, best_gains) as list of (cat_idx, exp_iou, base_iou, delta).

    Kept intentionally similar to `scripts/analyze.py:_top_per_class_deltas` to reuse logic.
    """
    exp_map = _pairs_to_map(exp_pairs)
    base_map = _pairs_to_map(base_pairs)
    rows: list[tuple[int, float, float, float]] = []
    for cat_idx in sorted(set(exp_map.keys()) | set(base_map.keys())):
        e = exp_map.get(cat_idx, float("nan"))
        b = base_map.get(cat_idx, float("nan"))
        if not (e == e and b == b):  # nan check
            continue
        rows.append((cat_idx, e, b, e - b))
    rows_sorted = sorted(rows, key=lambda r: r[3])
    worst = rows_sorted[:topn]
    best = list(reversed(rows_sorted[-topn:])) if rows_sorted else []
    return worst, best


def _semantic_group_for_supercategory(supercategory: str | None) -> str:
    s = (supercategory or "").strip().lower()
    if not s:
        return "unknown"
    # Keep groups aligned with common analysis needs (animals/vehicles/furniture...).
    mapping = {
        "person": "person",
        "vehicle": "vehicles",
        "animal": "animals",
        "furniture": "furniture",
        "electronic": "electronics",
        "appliance": "appliances",
        "kitchen": "kitchenware",
        "food": "food",
        "outdoor": "outdoor",
        "accessory": "accessories",
        "indoor": "indoor",
    }
    return mapping.get(s, s)


def _render_class_row(
    *,
    benchmark: str,
    fold: int,
    split: str,
    cat_idx: int,
    exp_iou: float | None,
    base_iou: float | None,
) -> dict[str, Any]:
    internal_id: int | None = None
    coco_id: int | None = None
    name: str | None = None
    supercategory: str | None = None
    semantic_group: str = "unknown"

    if benchmark.strip().lower() == "coco":
        internal_id = cat_idx_to_internal_class_id(cat_idx=cat_idx, fold=fold, split=split)
        if internal_id is not None:
            coco_id = internal_class_id_to_coco_id(internal_id)
            name = internal_class_id_to_name(internal_id)
            supercategory = internal_class_id_to_supercategory(internal_id)
            semantic_group = _semantic_group_for_supercategory(supercategory)

    delta: float | None = None
    if exp_iou is not None and base_iou is not None:
        delta = float(exp_iou - base_iou)

    return {
        "cat_idx": cat_idx,
        "class_internal_id": internal_id,
        "coco_id": coco_id,
        "class_name": name,
        "supercategory": supercategory,
        "semantic_group": semantic_group,
        "is_small_object": is_small_object_class_name(name),
        "exp_iou": exp_iou,
        "base_iou": base_iou,
        "delta": delta,
    }


@dataclass
class PerClassAnalyzer:
    """
    Layer 2 analyzer: per-class breakdown + delta patterns.

    This analyzer is intentionally model-agnostic and uses only eval-log per-class IoUs.
    """

    topn: int = 6
    split: str = "val"

    def run(
        self,
        *,
        exp_stats: ExpStats,
        exp_per_class: list[tuple[int, float]],
        base_stats: ExpStats | None = None,
        base_per_class: list[tuple[int, float]] | None = None,
        exp_eval_log_rel: str | None = None,
        base_eval_log_rel: str | None = None,
    ) -> AnalyzerOutput:
        benchmark, fold, nshot = exp_stats.k
        base_pairs = base_per_class or []
        exp_map = _pairs_to_map(exp_per_class)
        base_map = _pairs_to_map(base_pairs)

        # Build union of cat_idx keys to be robust to partial logs.
        cat_indices = sorted(set(exp_map.keys()) | set(base_map.keys()))

        class_rows: list[dict[str, Any]] = []
        for cat_idx in cat_indices:
            exp_iou = exp_map.get(cat_idx)
            base_iou = base_map.get(cat_idx) if base_pairs else None
            class_rows.append(
                _render_class_row(
                    benchmark=benchmark,
                    fold=fold,
                    split=self.split,
                    cat_idx=cat_idx,
                    exp_iou=exp_iou,
                    base_iou=base_iou,
                )
            )

        baseline_present = bool(base_stats is not None and base_pairs)

        # Group stats
        by_group: dict[str, list[dict[str, Any]]] = {}
        for r in class_rows:
            by_group.setdefault(str(r.get("semantic_group") or "unknown"), []).append(r)

        groups: list[dict[str, Any]] = []
        for g, items in sorted(by_group.items(), key=lambda kv: kv[0]):
            exp_vals = [x["exp_iou"] for x in items if x.get("exp_iou") is not None]
            base_vals = [x["base_iou"] for x in items if x.get("base_iou") is not None]
            deltas = [x["delta"] for x in items if x.get("delta") is not None]
            groups.append(
                {
                    "group": g,
                    "n_classes": len(items),
                    "exp_mean": _mean([float(v) for v in exp_vals]),
                    "base_mean": (_mean([float(v) for v in base_vals]) if baseline_present else None),
                    "delta_mean": (_mean([float(v) for v in deltas]) if baseline_present else None),
                }
            )

        # Top deltas
        worst_drops: list[dict[str, Any]] = []
        best_gains: list[dict[str, Any]] = []
        if baseline_present:
            worst, best = _top_per_class_deltas(exp_pairs=exp_per_class, base_pairs=base_pairs, topn=self.topn)
            for cat_idx, exp_iou, base_iou, delta in worst:
                worst_drops.append(
                    _render_class_row(
                        benchmark=benchmark,
                        fold=fold,
                        split=self.split,
                        cat_idx=cat_idx,
                        exp_iou=exp_iou,
                        base_iou=base_iou,
                    )
                    | {"delta": float(delta)}
                )
            for cat_idx, exp_iou, base_iou, delta in best:
                best_gains.append(
                    _render_class_row(
                        benchmark=benchmark,
                        fold=fold,
                        split=self.split,
                        cat_idx=cat_idx,
                        exp_iou=exp_iou,
                        base_iou=base_iou,
                    )
                    | {"delta": float(delta)}
                )

        # Pattern mining (evidence-only summaries)
        patterns: list[dict[str, Any]] = []
        if baseline_present:
            group_sorted = sorted(
                [g for g in groups if g.get("delta_mean") is not None],
                key=lambda x: float(x["delta_mean"]),
            )
            if group_sorted:
                patterns.append({"type": "group_delta_sorted", "groups": group_sorted})

            # Small-object pattern
            small = [r for r in class_rows if r.get("is_small_object") and r.get("delta") is not None]
            if len(small) >= 3:
                small_delta_mean = _mean([float(r["delta"]) for r in small if r.get("delta") is not None])
                patterns.append(
                    {
                        "type": "small_object_delta",
                        "n": len(small),
                        "delta_mean": small_delta_mean,
                    }
                )

        # Evidence refs
        evidence: list[EvidenceRef] = []
        if exp_eval_log_rel:
            evidence.append(EvidenceRef(kind="file", path=str(exp_eval_log_rel), meta={"role": "exp_eval_log"}))
        if base_eval_log_rel:
            evidence.append(EvidenceRef(kind="file", path=str(base_eval_log_rel), meta={"role": "baseline_eval_log"}))
        if baseline_present:
            evidence.append(
                EvidenceRef(
                    kind="metric",
                    snippet="per-class deltas computed from exp/base eval logs (cat_idx->class mapping + IoU deltas)",
                    meta={"topn": self.topn, "benchmark": benchmark, "fold": fold, "nshot": nshot},
                )
            )
        else:
            evidence.append(
                EvidenceRef(
                    kind="metric",
                    snippet="per-class breakdown computed from exp eval log only (baseline per-class missing)",
                    meta={"topn": self.topn, "benchmark": benchmark, "fold": fold, "nshot": nshot},
                )
            )

        # Hypotheses: evidence-based, testable, not direct conclusions.
        hypotheses: list[dict[str, Any]] = []
        if not baseline_present:
            hypotheses.append(
                {
                    "id": "H-PC-BASELINE-MISSING",
                    "hypothesis": "缺少相同K配置下的baseline逐类别分解数据，无法将观察到的逐类别IoU归因于模块效果还是运行间方差。",
                    "evidence": {
                        "baseline_present": False,
                        "n_classes_in_log": len(class_rows),
                    },
                    "tests": [
                        {
                            "purpose": "启用逐类别delta分析",
                            "action": "收集相同(benchmark, fold, nshot)配置下的baseline评估日志（包含逐类别行），重新计算delta。",
                            "expected_result": "Top drops/gains变得可用；假设可以与类别级delta关联。",
                        }
                    ],
                    "confidence": "high",
                    "tags": ["evidence_gap", "per_class"],
                }
            )
        else:
            # 1) Group-level delta hypothesis (largest drop group)
            neg_groups = [g for g in groups if g.get("delta_mean") is not None and g.get("n_classes", 0) >= 3]
            neg_groups_sorted = sorted(neg_groups, key=lambda x: float(x["delta_mean"]))
            if neg_groups_sorted:
                worst_group = neg_groups_sorted[0]
                hypotheses.append(
                    {
                        "id": "H-PC-GROUP-TRADEOFF",
                        "hypothesis": (
                            "该改动可能引入了语义权衡：某些语义组的退化程度超过其他组，"
                            "表明存在组依赖的失败模式（例如：纹理 vs. 边界 vs. 上下文依赖）。"
                        ),
                        "evidence": {
                            "worst_group": worst_group,
                            "worst_drops": worst_drops[: min(5, len(worst_drops))],
                            "best_gains": best_gains[: min(5, len(best_gains))],
                        },
                        "tests": [
                            {
                                "purpose": "通过可视化验证组依赖的失败模式",
                                "action": "从最差delta组中采样10个episode，对比baseline vs exp的预测结果（FN/FP/边界错误）。",
                                "expected_result": "该组出现一致的失败模式；为针对性修复提供信息。",
                            },
                            {
                                "purpose": "检查效果是否对seed稳健",
                                "action": "运行至少3个eval seed并重新计算逐类别delta；验证相同的组是否仍然最差。",
                                "expected_result": "如果是系统性问题，组级退化会在不同seed间持续存在。",
                            },
                        ],
                        "confidence": "low",
                        "tags": ["per_class", "semantic_group", "post_hoc"],
                    }
                )

            # 2) Small-object degradation hypothesis
            small_pattern = next((p for p in patterns if p.get("type") == "small_object_delta"), None)
            if small_pattern and (small_pattern.get("delta_mean") is not None):
                hypotheses.append(
                    {
                        "id": "H-PC-SMALL-OBJECTS",
                        "hypothesis": (
                            "该改动可能降低了细节/边界精度，不成比例地损害小物体"
                            "（过度平滑或高频细节不足的常见症状）。"
                        ),
                        "evidence": {
                            "small_object_delta": small_pattern,
                            "worst_drops": [r for r in worst_drops if r.get("is_small_object")][:5],
                        },
                        "tests": [
                            {
                                "purpose": "检查边界/细节错误",
                                "action": "检查最差的小物体episode；标注主要失败类型（边界侵蚀 vs FN vs FP）。",
                                "expected_result": "如果是过度平滑，边界会收缩/模糊，细小部分的FN增加。",
                            },
                            {
                                "purpose": "对推理保真度的敏感性",
                                "action": "保持其他不变；仅增加推理保真度参数（如denoise steps）并对比逐类别delta。",
                                "expected_result": "如果问题与推理细节相关，小物体delta会改善。",
                            },
                        ],
                        "confidence": "low",
                        "tags": ["per_class", "small_objects", "post_hoc"],
                    }
                )

        findings: dict[str, Any] = {
            "k": {"benchmark": benchmark, "fold": fold, "nshot": nshot},
            "split": self.split,
            "baseline_present": baseline_present,
            "classes": class_rows,
            "groups": groups,
            "top_drops": worst_drops,
            "top_gains": best_gains,
            "patterns": patterns,
        }

        summary_bits: list[str] = []
        summary_bits.append(f"K=({benchmark},{fold},{nshot})")
        if baseline_present:
            summary_bits.append(f"Δ per-class computed (topn={self.topn})")
        else:
            summary_bits.append("baseline per-class missing (no deltas)")

        return AnalyzerOutput(
            name="PerClassAnalyzer",
            summary="; ".join(summary_bits),
            findings=findings,
            evidence=evidence,
            hypotheses=hypotheses,
        )
