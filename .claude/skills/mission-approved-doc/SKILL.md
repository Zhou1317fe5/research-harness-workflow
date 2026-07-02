---
name: mission-approved-doc
description: Use when the input is a user-approved design doc or implementation plan and the agent needs to turn it into an executable issues CSV before handing off to CSV execution.
---

你现在是「批准文档执行器」。

# 目标

把一个**已获用户批准**的设计文档或计划文档转换为 `issues/*.csv`，然后交给 `mission-csv-execute` 闭环执行。

# 流程

## Phase 1：批准门验证（HARD STOP）

1. 确认输入文档存在
2. 完整读取文档
3. 确认用户已经明确批准该文档作为执行输入
4. 若未批准则硬停，只输出：
   ```
   文档 <path> 尚未获用户批准。
   请先明确批准后再执行。
   ```
5. 未经批准绝不执行任何代码变更

## Phase 2：读取与抽取

优先读取文档本体，再按需抽取以下信息：

| 来源 | 必须 | 提取内容 |
|------|------|----------|
| 设计文档 / 计划文档正文 | 是 | Goal, scope, constraints, affected files, task structure |
| 显式 testing / validation 章节 | 重要 | 验收口径、命令、风险点 |
| 与文档直接关联的 code/file refs | 重要 | `refs`, `area`, `required_mcp` 推断 |

字段抽取与拆分规则详见 `doc-field-mapping.md`。

## Phase 3：生成 CSV

生成文件：`issues/<YYYY-MM-DD_HH-mm-ss>-<topic>.csv`

关键规则：

- 正式任务 CSV 固定放在 `issues/`
- 生成前必须读取 `issues/TEMPLATE.csv`；它是本项目唯一 CSV schema、字段顺序、默认字段与状态枚举来源
- 使用 `csv.DictWriter` / `csv.writer` 或等价 structured serializer 写入 CSV；不得手写拼接 CSV 行
- 含英文逗号、括号、箭头、换行的字段必须由 CSV writer 正确转义；不得输出未加引号导致列漂移的自由文本行
- 写完后必须用 `csv.DictReader` 校验表头与 `issues/TEMPLATE.csv` 完全一致、所有行列数一致、无 `None` 溢出列、状态枚举合法
- `acceptance_criteria` 优先从文档里的 validation / testing / success criteria 提取
- **原子性约束**：单个 issue 必须是一个可独立验证、独立提交的原子变更
- `required_skills` 与 `required_mcp` 必须在生成阶段显式写全
- `refs` 必须至少包含 1 个 `path:line`
- 若 CSV 包含代码更改，且后续会启动训练、评估、远程运行或生成实验结果，必须在代码实施 + 本地轻量验证 block 之后、首次训练 / 运行 issue 之前插入 `PRERUN-REVIEW-01`
- `PRERUN-REVIEW-*` 是运行前代码实施审查，不是逐 issue 审查；`required_skills` 固定写 `pre-run-implementation-review`
- 若同一个 CSV 中存在多轮“新代码快照 → 训练 / 运行”，每轮首次运行前都必须有对应的 `PRERUN-REVIEW-N`；若审查后又修改了影响运行的代码，原 pre-run review 失效，必须新增或重跑 pre-run review
- `PRERUN-REVIEW-*` 记录的是本次训练 / 运行使用的 `pre_run_code_commit`，不是整个 CSV 的最终 commit
- 在普通执行 issue 之后，必须追加一条 `REVIEW-01` 作为首轮文档愿景验收；该行必须包含从源文档抽取的任务专属 claim/evidence 检查项，不能只写通用套话
- 初始化状态：`未开始` / `未提交`

### `PRERUN-REVIEW-01` 行规则

`PRERUN-REVIEW-01` 是训练 / 运行前的代码实施审查事件，只在“代码更改之后还有训练、评估、远程运行或实验结果生成”时生成。

生成 `PRERUN-REVIEW-01` 时，先抽取本次运行将依赖的代码意图、参数链路、轻量验证和运行命令，并写进 review 条件：

- 执行流程由 `pre-run-implementation-review` skill 定义；CSV 只记录审查事件、输入条件和 gate 结果
- 审查范围只覆盖首次运行前的已实施代码快照、验证证据和即将执行的运行命令
- 审查必须确认运行命令使用的 branch / commit 与 `pre_run_code_commit` 一致；不得用后续 artifact / analysis / review commit 替代
- 审查发现会污染实验结论的问题时，后续训练 / 运行 issue 不得启动；必须先插入修复 issue，并在修复后重新执行 pre-run review

