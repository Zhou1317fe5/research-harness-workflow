---
name: exp-analysis-hen
description: Analyze experiment results pulled by autodl-remote-pull-manifest (remote_artifacts) and generate a Hypothesis→Evidence→Next-Experiment (H→E→N) report. Use when you want evidence-first analysis from logs/metrics/vis and an actionable next-step plan to improve <主指标>.
metadata:
  short-description: H→E→N experiment analysis from remote_artifacts
---

# Experiment Analysis (H→E→N)

Lightweight Claude mirror of the Codex skill. Full implementation and current script live under:
`.codex/skills/exp-analysis-hen/`.

## Architecture: 3-Layer Analysis System

- **Layer 1: Quick Diagnosis (≤5s)** - Rapid triage
- **Layer 2: Root Cause Analysis** - Per-class, visual, training dynamics
- **Layer 3: Action Recommendations** - Prioritized next steps (P0/P1/P2)

## Evidence Rules

- Read artifacts from `research_workspace/experiments/<ExpID>/remote_artifacts/`.
- Write the report to `research_workspace/experiments/<ExpID>/analysis.md`.
- Put single-experiment auxiliary analysis, diagnosis, root-cause, next-step, and per-class notes under `research_workspace/experiments/<ExpID>/analysis/`.
- Put cross-experiment, route-level, or baseline comparison notes under `research_workspace/experiments/_cross_experiment/<RouteID>/`.
- Repeated eval summaries are not always repeated seeds. If eval dirs encode `iter*` or `checkpoint*`, report them by checkpoint and do **not** summarize them as `mean±std` or count them as seed evidence.
- For true repeated seed runs, `mean±std` is allowed and must be labeled as seed-level evidence.

## Usage

```bash
python3 .codex/skills/exp-analysis-hen/analyze.py --expid "E2025xxxx-xx"
python3 .codex/skills/exp-analysis-hen/analyze.py --expid "E2025xxxx-xx" --baseline-expid "E2025yyyy-yy"
```

**Output:** `research_workspace/experiments/<ExpID>/analysis.md`

## References
- `docs/SKILLs/agent-skills-standard.md`
- `.codex/skills/exp-analysis-hen/SKILL.md` (full documentation)
