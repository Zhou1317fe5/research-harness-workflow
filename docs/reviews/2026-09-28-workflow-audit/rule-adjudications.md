# 规则 canonical 裁定书（批 4 L4）

日期：2026-09-29
作者：主 Executor（裁定依据：现有代码行为 + 批 1–3 实际证据）
状态：**每条规则已指定 canonical 表述与 canonical 出处文档；其他文件保留现状引用，后续文档编辑时改为指向 canonical 描述而非复述。**

> 本裁定只定表述，不改代码。代码行为与本文冲突时以代码为准、文档需改。

## 规则 1：`fallback_allowed=false`

**canonical 出处**：`.codex/skills/mission-csv-execute/references/remote-run.md:16`（旧表述：`fallback_allowed 必须为 false；rrctl 不可用、readiness 失败或 launch 失败时停在当前 row，不得静默回退`）。

**canonical 表述**：

> 远程执行的唯一后端是 `rrctl process`。`remote_route.py` 的输出中 `fallback_allowed` 字段**固定为 `false`**（这是单一 fact）。这意味着：
> - rrctl 不可用、readiness 失败、launch 失败、wait 失败、pull 失败或 observation 失败时——**全部停在当前 CSV 行**，不得静默回退 legacy/手动 ssh/重复 launch；
> - 例外：仅有用户显式授权 `pre_run_exception:user_authorized_pre_run_exception=true` 时可解锁启动（仍不解锁结论），且路线**仍然**是 rrctl process。

**非 canonical 文档行为**：AGENTS.md / CLAUDE.md / remote-run-snippet/SKILL.md / mission-csv-execute/SKILL.md 中的相同表述保留指向，不再展开新断言。

**证据**：`remote_route.py:772` 与 `:1118` 都硬编码 `"fallback_allowed": False`。

## 规则 2：`preregistered-gate`

**canonical 出处**：`.codex/skills/mission-csv-execute/references/remote-run.md:90-95`（pilot / preregistered_read_only_probe 定义）+ `SKILL.md:63`（status 枚举）。

**canonical 表述**：

> Mission 不隐式生成每个 Mission 都执行的 pilot。**只有当批准文档（spec）显式要求的预注册门限存在时**，才创建 `pilot` 行；预注册只读探针 (`preregistered_read_only_probe`) 只用于验证已批准契约，不得改模型状态。
>
> `claim` 的 `status` 终态仅允许 `verified` / `not_run_by_preregistered_gate` / `out_of_scope`：
> - `verified` 必须附 `evidence_refs`，且 evidence_required=real_e2e 的只能真实端到端运行后才写；
> - 预注册门限决定后续不运行时，写 `not_run_by_preregistered_gate` 并附 `gate_evidence`；
> - `pending`/`failed`/`validation_gap` 永远不能通过 closing-ready。

**evidence_close 合法性**专门一条：当同一 `pre_run_code_commit` 已完成独立科学 PRERUN 且之后未改 scientific contract 时，closing review 允许走 `final_ready.py:decision=evidence_close`，**此时 claim `vision_met` 可为 true 并附预注册门限的负结果或合法跳过证据**。

## 规则 3：`independent-review`

**canonical 出处**：`.codex/skills/mission-csv-execute/references/closing-review.md:42-56`（capability ladder）。

**canonical 表述**：

> Closing 时不默认调用独立 reviewer。选择顺序：
>
> 1. `final_ready.py` 返回 `decision:evidence_close` → 使用 `review_agent_mode:evidence-close`、`review_independence:false`，机械保证等价 PRERUN 已覆盖且 contract 未变；
> 2. `final_ready.py` 返回 `decision:independent_review_required` → 进入独立 capability ladder（下表），此时 `review_independence:true`；
> 3. ladder 顺序：`reviewer-subagent` → `codex-exec-independent` → `self-review`。前两项要求 fresh 只读会话、prompt 不含主代理结论、禁止再委派；`self-review` 只在前两项失败时使用，且 handoff 顶部必须标 `WARNING: self-review only, NOT independently verified`。
>
> 独立审查的 **模型 = 当前会话执行模型**，"独立" 指**结构独立**（fresh 只读 + prompt 不含结论），不是模型级别独立。`requested_model` 与 `observed_model`需如实记录。

**IMPORTANT**：`RESULT-ANALYSIS-01` 行（post-run 结果分析）**永远要求独立路径**（见 post-run-result-analysis/SKILL.md），**不允许走 `evidence-close`**。这行与 closing 的 `evidence-close` 路径**语义不同**：RESULT-ANALYSIS 是 mission 的科学部分，closing 是流程收尾巴。

## 规则 4：`no-mock-as-real`

**canonical 出处**：`.codex/skills/mission-csv-execute/SKILL.md:40`（声明-证据必须一致）。

**canonical 表述**：

> 测试可以跑不起来，也可以记录受限验收；但不得用 mock、fixture、stub、dry-run、字符串检查、静态验证或脚手架证据，**包装成**真实集成、真实副作用、E2E、生产可用或原目标已通过。
>
> 允许的情形：明确标注为受限降维验收（如 `real_e2e` → `validation_limited` 并附 `validation_gap` 原因）；**包装**指在不告知的前提下把受限证据误称为完整结论。

