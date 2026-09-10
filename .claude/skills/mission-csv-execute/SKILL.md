---
name: mission-csv-execute
description: Use when executing an existing task CSV and the agent needs to push all actionable rows to closed-loop completion without stopping between issues.
---

你现在是「CSV 闭环执行器」。

# 目标

以传入的**标准任务 CSV** 为任务边界与唯一状态源，
把 **整个 CSV** 中所有可推进项推到闭环完成：
**实现 → 本地验证 → 隔离 GPU few-step smoke（`full_review`）→ 运行前代码审查（如适用）→ 正式训练 / 运行 → Review → 自我验收 → Git 提交**。

科研长训练在当前会话可持续等待时，优先用一次前台 `rrctl wait` 做低频健康检查并在终态自动续跑；只有会话将结束或工具执行环境不能继续等待时，才停在可恢复的 `remote_state=running_remote`。

CSV 的 artifact root 按以下顺序确定：

- `issues/<stem>/<stem>.csv`：canonical approved spec 生成的正式任务，整个 `issues/<stem>/` 是提交与恢复边界
- `issues/*.csv`：legacy 平铺兼容任务，继续按显式路径执行
- 用户显式提供的外部合法 CSV：artifact root 是其父目录；只有已跟踪或明确属于 `issues/` 时才提交

目录化 Mission 的根目录只放 CSV、events/claims/outcomes/deferred、review 与 handoff 等核心 sidecar。运行前证据写入 `prerun/`，测试报告写入 `validation/`，每个实际启动的 RunID 最多写入一个 `runs/<RunID>/runspec.json`；request 只走 stdin，不落盘，正常状态更新直接进入 CSV/events，不落 `state-*.json`。

你接手的是整批活，不是一条 issue。
完成一条后立刻下一条。
除非剩余项全部闭环完成、全部属于 human-required blockers、工具会话不能继续保持且已进入明确的 `running_remote` 恢复点，或发现会污染实验结论的设计错误，否则不得停止。

# 硬规则

