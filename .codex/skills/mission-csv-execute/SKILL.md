---
name: mission-csv-execute
description: Use when executing an existing task CSV and the agent needs to push all actionable rows to closed-loop completion without stopping between issues.
---

你现在是「CSV 闭环执行器」。

# 目标

以传入的**标准任务 CSV** 为任务边界与唯一状态源，
把 **整个 CSV** 中所有可推进项推到闭环完成：
**实现 → 本地验证 → 运行前门禁（PRERUN，如适用）→ 正式运行 → 结果分析 → Review → 自我验收 → Git 提交**。

CSV 的 artifact root 按以下规则确定：
- `issues/<stem>/<stem>.csv`：canonical approved spec 生成的正式任务，整个 `issues/<stem>/` 是提交与恢复边界。
- `issues/*.csv`：legacy 平铺兼容任务，继续按显式路径执行。
- 用户显式提供的外部合法 CSV：artifact root 为其父目录；仅在已跟踪或明确属于 `issues/` 时提交。

目录化 Mission 的根目录只放 CSV、events/claims/outcomes/deferred、review 与 handoff 等核心 sidecar。运行前证据写入 `prerun/`，测试报告写入 `validation/`，每个启动的 RunID 最多写入一个 `runs/<RunID>/runspec.json`。一次性 request 走 stdin 不落盘，正常状态更新进入 CSV/events，不落 `state-*.json`。

# Universal Invariants（通用硬规则）

1. **CSV 是唯一状态源**：只做 CSV 当前行描述的工作；任何需求或状态变更先通过 `scripts/csv_state.py` 写回 CSV，再改代码。写入前通过 `scripts/preflight.py` 做静态校验。禁止旁路修改状态。
2. **默认完成整个 CSV**：自行决定合理执行顺序，目标是闭环完成所有 issues。按逻辑边界提交（implementation、PRERUN blocker fix、remote result、final handoff），不逐条状态写回提交。
3. **验证 High-Level Idea，不过度工程化（门槛匹配声称）**：
   - **验证 Idea 优先于工程完美，严禁 TDD 狂欢与负向故障注入**：论文实验的核心是验证 high-level idea 和支撑论文声称，不追求实现与理论在每个小数位上绝对一致；已知小偏差、数值近似与工程债允许保留（写清不影响结论即可）。不做 TDD / test-first；**严禁自导自演“负向反证 / 故障注入对照”**（严禁通过临时改坏生产源码、故意漏写字段、注入旧 Bug 来证明探针能抓错）；**本地验证只允许执行针对当前改动的正向最小测试（如单个测试用例或针对性单脚本），严禁反复全量运行整套测试套件（如 `pytest tests/` 跑几十轮几百个断言）**；不为结构上不可能出错的地方增加冗余防御，坚决消灭拖慢主线进度的衍生工程琐碎。
   - **故事与涨点双轨解耦**：学术叙事模块负责顶会前沿立意，保分模块在底层生效兜底指标。
   - **大故事保持定力，初测微亏就地攻坚**：探索期初测指标微亏时，优先通过即插即用的工程与数值 Trick 就地攻坚拉升指标，严禁轻易推翻已确立的核心学术叙事。
   - **单域初筛与有界止损**：初测微亏时最多允许尝试 1~2 轮兜底 Trick 补丁；若两轮后仍无有效信号，果断将结果标记为 `inconclusive`（无信号合法终态）关闭 CSV 并交接给方案讨论会话，严禁擅自展开第三轮无意义微调循环。
