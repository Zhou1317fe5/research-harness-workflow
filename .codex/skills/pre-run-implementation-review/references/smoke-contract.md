# Pre-review Smoke Contract

For `full_review`, the candidate commit must pass an isolated production-reaching GPU smoke before the reviewer is called. Smoke proves dynamic reachability, runtime memory bounds, and numerical stability; it does not replace the scientific review.

## Smoke Execution Invariants

1. **Step / Episode Budget**:
   - 1 to 10 production steps for step-driven training or inference;
   - Strictly at most 1 minimal single-shot episode for episode-driven workflows. Multi-shot or multi-episode loops are strictly forbidden during smoke.
   - Expected runtime is strictly bounded (typically <5 minutes). Smoke is a dynamic connectivity probe, not an evaluation.
2. **Shift-Left Local Contract Validation (Pre-Smoke Gate)**:
   - Before launching remote GPU smoke, execute a fast local CPU dummy-tensor probe.
   - Construct micro dummy tensors (e.g. 32x32 or 64x64 random values).
   - Verify tensor shapes, dimension slicing, and dtype conversions across supported branches (1-shot/multi-shot layouts, disabled paths) without exceptions. Never push unverified layouts directly to GPU.
   - Exercise the actual request Builder → Adapter identity/required-artifact contract and output/asset-summary assembly with tiny local inputs, including the final save/check path. Cover production interfaces, not another hand-written copy; do not enlarge GPU smoke to discover Python interface or directory mistakes.
3. **Compute Topology & Throughput Assertion**:
   - Verify modified core operators execute on the target accelerator (`tensor.is_cuda` or accelerator device). Silent fallback to host CPU while holding GPU VRAM is strictly prohibited.
   - Per-step / episode latency must stay within normal accelerated limits.
   - Reuse the bounded smoke to check real storage access and projected cache/RAM size where relevant. A one-time load is not steady-state throughput; changing sampling order to fix I/O changes resume identity and cannot be labeled exact continuation of an old cursor. Bind sampler/quality-sample identity in project checkpoints without re-running old experiments to repair bookkeeping.
4. **Artifact-Driven Verification**:
   - Verification relies strictly on process `exit 0`, process monitor `status.json`, and structured JSON artifacts (`smoke_summary.json` / `summary.json`).
   - Never scrape console log text or rely on brittle regex matching in stdout. Never modify scientific code to appease stdout text assertions.
5. **Numerical Check**:
   - Training requires `finite_loss:true`.
   - Loss-free inference records `computation_kind:inference` and `finite_outputs:true` (checking actual tensor outputs before argmax or thresholding).
6. **Isolation & Cleanup**:
   - `official_metrics_disabled:true` and `artifact_ingest_disabled:true` (except the bounded Baseline-Equivalence Probe).
   - Terminal cleanup: delete checkpoints, optimizer states, and large intermediate files within the bound smoke output root on success, failure, or abort.
   - Retain `console.log`, `status.json`, and `smoke_summary.json`. Require `checkpoint_cleanup_completed:true` and `checkpoint_paths_remaining:[]`.
   - Cleanup is confined to disposable files of the current RunID. Preserve local pulled diagnostics and failed snapshots of every other RunID; never clear the shared `smoke-artifacts` root as a retry operation. Existing evidence is not recreated from a new attempt.
