"""
Layer 2: Training Dynamics Analyzer

Inputs:
- TensorBoard event files from remote_artifacts/train/logs/tensorboard/

Responsibilities:
1) Parse TensorBoard event files (train_loss, lr, loss/base, loss/total, loss/pw_mse)
2) Analyze convergence, stability, overfitting, training efficiency
3) Detect anomalies (NaN, divergence, plateau)
4) Generate evidence-based hypotheses about training dynamics
5) Produce structured AnalyzerOutput for Layer 3 consumption

Note: Uses TensorBoard official library for event parsing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .data_models import AnalyzerOutput, EvidenceRef, ExpStats, RedFlag, RedFlagSeverity

# TensorBoard imports
try:
    from tensorboard.backend.event_processing import event_accumulator
except ImportError:
    event_accumulator = None


def _mean(values: list[float]) -> float | None:
    vals = [float(x) for x in values if x == x]  # drop NaN
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _std(values: list[float]) -> float | None:
    vals = [float(x) for x in values if x == x]
    if len(vals) < 2:
        return None
    mean_val = sum(vals) / len(vals)
    variance = sum((x - mean_val) ** 2 for x in vals) / (len(vals) - 1)
    return float(variance ** 0.5)


def _load_tensorboard_scalars(tb_dir: Path) -> dict[str, list[tuple[int, float]]]:
    """Load scalar metrics from TensorBoard event files.

    Returns:
        dict mapping tag -> list of (step, value) tuples
    """
    if event_accumulator is None:
        return {}

    if not tb_dir.exists():
        return {}

    # Find event files
    event_files = list(tb_dir.glob("events.out.tfevents.*"))
    if not event_files:
        return {}

    # Load with EventAccumulator
    ea = event_accumulator.EventAccumulator(str(tb_dir))
    ea.Reload()

    scalars: dict[str, list[tuple[int, float]]] = {}
    for tag in ea.Tags().get("scalars", []):
        events = ea.Scalars(tag)
        scalars[tag] = [(e.step, e.value) for e in events]

    return scalars


def _detect_nan_or_inf(values: list[float]) -> bool:
    """Check if any value is NaN or Inf."""
    for v in values:
        if v != v or abs(v) == float('inf'):
            return True
    return False


def _detect_divergence(values: list[float], threshold: float = 100.0) -> bool:
    """Check if loss diverges (increases beyond threshold)."""
    if len(values) < 10:
        return False

    # Check if recent values are much larger than early values
    early_mean = _mean(values[:10])
    recent_mean = _mean(values[-10:])

    if early_mean is None or recent_mean is None:
        return False

    return recent_mean > early_mean * threshold


def _detect_plateau(values: list[float], window: int = 100, threshold: float = 0.01) -> bool:
    """Check if loss plateaus (no significant improvement in recent window)."""
    if len(values) < window * 2:
        return False

    # Compare recent window to earlier window
    early_mean = _mean(values[-window*2:-window])
    recent_mean = _mean(values[-window:])

    if early_mean is None or recent_mean is None or early_mean == 0:
        return False

    improvement = (early_mean - recent_mean) / early_mean
    return improvement < threshold


def _compute_convergence_rate(values: list[float]) -> float | None:
    """Estimate convergence rate (loss reduction per 1000 steps)."""
    if len(values) < 100:
        return None

    # Use first 10% and last 10% to estimate rate
    n = len(values)
    early_mean = _mean(values[:n//10])
    late_mean = _mean(values[-n//10:])

    if early_mean is None or late_mean is None or early_mean == 0:
        return None

    reduction = early_mean - late_mean
    rate_per_1000 = (reduction / n) * 1000
    return float(rate_per_1000)


@dataclass
class TrainingDynamicsAnalyzer:
    """
    Layer 2 analyzer: training dynamics from TensorBoard logs.

    This analyzer uses TensorBoard official library to parse event files.
    """

    def run(
        self,
        *,
        exp_stats: ExpStats,
        exp_tb_dir: Path,
        base_tb_dir: Path | None = None,
    ) -> AnalyzerOutput:
        benchmark, fold, nshot = exp_stats.k

        # Load TensorBoard scalars
        exp_scalars = _load_tensorboard_scalars(exp_tb_dir)
        base_scalars = _load_tensorboard_scalars(base_tb_dir) if base_tb_dir else {}

        baseline_present = bool(base_scalars)

        # Extract key metrics
        train_loss = exp_scalars.get("train_loss", [])
        lr_values = exp_scalars.get("lr", [])
        loss_base = exp_scalars.get("loss/base", [])
        loss_total = exp_scalars.get("loss/total", [])
        loss_pw_mse = exp_scalars.get("loss/pw_mse", [])

        # Extract values only (drop steps)
        train_loss_vals = [v for _, v in train_loss]
        lr_vals = [v for _, v in lr_values]
        loss_base_vals = [v for _, v in loss_base]
        loss_total_vals = [v for _, v in loss_total]
        loss_pw_mse_vals = [v for _, v in loss_pw_mse]

        # Red flags
        red_flags: list[RedFlag] = []

        # Check for NaN/Inf
        if _detect_nan_or_inf(train_loss_vals):
            red_flags.append(RedFlag(
                code="TRAIN_NAN",
                severity=RedFlagSeverity.critical,
                message="Training loss contains NaN or Inf values, indicating numerical instability.",
                evidence=[EvidenceRef(kind="metric", snippet="train_loss contains NaN/Inf")],
            ))

        # Check for divergence
        if _detect_divergence(train_loss_vals):
            red_flags.append(RedFlag(
                code="TRAIN_DIVERGENCE",
                severity=RedFlagSeverity.high,
                message="Training loss diverges (increases significantly), suggesting learning rate too high or gradient explosion.",
                evidence=[EvidenceRef(kind="metric", snippet="train_loss diverges")],
            ))

        # Check for plateau
        if _detect_plateau(train_loss_vals):
            red_flags.append(RedFlag(
                code="TRAIN_PLATEAU",
                severity=RedFlagSeverity.medium,
                message="Training loss plateaus with no significant improvement in recent steps.",
                evidence=[EvidenceRef(kind="metric", snippet="train_loss plateaus")],
            ))

        # Compute statistics
        train_loss_stats = {
            "n_steps": len(train_loss_vals),
            "initial": train_loss_vals[0] if train_loss_vals else None,
            "final": train_loss_vals[-1] if train_loss_vals else None,
            "mean": _mean(train_loss_vals),
            "std": _std(train_loss_vals),
            "convergence_rate": _compute_convergence_rate(train_loss_vals),
        }

        lr_stats = {
            "initial": lr_vals[0] if lr_vals else None,
            "final": lr_vals[-1] if lr_vals else None,
            "mean": _mean(lr_vals),
        }

        loss_component_stats = {
            "loss_base": {
                "mean": _mean(loss_base_vals),
                "final": loss_base_vals[-1] if loss_base_vals else None,
            },
            "loss_total": {
                "mean": _mean(loss_total_vals),
                "final": loss_total_vals[-1] if loss_total_vals else None,
            },
            "loss_pw_mse": {
                "mean": _mean(loss_pw_mse_vals),
                "final": loss_pw_mse_vals[-1] if loss_pw_mse_vals else None,
            },
        }

        # Evidence refs
        evidence: list[EvidenceRef] = []
        if exp_tb_dir:
            evidence.append(
                EvidenceRef(
                    kind="file",
                    path=str(exp_tb_dir),
                    meta={"role": "exp_tensorboard", "n_steps": len(train_loss_vals)},
                )
            )
        if base_tb_dir:
            evidence.append(
                EvidenceRef(
                    kind="file",
                    path=str(base_tb_dir),
                    meta={"role": "baseline_tensorboard"},
                )
            )

        # Hypotheses: evidence-based, testable
        hypotheses: list[dict[str, Any]] = []

        # H1: Slow convergence
        convergence_rate = train_loss_stats.get("convergence_rate")
        if convergence_rate is not None and convergence_rate < 0.001:
            hypotheses.append({
                "id": "H-TRAIN-SLOW-CONVERGENCE",
                "hypothesis": (
                    f"训练收敛缓慢（速率={convergence_rate:.6f} per 1000 steps），"
                    "表明学习率可能过低或优化效率不高。"
                ),
                "evidence": {
                    "convergence_rate": convergence_rate,
                    "train_loss_stats": train_loss_stats,
                },
                "tests": [
                    {
                        "purpose": "测试更高的学习率是否改善收敛",
                        "action": "将学习率提高2倍并监控收敛速率。",
                        "expected_result": "如果收敛速率改善且不发散，说明LR过低。",
                    },
                    {
                        "purpose": "检查优化器是否合适",
                        "action": "尝试不同的优化器（例如：AdamW -> SGD with momentum）并比较收敛。",
                        "expected_result": "如果收敛改善，说明优化器选择很重要。",
                    },
                ],
                "confidence": "medium",
                "tags": ["training", "convergence", "post_hoc"],
            })

        # H2: Loss component imbalance
        loss_base_mean = loss_component_stats["loss_base"].get("mean")
        loss_pw_mse_mean = loss_component_stats["loss_pw_mse"].get("mean")

        if loss_base_mean is not None and loss_pw_mse_mean is not None:
            if loss_pw_mse_mean > loss_base_mean * 10:  # PW-MSE dominates
                hypotheses.append({
                    "id": "H-TRAIN-LOSS-IMBALANCE",
                    "hypothesis": (
                        f"损失组件不平衡：loss/pw_mse（均值={loss_pw_mse_mean:.4f}）主导"
                        f"loss/base（均值={loss_base_mean:.4f}），可能掩盖baseline损失信号。"
                    ),
                    "evidence": {
                        "loss_component_stats": loss_component_stats,
                        "ratio": loss_pw_mse_mean / loss_base_mean if loss_base_mean > 0 else None,
                    },
                    "tests": [
                        {
                            "purpose": "测试重新平衡是否改善指标",
                            "action": "将PW-MSE损失权重降低50%并重新训练；比较mIoU。",
                            "expected_result": "如果mIoU改善，说明损失不平衡损害了baseline性能。",
                        },
                        {
                            "purpose": "完全消融PW-MSE损失",
                            "action": "禁用PW-MSE损失并重新训练；比较mIoU。",
                            "expected_result": "如果mIoU相似或更好，说明PW-MSE可能没有益处。",
                        },
                    ],
                    "confidence": "low",
                    "tags": ["training", "loss_components", "post_hoc"],
                })

        # H3: Training instability (high variance)
        train_loss_std = train_loss_stats.get("std")
        train_loss_mean = train_loss_stats.get("mean")

        if train_loss_std is not None and train_loss_mean is not None and train_loss_mean > 0:
            cv = train_loss_std / train_loss_mean  # coefficient of variation
            if cv > 0.3:  # >30% variation
                hypotheses.append({
                    "id": "H-TRAIN-INSTABILITY",
                    "hypothesis": (
                        f"训练损失显示出高方差（std={train_loss_std:.4f}，mean={train_loss_mean:.4f}，CV={cv:.2f}），"
                        "表明训练不稳定（例如：batch size过小、需要梯度裁剪）。"
                    ),
                    "evidence": {
                        "train_loss_stats": train_loss_stats,
                        "coefficient_of_variation": cv,
                    },
                    "tests": [
                        {
                            "purpose": "测试梯度裁剪是否稳定训练",
                            "action": "启用梯度裁剪（例如：max_norm=1.0）并监控损失方差。",
                            "expected_result": "如果方差降低，说明梯度爆炸导致了不稳定。",
                        },
                        {
                            "purpose": "检查batch size是否影响稳定性",
                            "action": "注意：batch_size=1是K/V Bank的架构约束；无法增加。",
                            "expected_result": "N/A（batch_size固定为1）。",
                        },
                    ],
                    "confidence": "medium",
                    "tags": ["training", "stability", "post_hoc"],
                })

        # Findings
        findings: dict[str, Any] = {
            "k": {"benchmark": benchmark, "fold": fold, "nshot": nshot},
            "baseline_present": baseline_present,
            "train_loss_stats": train_loss_stats,
            "lr_stats": lr_stats,
            "loss_component_stats": loss_component_stats,
            "red_flags": [{"code": f.code, "severity": f.severity.value, "message": f.message} for f in red_flags],
        }

        summary_bits: list[str] = []
        summary_bits.append(f"K=({benchmark},{fold},{nshot})")
        summary_bits.append(f"{train_loss_stats['n_steps']} training steps")
        if red_flags:
            summary_bits.append(f"{len(red_flags)} red flags")

        return AnalyzerOutput(
            name="TrainingDynamicsAnalyzer",
            summary="; ".join(summary_bits),
            findings=findings,
            evidence=evidence,
            hypotheses=hypotheses,
        )
