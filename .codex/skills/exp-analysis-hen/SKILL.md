---
name: exp-analysis-hen
description: Analyze experiment results pulled by autodl-remote-pull-manifest (remote_artifacts) and generate a Hypothesis→Evidence→Next-Experiment (H→E→N) report. Use when you want evidence-first analysis from logs/metrics/vis and an actionable next-step plan to improve <主指标>.
metadata:
  short-description: H→E→N experiment analysis from remote_artifacts
---

# Experiment Analysis (H→E→N)

Generate a strict **Hypothesis → Evidence → Next experiment** report from local artifacts under:
`research_workspace/experiments/<ExpID>/remote_artifacts/` (pulled by `autodl-remote-pull-manifest`).

## Prerequisites

**Required before running this skill:**
- `autodl-remote-pull-manifest` must be executed first to pull artifacts
- At least one metric source must exist:
  - Preferred: `remote_artifacts/eval/**/metrics/summary.json` (at least one)
  - Fallback: `remote_artifacts/results.json` (contains aggregated <主指标>/<辅助指标> + per-run metrics)
- Recommended: `exp-results-ingest-local` should be run first to populate 00-实验记录.md (provides experiment context)

**Consumed by (downstream skills):**
- None (this is typically the final analysis step)
- Optional: Manual review or further iteration based on recommendations

## Execution Order

```
1. autodl-remote-pull-manifest (pull artifacts to local)
2. exp-results-ingest-local (ingest into 00-实验记录.md) [recommended]
3. exp-analysis-hen (this skill) ← Generate H→E→N analysis report
4. [Manual review and next experiment planning]
```

## Architecture: 3-Layer Analysis System

This skill implements a **3-layer analysis architecture** for fast, structured experiment diagnosis:

- **Layer 1: Quick Diagnosis (≤5s)** - Rapid triage using `analyze.py`
  - Experiment classification (baseline/module_e2e/ablation/hyperparam/neutral)
  - Success criteria evaluation (success/failure/inconclusive)
  - Red flag detection (critical issues: NaN, divergence, high variance)
  - Analysis path routing (optimize/diagnose/investigate/abort)

- **Layer 2: Root Cause Analysis (on-demand)** - Deep dive into specific failure modes
  - **PerClassAnalyzer**: Per-class IoU deltas, semantic grouping, small object analysis
  - **VisualFailureModeAnalyzer**: Failure mode classification from vis/index.jsonl
  - **TrainingDynamicsAnalyzer**: Training stability, convergence, loss imbalance from TensorBoard logs

- **Layer 3: Action Recommendations** - Prioritized, actionable next steps
  - P0 (critical): Fix red flags, increase seeds
  - P1 (high): Enable visualization, check comparability
  - P2 (medium): Hypothesis tests extracted from Layer 2 findings

**Entry points:**
- `analyze.py`: 3-layer analysis → `analysis.md` (unified entry point)

## When to use
- You have `remote_artifacts/` locally and want an **evidence-first** analysis report + next experiments.
- You want baseline-aligned comparisons on the same K=(benchmark, fold, nshot).

## When not to use
- `remote_artifacts/eval/**/metrics/summary.json` is missing (pull/eval first).
- You only need to record metrics in `00-实验记录.md` without generating analysis (use `exp-results-ingest-local` only).

## What it reads (evidence-only)
- `remote_artifacts/eval/**/metrics/summary.json` (preferred; <主指标>/<辅助指标> + benchmark/fold/nshot)
- `remote_artifacts/results.json` (fallback; schema includes `evaluation_metrics[*].mIoU_mean/mIoU_std` and `runs[*].<主指标>`)
- `remote_artifacts/eval/**/logs/**/log.txt` (optional; args + per-class snippets if present)
- `remote_artifacts/eval/**/vis/**` (optional; render results; prefers `vis/index.jsonl`)
- `remote_artifacts/train/logs/**` (optional; stability/error snippets)
- `research_workspace/00-实验记录.md` (optional; extract experiment goal / module function / changed params if present)
- `research_workspace/module_research/<ModuleDir>/**/*.md` (optional; module doc context for H/N)

## What it writes
- Main report: `research_workspace/experiments/<ExpID>/analysis.md`
- Single-experiment auxiliary analysis, diagnosis, root-cause, next-step, and per-class notes:
  `research_workspace/experiments/<ExpID>/analysis/`
- Cross-experiment, route-level, or baseline comparison notes:
  `research_workspace/experiments/_cross_experiment/<RouteID>/`

## Usage

```bash
# 3-layer analysis with baseline comparison
python3 .codex/skills/exp-analysis-hen/analyze.py --expid "E2025xxxx-xx"
python3 .codex/skills/exp-analysis-hen/analyze.py --expid "E2025xxxx-xx" --baseline-expid "E2025yyyy-yy"
```

**Output:** `research_workspace/experiments/<ExpID>/analysis.md`
- Layer 1: Quick diagnosis (experiment type, status, red flags, analysis path)
- Layer 2: Per-class analysis, visual failure modes, training dynamics (if data available)
- Layer 3: Prioritized recommendations (P0/P1/P2)

## Notes
- Evidence is strictly derived from `remote_artifacts`; do not claim training/eval ran unless logs exist.
- Repeated eval summaries are not always repeated seeds. If eval dirs encode `iter*` or `checkpoint*`, report them by checkpoint and do **not** summarize them as `mean±std` or count them as seed evidence.
- For true repeated seed runs, `mean±std` is allowed and should remain clearly labeled as seed-level evidence.
- Layer 1 provides rapid triage (≤5s) with experiment classification and red flag detection.
- Layer 2 analyzers run on-demand based on available data (eval logs, vis, TensorBoard).
- Layer 3 generates prioritized recommendations (P0/P1/P2) based on analysis path.
- Default baseline reference is configurable in `analyze.py` (`DEFAULT_BASELINE_EXPID`).

## Verification

- Confirm output exists: `research_workspace/experiments/<ExpID>/analysis.md`
- Check Layer 1 diagnosis: experiment_type, status, red_flags, analysis_path
- Check Layer 2 sections (if data available): per-class analysis, visual failure modes, training dynamics
- Check Layer 3 recommendations: prioritized actions (P0/P1/P2)

## Safety & guardrails
- This skill always writes `analysis.md` to the experiment directory.
- One-off analysis docs belong in the matching experiment `analysis/` subdirectory, or `_cross_experiment/<RouteID>/` when the note spans multiple experiments.
- All analysis is evidence-based from `remote_artifacts/` only.
- Red flags are clearly marked with severity levels (critical/high/medium/low).

## References
- `docs/SKILLs/agent-skills-standard.md`
