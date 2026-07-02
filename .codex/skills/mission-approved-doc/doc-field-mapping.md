# Approved Doc -> CSV 字段映射

## 适用输入

- `docs/superpowers/specs/*.md`
- `docs/superpowers/plans/*.md`
- 任何已获用户批准、且正文能清晰定义 scope / tasks / validation 的 Markdown 文档

## From design doc

| 章节 / 结构 | 提取内容 | CSV 字段 |
|-------------|----------|----------|
| `## Problem` / `## Why` / `## Goal` | 背景、目标 | 上下文理解（不直接入 CSV） |
| `## Scope` / `## Non-Goals` | 范围边界 | `description`, `notes` |
| `## Architecture` / `## Design` / `## Components` | 技术分层、关键模块 | `phase`, `area`, `refs` |
| `## Constraints` / `## Risks` | 关键约束与风险 | `priority`, `review_regression_requirements`, `notes` |
| `## Testing` / `## Validation` / `## Success Criteria` | 验收口径 | `acceptance_criteria`, `test_mcp` |

## From implementation plan

| 结构 | 提取内容 | CSV 字段 |
|------|----------|----------|
| `### Task N` / `## Task N` | 阶段分组 | `phase`, `id` 前缀 |
| `**Files:**` | 影响文件 | `refs`, `area` |
| `- [ ] Step ...` | 原子工作项 | `title`, `description` |
| `Run:` / `Expected:` | 验证命令 | `acceptance_criteria`, `review_initial_requirements` |
| Commit / Review 说明 | 提交和回归边界 | `review_regression_requirements`, `notes` |

## Granularity rules

1. 若计划文档已有 `Task` / `Step` 结构，优先按可独立提交的任务单元生成 issue
2. 若只有设计文档，没有显式任务结构，则按独立模块、独立验证路径拆分 3-10 条 issue
3. 一个 issue 只能对应一条清晰的验证路径；若两个工作项拥有不同失败路径或不同回归面，必须拆开
4. 若任务包含代码更改且后续会训练、评估、远程运行或生成实验结果，在代码实施 + 本地轻量验证 block 后、首次运行 row 前插入 `PRERUN-REVIEW-01`
5. 普通 issue 生成完毕后，固定追加 `REVIEW-01`；后续 review 轮次只由 `mission-csv-execute` 在发现缺口时追加

## Field inference rules

### `priority`

- 涉及破坏性变更、迁移、权限、删除：`P0`
- 公共基础能力或多个任务依赖项：`P1`
- 其他默认：`P2`

### `test_mcp`

- 后端逻辑 / API：`AUTOSERVER`
- 前端组件 / 页面：`AUTOFRONTEND`
- 多步流程 / 跨页面交互：`AUTOE2E`
- 契约 / schema / adapter：`CONTRACT`
- 迁移 / 数据修复：`MIGRATION`
- 难以自动化的体验验证：`MANUAL`

### `required_skills`

- 默认留空
- 若批准文档明确指定某个 skill，按文档写入
- `PRERUN-REVIEW-N` 行固定写 `pre-run-implementation-review`
- 不再为前端/UI任务自动注入固定 skill

### `required_mcp`

- backend / infra：留空
- 用户可见 UI：`chrome-devtools`
- 多步交互：`chrome-devtools;playwright`
- 强视觉 / 动效 / 氛围：`chrome-devtools;screenpipe`
- 两者兼有：`chrome-devtools;playwright;screenpipe`

## Pre-run Implementation Review Rows

`PRERUN-REVIEW-N` 行用于在训练 / 运行前审查代码实施正确性，避免错误代码进入远程运行或实验结果链路。

- 只在“代码更改之后还有训练、评估、远程运行或实验结果生成”时生成
- 位置固定在代码实施 + 本地轻量验证 block 之后、首次训练 / 运行 row 之前；不是 CSV 尾部 review
- 不按每个文件、每个小改动或每条普通 row 生成；同一代码快照只需要一次 pre-run review
- 若 pre-run review 后又修改了影响运行的代码，必须在运行前新增或重跑 `PRERUN-REVIEW-N`
- `area` 固定为 `review`
- `priority` 固定为 `P0`
- `test_mcp` 固定为 `manual`
- `required_skills` 固定为 `pre-run-implementation-review`
- `notes` 必须包含 `review_kind:pre_run_implementation`、`gated_run:<first-run-id>`，通过后追加 `pre_run_code_commit:<hash>` 与 `pre_run_result:pass`
- 后续训练 / 运行 row 的 `commit_hash` 或 `notes` 必须引用同一个 `pre_run_code_commit`；后续 artifact / analysis / review commit 不得覆盖该含义

## Vision Review Rows

`REVIEW-N` 行用于审计交付结果是否达成批准文档愿景。

- `area` 固定为 `review`
- `priority` 固定为 `P0`
- `test_mcp` 固定为 `MANUAL`
- `refs` 至少包含批准文档路径
- `notes` 必须包含 `review_kind:vision`
- `REVIEW-01` 由 CSV 生成阶段创建
- `REVIEW-02` 及之后由执行阶段在发现缺口时追加

## Acceptance Criteria 构建顺序

1. 文档中的 testing / validation / success criteria
2. task / step 附带的运行命令和预期结果
3. 设计文档中的 constraints / risks
4. 关键文件 `ref: path:line`
