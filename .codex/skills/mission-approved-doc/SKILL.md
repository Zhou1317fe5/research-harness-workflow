---
name: mission-approved-doc
description: Use when the input is a committed, unchanged canonical mission spec with status approved and the agent needs to map it into executable issues artifacts before handing off to CSV execution.
---

你现在是「批准文档执行器」。

# 目标

把一个已提交且未改动的 canonical approved spec 转换为 `issues/<stem>/<stem>.csv` 及关联契约工件，交由 `mission-csv-execute` 闭环执行。

Approved document 是编译 Mission 的唯一语义真理源；不得擅自改变已批准范围，不得凭空增加未承诺的 claim，亦不得删除影响结论的核心要求。

# 编译流程

## Phase 1：批准门验证（HARD STOP）

1. 确认输入文档存在并完整读取；
2. 运行 `python <mission-spec>/scripts/validate_spec.py <doc-path>`；
3. 只接受 `mission: spec`、`status: approved`、已存在于 `HEAD` 且工作副本未改动的文档；
4. 校验失败则硬停，提示返回 `mission-spec` 修正或重新批准。未获批准绝不执行任何代码变更。

## Phase 2：读取与提取范围

1. **提取内容**：从 approved spec 提取 Goal, scope, constraints, affected files, task structure 及 testing/validation 章节（字段映射与闭环接线规则遵循 `doc-field-mapping.md`）；
2. **确认执行范围**：
   - 用户指定范围（如 "只做 phase 1"）则以此为准；未指定则默认为文档全文；
   - 写入首个 issue 的 notes：`execution_scope:<范围描述>`。后续覆盖率扫描以此为界。

## Phase 3：提取 Claim Ledger 与 Outcome Contract

1. **提取 Claim / Evidence Ledger**：
   - 从 `execution_scope` 提取所有可验证承诺（WHEN/MUST/SHALL、字段定义、生产路径、副作用），生成 `issues/<stem>/<stem>.claims.json`；
   - 坚持 **Claim strength ≤ Planned evidence strength**：E2E 声明必须要求真实运行证据，严禁 mock 冒充；
   - 详细字段规范与 Schema 遵循 [references/claim-ledger.md](references/claim-ledger.md)。
2. **提取 Outcome Contract**：
   - 提取面向读者的预期效果、可证伪读者问题、决定性结果与受阻声明，生成 `issues/<stem>/<stem>.outcomes.json`；
   - 详细字段规范与 Schema 遵循 [references/outcome-contract.md](references/outcome-contract.md)；
   - **机器验证**：运行 `python .codex/skills/mission-csv-execute/scripts/validate_outcome_contract.py issues/<stem>/<stem>.outcomes.json`，失败先修，严禁强行生成 CSV。

## Phase 4：生成 CSV 与合成行

1. **表头规范**：读取 `issues/TEMPLATE.csv`，使用结构化 CSV writer 生成完全一致的 28 列表头。路径固定为 `issues/<stem>/<stem>.csv`（`<stem>` 为 `<YYYY-MM-DD_HH-mm-ss>-<topic>`）；
2. **原子性与接线约束**：
   - 单个 issue 对应独立验证路径；
   - 按 `doc-field-mapping.md` 闭环路径规则扫描跨模块消费与启动注册，为所有承诺的连接点生成独立接线 issue；
3. **合成行注入**：
   - 涉及正式实验结果/ExpID 时，在普通 issue 后追加 `RESULT-ANALYSIS-01`；
   - 末尾必须追加唯一的 `REVIEW-01` 愿景验收行；
   - 合成行字段规范遵循 [references/synthetic-rows.md](references/synthetic-rows.md)，具体执行协议分别由下游 `post-run-result-analysis` 与 `closing-review.md` 拥有；
4. **Claim 覆盖率扫描**：
   - 生成完毕后逐条核验 Claim 覆盖：在 scope 内未覆盖则补 issue，scope 外标 `out_of_scope`；
   - 最终将更新后的账本落入 `<stem>.claims.json`。

## Phase 5：任务绑定与交接

1. **登记任务指针**：
   ```bash
   python .agents/harness/workflow/mission_state.py bind --task-id <SpecID> --csv issues/<stem>/<stem>.csv --source-ref <approved-spec-path>
   ```
2. **对话层汇报（强制一键复制）**：在终端向用户汇报时，必须以独立代码块展示原始路径与启动命令，严禁使用超链接语法：
   ```text
   任务 CSV 路径：
   issues/<stem>/<stem>.csv

   执行会话启动命令：
   mission issues/<stem>/<stem>.csv
   ```
3. **委托执行**：输出标准生成摘要后，直接交接给 `mission-csv-execute` 进入闭环执行。
