# Closing Review 与收尾编排规范

`REVIEW-*` 行用于判断整批工作是否按批准文档完成，并把实施结果与科学结果分开记录。同一规则适用于目录化 CSV 与 legacy 平铺 CSV。

## 1. 核心定位与职责分工

- **Closing Review 的核心职责**：回答「当前 Mission 是否已经满足证据、执行状态与交付要求，可以合法结束？」。
- **职责隔离**：
  - `RESULT-ANALYSIS-01` 回答「实验结果在科学机理上说明了什么」（由独立强模型在长训练入账后独占完成）；
  - `Closing Review` 回答「工程实施与台账是否可以合法收工交付」；
  - 收尾阶段不实现新功能、不重新设计方案、不开启第三轮无界实验优化。

## 2. Review 前置条件与机械门禁

在执行 REVIEW 行之前，必须通过以下前置检查：

1. **机械就绪核验（HARD GATE）**：
   运行 `final_ready.py` v2 对 28 列状态、remote terminal state、实验身份、metrics token、provenance、claim 终态、post-run result-analysis 和 scoped git status 做机械检查：
   ```bash
   python .codex/skills/mission-csv-execute/scripts/final_ready.py issues/<stem>/<stem>.csv
   ```
   失败时修当前 REVIEW 行，不创建等待型 REVIEW。
2. **所有前置非 review 行必须闭环**：
   - 对 canonical CSV，所有正式实验行闭环后，优先通过 `evidence-close` 结构化直接入账结项；涉及跨域重要结论争议时，方需运行 `RESULT-ANALYSIS-01`。
3. **前置未完成行与遗留台账清算**：
   - 若前序存在卡住行、更高轮次取代的重试行或预注册门控跳过行阻碍收工，**必须读取并执行 [references/closing-legacy-cleanup.md](closing-legacy-cleanup.md)** 进行清算，严禁死锁。
4. **台账完备性**：
   - 若输入 CSV 缺少 review 行，运行 `ensure_review_row.py` 合成 `REVIEW-01`；
   - 若 notes 包含 `claims:CLAIM-*` 但缺少可读 `claim_ledger:<path>`，先补齐账本，不得裸跑。

## 3. Closing 路由决策 (Closing Route Decision)

`final_ready.py` 是路由选择的机器权威，按其返回的 `decision` 分流：

1. **`decision:evidence_close`（直接结构化关闭）**：
   - 适用场景：L0–L2 普通任务已完成风险匹配测试；或科研任务的同一 `pre_run_code_commit` 已完成独立 scientific PRERUN 且之后未改变数据流、算子、指标或结果归属。
   - 执行方式：由主会话基于结构化证据直接完成，**不再次调用模型**。
   - 记录值：`review_agent_mode:evidence-close`、`review_independence:false`、三个模型字段记为 `not_applicable`。
2. **`decision:independent_review_required`（独立审查阶梯）**：
   - 适用场景：L3/L4 交付无等价独立审查、机械证据冲突、claim/evidence 疑似高估或发现 current-scope gap。
   - 执行方式：**读取并严格遵循 [references/closing-independent-review.md](closing-independent-review.md)**（执行审查模型铁律：必须使用当前会话执行模型，严禁私自越级借用高级审查模型；优先 `closing-reviewer-job`，失败降级 `self-review`）。

## 4. Human Handoff 施工交工单与机械验收

REVIEW 行执行后，必须产出面向人类决策者的施工交工单 `<csv-path-without-.csv>.handoff.md`：
1. **结构与规范**：**读取并遵循 [references/closing-handoff.md](closing-handoff.md)**（严格遵循大白话叙事九层结构、Outcome 表格逐字段对齐、架构变更 Mermaid 配图标准）；
2. **机械合同验收门禁（HARD GATE）**：
   落盘后必须运行机器验收检查：
   ```bash
   python .codex/skills/mission-csv-execute/scripts/check_handoff_contract.py <handoff-path> --csv <csv-path>
   ```
   退出码 0 时方可在 REVIEW 行 notes 记录 `handoff_contract:passed`。未通过严禁标记 `vision_met`。

## 5. Review 行闭环与最终交付

`REVIEW-N` 只有在 review log 已写入、handoff.md 已生成且 handoff contract 检查通过后方可完成：

- **若 `vision_met`**：
  - 必须同时满足 `handoff_contract:passed`；
  - 标记 `REVIEW-N` 完成；若无其他未完成行，运行 `csv_completion_errors()` 核验后结束 CSV；
  - **终端对话层最终汇报（严格禁问令）**：
    终端对话层汇报只包含客观三要素（完成状态、关键科学指标对比、交接文件路径）。**严禁在终端对话层输出任何疑问句、决策请示或向用户发问**；所有实现疑虑、架构讨论、后续实验建议一律沉淀至 `handoff.md` 与 `findings.jsonl`，交由后续方案讨论会话读取：
    ```text
    交接文件路径：
    - 方案讨论交接层：issues/<stem>/<stem>.review.md
    - 人类交付交工单：issues/<stem>/<stem>.handoff.md
    ```
- **若 `gaps_found`**：
  - 将 current-scope gaps 追加为 follow-up issues 并追加 `REVIEW-(N+1)`；标记当前 REVIEW 完成并继续执行。
- **若科研结果为负或无信号**：
  - `gate_failed`（预注册门控跳过）或 `inconclusive`（探索期两轮止损无信号）属于**合规正常科研终态**，直接在 `scientific_outcome` 记录并正常闭环收工，严禁陷入无界优化循环。
- **若存在 Human-required blocker**：
  - 参照 [references/closing-legacy-cleanup.md](closing-legacy-cleanup.md) 记录最小诉求，保持 `git_state=未提交`，停止并请求用户输入。
