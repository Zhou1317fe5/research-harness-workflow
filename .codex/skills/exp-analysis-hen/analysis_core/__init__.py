"""
Analysis Core - 3-Layer Experiment Analysis System

Layer 1: Quick Diagnosis (≤5s)
Layer 2: Root Cause Analysis (on-demand)
Layer 3: Action Recommendations (prioritized)
"""

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
    AnalyzerOutput,
    Priority,
    Confidence,
    Recommendation,
)

from .layer1 import (
    ExperimentClassifier,
    SuccessCriteria,
    RedFlagDetector,
    DecisionRouter,
    QuickDiagnosisEngine,
)

from .layer2_per_class import (
    PerClassAnalyzer,
)

from .layer2_visual import (
    VisualFailureModeAnalyzer,
)

from .layer2_training import (
    TrainingDynamicsAnalyzer,
)

from .layer3 import (
    RecommendationGenerator,
)

__all__ = [
    # Data models
    "ExperimentType",
    "Status",
    "AnalysisPath",
    "RedFlagSeverity",
    "RedFlag",
    "EvidenceRef",
    "ExpStats",
    "ExperimentContext",
    "QuickDiagnosis",
    "AnalyzerOutput",
    "Priority",
    "Confidence",
    "Recommendation",
    # Layer 1
    "ExperimentClassifier",
    "SuccessCriteria",
    "RedFlagDetector",
    "DecisionRouter",
    "QuickDiagnosisEngine",
    # Layer 2
    "PerClassAnalyzer",
    "VisualFailureModeAnalyzer",
    "TrainingDynamicsAnalyzer",
    # Layer 3
    "RecommendationGenerator",
]