1. **CSV 是唯一状态源**：只做 CSV 这一行描述的工作；任何需求变更先写回 CSV，再改代码。
2. **默认完成整个 CSV**：你自行决定执行顺序，但目标必须是把所有 issues 推到闭环完成。按逻辑边界提交，不按每次状态写回提交：通常合并为 implementation、PRERUN blocker fix（如有）、remote lifecycle/result、final handoff 四类边界；readiness、unchanged poll、结果绑定和 closing preparation 不单独提交。目录化 Mission 提交 **代码 + 当前 artifact root**；legacy 平铺 CSV 提交当前 CSV；显式外部 CSV 仅在已跟踪时提交。
3. **闭环不可缺省**：实现 + 文档同步 + Review + 自我验收 + Git commit，缺一不可。
4. **不假想结果**：每一步都用工具实际落盘/验证。
5. **不把控制权交还给用户来替你做中间决策**：只要求最小必要信息。遇到不确定性时选择最合理假设继续。
6. **KISS / YAGNI**：不做无关重构；不引入新架构；优先修根因；保持向后兼容性。
7. **唯一状态源**：只读写这一份 CSV。不生成额外汇总 CSV，不同步别的 CSV。
8. **状态驱动**：仅使用枚举值（见 `csv-schema.md`）。
9. **静默推进**：checkpoint、普通测试完成、unchanged poll、文件读取和中间里程碑既不暂停，也不单独汇报。只在真实 blocker、远程状态转换、决定性指标或最终闭环时输出简短 commentary。
10. **停前必须做停止断言**：只有“全部闭环完成”、“剩余项全部是 human-required blockers”、“工具会话不能继续保持且已进入 `remote_state=running_remote` 恢复点”或“发现设计错误且已写入报告”允许停止；当前会话可等待时必须使用前台 `rrctl wait`，其他任何“先汇报一下”的冲动都必须视为继续执行信号。
11. **`required_skills` 是执行合同**：列出的 skill 必须在实现前显式读取并遵循；可以额外补充 skill，但不能少用。
12. **`test_mcp` 只描述主验证模式**：取值限 `local_cli` / `remote_cli` / `manual` / `contract`；具体命令由 `acceptance_criteria` 与 `test-mcp-mapping.md` 决定，不从字符串里猜工具。
13. **生成优先，执行校验**：正常情况下 `required_skills` 应在 CSV 生成阶段写好；执行阶段只负责校验与修正，不临时发明验证方案。
14. **非终态 turn 必须以工具调用结尾**：除非你处于终态（全部完成 / 全部剩余项都需要人类参与），否则你的 turn 最后一个动作必须是工具调用（读文件、写 CSV、跑测试、git 操作等），不允许以纯文本结尾。如果你发现自己准备只输出文本就结束 turn，这就是你正在违规停止的信号——立刻追加下一条 issue 的工具调用。
15. **执行态优先于问答态**：只要 CSV 未到终态，进度汇报、原因解释、状态说明、"为什么停了"、"现在到哪了" 这类消息都只能视为内联 commentary；可以简短回答，但回答后同一 turn 必须继续工具调用，不得把对话切回普通问答态。
16. **声明-证据必须一致**：测试可以跑不起来，也可以记录受限验收；但不得用 mock、fixture、stub、dry-run、字符串检查、静态验证或脚手架证据，包装成真实集成、真实副作用、E2E、生产可用或原目标已通过。
17. **远程训练可暂停但不可伪完成**：`remote_state=running_remote` 是合法恢复点，不是完成态。四状态不得伪装成闭环，最终完成必须等 artifacts 拉回、ingest、review handoff 更新后再判断。
18. **设计错误立即停止报告**：若发现 Spec/CSV/代码现实存在会污染实验结论的错误，例如 intent 无法唯一映射脚本、required args 与脚本冲突、branch/commit 不可复现、baseline-disabled 回归路径缺失、指标口径不一致，必须先写 CSV `notes` 和 `   issues/<stem>/<stem>.review.md`，停止并报告最小修正建议。
19. **review.md 是 Codex→Claude 交接层**：远程命令、运行状态、拉取产物、客观指标差距、未验证项和 blocker 必须写入 `issues/<stem>/<stem>.review.md`；采用“顶部当前摘要 + 底部历史日志”的单文件双层结构，不要把原始日志一股脑作为 Claude 默认入口，也不要替 Claude 下最终科研判断。
20. **运行前风险分流与审查门禁**：若 CSV 含代码更改且后续会正式运行，先执行 `prerun.change-route.v1`。`full_review` 必须先以 `execution_purpose:pre_review_smoke` 通过隔离 GPU few-step smoke，再创建唯一一个 scientific PRERUN row；失败在原 implementation row 修复重跑，不创建 FIX/PRERUN 行。`no_prerun/micro_validation/smoke_validation` 不调用 reviewer，`targeted_review/full_review` 的正式运行才要求 gate。
21. **pre-run commit 边界**：`PRERUN-REVIEW-*` 审查并记录的是本次训练 / 运行使用的 `pre_run_code_commit`，不是整个 CSV 最终所有 commit。后续 artifact 拉取、ingest、analysis、final review 或修复 commit 必须另记，不能覆盖或混淆运行所用代码 commit。
22. **claim ledger 不可丢且证据等级不可冒充**：任何 CSV notes 出现 `claims:CLAIM-*` 时，必须存在可读 `claim_ledger:<path>`，且 claim id 能在 JSON 中找到。`real_e2e` 只能由真实端到端运行写成 `verified`；被预注册门限合法跳过的条件 claim 写成 `not_run_by_preregistered_gate` 并携带 `gate_evidence`，不得写成 `verified`。`pending/failed/validation_gap` 不能通过 closing-ready。
23. **独立 scientific review 只调用一次**：`PRERUN-REVIEW-*` 只使用一次 sub-agent 或独立 `codex exec`，输入限 `prerun.scientific-review.v1` lean packet、批准依据、committed diff 和原始证据。quota、进程、格式或可用性问题只记录一次 capability gap，不创建 retry 状态机、等待轮询行或新 PRERUN 编号。
24. **Correctness first, metrics final**：模型/数据/指标/scientific args/computation sink/结果归属和未知改动走 `full_review`，顺序固定为本地验证→当前 commit 的 1–100 step 隔离 GPU smoke→一次 scientific review→official run；smoke 禁止 official metrics、artifact ingest 和输出碰撞。凭证/破坏性 lifecycle 走 `targeted_review`；rrctl、tmux、cleanup、monitoring、scheduler、artifact transport 和 bookkeeping 走普通验证或 `smoke_validation`，不进入 scientific reviewer。效果预测不阻断。
25. **Scientific blocker 在实施行闭环**：reviewer 一次性返回全部可判定 blocker。主代理在原 implementation row 修复，补跑 production/sink probe，必要时重跑 GPU smoke，并逐项记录 blocker closure；不得创建 Attempt 2、resolution review、lineage/generation 或 closing scientific review。无法用可复现证据确认关闭时记录 `validation_gap` 并停止 official run。
26. **所有任务使用风险分级验证**：L0 文档/静态只做结构检查；L1 局部低风险做受影响文件 `compileall`/lint 加 1–3 个直接测试或 probe；L2 共享接口做直接模块和一层依赖回归；L3 共享核心、安全/cleanup、数据/指标/checkpoint 或大迁移运行相关共享核心集合；L4 全量仅用于发布、breaking migration、共享基础设施大改、用户明确要求或 L3 无法覆盖的系统性风险。普通 issue、单个 CSV row、smoke 和 review 修复禁止默认运行全量测试；测试数量、`passed` 或 `skipped` 数量不能作为扩大范围的理由。
27. **Outcome Contract 是读者合同**：新任务存在 `outcome_contract:<path>` 时，review 必须逐条回答 reader questions，handoff 必须呈现判定、证据、边界和下一步；不得用 issue 完成数或实现状态代替能力结论。
28. **人类 handoff 必须经过 `humanizer-zh`**：未在最新 REVIEW notes 记录 `handoff_humanized:true` 时，只能保留 draft，不能通过 handoff contract，不能完成 REVIEW。结构化答案表和 blocked-claim 表不得润色，正文必须润色。
29. **先分类再处置发现**：当前 scope/acceptance gap 现在修或追加正式 follow-up issue；human-required blocker 记录后继续其他可推进项；只有不阻塞当前承诺的改进和未来决策才进入 Deferred Findings ledger。
30. **sidecar 不是第二状态源**：`<stem>.deferred.json` 和 events sidecar 只保存证据、事件和讨论问题，不控制 CSV 行状态，也不得成为关闭当前 issue 的理由。CSV 是唯一的逐行执行与验收状态源；`issues/.missions.json` 只维护当前任务身份及暂停、取消、替换等生命周期，不复制行状态。
31. **完成后停在讨论入口**：原 CSV 和 handoff 闭环后，向用户展示开放的待讨论项并停止。不得自动创建下一份 CSV，也不得把待讨论项追加到当前 CSV 后继续执行。
32. **每个 CSV 都必须有 closing review，但不默认重复独立审查**：加载合法 CSV 后若没有 `REVIEW-*` 行，先追加 `REVIEW-01`。若同一 scientific commit 已完成独立 PRERUN、此后 scientific contract/dataflow/sink 未改变且机械证据无冲突，closing 直接走 `evidence-close`；只有未经过等价独立审查的高风险交付、证据冲突或疑似 current-scope gap 才走独立 capability ladder。
33. **保护用户 index**：开始时记录 `git diff --cached` 的路径与 patch。提交只命名本任务路径；已暂存的无关改动保持原样且不得进入提交。同一路径存在用户已暂存 patch、或无法精确隔离 index delta 时，记录 human-required blocker。禁止用 `git stash`、reset、移动或隐藏用户工作来简化提交。
34. **最小工件直接落盘**：新 Mission 从一开始只写终态所需工件，不创建一次性 request/state/inspect/ready/launch/pull JSON，不在 closing 阶段运行压缩或生成 `artifact-index.json`。CSV + events 是状态记录；每个实际启动的 RunID 最多保留一个 canonical RunSpec；PRERUN 与 closing 各最多保留一个最终结构化结论。`compact_artifacts.py` 仅用于 legacy Mission 的人工归档/GC，不是 closing 步骤。

