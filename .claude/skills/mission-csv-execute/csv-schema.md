# CSV Schema

## Canonical Source

`issues/TEMPLATE.csv` is the single source of truth for formal `issues/*.csv` files in this repository.

- Before generating, repairing, or executing an `issues/*.csv`, read `issues/TEMPLATE.csv`.
- Use its header order, column count, default row shape, and project status conventions.
- This document is descriptive guidance only. If it conflicts with `issues/TEMPLATE.csv`, the template wins.
- Do not use older mission-generic headers or raw hand-written CSV rows.

Current project header has 28 columns:

```csv
id,priority,phase,area,title,description,acceptance_criteria,test_mcp,required_skills,required_mcp,review_initial_requirements,review_regression_requirements,dev_state,review_initial_state,review_regression_state,git_state,owner,refs,notes,spec_id,exp_id,run_id,remote_state,artifact_path,branch,commit_hash,next_action,updated_at
```

## Field Rules

| Field | Rule |
|------|------|
| `id` | Stable issue id, e.g. `<SpecID>-01`; pre-run code review rows use `PRERUN-REVIEW-N`; final review row is usually `REVIEW-01`. |
| `priority` | `P0`, `P1`, or `P2`. |
| `phase` | Project phase token such as `implement`, `test`, `pre_run_review`, `remote`, `artifact`, `review`; preserve existing local vocabulary. |
| `area` | Task area token; keep concise and project-specific. |
| `title` | Short human-readable task title. |
| `description` | Task boundary. May contain commas, arrows, parentheses, or newlines; must be CSV-escaped by a writer. |
| `acceptance_criteria` | Verifiable acceptance criteria. May contain commas, arrows, parentheses, or newlines; must be CSV-escaped by a writer. |
| `test_mcp` | Primary verification surface such as `local_cli`, `remote_cli`, or `manual`. |
| `required_skills` | Semicolon-separated skill names that must be read before implementation; leave empty if none. `PRERUN-REVIEW-N` rows use `pre-run-implementation-review`. |
| `required_mcp` | Semicolon-separated tool ids required for evidence; leave empty if none. |
| `review_initial_requirements` | Initial review requirements. |
| `review_regression_requirements` | Regression review requirements. |
| `dev_state` | `未开始`, `进行中`, or `已完成`. |
| `review_initial_state` | `未开始`, `进行中`, or `已完成`. |
| `review_regression_state` | `未开始`, `进行中`, or `已完成`. |
| `git_state` | `未提交` or `已提交`. |
| `owner` | Usually `codex`. |
| `refs` | Semicolon-separated file refs, paths, commands, or artifact refs. |
| `notes` | Free-form machine-readable notes and evidence tags; do not store secrets. |
| `spec_id` | Associated SpecID. |
| `exp_id` | Associated ExpID when relevant, otherwise empty. |
| `run_id` | Associated RunID when relevant, otherwise empty. |
| `remote_state` | Project remote state such as `not_applicable`, `pending`, `running_remote`, `artifacts_pulled`, `ingested`, or task-specific terminal states already used by the current CSV. |
| `artifact_path` | Local or remote artifact path when relevant. |
| `branch` | Expected branch. |
| `commit_hash` | Commit hash evidence when available. |
| `next_action` | Next executable issue id or recovery instruction. |
| `updated_at` | Update date/time. |

## Write And Validation Rules

- Generate and repair CSV with `csv.DictWriter`, `csv.writer`, or an equivalent structured serializer.
- Never hand-concatenate CSV rows. Unescaped English commas are a format bug, not a design decision.
- Preserve UTF-8 text. Follow the encoding/newline style of `issues/TEMPLATE.csv`; do not add a BOM unless the template has one.
- Every row must have exactly the template header fields. Extra fields, missing fields, or `None` overflow columns are invalid.
- Malformed quoting, comma drift, line breaks inside unquoted fields, and typoed status enums are machine-detectable format errors and should be repaired without changing task semantics.

Minimum structural check:

```bash
python3 - <<'PY'
import csv
from pathlib import Path

template = Path("issues/TEMPLATE.csv")
target = Path("<target.csv>")

with template.open(newline="", encoding="utf-8") as f:
    header = next(csv.reader(f))

with target.open(newline="", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    assert reader.fieldnames == header, (reader.fieldnames, header)
    for line_no, row in enumerate(reader, start=2):
        assert None not in row, f"overflow columns at line {line_no}: {row[None]}"
        for key in header:
            assert key in row, f"missing {key} at line {line_no}"
PY
```

## Notes Tags

Keep commonly used `notes` tags stable: `picked_reason:`, `done_at:`, `validation_limited:`, `manual_test:`, `skills_used:`, `mcp_used:`, `evidence:`, `risk:`, `assumption:`, `decision_debt:`, `review_kind:pre_run_implementation`, `review_kind:vision`, `source_doc:<path>`, `gated_run:<id>`, `pre_run_code_commit:<hash>`, `pre_run_result:pass`.
