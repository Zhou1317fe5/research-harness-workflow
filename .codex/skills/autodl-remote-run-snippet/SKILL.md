---
name: autodl-remote-run-snippet
description: Use when a research CSV/Spec needs a remote AutoDL train→eval command from train/eval intent, constraints, and scripts available under scripts/.
metadata:
  short-description: Remote run snippet generator
---

# AutoDL Remote Run Snippet

Generate a remote **train → eval** command from Claude/user-provided intent and local `scripts/` inventory.

Claude should provide the experimental intent and constraints. Codex is responsible for resolving those into concrete train/eval scripts and a runnable command.

## Prerequisites

**Required before running this skill:**
- Local code changes must be committed and pushed to git
- Spec/CSV/review context must state train intent, eval intent, required args, branch, expected artifact path, and any hard constraints
- Remote server must have git access to the repository
- Direct remote execution requires explicit user authorization plus usable SSH credentials/session

**Consumed by (downstream skills):**
- `autodl-remote-pull-manifest` - Pulls artifacts after remote execution completes

## Execution Order

```
1. Read Spec/CSV/review context for train/eval intent and constraints
2. Inspect scripts/ and resolve a unique train script + eval script
3. autodl-remote-run-snippet (this skill) ← Generate remote command
4. [User copies command to remote] OR [Codex runs it via SSH if explicitly authorized]
5. Mark CSV remote_state=running_remote and record command/session/output path
6. [Wait for training/evaluation to complete]
7. autodl-remote-pull-manifest (pull artifacts to local)
8. exp-results-ingest-local (ingest into 00-实验记录.md)
9. Write/update issues/<SpecID>.review.md with objective result summary for Claude
```

## Usage

1) Read intent and constraints from the active Spec/CSV/review context.

Minimum intent fields:

- train intent: module/baseline, dataset/fold/shot, key flags, expected script family
- eval intent: dataset/fold/shot, checkpoint selection, metric/report expectation
- constraints: branch, commit, required args, forbidden args, expected output/artifact path

2) Inventory candidate scripts:

```bash
python3 .codex/skills/autodl-remote-run-snippet/scripts/generate_snippet.py
```

3) Resolve scripts by matching intent to script names and contents. Prefer exact module/dataset/fold/shot matches. If exactly one train script and one eval script satisfy the intent, generate the command:

```bash
python3 .codex/skills/autodl-remote-run-snippet/scripts/generate_snippet.py \
  --branch "<branch>" \
  --train-script "scripts/<train_*.sh>" \
  --eval-script "scripts/<eval_*.sh>"
```

4) If there is no unique mapping, do not guess. Record the ambiguity in CSV `notes` or `issues/<SpecID>.review.md` and ask for the minimum missing decision.

## Rules
- Codex resolves scripts from intent; the user does not need to manually choose scripts unless the intent is ambiguous.
- Do not invent train/eval scripts. Use scripts that exist under `scripts/`, or report a blocker.
- Do not silently change experimental constraints to fit an available script.
- Prefer `scripts/train_eval_shutdown_autodl.sh` for train→eval→optional shutdown.
- After command generation, write the exact command, branch/commit, expected output path, and execution owner to CSV `notes` or `issues/<SpecID>.review.md`.
- If the command is handed to the user to paste remotely, mark the CSV row `remote_state=running_remote` and stop only at that recoverable pause point.
- If SSH credentials/session and explicit user authorization are available, Codex may run the generated command remotely, then still record the remote session and output path.
- `remote_state=running_remote` is not completion. Resume later by pulling artifacts, ingesting results, and updating the review handoff.
