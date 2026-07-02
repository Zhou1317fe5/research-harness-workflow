"""
Layer 1: Quick Diagnosis Engine

Components:
- ExperimentClassifier: Identify experiment type
- SuccessCriteria: Judge success/failure/inconclusive
- RedFlagDetector: Detect critical issues
- DecisionRouter: Route to appropriate analysis path
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .data_models import (
    ExperimentType,
    Status,
    AnalysisPath,
    RedFlagSeverity,
    RedFlag,
    EvidenceRef,
    ExpStats,
    ExperimentContext,
    QuickDiagnosis,
)


@dataclass
class ExperimentClassifier:
    """Classify experiment type based on context."""

    def __init__(self):
        # Import baseline config dynamically
        try:
            from .utils import DEFAULT_BASELINE_EXPID
            self.baseline_expid_default = DEFAULT_BASELINE_EXPID
        except ImportError:
            self.baseline_expid_default = "E20260113-01"

    def classify(
        self,
        *,
        expid: str,
        module_dir: str,
        experiment_name: str | None,
        delta_mean: float | None,
        compare_mode: str,
    ) -> ExperimentType:
        """
        Classify experiment type.

        Returns:
            ExperimentType: baseline/module_e2e/ablation/hyperparam/neutral
        """
        name = (experiment_name or "").lower()
        mdir = (module_dir or "").strip()

        # 1) Hard baseline
        if expid == self.baseline_expid_default:
            return ExperimentType.baseline
        if ("baseline" in name or "sd_init" in name) and not mdir:
            return ExperimentType.baseline

        # 2) Ablation pattern
        if "ablation" in name or "abl" in name or compare_mode == "infer_ablation":
            return ExperimentType.ablation

        # 3) Hyperparam pattern (by naming)
        hyperparam_keywords = ["sweep", "hp", "grid", "lr", "denoise", "tau", "lambda"]
        if any(k in name for k in hyperparam_keywords):
            return ExperimentType.hyperparam

        # 4) Module end-to-end (module dir exists + e2e)
        if mdir and compare_mode == "e2e":
            return ExperimentType.module_e2e

        # 5) Fallback: neutral bucket
        return ExperimentType.neutral


@dataclass
class SuccessCriteria:
    """Judge experiment success based on metrics and type."""

    # Thresholds align with existing health routing hints (analyze.py:_assess_module_health)
    success_delta: float = 0.5
    fail_delta: float = -1.0
    high_variance_std: float = 0.5

    def judge(
        self,
        *,
        exp_stats: ExpStats,
        base_stats: ExpStats | None,
        experiment_type: ExperimentType,
    ) -> Status:
        """
        Judge experiment status.

        Returns:
            Status: success/failure/inconclusive
        """
        if base_stats is None or exp_stats.miou_mean is None or base_stats.miou_mean is None:
            return Status.inconclusive

        delta = exp_stats.miou_mean - base_stats.miou_mean
        exp_std = exp_stats.miou_std

        # Baseline: treat as inconclusive unless within sanity range
        if experiment_type == ExperimentType.baseline:
            return Status.inconclusive

        # Variance gate: too noisy => inconclusive (need more seeds)
        if exp_std is not None and exp_std >= self.high_variance_std:
            return Status.inconclusive

        # Type-specific thresholds
        if delta >= self.success_delta:
            return Status.success
        if delta <= self.fail_delta:
            return Status.failure

        return Status.inconclusive


@dataclass
class RedFlagDetector:
    """Detect critical issues that require immediate attention."""

    def detect(
        self,
        *,
        train_snippets: list[str],         # from existing _extract_train_snippets
        eval_args_diff: dict[str, Any],    # (model_id_diffs/intended_diffs/comparability_diffs)
        compare_mode: str,
        required_evidence: dict[str, bool], # e.g. {"has_eval_log": True, "has_vis": False}
    ) -> list[RedFlag]:
        """
        Detect red flags from evidence.

        Returns:
            List of RedFlag objects with severity and evidence
        """
        flags: list[RedFlag] = []

        # A) Training crash/anomaly
        if any("关键异常/告警" in ln for ln in train_snippets):
            flags.append(RedFlag(
                code="TRAIN_ANOMALY",
                severity=RedFlagSeverity.critical,
                message="训练日志命中异常关键词（traceback/error/nan/oom 等），先排查稳定性再谈指标。",
                evidence=[EvidenceRef(kind="snippet", snippet="\n".join(train_snippets[:40]))],
            ))

        # B) Comparability mismatches (K/threshold/seed)
        comparability_diffs = eval_args_diff.get("comparability_diffs") or []
        for (k, ev, bv) in comparability_diffs:
            flags.append(RedFlag(
                code="EVAL_NOT_COMPARABLE",
                severity=RedFlagSeverity.high,
                message=f"评估关键参数不一致：{k} exp={ev} vs base={bv}（高风险混淆）。",
                evidence=[EvidenceRef(kind="diff", meta={"key": k, "exp": ev, "base": bv})],
            ))

        # C) Checkpoint mismatch severity depends on compare_mode
        model_diffs = eval_args_diff.get("model_id_diffs") or []
        if model_diffs:
            sev = RedFlagSeverity.high if compare_mode == "infer_ablation" else RedFlagSeverity.low
            msg = ("infer_ablation 下 checkpoint/unet_ckpt_path 不同属于高风险混淆"
                   if compare_mode == "infer_ablation"
                   else "e2e 对比允许 checkpoint 不同（仅作记录）")
            flags.append(RedFlag(
                code="MODEL_ID_MISMATCH",
                severity=sev,
                message=msg,
                evidence=[EvidenceRef(kind="diff", meta={"model_id_diffs": model_diffs})],
            ))

        # D) Missing evidence (not fatal but routes to investigate)
        if required_evidence.get("has_eval_log") is False:
            flags.append(RedFlag(
                code="MISSING_EVAL_LOG",
                severity=RedFlagSeverity.medium,
                message="缺少 eval log，无法核验 args/per-class。",
            ))
        if required_evidence.get("has_vis") is False:
            flags.append(RedFlag(
                code="MISSING_VIS",
                severity=RedFlagSeverity.low,
                message="缺少 vis，可视化失败模式无法定位（建议 visualize=1）。",
            ))
        if required_evidence.get("has_per_class_data") is False:
            flags.append(RedFlag(
                code="MISSING_PER_CLASS",
                severity=RedFlagSeverity.low,
                message="eval log 中缺少 per-class 信息，无法进行逐类别分析。",
            ))
        if required_evidence.get("has_multiple_seeds") is False:
            flags.append(RedFlag(
                code="INSUFFICIENT_SEEDS",
                severity=RedFlagSeverity.low,
                message="seed 数量不足（建议至少 3 个：0/42/3407），无法评估运行间方差。",
            ))

        return flags


@dataclass
class DecisionRouter:
    """Route to appropriate analysis path based on quick diagnosis."""

    def route(
        self,
        *,
        experiment_type: ExperimentType,
        status: Status,
        red_flags: list[RedFlag],
    ) -> AnalysisPath:
        """
        Determine analysis path.

        Returns:
            AnalysisPath: optimize/diagnose/investigate/abort
        """
        # Critical red flags => abort
        if any(f.severity == RedFlagSeverity.critical for f in red_flags):
            return AnalysisPath.abort

        # High-severity comparability issues => diagnose
        high_severity_codes = {"EVAL_NOT_COMPARABLE", "MODEL_ID_MISMATCH"}
        if any(f.code in high_severity_codes and f.severity == RedFlagSeverity.high
               for f in red_flags):
            return AnalysisPath.diagnose

        # Route by status
        if status == Status.success:
            return AnalysisPath.optimize
        if status == Status.failure:
            return AnalysisPath.diagnose

        return AnalysisPath.investigate


@dataclass
class QuickDiagnosisEngine:
    """
    Layer 1 engine: orchestrates quick diagnosis components.

    Usage:
        engine = QuickDiagnosisEngine()
        diagnosis = engine.run(ctx, exp_stats, base_stats, train_snippets, eval_args_diff, evidence)
    """

    classifier: ExperimentClassifier = None
    criteria: SuccessCriteria = None
    detector: RedFlagDetector = None
    router: DecisionRouter = None

    def __post_init__(self):
        if self.classifier is None:
            self.classifier = ExperimentClassifier()
        if self.criteria is None:
            self.criteria = SuccessCriteria()
        if self.detector is None:
            self.detector = RedFlagDetector()
        if self.router is None:
            self.router = DecisionRouter()

    def run(
        self,
        *,
        ctx: ExperimentContext,
        exp_stats: ExpStats,
        base_stats: ExpStats | None,
        train_snippets: list[str],
        eval_args_diff: dict[str, Any],
        required_evidence: dict[str, bool],
    ) -> QuickDiagnosis:
        """
        Run quick diagnosis (Layer 1).

        Returns:
            QuickDiagnosis with type/status/red_flags/path
        """
        # Step 1: Classify experiment type
        exp_type = self.classifier.classify(
            expid=ctx.expid,
            module_dir=ctx.module_dir,
            experiment_name=ctx.experiment_name,
            delta_mean=ctx.delta_mean,
            compare_mode=ctx.compare_mode,
        )

        # Step 2: Judge success
        status = self.criteria.judge(
            exp_stats=exp_stats,
            base_stats=base_stats,
            experiment_type=exp_type,
        )

        # Step 3: Detect red flags
        red_flags = self.detector.detect(
            train_snippets=train_snippets,
            eval_args_diff=eval_args_diff,
            compare_mode=ctx.compare_mode,
            required_evidence=required_evidence,
        )

        # Step 4: Route to analysis path
        path = self.router.route(
            experiment_type=exp_type,
            status=status,
            red_flags=red_flags,
        )

        # Generate notes
        notes = self._generate_notes(exp_type, status, path, red_flags)

        return QuickDiagnosis(
            experiment_type=exp_type,
            status=status,
            red_flags=red_flags,
            analysis_path=path,
            notes=notes,
        )

    def _generate_notes(
        self,
        exp_type: ExperimentType,
        status: Status,
        path: AnalysisPath,
        red_flags: list[RedFlag],
    ) -> list[str]:
        """Generate human-readable notes for quick diagnosis."""
        notes = []

        # Type note
        type_msgs = {
            ExperimentType.baseline: "这是 baseline 实验",
            ExperimentType.module_e2e: "这是模块端到端对比实验",
            ExperimentType.ablation: "这是消融实验",
            ExperimentType.hyperparam: "这是超参数调优实验",
            ExperimentType.neutral: "实验类型不明确（delta 接近 0 或缺少模块标识）",
        }
        notes.append(type_msgs.get(exp_type, "未知实验类型"))

        # Status note
        status_msgs = {
            Status.success: "指标提升明显（Δmean ≥ 0.5）",
            Status.failure: "指标退化明显（Δmean ≤ -1.0）",
            Status.inconclusive: "结果不确定（方差大或 delta 接近 0）",
        }
        notes.append(status_msgs.get(status, "状态未知"))

        # Red flag summary
        if red_flags:
            critical = sum(1 for f in red_flags if f.severity == RedFlagSeverity.critical)
            high = sum(1 for f in red_flags if f.severity == RedFlagSeverity.high)
            if critical > 0:
                notes.append(f"发现 {critical} 个严重问题（critical）")
            if high > 0:
                notes.append(f"发现 {high} 个高风险问题（high）")
        else:
            notes.append("未发现明显红旗")

        # Path note
        path_msgs = {
            AnalysisPath.optimize: "建议路径：优化（扩展验证/低成本改进）",
            AnalysisPath.diagnose: "建议路径：诊断（定位退化根因）",
            AnalysisPath.investigate: "建议路径：调查（补齐证据）",
            AnalysisPath.abort: "建议路径：中止（先修复严重问题）",
        }
        notes.append(path_msgs.get(path, "未知路径"))

        return notes