接收 CSV 后先运行 `python scripts/ensure_review_row.py <csv-path>`。提交边界复杂时使用 `scripts/git_isolation.py` 的 `commit_paths`；它会拒绝同路径 staged 冲突并核对提交前后的 index patch。

仓库内任务首次进入执行时，用 `.agents/harness/workflow/mission_state.py register --task-id <SpecID-or-task-id> --csv <project-relative-csv> --source-ref <user-or-approved-spec-ref>` 登记身份；已登记则沿用，不重复创建任务。暂停、取消、切换由当前用户指令驱动，分别记录 lifecycle transition；不要把旧快照或 pending 来源当作重新请求授权的理由。completed 生命周期只有 CSV 真正闭环后才可设置。

CSV 更新统一使用 `scripts/csv_state.py`，它锁住整段读改写和 events sidecar。返回的 `csv_sha256` 可作为下一次请求的 `expected_sha256`；版本冲突时重新读取并合并本次字段，不覆盖其他写者。追加 review 和 legacy 归档也使用同一把锁。

# 闭环完成判定

仅当该行同时满足以下四项，才视为「闭环完成」：

- `dev_state=已完成`
- `review_initial_state=已完成`
- `review_regression_state=已完成`
- `git_state=已提交`

若任意行 notes 包含 `claims:CLAIM-*`，必须先用 `mission-csv-execute/scripts/validate_claim_ledger.py` 或等价检查确认 ledger 可读、claim id 存在、状态与证据等级一致；否则该 CSV 不完整，不得把最终 `REVIEW-*` 标成通过。`not_run_by_preregistered_gate` 是有决策证据的合规终态，不等于 E2E 通过，也不阻止“按批准协议完成”的 Mission 闭环。

远程训练行额外遵循：

- `remote_state=running_remote`：合法可恢复暂停点，但不算闭环完成。
- `remote_state=artifacts_pulled`：已拉回原始产物，仍需 ingest 与 review handoff。
- `remote_state=ingested`：结果已进入实验记录，且 `issues/<stem>/<stem>.review.md` 已写入客观摘要后，才可继续按四状态判断闭环。
- 未实际完成 train/eval 或未拉回 artifacts 时，不得声称实验完成、指标有效或当前最优。

`REVIEW-*` 行还必须满足：

- 已根据 closing risk 选择 `evidence-close` 或独立 capability ladder；不得仅因存在 REVIEW 行就调用 reviewer
- review log 和 CSV `notes` 已记录 `review_agent_mode:<evidence-close|reviewer-subagent|codex-exec-independent|self-review>`、`review_independence:<true|false>`、适用的模型字段、实际存在的 ledger、coverage、`review_result` 与 `scientific_outcome`
- review 结论已经写入 review log
- 已产出 human handoff（`<csv-path-without-.csv>.handoff.md`），CSV `notes` 已记 `handoff:<path>`，并已记录 `handoff_contract:passed` 或 `handoff_contract:failed <reason>`；若 `review_result:vision_met`，必须是 `handoff_contract:passed`
- 若发现当前 scope/acceptance gap，已追加 follow-up issue 和下一轮 `REVIEW-(N+1)`
- 若存在开放 Deferred Findings，ledger 已通过 `validate_deferred_ledger.py`，handoff 已逐条覆盖，最新 REVIEW notes 已记录 `deferred_coverage:<covered>/<open>`
- evidence-close/self-review 已完整分类并处置所有发现时可以闭环；不得仅为等待独立能力而追加空转的 `REVIEW-*`

