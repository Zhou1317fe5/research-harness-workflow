"""
Layer 3: Action Recommendation Generator

Inputs:
- QuickDiagnosis from Layer 1
- List of AnalyzerOutput from Layer 2 analyzers

Responsibilities:
1) Synthesize findings from all layers
2) Generate prioritized, actionable recommendations
3) Each recommendation includes: purpose, action, expected_result, cost, confidence, priority
4) Route recommendations based on analysis_path (optimize/diagnose/investigate/abort)
5) Produce structured list[Recommendation] for user consumption
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .data_models import (
    AnalyzerOutput,
    AnalysisPath,
    Confidence,
    EvidenceRef,
    Priority,
    QuickDiagnosis,
    Recommendation,
    RedFlagSeverity,
    Status,
)


def _extract_tests_from_hypotheses(hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract testable actions from Layer 2 hypotheses."""
    tests = []
    for h in hypotheses:
        h_id = h.get("id", "H-UNKNOWN")
        confidence = h.get("confidence", "low")
        for test in h.get("tests", []):
            tests.append({
                "hypothesis_id": h_id,
                "hypothesis_confidence": confidence,
                "purpose": test.get("purpose", ""),
                "action": test.get("action", ""),
                "expected_result": test.get("expected_result", ""),
            })
    return tests


