# Pre-review Smoke Contract

For `full_review`, the candidate commit must pass an isolated production-reaching GPU smoke before the reviewer is called. Smoke proves dynamic reachability, runtime memory bounds, and numerical stability; it does not replace the scientific review.

## Smoke Execution Invariants

1. **Step / Episode Budget & Dual Assertions**:
   - 1 to 10 production steps for step-driven training or inference;
   - Strictly at most 1 minimal single-shot episode for episode-driven workflows (typically <30 seconds runtime). Multi-shot or multi-episode loops are strictly forbidden during smoke.
   - **双核心断言闭环（真数据 1-Episode）**：Smoke 优先使用 1 个真实生产 Episode（1~2 张真实样本）并在其执行期直接完成两项硬核检验：
     (1) **工件落盘断言（Artifact Collection Assertion）**：核验目标工件（特征图、数组、中间参数、sidecar）真实写盘，字段非空、非零、无 NaN，产物清单与契约一致；
     (2) **假设参数位移断言（Hypothesis Parameter Delta Assertion）**：本轮待验证假设的核心模块参数在适应后存在非零位移（$\Delta > 0$）且梯度有效，杜绝参数冻结、未加入优化器或反传脱落的假跑。
   - **严禁二级探针（No Pilot Spiral）**：只要 1-Episode 双断言通过，控制面判定连通性与收集完备性 100% 成立，直接放行正式全量运行；**严禁模型自行派发 pilot10、pilot30 等规模验证套娃**。
   - **严禁全量数据集遍历与全量哈希**：Smoke 阶段严格禁止遍历全量数据集、生成全量数据哈希或提取全量特征缓存。必须使用极小切片（<=20 样本）或快速样本，坚决消灭 Smoke 前长达数十分钟的 CPU/磁盘空转。
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
7. **Closed-Scope Exemption & Synthetic Alternative (封闭评测任务门禁豁免)**:
   - 当任务在工程实现与批准 Spec 上为**严格封闭的全量评测**（例如固定 600 集全量评测，代码解析器拒绝任何短运行参数，且 Spec 明确禁止自主裁剪真实数据集）时，严禁为迎合门禁而私自裁剪数据集或在生产代码中开辟短评测分支。
   - 此时执行器必须自动采用等价替代契约：
     - `disposition`: `"not_applicable"`
     - `reason`: `"closed_full_evaluation_scope_no_short_route"`
     - `alternative_validation`: `"synthetic_cuda_probe + builder_adapter_contract"`
   - 只要具备已通过的本地张量/CUDA 合成探针（Synthetic CUDA Probe）及生产 Builder/Adapter 边界合同，Reviewer 与机器门禁必须直接判定放行，**严禁原地停滞向用户索要豁免授权或抛出选择题**。
8. **彻底废除数据级加密证据链（No Dataset Chain of Custody）**:
   - 严禁要求对数据集图像、掩码或样本逐文件计算 SHA256/哈希做防篡改审计或数据泄漏审计；
   - 静态数据集按路径直接读取，代码溯源仅认 Git Commit，严禁在 Smoke 或任何实验运行中建立区块链式的“数据级加密证据链”。