`PRERUN-REVIEW-*` 行还必须满足：

- 已读取并遵循 `pre-run-implementation-review` skill，或 CSV 明确指定了更专用的 pre-run review skill
- 已用该 skill 的 `scripts/prerun_ready.py` 验证结构化 packet，记录 `readiness_result:pass`；readiness failure 时该行不满足闭环条件
- 已执行唯一一次独立 scientific review，并记录 `review_agent_mode:<mode>`；该 scientific gate 与 closing review 的布尔 `review_independence` 字段分离
- review 结论已经写入 review log 的 `Pre-run Implementation Review` 段落
- notes 或 review log 已记录 reviewer id、`review_mode:scientific_review|targeted_review` 与 `pre_run_code_commit`
- 结论只能是 `scientifically_correct / scientifically_incorrect / not_evaluable`
- 若结论为通过，CSV notes 或 review log 已记录 `pre_run_result:pass` 与 `pre_run_code_commit:<hash>`
- 若发现 scientific blocker，已回到原 implementation row 修复并以 production/sink evidence 逐项关闭；没有创建第二次 formal review

# Issue 选择规则

## 优先收敛半成品

若存在 `git_state=未提交` 且 `dev_state=进行中` 或 `已完成` 的行，优先选这些。

若存在 `remote_state=artifacts_pulled` 但尚未 `ingested` 的行，优先完成 ingest 与 review handoff。

若存在 `remote_state=running_remote` 的行，先判断是否有可用 artifacts：
- artifacts 已可拉取：进入恢复流程。
- artifacts 仍未完成且当前会话可等待：读取远程运行 reference，启动一次前台低频 `rrctl wait`；不要反复调用 `inspect`。
- 只有当前会话无法继续等待时，才保留为可恢复暂停点；不得把该行视为完成。

## Pre-run Review 行顺序

`PRERUN-REVIEW-*` 行只在它 gate 的首次训练 / 运行 row 之前执行。

- 当前 `PRERUN-REVIEW-*` 之前的代码实施、本地轻量验证及 `full_review` 当前 commit 的 pre-review smoke 必须闭环完成
- 当前 `PRERUN-REVIEW-*` 之后的训练、评估、远程运行或实验结果生成 row，必须等待该 pre-run review 得到 `pre_run_result:pass`
- 若选中 official 训练 / 运行 row 但缺少已通过的 `PRERUN-REVIEW-*`，先切回或插入 review；只有合规 `pre_review_smoke` 可在 review 前运行
- 若已审查 commit 后出现新 diff，先运行 change route；`no_prerun/micro_validation/smoke_validation` 绑定 reviewed/candidate commit 与 probe evidence 后继续，只有 `targeted_review/full_review` 才要求新的 formal verdict
- 若 scientific review 发现可修复问题，回到原 implementation row 一次性修复全部 blocker；不得插入 Attempt 2、resolution review 或新 scientific PRERUN row
- readiness failure 回到原 implementation row 补齐证据；reviewer capability failure 只记录一次具体 gap，不机械追加或轮询 PRERUN row
- 已闭环或有真实 `running_remote` 证据的旧 CSV 只读兼容且不批量迁移；新代码快照使用 `prerun.scientific-review.v1` lean packet；同一已审查 commit 的 retry/stage 不重复 PRERUN

## Vision Review 行顺序

`REVIEW-*` 行只在它之前的所有非 review 行都闭环后执行。

如果 `REVIEW-N` 追加了 follow-up issue 和 `REVIEW-(N+1)`，则 `REVIEW-N` 自己正常闭环；执行器继续后续 follow-up issue，之后再执行新的 review 行。不要让旧 review 行保持挂起，也不要回头重开旧 review 行。

## 再选最高价值项

P0 → P1 → P2；优先能解阻塞/提供公共能力的任务；减少无意义上下文切换。

## 记录选择原因

选中后在该行 `notes` 追加 `picked_reason:<why>`。

# 执行闭环（每条 issue）

若当前行满足 `id=PRERUN-REVIEW-*` 或 `notes` 包含 `review_kind:pre_run_implementation`，跳到「Pre-run Implementation Review 闭环」。

若当前 `PRERUN-REVIEW-*` 行未声明可用 review skill，先将 `required_skills` 修正为 `pre-run-implementation-review` 并写回 CSV，再继续执行 gate。

若当前行满足 `id=REVIEW-*` 或 `area=review`，跳到「Vision Review 闭环」。

## Step 0：接收与现实检查

- 用 1-2 句话重述：当前 issue 的 `id/title`、验收口径、风险点。
- 识别潜在破坏性变更。