@dataclass
class RecommendationGenerator:
    """
    Layer 3: Generate prioritized action recommendations.

    This generator synthesizes findings from Layer 1 and Layer 2 to produce
    actionable, prioritized recommendations for the user.
    """

    def run(
        self,
        *,
        diagnosis: QuickDiagnosis,
        layer2_outputs: list[AnalyzerOutput],
        required_evidence: dict[str, bool] | None = None,
        module_docs: dict[str, str] | None = None,
        module_dir: str | None = None,
    ) -> list[Recommendation]:
        recommendations: list[Recommendation] = []

        # Route based on analysis_path
        if diagnosis.analysis_path == AnalysisPath.abort:
            recommendations.extend(self._generate_abort_recommendations(diagnosis))

        elif diagnosis.analysis_path == AnalysisPath.investigate:
            recommendations.extend(self._generate_investigate_recommendations(diagnosis, layer2_outputs, required_evidence))

        elif diagnosis.analysis_path == AnalysisPath.diagnose:
            recommendations.extend(self._generate_diagnose_recommendations(diagnosis, layer2_outputs))

        elif diagnosis.analysis_path == AnalysisPath.optimize:
            recommendations.extend(self._generate_optimize_recommendations(diagnosis, layer2_outputs))

        # Add module review recommendations if module docs available
        if module_docs and module_dir:
            recommendations.extend(self._generate_module_review_recommendations(
                diagnosis, layer2_outputs, module_docs, module_dir
            ))

        return recommendations

    def _generate_abort_recommendations(self, diagnosis: QuickDiagnosis) -> list[Recommendation]:
        """Generate recommendations for abort path (critical red flags)."""
        recommendations = []

        # Critical red flags must be fixed first
        critical_flags = [f for f in diagnosis.red_flags if f.severity == RedFlagSeverity.critical]
        for flag in critical_flags:
            if flag.code == "TRAIN_NAN":
                recommendations.append(Recommendation(
                    purpose="Fix training NaN/Inf issue",
                    action="Check for numerical instability: (1) Reduce learning rate by 10x; (2) Enable gradient clipping (max_norm=1.0); (3) Check for division by zero in loss computation.",
                    expected_result="Training loss becomes finite and stable.",
                    cost="GPU: 1h (retrain); human: 30min (debug)",
                    confidence=Confidence.high,
                    priority=Priority.P0,
                    evidence_links=flag.evidence,
                ))

        # High-severity red flags
        high_flags = [f for f in diagnosis.red_flags if f.severity == RedFlagSeverity.high]
        for flag in high_flags:
            if flag.code == "TRAIN_DIVERGENCE":
                recommendations.append(Recommendation(
                    purpose="Fix training divergence",
                    action="Reduce learning rate by 5x and enable gradient clipping (max_norm=1.0). Monitor loss curve for stability.",
                    expected_result="Training loss converges smoothly without divergence.",
                    cost="GPU: 1h (retrain); human: 20min",
                    confidence=Confidence.high,
                    priority=Priority.P0,
                    evidence_links=flag.evidence,
                ))

        return recommendations

    def _generate_investigate_recommendations(
        self, diagnosis: QuickDiagnosis, layer2_outputs: list[AnalyzerOutput], required_evidence: dict[str, bool] | None = None
    ) -> list[Recommendation]:
        """Generate recommendations for investigate path (insufficient evidence)."""
        recommendations = []

        # Use required_evidence if provided, otherwise default to empty dict
        evidence = required_evidence or {}

        # P0: Fill critical evidence gaps - only if actually missing
        if not evidence.get("has_multiple_seeds", False):
            recommendations.append(Recommendation(
                purpose="Increase seed diversity for robust evaluation",
                action="Run evaluation with at least 3 seeds (0, 42, 3407) to reduce variance and enable statistical significance testing.",
                expected_result="mIoU std < 1.0; confident assessment of delta significance.",
                cost="GPU: 30min per seed; human: 5min",
                confidence=Confidence.high,
                priority=Priority.P0,
                evidence_links=[],
            ))

        # P1: Enable visualization if missing
        if not evidence.get("has_vis", False):
            recommendations.append(Recommendation(
                purpose="Enable visualization for failure mode analysis",
                action="Set visualize=1 in eval config; run evaluation to generate vis images.",
                expected_result="vis/index.jsonl and vis images available for Layer 2 visual analysis.",
                cost="GPU: 10min (eval); human: 2min",
                confidence=Confidence.high,
                priority=Priority.P1,
                evidence_links=[],
            ))

        # P1: Enable per-class logging if missing
        if not evidence.get("has_per_class_data", False):
            recommendations.append(Recommendation(
                purpose="Enable per-class IoU logging for detailed analysis",
                action="Ensure eval log outputs per-class IoU data (check logging configuration).",
                expected_result="Eval log contains per-class IoU breakdown for all 20 classes.",
                cost="Human: 5min (config check)",
                confidence=Confidence.high,
                priority=Priority.P1,
                evidence_links=[],
            ))

        # P2: Extract tests from Layer 2 hypotheses (if any)
        for out in layer2_outputs:
            tests = _extract_tests_from_hypotheses(out.hypotheses)
            for test in tests[:2]:  # Top 2 tests per analyzer
                recommendations.append(Recommendation(
                    purpose=test["purpose"],
                    action=test["action"],
                    expected_result=test["expected_result"],
                    cost="Varies (see action)",
                    confidence=Confidence.medium if test["hypothesis_confidence"] == "high" else Confidence.low,
                    priority=Priority.P2,
                    evidence_links=[],
                ))

        return recommendations

    def _generate_diagnose_recommendations(
        self, diagnosis: QuickDiagnosis, layer2_outputs: list[AnalyzerOutput]
    ) -> list[Recommendation]:
        """Generate recommendations for diagnose path (failure, need root cause)."""
        recommendations = []

        # P0: Check comparability first
        comparability_flags = [f for f in diagnosis.red_flags if "COMPARABILITY" in f.code or "MISMATCH" in f.code]
        if comparability_flags:
            recommendations.append(Recommendation(
                purpose="Ensure baseline comparability",
                action="Verify that exp and baseline use identical eval parameters (除了 intended changes). Fix any unintended differences.",
                expected_result="Eval args match; delta is attributable to module change only.",
                cost="Human: 10min (check); GPU: 30min (re-eval if needed)",
                confidence=Confidence.high,
                priority=Priority.P0,
                evidence_links=[f.evidence for f in comparability_flags][0] if comparability_flags else [],
            ))

        # P1: Extract high-confidence tests from Layer 2 hypotheses
        all_tests = []
        for out in layer2_outputs:
            tests = _extract_tests_from_hypotheses(out.hypotheses)
            all_tests.extend(tests)

        # Prioritize high-confidence tests
        high_conf_tests = [t for t in all_tests if t["hypothesis_confidence"] in ["high", "medium"]]
        for test in high_conf_tests[:3]:  # Top 3 high-confidence tests
            recommendations.append(Recommendation(
                purpose=test["purpose"],
                action=test["action"],
                expected_result=test["expected_result"],
                cost="Varies (see action)",
                confidence=Confidence.high if test["hypothesis_confidence"] == "high" else Confidence.medium,
                priority=Priority.P1,
                evidence_links=[],
            ))

        # P2: Visual inspection (if vis available)
        has_vis = any("vis" in str(e.path or "") for out in layer2_outputs for e in out.evidence)
        if has_vis:
            recommendations.append(Recommendation(
                purpose="Manual inspection of worst failure cases",
                action="Inspect top 10 worst vis samples; label failure modes (FN/FP/boundary/small-object). Cross-reference with per-class top drops.",
                expected_result="Identify dominant failure pattern; informs targeted fixes.",
                cost="Human: 30min",
                confidence=Confidence.medium,
                priority=Priority.P2,
                evidence_links=[],
            ))

        return recommendations

    def _generate_optimize_recommendations(
        self, diagnosis: QuickDiagnosis, layer2_outputs: list[AnalyzerOutput]
    ) -> list[Recommendation]:
        """Generate recommendations for optimize path (success, expand validation)."""
        recommendations = []

        # P0: Expand validation to other folds/shots
        recommendations.append(Recommendation(
            purpose="Validate generalization across folds/shots",
            action="Run evaluation on at least 2 other folds (e.g., fold=1,2) with same shot setting. Verify mIoU improvement is consistent.",
            expected_result="mIoU improvement holds across folds; confirms module is robust.",
            cost="GPU: 1h per fold; human: 10min",
            confidence=Confidence.high,
            priority=Priority.P0,
            evidence_links=[],
        ))

        # P1: Low-cost inference optimization
        recommendations.append(Recommendation(
            purpose="Optimize inference hyperparameters",
            action="Grid search over denoise_steps (e.g., [10, 20, 50]) and guidance_scale (e.g., [1.0, 2.0, 5.0]). Keep training fixed.",
            expected_result="Find optimal inference config; potential +1-2 mIoU gain at no training cost.",
            cost="GPU: 2h (grid search); human: 15min",
            confidence=Confidence.medium,
            priority=Priority.P1,
            evidence_links=[],
        ))

        # P2: Robustness check (increase seeds)
        recommendations.append(Recommendation(
            purpose="Verify improvement is not due to random seed luck",
            action="Run evaluation with 3+ seeds (0, 42, 3407). Compute mean and std; verify delta > 2*std.",
            expected_result="Improvement is statistically significant; not due to variance.",
            cost="GPU: 30min per seed; human: 5min",
            confidence=Confidence.high,
            priority=Priority.P2,
            evidence_links=[],
        ))

        return recommendations

    def _generate_module_review_recommendations(
        self,
        diagnosis: QuickDiagnosis,
        layer2_outputs: list[AnalyzerOutput],
        module_docs: dict[str, str],
        module_dir: str,
    ) -> list[Recommendation]:
        """
        Generate module-level review recommendations.

        Compares module design goals (from documentation) with actual results
        to identify potential module design issues or tuning opportunities.
        """
        recommendations = []

        # Extract key information from module docs
        doc_summary = self._summarize_module_docs(module_docs)

        # Generate recommendations based on experiment status
        if diagnosis.status == Status.failure:
            # Module failed to improve or degraded performance
            recommendations.append(Recommendation(
                purpose="Review module design vs. failure modes",
                action=(
                    f"Compare module goals (from {module_dir} documentation) with observed failure patterns. "
                    f"Key questions: (1) Does the module mechanism align with the failure modes? "
                    f"(2) Are loss weights/hyperparameters appropriate? "
                    f"(3) Does the module need ablation studies to isolate the issue? "
                    f"Module docs available: {', '.join(module_docs.keys())}"
                ),
                expected_result="Identify root cause: module design flaw, hyperparameter issue, or implementation bug.",
                cost="Human: 30-60min (review + analysis)",
                confidence=Confidence.high,
                priority=Priority.P1,
                evidence_links=[],
            ))

        elif diagnosis.status == Status.inconclusive:
            # Module shows no clear improvement
            recommendations.append(Recommendation(
                purpose="Evaluate module effectiveness and tuning opportunities",
                action=(
                    f"Review {module_dir} module design: (1) Are the module's assumptions valid for this task? "
                    f"(2) Are hyperparameters (e.g., loss weights, layer positions) properly tuned? "
                    f"(3) Does the module need stronger/weaker regularization? "
                    f"Consider ablation experiments to isolate module components. "
                    f"Module docs: {', '.join(module_docs.keys())}"
                ),
                expected_result="Identify tuning opportunities or determine if module is unsuitable for this baseline.",
                cost="Human: 30min (review); GPU: varies (if ablation needed)",
                confidence=Confidence.medium,
                priority=Priority.P1,
                evidence_links=[],
            ))

        elif diagnosis.status == Status.success:
            # Module succeeded - suggest further optimization
            recommendations.append(Recommendation(
                purpose="Optimize successful module for maximum gain",
                action=(
                    f"Module shows improvement. Review {module_dir} documentation for: "
                    f"(1) Hyperparameter tuning opportunities (e.g., loss weights, layer depth). "
                    f"(2) Additional module variants mentioned in docs. "
                    f"(3) Combination with other modules. "
                    f"Module docs: {', '.join(module_docs.keys())}"
                ),
                expected_result="Further improve mIoU through module optimization.",
                cost="Human: 20min (review); GPU: varies (if experiments needed)",
                confidence=Confidence.medium,
                priority=Priority.P2,
                evidence_links=[],
            ))

        # Add module-specific insights if available
        if doc_summary.get("has_hyperparameters"):
            recommendations.append(Recommendation(
                purpose="Review module hyperparameters",
                action=(
                    f"Module documentation mentions hyperparameters. "
                    f"Review current settings vs. recommended ranges. "
                    f"Consider grid search or ablation on key parameters."
                ),
                expected_result="Identify suboptimal hyperparameter settings.",
                cost="Human: 15min (review); GPU: varies (if tuning needed)",
                confidence=Confidence.medium,
                priority=Priority.P2,
                evidence_links=[],
            ))

        return recommendations

    def _summarize_module_docs(self, module_docs: dict[str, str]) -> dict[str, Any]:
        """Extract key information from module documentation."""
        summary = {
            "has_hyperparameters": False,
            "has_ablation_plan": False,
            "has_expected_metrics": False,
        }

        for doc_name, content in module_docs.items():
            content_lower = content.lower()

            # Check for hyperparameters
            if any(kw in content_lower for kw in ["超参", "hyperparameter", "权重", "weight", "λ", "lambda"]):
                summary["has_hyperparameters"] = True

            # Check for ablation plans
            if any(kw in content_lower for kw in ["消融", "ablation", "对比实验"]):
                summary["has_ablation_plan"] = True

            # Check for expected metrics
            if any(kw in content_lower for kw in ["预期", "expected", "目标", "target", "miou"]):
                summary["has_expected_metrics"] = True

        return summary
