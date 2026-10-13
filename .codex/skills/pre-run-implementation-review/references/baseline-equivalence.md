# Baseline-Equivalence Probe Specification

Required when the change claims any of: baseline-preserving, zero-init no-op, disabled-path equivalence, or reuse of a canonical implementation.

## Exploration-Stage and Side-by-Side Exemption

In the exploration stage (spec §9.0.1 minimum mechanism screen) or when baseline and candidate arms are evaluated side-by-side in the same screen run, an empirical remote GPU Baseline-Equivalence Probe is **NOT** required before launching the screen:
- When both arms execute side-by-side, the baseline arm runs its own independent canonical implementation directly.
- Structural verification or local CPU unit tests (proving disabled paths bypass the modification or produce zero residual on dummy tensors) are sufficient for the pre-run gate.
- If equivalence is not measured empirically, the reviewer records `Baseline/disabled path` as `correct` (grounded in local unit evidence) or `not_applicable` (no standalone equivalence claimed); it must **NOT** record `not_evaluable` to block an exploration screen.

## Empirical Measurement Protocol

Structural evidence does not establish equivalence. Zero residual, zero additivity, and matching counters are necessary, not sufficient: a path disabled elsewhere in the forward can still change the output while every new residual reads exactly zero.

The claim must be settled by numerical measurement:
1. Run the production entrypoint before any parameter update on one fixed evaluation setting (dataset, project parameters, fixed seed, small fixed sample count); for inference, use the same frozen weights and bounded batches;
2. Compare against the named reference under the same cell and post-processing;
3. Record `reference_id`, `reference_weights_path`, `candidate_weights_path`, both metric values, absolute difference, and tolerance;
4. This is the only metric a smoke may compute; it is not an official result and is never ingested.

## Probe Schema & Packet Attachment

For an equivalence claim, set `pre_review_smoke.baseline_equivalence_required:true` and attach `baseline_equivalence_probe` in that same smoke object:

```json
{
  "reference_id": "named canonical reference",
  "reference_weights_path": "reference weights path",
  "candidate_weights_path": "candidate weights path",
  "metric_name": "metric used by the fixed comparison",
  "reference_metric": 0.25,
  "candidate_metric": 0.25,
  "absolute_difference": 0.0,
  "tolerance": 0.000001,
  "evaluation_setting": {
    "dataset": "fixed evaluation dataset",
    "parameters": {},
    "seed": 0,
    "sample_count": 1,
    "post_processing": "identical post-processing for both paths"
  },
  "evidence_paths": ["path to measured comparison evidence"]
}
```

Verdict feeds the `Baseline/disabled path` dimension directly:
- Within tolerance -> `correct`;
- Outside tolerance -> `incorrect` (this is a blocker);
- Probe not runnable -> `not_evaluable` (the official run does not start).