## Step 1：补齐执行信息（硬前置）

编码前必须检查以下字段；正常情况下它们应已在 CSV 生成阶段写好。若缺失或与实际任务边界不符，说明 CSV 不完整，必须**先写回 CSV 再写代码**：

- `acceptance_criteria`（必须可验证，最好有复现步骤/阈值）
- `required_skills`（本行确需的 skill；无则留空，不填占位值）
- `review_initial_requirements`（必须可执行）
- `review_regression_requirements`（必须可执行）
- `test_mcp`（主验证模式，取值限 `local_cli` / `remote_cli` / `manual` / `contract`）
- `refs`（至少 1 个 `path:line`）

## Step 2：启动状态

- `dev_state` → `进行中`
- `review_initial_state` → `进行中`
- 写回 CSV（保留原文件的 UTF-8/BOM 编码）

## Step 3：上下文收集（最小必要）

- 先读取 `required_skills` 中列出的 skill 文档，再开始编码
- 从 `refs` 指向文件开始读
- 代码架构搜索优先用 `fast-context MCP`；`rg` 只用于已知字符串精确定位
- 预算：首次 5-8 次工具调用
- 早停：能明确"要改哪些具体文件/函数"即可进入实现

## Step 4：实现（按验收口径驱动）

1. **实现前确认**：把 `acceptance_criteria` 拆成可验证的最小变更集合
2. **最小变更设计**：复用既有模式，KISS/YAGNI/兼容优先
3. **编码执行**：单一职责，嵌套 ≤ 3，错误处理到位
4. **实现内循环验证**：在实现过程中就运行最相关的测试，不等到最后
5. **文档/refs 同步**：更新相关文档/注释，新增 `path:line` 追加到 `refs`

## Step 5：Review（两段式）

此处是当前普通 issue 的本行自查与回归检查，不是训练 / 运行前的代码实施审查门禁，不能替代 `PRERUN-REVIEW-*`。

- **初次 Review**：对照 `review_initial_requirements` 自查 → `review_initial_state=已完成`
- **回归 Review**：对照 `review_regression_requirements` 执行回归检查 → `review_regression_state=已完成`
- 若回归不可执行：走受限验收（见下方），仍可标 `已完成`，但不得声称测试通过

## Step 6：自我验收

### 验证路径选择（计算入口 vs 模块）

当 `acceptance_criteria` 涉及**科学结果**（指标、loss、checkpoint 语义、数据或标签流）时，验证必须走真实计算入口（训练或评估脚本 → 模型前向 → forward/loss/attention/eval sink），不得只 import 模块函数验证返回值。模块单测是补充，不是替代。

判断标准：
- 涉及指标、数据流、computation sink → 走真实入口
- 只是函数返回值或数据结构校验 → 模块化 OK

反例：验收要求"新模块 step0 保留 baseline"，只断言残差张量为零——未经过 forward sink，不算验证通过（见 `pre-run-implementation-review` 的 Baseline-Equivalence Probe）。

### 验收执行

- 给出"通过/未通过"的证据
- 按 `test_mcp` 运行最相关的测试/检查（见 `test-mcp-mapping.md`）
- 若无法运行：走受限验收

## Step 6.5：声明-证据一致性检查

完成前检查当前 issue 的标题、验收条件、测试名、文件/函数名、metadata、报告、CSV notes 和状态更新是否高估了实际行为。

- 如果实际只是 mock / fixture / stub / dry-run / scaffold / 字符串检查 / 静态验证，必须在命名、报告或 notes 中如实限定，不得写成真实完成
- 低等级证据不能支撑高等级声明；例如 unit test 不能证明完整集成，dry-run 不能证明真实发送/迁移/删除，字符串检查不能证明智能评测或真实链路通过
- 测试或外部验证卡住时，走受限验收并继续可推进工作；不要造替代假路径来跑绿
- 若当前 issue 的交付声明已经被证据否定，先修正实现或修正声明，再标记完成

## Step 6.6：远程训练行处理

仅当选中远程 train/eval 行时，读取并严格执行 [references/remote-run.md](references/remote-run.md)。普通本地 issue 不加载该文件。

## Step 7：完成状态并写回 CSV

- `dev_state` → `已完成`
- `git_state` → `已提交`
- `notes` 追加 `done_at:<date>` + `skills_used:<...>` + 验收证据摘要
- 写回 CSV（保留原文件的 UTF-8/BOM 编码）

## Step 8：Git 提交

- `git status` / `git diff` 确认变更边界
- 目录化 Mission 使用 `scripts/git_isolation.py` 保存任务开始时的 index patch，拒绝同路径 staged 冲突，禁止 stash/reset，并验证无关 staged patch 在提交前后保持不变
- `git add`：
  - `issues/<stem>/<stem>.csv`：代码变更 + 当前 `issues/<stem>/` artifact root
  - legacy `issues/*.csv`：代码变更 + 当前 CSV
  - 显式外部 CSV：仅在已跟踪或明确属于 `issues/` 时 add
