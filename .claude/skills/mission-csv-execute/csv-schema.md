# CSV Schema

## Canonical Source

`issues/TEMPLATE.csv` is the single source of truth for directory artifacts under `issues/<stem>/` and legacy flat `issues/*.csv`.

- Before generating, repairing, or executing any repository Mission CSV, read `issues/TEMPLATE.csv`.
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
| `id` | Stable issue id, e.g. `<SpecID>-01`; pre-run code review rows use `PRERUN-REVIEW-N`; final vision review row is usually `REVIEW-01`. |
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
| `remote_state` | Exact enum: empty before launch, `not_applicable`, `running_remote`, `completed`, `artifacts_pulled`, `ingested`, or `failed`. Only `not_applicable`, `completed`, and `ingested` are terminal for Mission closing. |
| `artifact_path` | Local or remote artifact path when relevant. |
| `branch` | Expected branch. |
| `commit_hash` | Commit hash evidence when available. For train/eval/remote rows this should identify the reviewed `pre_run_code_commit`; later artifact/analysis/review commits belong in `notes` unless the row itself is about those commits. |
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

Keep commonly used `notes` tags stable:

| Tag | Meaning |
|-----|---------|
| `picked_reason:<why>` | Why this issue was selected next. |
| `done_at:<date>` | Completion timestamp. |
| `validation_limited:<reason>` | Target validation was objectively unavailable. |
| `manual_test:<command-or-steps>` | Manual follow-up validation when credentials/services become available. |
| `skills_used:<skill;skill>` | Skills actually used. |
| `mcp_used:<tool;tool>` | MCP/tools actually used. |
| `mcp_evidence:<tool> <summary>` | Tool evidence summary. |
| `evidence:<what>` | Validation evidence. |
| `risk:<low\|medium\|high> <note>` | Risk note. |
| `assumption:<note>` | Reasonable assumption used to continue. |
| `decision_debt:<note>` | Non-blocking decision debt for review log. |
| `execution_scope:<scope>` | Approved-doc execution boundary for claim extraction and review. |
| `claim_ledger:<path>` | Persisted claim/evidence ledger JSON path. Any `claims:CLAIM-*` tag must resolve here. |
| `claims:<CLAIM-001,CLAIM-002>` | Claim ids covered by the current issue. |
| `claim_coverage:<covered>/<total>` | Approved-doc claim coverage. |
| `claim_coverage_status:<pending\|complete\|gaps\|unknown>` | Review judgment of claim coverage. |
| `evidence_level:<level>` | Highest evidence level: `real_e2e`, `integration`, `unit`, `static`, `mock_allowed`, `limited_allowed`. |
| `production_path:<covered\|not_required\|deferred\|gap>` | Whether the documented production/research path is wired. |
| `mock_allowed:<reason>` | Source doc explicitly permits mock/fake/dry-run/static evidence. |
| `limited_allowed:<reason>` | Source doc explicitly permits limited validation. |
| `out_of_scope:<section>;<reason>` | Source-doc promise outside this `execution_scope`. |
| `review_kind:pre_run_implementation` | Pre-run code implementation review row. |
| `review_kind:vision` | Final approved-doc vision review row. |
| `outcome_contract:<path>` | Frozen reader-question and blocked-claim contract next to the CSV. |
| `deferred_ledger:<path>` | Sibling Deferred Findings ledger; it never controls CSV state. |
| `deferred_findings:<DF-001,DF-002>` | Deferred ids referenced by the current row. |
| `deferred_coverage:<covered>/<open>` | Open deferred findings rendered in the final handoff. |
| `review_agent_mode:<mode>` | Closing mode: `evidence-close`, `reviewer-subagent`, `codex-exec-independent`, `self-review`, or `pending`. |
| `review_independence:<true\|false\|pending>` | Boolean closing-review independence; `evidence-close` and self-review are `false`. |
| `review_requested_model:<model>` | Requested independent reviewer model, normally `gpt-5.6-sol`; `evidence-close` uses `not_applicable`. |
| `review_observed_model:<model>` | Model observed from session metadata/event stream; `evidence-close` uses `not_applicable`. |
| `review_model_evidence:<session-metadata\|event-stream\|parent-runtime\|unknown\|not_applicable\|pending>` | Source supporting the observed model value; `pending` is generation-only. |
| `review_result:<vision_met\|gaps_found\|limited_review>` | Closing review outcome. |
| `scientific_outcome:<hypothesis_supported\|hypothesis_not_supported\|gate_failed\|inconclusive\|not_applicable>` | Scientific result, separate from Mission execution success. |
| `review_json:<path>` | Raw structured review output under artifact-root `reviews/`. |
| `handoff:<path>` | Human-facing handoff path. Use `handoff:generation_failed <reason>` only while rendering a fallback. |
| `handoff_humanized:<true\|false>` | Whether visible handoff prose was processed with `humanizer-zh`; machine tables remain byte-stable. |
| `handoff_contract:<passed\|failed ...>` | Mechanical Handoff Contract result. |
| `source_doc:<path>` | Approved canonical source document. |
| `gated_run:<id>` | Run row gated by a pre-run review. |
| `review_mode:<scientific_review\|targeted_review>` | Single pre-run reviewer scope for the gated run. |
| `review_result:<scientifically_correct\|scientifically_incorrect\|not_evaluable\|targeted_correct\|targeted_incorrect>` | Result returned by the one independent pre-run reviewer. |
| `blocker_closure_evidence:<path>` | Production/sink evidence that closes every blocker reported by the reviewer before `pre_run_result:pass`. |
| `command_owner:<rrctl\|legacy>` | Remote control owner. Omission defaults actionable remote rows to rrctl; stored closed/running history is not rewritten. |
| `legacy_reason:<reason>` | Required reason for an actionable `command_owner:legacy` exception. |
| `legacy_migration_deadline:<date>` | Legacy migration deadline; this or `legacy_migration_issue` is required. |
| `legacy_migration_issue:<id>` | Legacy migration issue; this or `legacy_migration_deadline` is required. |
| `legacy_responsible_component:<owner>` | Required component/owner responsible for the legacy exception. |
| `readiness_result:<pass\|failed>` | Deterministic packet readiness result before reviewer invocation. |
| `readiness_gap:<diagnostic>` | Actionable readiness failure; return to the implementation row and fill the missing evidence. |
| `packet_sha256:<sha>` | Digest of the normalized structured review packet. |
| `scientific_reviewer_gap:<reason>` | One recorded capability gap when the independent scientific reviewer is unavailable; do not create retry rows. |
| `pre_run_code_commit:<hash>` | Code snapshot reviewed before training/eval/remote run. |
| `pre_run_result:pass` | Pre-run review allowed the gated run. |
