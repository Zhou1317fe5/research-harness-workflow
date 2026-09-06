---
name: mission-spec
description: Use when a mission starts from a natural-language request, a draft canonical spec, or a Markdown document without mission frontmatter and the agent must discuss requirements, write an approvable spec, record explicit approval, and hand the approved document to execution.
---

# Mission Spec

Own requirement discussion and the `draft -> approved` spec transition. Do not implement the work and do not write a separate implementation plan.

Declare: `使用 mission-spec skill，讨论并生成可批准的 canonical spec。`

## Workflow

1. Read the request and the relevant repository context.
2. If `lite-arch-recall` is installed, run it only before a genuine architecture-bearing decision (hard-to-reverse system boundary, data ownership, protocol, persistence, or cross-module control-flow choice) against applicable `docs/adr/` records. Ordinary model/module iteration, hyperparameter changes, experiment setup, bug fixes, and CSV bookkeeping do not trigger ADR recall.
3. Ask one question at a time only while a material goal, constraint, scope boundary, or acceptance condition remains unresolved.
4. Present alternatives only when real alternatives exist. Recommend one and state concrete tradeoffs.
5. Once the design is determined, write a draft to `docs/specs/<YYYY-MM-DD>-<topic>.md`.
6. Run `python scripts/validate_spec.py <spec> --allow-uncommitted-approved` and fix every error.
7. Show the final draft and require explicit user approval of that exact content. Do not implement before approval.
8. After approval, set `status: approved` and add the current RFC 3339 `approved_at` timestamp with timezone. Do not change the approved body in the same edit.
9. For a genuine architecture-bearing decision, run installed `lite-arch` and print its required three-line decision block before creating, amending, superseding, or skipping an ADR. Do not create ADRs for ordinary scientific iterations or routine implementation choices.
10. Commit the approved spec and any draft ADR created by the optional gate before execution. Then run `validate_spec.py` without the allow flag to prove the approved file is committed and unchanged from `HEAD`.
11. Route the committed spec to `mission-approved-doc`, unless the user explicitly chooses ordinary direct implementation.

## Canonical format

Use exactly one scalar frontmatter block:

```yaml
---
mission: spec
status: draft
created: YYYY-MM-DD
---
```

After approval:

```yaml
---
mission: spec
status: approved
created: YYYY-MM-DD
approved_at: YYYY-MM-DDTHH:MM:SS+08:00
---
```

Required sections:

- `Goal`
- `Scope`
- `Design`
- `Acceptance Criteria`

Add `Non-goals`, `Alternatives`, `Compatibility`, `Security`, `Rollout`, or `Rollback` only when they carry real information.

The validator accepts only unique `key: scalar` entries for the four declared fields. It rejects nested values, arrays, aliases, tags, duplicate keys, unknown states, invalid dates, missing fields, and additional frontmatter blocks.

## Approval integrity

- Approval applies to the exact draft the user saw.
- Any later content change invalidates approval. Change the status back to `draft`, remove `approved_at`, and ask for approval again.
- A later session may trust an approved spec only when `validate_spec.py` confirms that it exists in `HEAD` and the working copy is unchanged.
- Never infer approval from positive discussion, implementation permission for another artifact, or a previous version of the document.

## Writing boundary

Run `humanizer-zh` on prose before presenting or committing the spec. Do not let it rewrite frontmatter, code identifiers, paths, commands, dates, or machine-checked fields.

`mission-spec` produces and approves the spec. `mission-approved-doc` maps an approved spec into execution artifacts. `mission-csv-execute` owns execution state. Keep those responsibilities separate.