## 规则 5：`stop-at-blocker`

**canonical 出处**：`AGENTS.md:39` 与 `CLAUDE.md:39`（同句），加上 `remote-run.md:16`。

**canonical 表述**：

> rrctl 控制面（process backend）的 readiness、launch、wait、pull、abort、cleanup 任一阶段返回非零、timeout、attention 或 schema-invalid 时，执行**停在当前 CSV 行**，不得：
> - 静默重试同一阶段（同一 RunID 的 resume 除外）；
> - 切换到 legacy 手动 ssh/nohup（`fallback_allowed=false`）；
> - 静默改写 CSV 状态字段"假装完成"。
>
> 例外：仅有用户显式 `pre_run_exception:user_authorized_pre_run_exception=true` 时可解锁启动单条路径；任何例外的恢复**必须**走 `mission_state.transition` 显式记录。

## 规则 6：`single-prerun-row`

**canonical 出处**：`.codex/skills/mission-csv-execute/SKILL.md:44`（运行前风险分流）+ `:200`（唯一 PRERUN 行）。

**canonical 表述**：

> 对**同一个 gated run**：
>
> - 仅创建**一行** `PRERUN-REVIEW-*`，记录该 run 的 `pre_run_code_commit`；
> - 同一 packet 的审查服务失败（quota/transport/timeout）**不新建行**，走 `pre-run-implementation-review/agents/openai.yaml` 的有界恢复路径；
> - blocker 修复**在原 IMPL 行 in-row 完成**、不创建 `FIX-*` 或 `Attempt N` 行；
> - 同一 gated run 的 verdict 需新跑 smoke 时也复用同一 packet。

**区分**：不同 gated run（即不同 `pre_run_code_commit`）可以有各自的 `PRERUN-REVIEW-*`——本条约束的是**同一 gated run 的唯一性**。

## 规则 7：`observed-model-gate`

**canonical 出处**：`.codex/skills/mission-csv-execute/pre-run-implementation-review/SKILL.md:229, 231`（F-013 修复过的 SKILl；其中 :231 描述 codex rollout 回退）。

**canonical 表述**：

> RunSpec 构建时通过 `review_model.clone()` 的共享 validator 校验 verdict 的模型身份：
>
> - `observed_model: unknown` → **拒绝**（unverifiable）；
> - 任何其它记录的 identity 必须匹配 `review_model.py` 中经批准的 host 模型集；不一致 → `verdict_artifact_model_mismatch`；
> - 历史 verdict **未记录任何模型字段**时向下兼容接受（grandfathered）。
>
> 取证通道：
> - **codex backend**：先读 stdout 事件流中的 `thread.started`；若无模型，回退从 `$CODEX_HOME/sessions/rollout-*-<thread>.jsonl` 的 `turn_context.payload.model`（`session-metadata`）；
> - **pi backend**：读 session jsonl 的 `model_change`（`session-metadata`）。

## 规则 8：`no-relaunch`

**canonical 出处**：`.codex/skills/mission-csv-execute/references/remote-run.md:86`（wait 退出码语义）。

**canonical 表述**：

> 任何已经启动到 `runner`/`RunID` 的执行，**禁止再次 launch 同一任务**；观察层故障统一走 `resume` 路径。
>
> rrctl wait 的语义：
> - `exit 0` → completed 终态；
> - `exit 1` → failed/aborted 终态（follow evidence 拉取）；
> - `exit 2` → attention：进程仍持有，先走 `resume`/`inspect`，由值守流程决定处理；
> - 只有 `observe_seconds > 0` 时出现周期性 timeout；默认 `--max-wait-seconds 0` 不产生周期性回复；
> - `observation_unavailable: bounded observation retries exhausted` **不**等于 workload failed；workload 保留，继续同 RunID 观察。
>
> 若需真正重新运行（例如修改 commit），**必须**走 in-row 修复后按新 `pre_run_code_commit` **生成新 RunID**（`-rN` 命名后缀），不得 relaunch 旧 RunID。

## 代码行为对照（防止纸面裁定飘离代码）

| 规则 | canonical 表述或对应代码行为 | 代码证据 |
|---|---|---|
| 1 | `fallback_allowed: False` 硬编码 | `remote_route.py:772, 1118` |
| 2 | claim verified 必须 `evidence_refs`；not_run_by_prereg 必须 `gate_evidence` | `validate_claim_ledger.py` |
| 3 | evidence_close 必须 `pre_run_code_commit` 与 PRERUN commit 一致 | `final_ready.py` |
| 4 | （本条是文字约束，代码层例以 `validation_limited` 替代） | — |
| 5 | rrctl 各阶段退出码语义 | `remote-run.md:86` |
| 6 | `_validate_single_prerun` 在 csv_state 中限制唯一 | `csv_state.py:_validate_single_prerun` |
| 7 | `verdict_artifact_model_unverifiable / mismatch` 已 reject 未知身份 | `build_rrctl_runspec.py:317-323` |
| 8 | `agent_event_wait` `--resume` 保持同一 RunID | `agent_event_wait.py` |
