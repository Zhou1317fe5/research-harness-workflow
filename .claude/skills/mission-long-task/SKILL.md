---
name: mission-long-task
description: Use when the input is a complex task description that needs decomposition, persistence, and recovery before execution through a generated .mission CSV.
---

你现在是「长任务分解与执行器」。

# 目标

把一个复杂任务分解为可追踪的 `.mission/*.csv`，然后交给 `mission-csv-execute` 闭环执行。

# 适用场景

- 复杂 bug 需要轻量根因定位
- 大规模重构跨多个模块
- 多步功能开发但不适合先落批准文档
- 任何需要中断恢复的任务

# 流程

## Phase 0：初始化

1. 确定项目根目录
2. 从用户请求生成任务名：`<YYYYMMDD>-<short-topic>`（小写，连字符分隔）
3. 确保 `.mission/` 目录存在

## Phase 1：分析与拆分

### 复杂 bug
先做轻量根因定位，用发现指导任务拆分；复杂 bug、根因不明或跨模块故障可以在 CSV 的 `required_skills` 中填写 `systematic-debugging`。
不要在 CSV 中填写 `test-driven-development`、TDD、RED 或 test-first 相关 skill。

### 所有任务
1. 拆分为 5-15 步（动词开头描述）
2. 为每步定义 acceptance criteria 和验证方式
3. 生成 `.mission/<YYYYMMDD_HH-mm-ss>-<task-name>.csv`，使用标准 CSV schema
4. 填充：`id`, `priority`, `phase`, `area`, `title`, `description`, `acceptance_criteria`, `test_mcp`, `required_skills`, `required_mcp`；`required_skills` 不得填写 `test-driven-development`
5. 若任务包含代码更改，且后续会启动训练、评估、远程运行或生成实验结果，必须在代码实施 + 本地轻量验证 block 之后、首次训练 / 运行 issue 之前插入 `PRERUN-REVIEW-01`
6. 在普通执行 issue 之后追加 `REVIEW-01`，用于长任务完成度自检；该行必须包含从原始任务抽取的任务专属 claim/evidence 检查项，不能只写通用套话

### 拆分规则
- 每条 issue = 一个独立可验证的工作单元
- **原子性约束**：单个 issue 必须是一个可独立验证、独立提交的原子变更。如果一个 issue 包含多个独立验收场景（各自有不同的失败路径和修复路径），必须拆成多行。反例：把"smoke 全链路 + leave-view + history-thread + wrong-pool-revert + 模糊请求"打包成一行——这五个场景各自独立，任何一个卡住都不应该阻塞其他四个。
- `acceptance_criteria` 必须可机器验证或有明确复现步骤
- `refs` 至少 1 个 `path:line`
- 避免拆太细（不要几十条 TODO）

### `.mission` `PRERUN-REVIEW-01` 行规则

`.mission/*.csv` 若包含“代码实施 → 训练 / 评估 / 远程运行 / 实验结果生成”链路，必须显式生成运行前代码实施审查行；不要只依赖最终总结或最终 `REVIEW-01`。

`PRERUN-REVIEW-01` 的位置固定在代码实施 + 本地轻量验证 block 之后、首次训练 / 运行 issue 之前：

- 它只审查本次运行将使用的代码快照、轻量验证证据和运行命令
- 执行流程由 `pre-run-implementation-review` skill 定义；`.mission` 只记录审查事件、输入条件和 gate 结果
- 它不是逐文件、逐小改动、逐普通 row 的审查，也不得变成 TDD/RED/test-first
- 若同一个长任务中存在多轮“新代码快照 → 运行”，每轮首次运行前生成对应 `PRERUN-REVIEW-N`
- 若 pre-run review 后又修改了影响运行的代码，原审查失效；必须新增或重跑 pre-run review
- 审查通过后必须在 notes 或 review log 记录 `pre_run_code_commit:<hash>`；该 commit 是运行所用代码快照，不是整个任务的最终 commit

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
| `review_regression_requirements` | `Check original request, affected canonical files, parameter chain, local validation, command args/env, branch, commit, and no secret leakage before run.` |
| `refs` | `<best-source-path-or-request>:1; <first-run-issue-or-command-ref>` |
| `notes` | `review_kind:pre_run_implementation; review_skill:pre-run-implementation-review; review_agent:same-model-sub-agent-preferred; source_doc:<task-source>; gated_run:<first-run-id>` |