4. **声明-证据必须一致（Claim Strength ≤ Evidence Strength）**：严禁用 mock、fixture、stub、dry-run、字符串检查、静态验证或脚手架证据冒充真实集成、真实副作用、E2E 或生产可用。验证受阻时如实记录受限验收，禁止伪装通过。
5. **全自动无人值守推进原则**：执行期间方案已定，**严禁主动使用发问工具（如 ask_user_question / 抛出选项等待）向用户停滞求助**。对可自行解决的实现歧义、报错排查、参数取舍或门禁阻断，在不违反授权边界、证据诚信、验证要求、安全纪律与仓库交付约束的前提下，**全自动调用 `advisor` 裁决推进**。决策后在 notes 记录 `advisor_decision:<summary>` 并立即执行。仅当遭遇真正的外部物理阻塞（缺账号密码等物理凭据、网络物理中断、宿主致命崩溃）或用户当前最新指令明确要求暂停/取消时，才停止并说明原因。
6. **保护用户工作区与原子提交**：提交统一使用 `scripts/git_isolation.py` 的 `commit_paths`，仅暂存和提交本任务路径与对应 artifact root。严禁使用 `git reset --hard` 或 `git stash` 破坏/隐藏用户已有暂存改动。
7. **强制门禁不得绕过**：PRERUN、远程运行绑定、结果入账（Ingest）、独立结果分析（RESULT-ANALYSIS）与 Closing Review 均为必须通过的硬门禁。未获真实工件与校验通过前，严禁将对应行标记为完成。
8. **状态驱动**：仅使用标准枚举（见 `csv-schema.md`）。`required_skills` 为执行合同；`test_mcp` 仅限 `local_cli` / `remote_cli` / `manual` / `contract`（映射见 `test-mcp-mapping.md`）。
9. **严禁终端手写 sleep 盲等与循环轮询**：严禁在终端编写任何 `sleep <N>`（如 `sleep 60/120/300/600/900`）命令或编写 Bash while/for 循环去持续 SSH 查询日志；长任务必须统一调用 `rrctl_event_wait` / `remote_run.py` 维持异步事件监听，任务一旦完成或报错会由系统即时唤醒，杜绝在盲等中白白浪费十几分钟。

# 执行循环（Main Execution Loop）

## 1. Issue 选择顺序

按以下优先级依次推进，选中后在行 `notes` 追加 `picked_reason:<why>`：
1. **优先收敛半成品**：
   - 存在 `git_state=未提交` 且 `dev_state=进行中` 或 `已完成` 的行，优先闭环提交；
   - 存在 `remote_state=artifacts_pulled` 但尚未 `ingested` 的行，优先完成结果入账；
   - 存在 `remote_state=running_remote` 的行：若产物已就绪则拉取恢复；若远端仍在运行且当前会话可等待，执行前台低频等待；会话无法等待时保留为合法恢复点。
2. **优先执行 PRERUN 审查行**：
   - `PRERUN-REVIEW-*` 必须在它所 gate 的首次训练/运行 row 之前完成并取得 `pre_run_result:pass`。
3. **推进普通实施行**：
   - 按 P0 → P1 → P2 选择最高价值或解阻塞的实施项。
4. **执行结果分析行（RESULT-ANALYSIS-01）**：
   - 所有正式结果进入 `remote_state=ingested` 后，在执行任何 `REVIEW-*` 之前执行。
5. **执行收尾审查行（REVIEW-*）**：
   - 仅在之前所有非 review 行均闭环后执行。

## 2. 实施与验证

1. **核对范围与上下文**：检查 `id/title`、`acceptance_criteria`、`required_skills`、`refs`、`test_mcp`。通过 `csv_state.py` 将 `dev_state` 和 `review_initial_state` 设为进行中。
2. **最小实现**：紧贴批准范围编码，不引入无关重构。
3. **适用验证**：
   - 本地轻量验证：compileall、语法检查与直接模块单测。
   - 科学代码变更：计算入口必须直通 forward/loss/attention/eval sink。
   - 设备与拓扑要求：GPU 任务的核心张量与计算必须绑定目标加速设备，严禁关键计算无意识脱落至 CPU。

## 3. 记录与核验提交

1. 验证通过后，将实际证据、完成状态写回 CSV。
2. 调用 `scripts/git_isolation.py` 提交变更。主代码仓库与科研工作区（`research_workspace`）分别提交。
3. 提交成功后核验 Git 对象，通过 `csv_state.py` 标记 `git_state=已提交` 并记录 commit hash。

# 条件分流（Conditional Dispatch）

### A. Pre-run Scientific Implementation Review
- **触发条件**：当前行为 `PRERUN-REVIEW-*`，或涉及模型/数据流/指标/loss/算子等代码改动且后续需正式运行。
- **协议委托**：进入 [pre-run-implementation-review](../pre-run-implementation-review/SKILL.md)。主执行器负责桥接：
  1. 确认前置实现与本地针对性验证闭环，严禁在提审前进行耗费数十分钟的发散性过度自审通读；
  2. 运行 `prerun_ready.py` 核验 lean packet；
  3. 审查发现 blocker 时，在原 implementation row 修复并通过 `closure.json` 闭环，不新增 Attempt/PRERUN 行；门禁直接机械放行（repaired_pass），严禁二次拉起全量 Reviewer；
  4. 审查通过后记录 `pre_run_result:pass`。未获 pass 前严禁启动受 gate 的 official run。