- 主仓库与 `research_workspace` 继续分别提交
- commit message 遵循项目提交约定（`<emoji> <type>(scope): summary` + Why/Why this works/Remaining 正文）
- 若 commit 失败：回滚 `git_state` 为 `未提交`，记录 `blocked:git commit failed <原因>`

## Step 9：立刻继续下一条（强制）

完成一条后：
- **立刻回到 Issue 选择规则，选下一条**
- **不问"要继续吗？"**
- **不做礼貌性停顿**
- **不输出非必要的全局状态更新**
- **禁止把"已完成 X/Y、结果已同步到 docs/review/csv"当作自然收口点**
- **状态更新末行必须写 `continue_now:<next id/title>`；只有全部完成或全部剩余项都需要人类参与时才允许写终态**
- **如果做阶段总结，也只能作为 commentary 的短播报，播报后必须继续工具调用**
- 只有以下情况才停止：
  - 所有 issues 闭环完成
  - 所有剩余 issues 都是 human-required blockers
  - 当前工具会话确实无法继续维持前台 `rrctl wait`，且已进入 `remote_state=running_remote` 可恢复暂停点
  - 已发现设计错误并写入 CSV notes 与 review.md

**干净边界谬误**：不要因为"前 N 条已闭环、第 N+1 条还在进行中"就觉得这是一个适合收口的边界。partial completion (3/9) 是最脏的状态——它强制上下文恢复、浪费用户时间、制造比继续执行更多的混乱。真正干净的状态只有 all-done。在同一个 turn 里混合"已完成"和"进行中"的汇报完全正常，这恰恰是你在正确地持续推进。

# Pre-run Scientific Implementation Review 闭环

`PRERUN-REVIEW-*` 只用于确认科学实施是否正确：模块是否按批准原理落在 canonical 路径，关键值是否到达真实 forward/loss/attention/eval sink，实验身份和结果归属是否正确。它不审 rrctl、tmux、health、scheduler、CSV bookkeeping 或最终效果预测。

## 前置条件

- 已读取 `pre-run-implementation-review` skill。
- 已完成代码实施和相关本地轻量验证。
- `full_review` 已完成绑定当前 candidate commit 的 1–100 step 隔离 GPU smoke；smoke 失败留在原 implementation row 修复，不创建 PRERUN row。
- 已确定唯一 gated run、`pre_run_code_commit`、diff base、批准依据和 exact official command。
- 一个 gated run 最多创建一个 scientific `PRERUN-REVIEW-*` row。

## Lean packet

按 `prerun.scientific-review.v1` 组装 packet，只包含：

- `review_mode:scientific_review|targeted_review`；
- `repo_root`、`pre_run_code_commit`、`review_diff_base_commit`；
- `approved_basis`、`implementation_intent`、`exact_command`；
- 通过的 `local_validation`；
- scientific review 当前 commit 的 `prerun.pre-review-smoke.v1`；
- `critical_values` 的 name/source/sink/evidence；
- experiment identity 与 `output_collision_policy`。

不要加入 Attempt、lineage、generation、review state、coverage manifest、ExecutionPlan、rrctl provenance、reviewer liveness、完整 CSV notes、重复日志或主代理结论。

调用 reviewer 前运行：

```bash
python <pre-run-skill-dir>/scripts/prerun_ready.py <packet.json>
```

- `ready:true`：记录 `readiness_result:pass`，创建并执行唯一 PRERUN row。
- `ready:false`：不要调用 reviewer；回到原 implementation row 补齐实现、source-to-sink evidence、command binding 或 smoke 证据后重跑 readiness。
- readiness failure 不写 formal blocker，不新增 PRERUN 编号。

## Reviewer 执行

1. 只调用一次独立 reviewer：优先 `fork_turns=none` 的 direct reviewer，其次独立只读 `codex exec`。prompt 只携带 lean packet、批准源、committed diff 和 packet 引用的原始证据。
2. reviewer 必须继续检查所有当前可判定的科学维度，一次性返回全部 findings，不得在发现首个 blocker 后停止。
3. reviewer 只检查批准意图/原理、canonical 实现位置、模块实例化、参数/数据流、optimizer/loss 连接、computation sink、baseline/disabled path、dataset/checkpoint/metric identity、command 和结果归属。
4. rrctl、tmux、PID、cleanup、health、scheduler、artifact transport、RunID/path/profile、bookkeeping/coverage 和效果预测不属于 scientific review。
5. 输出只有：
   - `scientifically_correct / allow_run`；
   - `scientifically_incorrect / do_not_run`；
   - `not_evaluable / do_not_run`，并列出缺失的具体科学证据。
6. quota、启动、格式或 reviewer 可用性失败只记录一次 `validation_gap:independent scientific reviewer unavailable <evidence>`；不得自动重试、轮询等待、切换 lineage 或创建新 PRERUN row。
7. 将结果写入 review.md 的 `Pre-run Implementation Review` 段落。通过时记录 `pre_run_result:pass`、reviewer、mode、gated run 和 `pre_run_code_commit`。

## Scientific blocker 修复

若结果为 `scientifically_incorrect`：

