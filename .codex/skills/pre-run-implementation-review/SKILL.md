---
name: pre-run-implementation-review
description: Review implemented research code after local lightweight validation and before the first training, evaluation, remote run, or experiment-result generation. Use for PRERUN-REVIEW rows, train/eval launch gates, AutoDL/tmux runs, or any task where wrong code would waste training time or contaminate conclusions.
---

# Pre-run Implementation Review

## Purpose

Use this as a one-time pre-run gate after code implementation and local lightweight validation, before launching training/eval/remote execution. The goal is to catch wrong implementation, broken data flow, wrong command binding, or invalid experiment setup before expensive runs start.

This is not TDD, not per-file review, and not final scientific result review.

## Required Inputs

Build a review packet from source artifacts, not from the main agent's confidence:

- Original requirement, Spec, or CSV intent.
- Current CSV row and the gated run row.
- Current `git diff` or reviewed `pre_run_code_commit`.
- Local validation evidence: `compileall`, unit tests, parameter-chain checks, config parsing, or smoke probes.
- Exact train/eval/remote command to run, with secrets redacted.
- Branch, commit, conda env, dataset, checkpoint, fold, shot, seed (按你的评测口径), output path.
- Project constraints: canonical files, baseline fallback, metric policy, remote artifact rules.

If any required input is missing, result is `blocked` unless the missing item is explicitly not applicable.

## Review Procedure

1. Verify intent alignment.
   Check the implementation matches Spec/CSV intent and non-goals. Reject changes that implement a different mechanism, move the experimental target, or add unrelated behavior.

2. Verify code location.
   Confirm behavior changes landed in active canonical files and scripts. Model behavior changes should land in your canonical files such as your canonical model files; v2 files are compatibility wrappers unless the task explicitly changes wrapper behavior.

3. Trace parameter data flow.
   Follow important values from entry to sink:
   `CLI/CSV command -> argparse/config -> train script -> pipeline/UNet/module config -> forward/loss/attention/eval sink`.
   Check names, defaults, bool parsing, list/block parsing, dtype/device assumptions, and branch conditions.

4. Check runtime state.
   Confirm configured modules expose expected state after setup:
   selected blocks/layers are nonzero, modules are instantiated, attributes exist, parameters enter optimizer groups, loss components are connected, and disabled paths remain valid.

5. Check sink effect.
   Ensure the implemented mechanism is actually read by the computation that matters: attention bias, gate, warmup, loss, mask, prototype, metric, or eval output. Do not accept config-file evidence alone.

6. Require a minimal probe when cheap and relevant.
   Use the smallest local or remote-safe probe that reaches the sink without full training:
   config parse, model construction, selected-layer audit, one fake/real mini forward, 1-step smoke, or targeted diagnostic log.
   Example checks: `active_layers > 0`, `last_warmup_weight is not None`, `loss_component is logged`, `gate_grad exists`, `selected_blocks` match intent.

7. Verify baseline and disable paths.
   Feature-off or baseline behavior must still run. Check default values, fallback branches, and unrelated routes such as baseline are not silently changed.

8. Verify run command binding.
   The command must run the reviewed branch/commit, use the expected script, and pass correct args. The run row's `commit_hash` or notes must point to the reviewed `pre_run_code_commit`, not later artifact/analysis commits.

9. Verify experiment validity.
   Check benchmark, fold, shot, seed (按你的评测口径), checkpoint, dataset paths, output directory uniqueness, and metric policy. <主指标> is primary; <辅助指标> is auxiliary. Do not allow setup drift that would make results incomparable.

10. Verify recoverability and secrecy.
   Ensure tmux/session, EXP_ROOT, logs, artifact path, resume instructions, and review handoff are recorded. Never echo or write secrets from `scripts/.env`.

## Decision Rules

- `pass`: all critical checks are satisfied, validation gaps are honest and non-blocking, and the run command is bound to the reviewed code snapshot.
- `blocked`: intent, data flow, runtime state, sink effect, baseline fallback, command binding, or experiment validity is unclear or wrong.

Do not allow a run with "probably correct" data flow. If a parameter reaches config but not the forward/loss/eval sink, block the run.

## Output Format

Write the review result into the CSV notes and review log using this structure:

```markdown
## PRERUN-REVIEW-N
- Result: pass | blocked
- Decision: allow_run | do_not_run
- Gated run: <csv id or command>
- Code snapshot: <branch>/<pre_run_code_commit>
- Intent: pass | issue
- Code location: pass | issue
- Parameter data flow: pass | issue
- Runtime state: pass | issue | not_checked
- Sink effect: pass | issue | not_checked
- Baseline/disable path: pass | issue | not_applicable
- Local validation: <commands and outcomes>
- Minimal probe: <probe and key observation, or validation_gap with reason>
- Run command binding: pass | issue
- Experiment validity: pass | issue
- Recoverability/secrecy: pass | issue
- Blockers: <none or exact blockers>
- Validation gaps: <none or honest gaps>
```

If blocked, insert or request fix work before the gated run. Do not start training/eval/remote execution until a later pre-run review passes.
