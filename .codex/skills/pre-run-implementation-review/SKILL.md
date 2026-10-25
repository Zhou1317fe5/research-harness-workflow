---
name: pre-run-implementation-review
description: Apply risk-based implementation checks before official research runs; require formal review only for targeted_review or full_review.
---

# Pre-run Scientific Implementation Review

## 1. Purpose & Scientific Principles

PRERUN 是正式实验前的一票否决科学门禁，目的不是追求代码内部完美，而是排除**可能翻转科研结论的实现错误或实验无效性**。

遵循以下科研执行原则：
- **验证 High-Level Idea，不过度工程化**：论文实验的核心是验证 high-level idea 和支撑论文声称，不追求实现与理论在每个小数位上绝对一致；已知小偏差、数值近似与工程债允许保留（写清不影响结论即可）；不做 TDD / test-first，不为结构上不可能出错的地方增加冗余防御，坚决消灭拖慢主线进度的衍生工程琐碎。
- **故事与涨点双轨解耦**：学术叙事模块负责顶会前沿立意，保分模块在底层生效兜底指标。PRERUN 的职责是验证代码是否具备进入正式实验的资格，而不是看到局部指标波动就推翻研究假设。
- **大故事保持定力，初测微亏就地攻坚**：探索期初测指标微亏时，优先通过即插即用的工程与数值 Trick 补丁就地攻坚拉升指标，严禁轻易推翻已确立的核心学术叙事。
- **单域初筛与有界止损纪律**：初测微亏最多允许尝试 1~2 轮兜底 Trick 补丁；若两轮后仍无有效信号，果断将结果标记为 `inconclusive` 终态收工，严禁展开第三轮无意义死磕或微调循环。

普通文档、记账、测试、启动绑定、调度或监控改动不创建 PRERUN 行，不调用 Reviewer。只有涉及模型计算、数据流、损失函数、指标语义、关键算子或结果归属的代码变更才进入完整审查序列。

**超参扫描与评测豁免铁律**：
- 纯超参数微调（如调整 learning rate、temperature、loss weight、迭代轮数等）、命令行参数变动、全量评测任务、基线复现或主表重跑，**严禁创建 PRERUN 行，严禁调用 Reviewer，严禁运行前置 GPU Smoke**；
- 超参数必须通过启动命令行或 RunSpec 配置传入，严禁通过修改 Python 源码默认值来改参，保持代码库 0 变更，直接进入正式 EVAL/TRAIN 行。

## 2. Risk Routing

在提交的代码变更上运行 `prerun_route.py` 确定审查级别：

```bash
python <skill-dir>/scripts/prerun_route.py <change-manifest.json>
```

- `no_prerun`：文档、状态、纯单测或格式改动。不创建 PRERUN 行。
- `hyperparameter_or_evaluation_config_only`（映射至 `no_prerun`）：纯超参数微调、评测配置、命令行参数调整。不创建 PRERUN 行，直接放行。
- `micro_validation`：确定性运行时绑定变更，且保持科学契约并通过生产探针。不调用 Reviewer。
- `smoke_validation`：进程监管、监控、调度或产物传输。通过生产环境 Launcher Smoke 验证，不调用 Reviewer。
- `targeted_review`：凭据安全或破坏性生命周期变更。仅审查相邻安全不变式。
- `full_review`：模型计算、特征流、损失函数、指标计算、检查点、关键参数或未知改动。要求当前 Commit 的 GPU Smoke 与一次独立的 `scientific_review`。

## 3. Core Scientific Invariants