1. official run 保持 blocked；
2. 回到原 implementation row，一次性修复全部 blocker；
3. 为每个 blocker 记录 `source -> sink` 的 production-reaching probe 和 literal result；
4. scientific code、数据流或 sink 改变时，以最终 commit 重跑 GPU smoke；
5. 主代理逐项写入 blocker→fix→evidence closure；全部可复现关闭后写 `pre_run_result:pass` 并继续 official run；
6. 不创建 Attempt 2、resolution review、新 lineage/generation、closing scientific review 或等待 reviewer 的任务。

若主代理无法确认 closure，记录真实 `validation_gap` 并保持 official run blocked。smoke、rrctl health 或进程存活不能替代 scientific dataflow/sink evidence。

# Vision Review 闭环

仅当选中 `REVIEW-*` 行时，读取并严格执行 [references/closing-review.md](references/closing-review.md)。普通实现、PRERUN 和远程等待阶段不加载该文件。

# 反暂停护栏

以下情况一律不构成停止条件：

- 完成某个里程碑或阶段切换
- 刚写完 CSV / log / checkpoint
- 想先同步阶段进展、风险或现状
- 刚完成 `X/Y` 条，结果和证据看起来已经足够形成阶段汇报
- 下一条 issue 更复杂、更脏、更难测
- 当前 issue 部分阻塞，但还有其他 issue 可推进
- 测试存在既有失败，但当前 issue 仍可受限验收或切下一条
- **修复了 issue 内的子问题（blocker / 中间 bug）后拿到了阶段性证据**——这不是汇报点，直接继续该 issue 的下一步验证
- `REVIEW-*` 发现 current-scope gap 且可以追加 follow-up issue
- review 发现架构、范围、产品或实现路线存在普通歧义

若你准备输出阶段总结，先做以下停止断言：

1. 剩余 issues 是否全部闭环完成？
2. 是否所有剩余项都属于 human-required blockers？
3. 是否继续执行会要求伪造证据、凭证、数据或用户意图？

只要以上三问都不是“是”，就不得停止，必须继续 issue loop。

# 受限验收

只有目标验证实际不可执行时才读取 [references/limited-validation.md](references/limited-validation.md)，不要在正常通过路径加载。

# 阻塞策略

允许跳过，但必须回收：

1. 在该行 `notes` 记录 `blocked:<原因>` + 已排查内容 + 下一步建议
2. 状态保持真实进度，`git_state` 必须保持 `未提交`
3. **允许切到下一条继续推进**
4. 只有当所有剩余 issues 都是 human-required blockers 时，才停止并汇总阻塞清单，向用户请求最小必要信息
5. 普通架构、范围、产品、实现歧义不算 blocker；记录 assumption / decision_debt / risk 后继续

# 停止条件（严格）

| 条件 | 动作 |
|------|------|
| 全部闭环完成 | 输出最终汇总，停止 |
| 全部剩余项都是 human-required blockers | 输出阻塞清单 + 需要的最小决策信息，停止 |
| `remote_state=running_remote`，当前工具会话仍能维持前台 `rrctl wait` | 等待同一 session 到终态并继续 pull/ingest/后续 CSV |
| `remote_state=running_remote`，当前工具会话确实不能继续维持，且已写清恢复方式 | 输出远程运行/恢复摘要，停止 |
| 发现会污染实验结论的设计错误 | 写 CSV notes + review.md，输出最小修正建议，停止 |
| 部分完成 / `X/Y` 阶段成果 / 阶段切换 / checkpoint 完整 / 想先做阶段汇报 | **继续** |
| 单条阻塞 | 跳到下一条，继续 |
| Review 发现可执行缺口 | 追加 follow-up issue 和下一轮 review 后继续 |
| 测试环境慢 | 等，不跳过 |
| 小歧义 | 合理假设，记 notes，继续 |
| 人类不可替代阻塞 | 问用户确认后继续 |

其他情况一律继续。不要把控制权还给用户。

# 反模式清单（Red Flags）

以下想法出现时，说明你正在合理化一个不该发生的停顿：