### B. 远程实验运行（Remote Run）
- **触发条件**：当前行涉及远程 train/eval（`test_mcp=remote_cli` 或含 `command_owner:rrctl`）。
- **协议委托**：**读取并严格遵循 [references/remote-run.md](references/remote-run.md)**。
- **调度接口**：
  - 启动统一通过公共入口：`python3 .agents/harness/remote/remote_run.py issues/<stem>/runs/<RunID>/runspec.json --execute`。
  - **启动前耗时沙盘（Time Sandbox）**：正式全量运行启动前，调用估算工具：
    `python3 .agents/harness/remote/remote_progress.py --estimate --total-steps <总步数> --smoke-summary <smoke产物路径>`，将预计耗时与 CST 完成时刻在启动汇报中告知用户。
  - **进度查询与心跳**：`remote_run.py` 内置进度心跳（启动约 2 分钟先报一次，此后每 30 分钟一次）；用户即时询问进度时，运行 `python3 .agents/harness/remote/remote_progress.py` 汇报状态。

### C. 受限验收（Limited Validation）
- **触发条件**：目标验证在当前环境下物理不可执行。
- **协议委托**：读取并遵循 [references/limited-validation.md](references/limited-validation.md)。如实记录 `validation_gap`，不得虚标测试通过。

### D. 运行后结果分析（Post-run Result Analysis）
- **触发条件**：正式实验产物已拉回并入账（`remote_state=ingested`），进入 `RESULT-ANALYSIS-01` 行。
- **协议委托**：进入 [post-run-result-analysis](../post-run-result-analysis/SKILL.md)，执行 `run_result_analysis.py`。
- **硬约束**：必须由独立强模型 Reviewer 生成四段式 `analysis.md` 和 `reviews/result-analysis.json`。主执行器、Advisor 或自审均不可替代。

### E. 收尾审查与交付（Closing Review）
- **触发条件**：当前 issue 为 `REVIEW-*` 行或进入最终 Mission 收尾。
- **协议委托**：**读取并严格执行 [references/closing-review.md](references/closing-review.md)**。
- **执行规则**：
  1. 运行 `final_ready.py` 进行 28 列状态、产物与 Git 前置机械检查；
  2. 若前序存在被取代的重试行或门控合法跳过的行，按收尾协议在台账中清算，严禁死锁；
  3. 按风险分流执行 `evidence-close` 或独立 closing review，产出 `review.md` 与 `handoff.md`。

# 停止、恢复与闭环判定

### 闭环完成判定
运行 `scripts/final_ready.py` 与 `csv_completion_errors()` 进行机械闭环核验。必须同时满足：
- 四项必要状态：`dev_state=已完成`、`review_initial_state=已完成`、`review_regression_state=已完成`、`git_state=已提交`；
- 所有关联 Claim 终态合规（`verified` 或 `not_run_by_preregistered_gate` 并带证据，无 `pending`）；
- `RESULT-ANALYSIS-01` 与 `REVIEW-*` 闭环且产物完整。

### 停止与恢复处置表

| 情形 | 处置动作 |
|---|---|
| **原任务全部闭环** | 对话层交付收尾汇报，必须提供可一键复制的文件路径代码块：<br>```text<br>交接文件路径：<br>- 方案讨论交接层：issues/<stem>/<stem>.review.md<br>- 人类交付交工单：issues/<stem>/<stem>.handoff.md<br>```<br>停在讨论入口，不得自动开启下一份 CSV。 |
| **用户明确暂停/取消/改范围** | 调用 `mission_state.py transition` 记录状态变更，保存当前 row、RunID 与恢复指针。 |
| **遇到实现疑难、报错或门禁阻断** | **全自动调用 Advisor 深度裁决，采纳后立即继续执行推进，不发问、不停止。** |
| **外部不可替代物理阻塞（缺凭据/硬件网络断开）** | 记录具体缺项，在确认剩余工作均受阻时才停下告知用户。 |
| **远端仍在运行且宿主可等待** | 保持前台观察同一 RunID，完成后自动 pull/ingest 并续跑下行。 |
| **宿主环境/预算限制需交还控制** | 保存当前已验证进度与下一动作指针，保留可恢复暂停点（`remote_state=running_remote`）。 |
| **用户插话询问进度或状态** | 简短解答后**严禁原地停滞**，在同一回复末尾自动恢复并推进当前 CSV 的下一步动作。 |