建议字段：

| 字段 | 值 |
|------|----|
| `id` | `PRERUN-REVIEW-01` |
| `priority` | `P0` |
| `phase` | `pre_run_review` 或首次运行前的阶段序号 |
| `area` | `review` |
| `title` | `Pre-run implementation review before training/run` |
| `description` | `Review implemented code, local validation, command args, branch, and commit before starting the first training/eval/remote run.` |
| `acceptance_criteria` | `WHEN implementation and local validation rows before the first run are closed THEN review code diff/commits, validation evidence, command args, branch, and commit; WHEN blockers are found THEN do not start run and add follow-up before the gated run; WHEN pass THEN record pre_run_code_commit and allow the gated run.` |
| `test_mcp` | `manual` |
| `required_skills` | `pre-run-implementation-review` |
| `required_mcp` | `exec` |
| `review_initial_requirements` | `Verify implementation and local validation rows before the gated run are closed; identify the exact code commit/diff under review.` |
| `review_regression_requirements` | `Check approved doc intent, affected canonical files, parameter chain, local validation, command args/env, branch, commit, and no secret leakage before run.` |
| `refs` | `<doc-path>:1; <first-run-issue-or-command-ref>` |
| `notes` | `review_kind:pre_run_implementation; review_skill:pre-run-implementation-review; review_agent:same-model-sub-agent-preferred; source_doc:<doc-path>; gated_run:<first-run-id>` |

### `REVIEW-01` 行规则

`REVIEW-01` 是审计事件，不是普通实现任务。

生成 `REVIEW-01` 时，先从批准文档中抽取用户真正承诺的结果，并写进 review 条件：

- 若文档声明完成某个真实行为、真实副作用、真实集成、真实迁移、真实发送、真实同步、可见交互或端到端流程，review 条件必须检查证据是否支撑同等级声明
- 若交付只使用 mock、fixture、stub、dry-run、scaffold、字符串检查或静态验证，review 条件必须要求它被如实标注，且不得冒充真实完成
- 若测试或外部验证无法运行，review 条件必须检查是否记录 `validation_limited` / `manual_test` / `risk`，不得用替代假路径伪装通过
- `review_regression_requirements` 必须包含 2-4 条来自源文档的任务专属检查项；如果无法抽取，至少写明要审查 claim/evidence 是否一致

建议字段：

| 字段 | 值 |
|------|----|
| `id` | `REVIEW-01` |
| `priority` | `P0` |
| `phase` | 最后阶段序号 |
| `area` | `review` |
| `title` | `Review documented vision against delivered work` |
| `description` | `Compare approved-doc claims with delivered behavior, evidence level, CSV state, validation evidence, and review log.` |
| `acceptance_criteria` | `WHEN all non-review issues before this row are closed THEN check source-specific claim/evidence alignment; WHEN gaps or overstated claims are found THEN append follow-up issues and REVIEW-02; WHEN no gaps remain THEN close the CSV.` |
| `test_mcp` | `MANUAL` |
| `required_skills` | 留空；仅当批准文档或用户明确要求 sub-agent/code-review 时填写 |
| `required_mcp` | 留空，除非文档本身要求浏览器或外部验证 |
| `review_initial_requirements` | `Verify all prior non-review rows are closed before running this review.` |
| `review_regression_requirements` | `Check approved doc goals, non-goals, acceptance criteria, delivered diff, validation evidence, prior review logs, and source-specific claim/evidence alignment.` |
| `refs` | `<doc-path>:1` |
| `notes` | `review_kind:vision; review_agent:self-check; source_doc:<doc-path>` |

## Phase 4：生成后摘要

```
生成完成
- 快照: issues/<timestamp>-<topic>.csv
- 来源: <doc-path>
- Issues: N 条（含 PRERUN-REVIEW-* 如适用，含 REVIEW-01）
- P0 任务: M 条
- 下一步: 等待 Codex 使用 `/goal @issues/<file>.csv` 闭环执行
```

## Phase 5：委托执行

本仓库中 Claude Code 到此停止；不得自动进入 `mission-csv-execute` 执行 CSV。实际闭环执行交给 Codex。
