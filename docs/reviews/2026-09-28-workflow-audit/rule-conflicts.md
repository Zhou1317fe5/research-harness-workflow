# 规则冲突 / 人机协议裁定登记（批 4 L4）

日期：2026-09-29
作者：主 Executor
仲裁依据：批 2 会话回放中的真实用户行为 + 文档语义

## F-010：独立 reviewer 触发时机与用户介入的冲突

**场景**：09-22 hera-cgm 用户在独立 reviewer 触发前要求"先用 post-run-result-analysis 总结已有实验，写出 review.md，我先让外部模型分析一下"。该指令让 workflow 的"独立科学分析门禁"顺序被外部预分析打断。

**裁定**：

> 用户介入**优先于流水线上的自动触发**。`post-run-result-analysis` 的独立 reviewer **可以**被用户的"先产出人可读 review 摘要"延迟，但需满足：
>
> 1. **延迟期间** CSV `dev_state` 仍为"进行中"，`review_initial_state` 仍为"未开始"（mission 未完成闭环）；
> 2. 用户给出的外部 model/judgement 不能替代 `scientific-reviewer/subagent` 的产出，**只能作为补充外部意见**；
> 3. **用户明确接受外部评估结论后**，执行者仍需按规定流程补 `scientific-reviewer` 的独立产出，且**不允许**引用用户 copies of external model 的 `_text` 字段作为结论输入（以免污染独立性）；
> 4. 所有人工插入的暂停必须记录在 `mission_state.transition`（`paused` 状态），不是简单继续 `mission-state active`。

**canonical 出处**：`.codex/skills/mission-csv-execute/SKILL.md` 「自行推进授权内的工作」+「运行前风险分流与审查门禁」+ `rule-adjudications.md` 规则 3。

## F-011：独立分析时序 vs closing 冻结点

**场景**：09-29 hera-gsr-scnp-region-full 中，`RESULT-ANALYSIS-01` 独立分析在 CSV closing 行尚未完成时就产出，其 validation_gaps 记录了 closing 当时的中间态；closing 随后又根据这份独立 analysis 里的 gaps 更新后续行。

**裁 定**：

> 独立分析（`RESULT-ANALYSIS-01`）与 closing 行**允许并行**，不需要"closing 冻结后才开始独立分析"，但以下内容要求：
>
> 1. 独立分析**不依赖** closing 中的任何结论字段（如 `scientific_outcome`、`claim_coverage_status`），它的产出仅限原始证据 + approved spec + committed diff；
> 2. 独立分析完成后，**应明确冻结为 REVIEW-* 上界 artifacts**——后续 closing 行的修正（例如根据 gaps 补充 wiring 报告）**不得让独立分析重新生成**；
> 3. 如果**独立分析**把 "closing 时尚未完成" 标为 `validation_gap`，意味着 analysis 的产出是在 closing 之前完成的；closing 行随后补全证据链， **不允许**回头修改独立分析的 `validation_gaps` 字段——这些 gap 是分析时的事实记录。

**canonical 出处**：`.codex/skills/post-run-result-analysis/SKILL.md` 与 `rule-adjudications.md` 规则 3（RESULT-ANALYSIS-01 行与 closing 的 evidence-close 分离）。

## F-015：mission_state paused/current_task 与 cancelled CSV 唯一绑定

**场景**：用户提问 `mission_state.transition("paused")` 不清空 `current_task`；恢复工具应将「current+paused」视为可启动；以及取消/终态任务的 CSV 唯一绑定使"替换重跑"场景被闭塞。

**裁定**：

1. **`transition("paused")`**：**保留**当前 `current_task` 指针。语义上当任务进入 `paused` 后，当前任务仍是"待恢复任务"；恢复工具检查 `current_task` 并且 `status` 处于 `preparing/active` 才视为可启动。**不调整 current_task**。
2. **cancelled CSV 唯一绑定**：cancelled/superseded/completed 任务的 CSV 名字**不能**被新 task 抢占。若要"替换重跑"，应：
   - 通过 `mission_state.transition("superseded")` 主动归档原任务（带 replacement_reason）；
   - 或用 `-rN` 后缀命名新的 Mission CSV。
   **不允许**"悄悄重用旧 CSV"来期待 resume 语义。

**canonical 出处**：`.agents/harness/workflow/mission_state.py:transition` 的行为描述文本。

## F-017：hindsight verify_source dirty→unverified 而非 changed

**场景**：`hindsight_memory.verify_source` 在工作树 dirty 时因 `commit_bytes` 抛 `MemorySyncError`，被 except 捕获返回 `"unverified"`。语义上 dirty ≠ changed，但用户难以区分"根本没核过"和"被改过"。

**裁定**：

> **保持当前实现不变**，但文档上明确：
>
> - `unverified` = **无法给出历史 commit 与工作树一致的证据**（包括 file missing / dirty / non-git / read fail 等任何无法证据情况）；
> - `changed` = **已确认**当前工作树与历史 commit 不一致（工作树干净但 sha 不匹配）;
> - 用户接收到 `unverified` 时应该：先清理工作树或 stash 再来源核对， 不要试图通过 `verify_source` 来获得"被改过"的确认。
>
> **不修代码**，只更新 `.agents/harness/memory/hindsight_memory.py` 的 docstring 与本文件的分类说明。

**canonical 出处**：本条裁定本身（后续 `hindsight_memory.py` 的 docstring 应明确指出 unverified 的语义）。

## R6：文字防御 canonical（F-008 扩展）

已生成 `rule-adjudications.md` 作为全部 8 条规则的 canonical 表述集中出处。其他文档保留现状引用，不再展开新断言；以后所有文档编辑**只能引用 `rule-adjudications.md`**，不得在其外复述不同表述。

**证据**：
- `rule-adjudications.md` 是本表中所有 8 条规则的**唯一 canonical 出处**。
- `fallback_allowed=False` 的代码硬编码事实在 `remote_route.py:772,1118`——文档代码一致。

## Goal 协议 vs skill 停止条件 核对

- `goal_blocked` / `goal_wait` 工具语义与 mission-csv-execute 的「停在当前 row」表述**一致**：`goal_blocked` 是执行者上报用户外部 blocker 的断言；skill 的「停在当前」是执行者在该 row 上的内部停止立场。两者 **不冲突**，且建议 skill 文档补一条说明："执行者遇到远程 blocker 时应停在当前 row，并通过 `goal_blocked` 上报用户（保持 `fallback_allowed=false`）"。
- `agent_event_wait` 与 `rrctl_event_wait` 的 resume 语义与 rule-adjudications.md 规则 8 no-relaunch 一致。
