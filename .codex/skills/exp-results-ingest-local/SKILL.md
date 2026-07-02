---
name: exp-results-ingest-local
description: Ingest pulled remote artifacts under research_workspace/experiments/<ExpID>/remote_artifacts, summarize latest eval (summary.json), and update research_workspace/00-实验记录.md (index row + detailed section). Use when you want to record <主指标>/<辅助指标> into 00-实验记录.md after pulling remote artifacts.
metadata:
  short-description: Local ingest + experiment record update
---

# Experiment Results Ingest (Local)

Ingest remote artifacts stored under `research_workspace/experiments/<ExpID>/remote_artifacts/`, then update the experiment record doc:
`research_workspace/00-实验记录.md`.

## Prerequisites

**Required before running this skill:**
- `autodl-remote-pull-manifest` must be executed first to pull artifacts
- `remote_artifacts/eval/**/metrics/summary.json` must exist (at least one)
- `remote_artifacts/results.json` should exist (optional but recommended for metadata)

**Consumed by (downstream skills):**
- `exp-analysis-hen` - Reads 00-实验记录.md for experiment context

## Execution Order

```
1. autodl-remote-pull-manifest (pull artifacts to local)
2. exp-results-ingest-local (this skill) ← Ingest into 00-实验记录.md
3. exp-analysis-hen (generate analysis report)
```

## When to use
- You already pulled `remote_artifacts/` (via `autodl-remote-pull-manifest`) and want to **update `00-实验记录.md`** with the latest eval results.

## When not to use
- You only want to inspect metrics without modifying files (use `--dry-run`).
- `remote_artifacts/eval/**/metrics/summary.json` is missing (fix the pull/eval first).

## What it reads
- `remote_artifacts/results.json` (optional but preferred)
- `remote_artifacts/eval/**/metrics/summary.json` (required for <主指标>/<辅助指标>)
- `remote_artifacts/remote_meta.json` (optional; used to fill remote EXP_ROOT path)

## What it updates
- Inserts/updates the ExpID row in the **top-level index table(s)**:
  - If the ExpID row already exists (in any table whose header starts with `| ExpID` and is located before the first `# E...` detail heading), update it in-place.
  - Otherwise, insert a new row into the **“20251222更新后实验 …”** index table as a fallback.
- Inserts/updates a detailed section for the ExpID:
  - If missing, it is inserted **immediately below the index tables** (i.e., before the first existing detail section), so new ExpIDs appear first.
  - The ingest block is **plain markdown** (no marker comments / no special headings): bullets + one `summary.json` table; it is updated idempotently by matching `- EXP_ROOT：...` within the ExpID section.

Notes:
- If an ExpID row already exists in any top-level index table row (header starts with `| ExpID`), it updates that row in-place (no duplicate row insertion).
- `experiment_name` is inferred from EXP_ROOT when available (e.g. `/.../experiments/<experiment_name>/<timestamp>` → `<experiment_name>`).
- Default baseline (reference) is configurable in `.codex/skills/exp-results-ingest-local/scripts/ingest.py` (`DEFAULT_BASELINE_EXPID`, `DEFAULT_BASELINE_K`) and is filled automatically in the detail bullets when K matches.

## Usage
```bash
# Recommended: preview changes first (no file writes)
python3 .codex/skills/exp-results-ingest-local/scripts/ingest.py --expid "E2025xxxx-xx" --dry-run

# Write into 00-实验记录.md (optionally create a backup)
python3 .codex/skills/exp-results-ingest-local/scripts/ingest.py --expid "E2025xxxx-xx" --backup

# Optional: compare against a user-chosen baseline ExpID
python3 .codex/skills/exp-results-ingest-local/scripts/ingest.py --expid "E2025xxxx-xx" --baseline-expid "E2025yyyy-yy"

python3 .codex/skills/exp-results-ingest-local/scripts/ingest.py --expid "E2025xxxx-xx"
```

## Verification
- After a write run, verify the diff: `git diff research_workspace/00-实验记录.md`
- Ensure the ExpID row and detail section were updated/created, and the “主结果” matches the selected primary result:
  - checkpoint eval set → highest checkpoint (for example checkpoint15000), with all checkpoints listed separately.
  - true repeated seeds → mean/std summary.

## Safety & guardrails
- This skill **modifies** `research_workspace/00-实验记录.md`; run with `--dry-run` first if unsure.
- Avoid running it on incomplete `remote_artifacts` to prevent recording partial/incorrect metrics.
- Repeated eval runs are not always repeated seeds. If eval dirs encode `iter*` or `checkpoint*`, record them as checkpoint rows and do **not** summarize them as `mean±std` or seed evidence.
- For true repeated seed runs (e.g. seeds 0/1/2), the detail section summarizes mean/std and best <主指标> for the primary K, and (optionally) compares against `--baseline-expid` for the same K.

## References
- `docs/SKILLs/agent-skills-standard.md`
