"""
Core data models for the 3-layer analysis system.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ExperimentType(str, Enum):
    """Experiment classification types."""
    baseline = "baseline"
    module_e2e = "module_e2e"
    ablation = "ablation"
    hyperparam = "hyperparam"
    neutral = "neutral"


class Status(str, Enum):
    """Experiment success status."""
    success = "success"
    failure = "failure"
    inconclusive = "inconclusive"


class AnalysisPath(str, Enum):
    """Analysis routing paths based on quick diagnosis."""
    optimize = "optimize"         # Success: expand validation / low-cost optimization
    diagnose = "diagnose"         # Failure: prioritize stability / comparability / mechanism
    investigate = "investigate"   # Insufficient evidence: fill gaps / add runs
    abort = "abort"               # Critical red flags: fix first, then discuss metrics


class RedFlagSeverity(str, Enum):
    """Red flag severity levels."""
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


@dataclass(frozen=True)
class EvidenceRef:
    """Reference to evidence (file/metric/diff/image)."""
    kind: str                 # "file" | "metric" | "diff" | "image"
    path: str | None = None   # repo-relative path recommended
    snippet: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RedFlag:
    """Red flag detected during quick diagnosis."""
    code: str                 # e.g. "TRAIN_NAN", "EVAL_ARGS_MISMATCH", "DATA_LEAK_RISK"
    severity: RedFlagSeverity
    message: str
    evidence: list[EvidenceRef] = field(default_factory=list)


@dataclass
class ExpStats:
    """Experiment statistics for a given K=(benchmark, fold, nshot)."""
    k: tuple[str, int, int]                 # (benchmark, fold, nshot)
    miou_mean: float | None
    miou_std: float | None
    fb_iou_mean: float | None = None
    runs: list[dict[str, Any]] = field(default_factory=list)  # compatible with existing schema
    best_run_rel: str | None = None


@dataclass
class ExperimentContext:
    """Context information for an experiment."""
    expid: str
    experiment_name: str | None
    module_dir: str
    compare_mode: str                 # "e2e" | "infer_ablation"
    delta_mean: float | None
    changed_flags: list[str]          # from 00-实验记录.md
    changed_keys: list[str]           # flag->arg key mapping
    remote_artifacts_dir: Path
    baseline_expid: str | None
    baseline_remote_artifacts_dir: Path | None


@dataclass
class QuickDiagnosis:
    """Output of Layer 1 quick diagnosis."""
    experiment_type: ExperimentType
    status: Status
    red_flags: list[RedFlag]
    analysis_path: AnalysisPath
    notes: list[str] = field(default_factory=list)


@dataclass
class AnalyzerOutput:
    """Output from a Layer 2 analyzer."""
    name: str
    summary: str
    findings: dict[str, Any]          # structured payload for Layer 3
    evidence: list[EvidenceRef]
    hypotheses: list[dict[str, Any]] = field(default_factory=list)


class Priority(str, Enum):
    """Recommendation priority levels."""
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class Confidence(str, Enum):
    """Recommendation confidence levels."""
    high = "high"
    medium = "medium"
    low = "low"


@dataclass
class Recommendation:
    """Action recommendation from Layer 3."""
    purpose: str
    action: str
    expected_result: str
    cost: str                 # "GPU: 1h; human: 10min"
    confidence: Confidence
    priority: Priority
    evidence_links: list[EvidenceRef] = field(default_factory=list)