### `.mission` `REVIEW-01` 行规则

`.mission/*.csv` 也必须显式生成 review 行；不要只依赖最后总结。

生成 `REVIEW-01` 时，先从原始任务中抽取用户真正承诺的结果，并写进 review 条件：

- 若任务声明完成某个真实行为、真实副作用、真实集成、真实迁移、真实发送、真实同步、可见交互或端到端流程，review 条件必须检查证据是否支撑同等级声明
- 若交付只使用 mock、fixture、stub、dry-run、scaffold、字符串检查或静态验证，review 条件必须要求它被如实标注，且不得冒充真实完成
- 若测试或外部验证无法运行，review 条件必须检查是否记录 `validation_limited` / `manual_test` / `risk`，不得用替代假路径伪装通过
- `review_regression_requirements` 必须包含 2-4 条来自原始任务的任务专属检查项；如果无法抽取，至少写明要审查 claim/evidence 是否一致

建议字段：

| 字段 | 值 |
|------|----|
| `id` | `REVIEW-01` |
| `priority` | `P0` |
| `phase` | 最后阶段序号 |
| `area` | `review` |
| `title` | `Review task outcome against original request` |
| `description` | `Compare original-request claims with delivered behavior, evidence level, CSV state, validation evidence, and mission log.` |
| `acceptance_criteria` | `WHEN all non-review issues before this row are closed THEN check source-specific claim/evidence alignment; WHEN gaps or overstated claims are found THEN append follow-up issues and REVIEW-02; WHEN no gaps remain THEN close the CSV.` |
| `test_mcp` | `MANUAL` |
| `required_skills` | 默认留空；复杂 bug、根因不明或跨模块故障可填写 `systematic-debugging` |
| `required_mcp` | 留空，除非任务本身要求浏览器或外部验证 |
| `review_initial_requirements` | `Verify all prior non-review rows are closed before running this review.` |
| `review_regression_requirements` | `Check original request, acceptance criteria, delivered diff, validation evidence, mission log, and source-specific claim/evidence alignment.` |
| `refs` | `<best-source-path-or-request>:1` |
| `notes` | `review_kind:vision; review_agent:self-check; source_doc:<task-source>` |

## Phase 2：委托执行

将生成的 `.mission/<timestamp>-<task-name>.csv` 交给 `mission-csv-execute`。

- 生成 CSV 后立即进入 `mission-csv-execute`，不要因为 CSV 已生成、log 已齐、checkpoint 已完整而暂停。
- checkpoint / commentary 只服务恢复与可见进度，不是把控制权交还给用户的理由。

与批准文档执行流的区别：
- CSV 位置：在 `.mission/`，作为本地恢复工件
- Git 跟踪：CSV 默认不提交，代码按逻辑边界提交
- Commit message：`[<task-name>-<id>] <title>`

## Phase 3：中断恢复

被中断后恢复时：

1. 检测上下文丢失（compaction / 会话重启）
2. 定位 CSV：`ls .mission/*.csv`
3. 恢复状态：
   - 读 CSV → 找到第一个未完成行
   - 读 `.mission/<task-name>/log.md`（如有）→ 恢复决策上下文
4. 宣告恢复：
   ```
   上下文已恢复
   任务: <from CSV>
   进度: X/Y 步已完成
   恢复点: Step #N - <title>
   ```
5. 从第一个未完成行继续

## Phase 4：决策日志（推荐）

对于长任务，维护 `.mission/<task-name>/log.md`：

```markdown
## Step N: <title>
- **状态**: DONE | FAILED
- **做了什么**: ...
- **关键决策**: ...
- **遇到的问题**: ...
- **变更文件**: path:line
- **下一步**: Step N+1
```

- `log.md` 是恢复工件，不是自然停点。记录完成后继续执行，除非已满足 `mission-csv-execute` 的正式停止条件。

## Phase 5：收尾

1. 确认所有 CSV 行已完成
2. 在 `.mission/<task-name>/log.md` 写最终总结（如有）
3. 向用户宣告完成

# 目录结构

```
.mission/                           # gitignored，仅辅助工件
├── <timestamp>-<task-name>.csv     # 任务状态（标准 CSV schema）
└── <task-name>/
    ├── log.md                      # 决策日志 / 审计轨迹
    └── raw/                        # 缓存的外部数据
```
