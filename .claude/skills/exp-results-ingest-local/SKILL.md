---
name: exp-results-ingest-local
description: Ingest pulled remote artifacts under research_workspace/experiments/<ExpID>/remote_artifacts, summarize latest eval (summary.json), and update research_workspace/00-实验记录.md (index row + detailed section). Use when you want to record <主指标>/<辅助指标> into 00-实验记录.md after pulling remote artifacts.
metadata:
  short-description: Local ingest + experiment record update
---

# Experiment Results Ingest (Local)

Lightweight Claude mirror of the Codex skill. Full implementation and current script live under:
`.codex/skills/exp-results-ingest-local/`.

## Evidence Rules

- Read artifacts from `research_workspace/experiments/<ExpID>/remote_artifacts/`.
- Update `research_workspace/00-实验记录.md`.
- Repeated eval summaries are not always repeated seeds. If eval dirs encode `iter*` or `checkpoint*`, record them as checkpoint rows and do **not** summarize them as `mean±std` or seed evidence.
- For checkpoint eval sets, “主结果” is the highest checkpoint, for example checkpoint15000, with all checkpoints listed separately.
- For true repeated seed runs, `mean±std` is allowed and must be labeled as seed-level evidence.

## Usage
```bash
python3 .codex/skills/exp-results-ingest-local/scripts/ingest.py --expid "E2025xxxx-xx" --dry-run
python3 .codex/skills/exp-results-ingest-local/scripts/ingest.py --expid "E2025xxxx-xx" --backup
python3 .codex/skills/exp-results-ingest-local/scripts/ingest.py --expid "E2025xxxx-xx" --baseline-expid "E2025yyyy-yy"
python3 .codex/skills/exp-results-ingest-local/scripts/ingest.py --expid "E2025xxxx-xx"
```

## Verification

- Start with `--dry-run` and inspect the diff before writing.
- Confirm checkpoint eval sets render as checkpoint rows, not `<主指标>(mean±std)`.
- Confirm true seed repeats still render as seed-level mean/std.

## References
- `docs/SKILLs/agent-skills-standard.md`
- `.codex/skills/exp-results-ingest-local/SKILL.md` (full documentation)
