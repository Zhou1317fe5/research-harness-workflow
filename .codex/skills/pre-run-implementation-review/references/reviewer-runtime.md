# Reviewer Runtime, Packet & Service Recovery

## 1. Lean Packet (`prerun.scientific-review.v1`)

Create one JSON packet containing only:
- `review_mode`: `scientific_review` or `targeted_review`;
- repository, reviewed commit, diff base, approved basis, implementation intent, exact official command;
- passing local validations;
- current-commit `prerun.pre-review-smoke.v1` evidence for `scientific_review`;
- critical values with expected source, sink, and production-reaching evidence;
- experiment identity and output collision policy.

Validate the packet with:
```bash
python <skill-dir>/scripts/prerun_ready.py <packet.json>
```
`ready:false` means missing reviewable evidence. Fill the reported gap and rerun readiness before creating the PRERUN row.

## 2. Reviewer Process Lifecycle & Lock Ownership

- **Ownership via flock**: `reviewer_job.py` takes an exclusive file lock on `<job-dir>/job.lock`. A second concurrent runner fails with `reviewer job is already running`.
- **Never use fuzzy `pkill -f reviewer_job`**: The executor's own shell command line contains this string. Running `pkill -f reviewer_job` will terminate the executor's own shell.
- **Service Recovery**:
  1. Treat quota errors, launcher failures, and silence before verdict as review-service failures.
  2. Bounded same-session resume: `reviewer_job.py` records session metadata and resumes the recorded session across transport failures without generating a second scientific opinion.
  3. If a stale job must be cleared: find the exact PID recorded in `job.json`, terminate that specific PID, and re-run the same `reviewer_job.py` command. The runner records `service_failed` and resumes the session.
  4. At most one replacement execution is allowed. If replacement also fails, record the capability gap once and halt the official run.

## 3. Verdict Artifact & Model Verification

The gate accepts only `verdict.json` with schema `prerun.scientific-verdict.v1`.
- Reviewer prose or exit code alone cannot open the gate.
- `requested_model` and `observed_model` are checked by `.agents/harness/review_model.py`.
- Model configuration lives only in `.agents/harness/config/review_contract.toml`.