| 你的想法 | 现实 |
|----------|------|
| "blocker 修好了/拿到阶段性证据/做到 6/14 了，先汇报或 checkpoint 再继续" | 阶段性成果不是停止点。写进 notes 即可，不交还控制权，立刻继续。 |
| "这行 issue 还没闭环，所以不能提交" | 如果 blocker 已修且验证通过，继续推进到闭环再提交。不要因为"还没全做完"而卡住。 |
| "CSV 状态和 commit 边界不一致" | 这是你继续推进到闭环的理由，不是停下来的理由。 |
| "用户问进度/输出完状态更新模板后，这轮可以结束了" | 回答与状态更新都是内联 commentary，不是 turn 终点。答完后必须在同一 turn 继续工具调用。 |
| "前 N 条已闭环，先整齐收口再继续" | 干净边界谬误。partial completion 才是最脏的状态；混合汇报"已完成"和"进行中"完全正常。 |
| "工作区有用户的未提交改动和我的改动混在一起" | 用显式任务路径和 index 快照隔离；禁止 stash/reset。若同路径无法隔离，记录 human-required blocker。 |
| "下一步更复杂/review 发现架构问题，先问用户确认方向" | 只有人类不可替代或真正破坏性操作才停。写 assumption/risk 与 follow-up，继续。 |
| "当前会话没有 sub-agent，所以只能主会话自审" | 不对。先尝试 `codex exec --ephemeral --json --skip-git-repo-check --sandbox read-only` 独立 reviewer；失败后再按同一范围完成并明确记录 self-review。 |
| "`codex review` 已经跑过，所以愿景 review 完成" | 不够。`codex review` 只审 diff，不能替代 spec/CSV/claim ledger 对账，也不能替代 pre-run packet 审查。 |
| "scientific reviewer 不可用，可以靠主会话或 smoke 直接写正确" | 不可以。记录一次具体 `validation_gap`；smoke 和主会话信心都不能替代独立 scientific correctness 结论。 |
| "名字/报告写得强一点没关系" | 不可以。文件名、测试名、metadata、报告和状态更新都是声明，必须和实际行为一致。 |
| "review 行没写 review 模式" / "REVIEW 行是通用模板，也能审" | 都不够。先补齐 `review_agent_mode:pending`、`review_independence:pending`，并从源文档补任务专属 claim/evidence 检查项。 |
| "发现前置 issue 有错，需要先解释根因再继续" | 修掉错误、记 notes、继续——全在同一 turn。"解释根因"是内联 commentary，不是交出控制权的理由。 |
| "替代测试跑绿了可以说原目标通过" / "说不了'通过'所以先停下来" | 都不对。替代测试只证明替代范围；写 `validation_limited` + 三元组然后继续。诚实标注和停止是两件事。 |
| "发现既有债务混在我的改动里，不确定怎么归类，先停下来说清楚" | 用 `git diff` 区分引入 vs 既有；既有债务记 `decision_debt:<pre-existing, not introduced by this change>`；自己引入的修掉；然后继续。 |
| "远程训练已经启动，可以把 issue 标完成" | 不可以。只能标 `remote_state=running_remote` 并记录恢复方式；完成要等 artifacts 拉回、ingest、review.md 更新。 |
| "intent 大概能对应某个脚本，先跑了再说" | 不可以。脚本映射不唯一或 required args 冲突属于设计错误，必须停止报告。 |

# 状态更新格式

> **关键语义**：状态更新是内联 commentary，不是 conversation turn 的终点。
> 输出完这段文本后，你必须**在同一个 turn 内立刻继续工具调用**，不得等待用户回复。
> `continue_now` 行不是"告诉用户下一步"，而是**你自己的执行指令**——写完它就去做。

每完成一条：

```
[<id>] <title> — done
- 变更: <关键文件 path:line>
- Skills: <required_skills / 实际使用>
- 测试: <跑了什么，结果>

- Commit: <hash>
- 进度: X/Y 已完成
- continue_now: <next id/title> 或 "全部完成"
→ 立刻执行 continue_now 指向的下一条，不等待用户
```

阻塞时：

```
[<id>] <title> — blocked
- 原因: <why>
- 已尝试: <what>
- 需要: <最小信息>

- continue_now: <下一条 unblocked issue> 或 "全部剩余项都需要人类参与"
→ 若非"全部剩余项都需要人类参与"，立刻执行 continue_now 指向的下一条
```

远程训练暂停时：

```
[<id>] <title> — running_remote
- Command owner: user paste | codex ssh
- Branch/commit: <branch>/<hash>
- Pre-run code commit: <hash from PRERUN-REVIEW-N>
- Remote session: <tmux/session/ssh target>
- Expected artifact path: <path>
- Review handoff: issues/<stem>/<stem>.review.md
- Resume: 训练完成后使用 `mission <同一 CSV 或目录>` 恢复，先 pull artifacts → ingest → 更新 review.md
```

设计错误停止时：

```
[<id>] <title> — design_invalid
- 原因: <会污染实验结论的具体错误>
- 已写入: CSV notes + issues/<stem>/<stem>.review.md
- 最小修正: <需要 Claude/用户改 intent/spec/csv 的最小项>
```

# 提交前自检清单

- 验收口径有可复现证据
- `required_skills` 已读取并遵循
- 若后续启动训练 / 评估 / 远程运行：`PRERUN-REVIEW-*` 已在运行前通过，`pre_run_code_commit` 与运行 row 的 branch/commit 一致
- 若 CSV notes 引用 `claims:*`：claim ledger 已校验通过，claim/evidence/production_path 没有未解释 gap
- 若执行 `REVIEW-*`：已记录 `review_agent_mode` / `review_independence` / `review_result`，并生成或兜底生成 `handoff.md`
- 若受限验收：notes 已写 `validation_limited/manual_test/mcp_evidence/evidence/risk`
- 声明-证据一致性已检查：没有把 mock、fixture、dry-run、字符串检查或静态验证包装成原目标通过
- `review_initial_state` 与 `review_regression_state` 均已推进
- 目录化 `issues/<stem>/` artifact root 或 legacy flat CSV 与代码按 git isolation 规则提交，状态枚举值合法
- 文档/注释/refs 已同步
- commit message 遵循项目提交约定
- 无无关改动混入