1. **证据与声称匹配（Evidence/Claim Matching）**：PRERUN 证据强度必须足以支撑交付声明。严禁用 Mock、Fixture、Dry-run 或静态检查冒充动态端到端完成。
2. **计算拓扑与算力防脱落（Compute Topology Integrity）**：声明使用 GPU 等加速设备时，核心数学算子、模型前向与关键特征计算必须显式绑定目标加速设备，严禁在关键路径上无意识脱落至主机 CPU 串行执行导致性能假死与资源空转；单步耗时必须处于加速执行的合理量级内。
3. **Smoke 真实性与封闭任务豁免（Smoke Validity & Closed-Scope Exemption）**：
   - Smoke 必须运行真实生产入口与目标加速设备，仅认进程 `exit 0`、`status.json` 及结构化工件（如 `smoke_summary.json`），严禁抓取控制台文本或以正则断言判失败。详细规约见 [references/smoke-contract.md](references/smoke-contract.md)。
   - **封闭全量评测任务自动豁免**：若任务为封闭全量评测（代码拒绝短运行参数且 Spec 严禁裁剪真实数据），严禁在生产代码增加短分支，直接以 `disposition: not_applicable`、`reason: closed_full_evaluation_scope_no_short_route` 配套本地合成 CUDA 探针及 Adapter 边界合同放行，严禁停滞向用户索要授权。
4. **等价性数值判定（Baseline-Equivalence）**：当变更声明“保留基线”、“零初始化等价”或“禁用路径等价”时，必须以实际数值证据判定，严禁仅通过阅读代码断言等价。探索阶段 side-by-side 对照具备免跑豁免。详细规约与 Schema 见 [references/baseline-equivalence.md](references/baseline-equivalence.md)。

## 4. Formal Review Protocol

对于 `targeted_review` 与 `full_review`：

1. **构造 Lean Packet**：使用 `prerun.scientific-review.v1` Schema，通过 `python <skill-dir>/scripts/prerun_ready.py <packet.json>` 完成机器预检（`ready:false` 则回原实施行补齐证据）。格式规约与 Reviewer 运维见 [references/reviewer-runtime.md](references/reviewer-runtime.md)。
2. **调度独立 Reviewer**：

<!-- reviewer-launcher:start -->
Write the complete review task to a file, then run the project-owned persistent reviewer job. It records the independent session before waiting, resumes that same session across transport failures, and writes the only accepted verdict artifact:

```bash
python .agents/harness/reviewer_job.py \
  --backend codex \
  --packet <packet.json> \
  --task <review-task.md> \
  --job-dir <mission-dir>/reviews/<prerun-row>
# 模型不在命令行复述：runner 按 --backend 取 review_model.review_job_model(<backend>)；切换模型只改 review_contract.toml
```

Do not add `--ephemeral`, launch a parallel reviewer, or bypass this job with a one-shot `codex exec`.
<!-- reviewer-launcher:end -->

3. **单一独立 Reviewer 原则**：只派发一次 Reviewer，一次性返回全部可判定 Blockers。严禁通过重复派发换 Reviewer 刷 PASS。服务级故障按 [references/reviewer-runtime.md](references/reviewer-runtime.md) 协议进行有界恢复（严禁使用 `pkill -f reviewer_job`）。

## 5. Blocker Repair & Closure

将 Reviewer 提出的问题按批准 Spec 的 Research Contract（§9.0）分类：
- **前提类（Precondition）**：机制未到达输出、存在数据泄漏、指标对比不公平、算子与理论矛盾。**必须在原实施行修复**。
- **诊断类（Diagnostic）**：数值精度打磨、工程规范或无关边界。记录为 `validation_gap`，不阻断正式运行。

**修复闭环协议**：
1. 主代理在原实施行完成修复，通过本地针对性单测验证；
2. 将修复代码 Diff 与测试证据写入 `closure.json` 机器闭环；
3. **若前序 Smoke 已验证动态连通性且本次修复由本地测试确定性覆盖，严禁重复运行耗时 GPU Smoke**；
4. 严禁创建 Attempt 2、Resolution Review 或新的 PRERUN 行。

## 6. Verdict Semantics & Gate Rule

审查输出必须是结构化 `verdict.json`（Schema `prerun.scientific-verdict.v1`）：
- `scientifically_correct`：实现、数据流、拓扑与动态可达性通过，放行正式运行；
- `scientifically_incorrect`：存在明确科学缺陷，阻断正式运行，直至所有 Precondition Blockers 经 `closure.json` 验证闭环；
- `not_evaluable`：仅用于 Spec 自身存在不可调和的矛盾。

**强制门禁（Mandatory Gate）**：
若路由判定需要 PRERUN，**在取得合法的 `pre_run_result:pass` 之前，任何受 Gate 保护的正式运行一票否决，严禁启动**。
