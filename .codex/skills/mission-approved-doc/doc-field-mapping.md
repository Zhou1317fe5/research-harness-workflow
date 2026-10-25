# Approved Doc -> CSV 字段映射

`issues/TEMPLATE.csv` 是本项目唯一固定的科研 28 列 CSV schema。本文件只说明 canonical approved spec 如何映射到该表头；显式外部 compatibility CSV 可保留官方 19 列，但新任务不得生成 19 列。

## 适用输入

- `docs/specs/*.md`
- frontmatter 通过 `mission-spec/scripts/validate_spec.py` 校验
- `status: approved`，已提交且工作副本与 `HEAD` 一致

## From canonical spec

| 章节 / 结构 | 提取内容 | CSV 字段 |
|-------------|----------|----------|
| `## Problem` / `## Why` / `## Goal` | 背景、目标 | 上下文理解（不直接入 CSV） |
| `## Scope` / `## Non-Goals` | 范围边界 | `description`, `notes` |
| `## Architecture` / `## Design` / `## Components` | 技术分层、关键模块 | `phase`, `area`, `refs` |
| `## Constraints` / `## Risks` | 关键约束与风险 | `priority`, `review_regression_requirements`, `notes` |
| `## Testing` / `## Validation` / `## Success Criteria` | 验收口径 | `acceptance_criteria`, `test_mcp` |

## Granularity rules

1. 若计划文档已有 `Task` / `Step` 结构，优先按可独立提交的任务单元生成 issue
2. 若只有设计文档，没有显式任务结构，则按独立模块、独立验证路径拆分 3-10 条 issue
3. 一个 issue 只能对应一条清晰的验证路径；若两个工作项拥有不同失败路径或不同回归面，必须拆开
4. 普通 issue 生成完毕后，固定追加 `REVIEW-01`；后续 review 轮次只由 `mission-csv-execute` 在发现缺口时追加

## Claim / Evidence Ledger

生成 issue 前先抽取可验证 claim；CSV 生成质量以 claim 覆盖为准，不以 issue 数量为准。

| Spec 信号 | claim 类型 | 生成要求 |
|-----------|------------|----------|
| WHEN/MUST/SHALL/必需/必须 | 行为承诺 | 至少一个 issue 的 AC 显式覆盖 |
| 状态枚举/字段定义/API schema | 契约承诺 | AC 写字段、状态、错误码或 schema 验证 |
| 启动行为/注册/consumer/subscriber | 生产路径承诺 | 必须有接线 issue 或路径式 AC |
| 真实发送/同步/迁移/副作用/provider | 真实集成承诺 | `evidence_required` 至少为 `integration`，受限时必须写 `validation_limited` |
| mock/fake/dry-run/static 明确允许 | 受限证据承诺 | notes 写 `mock_allowed` / `limited_allowed`，review 不得升级声明 |
| non-goal/future/deferred | 范围边界 | 不生成实现 issue；notes 写 `out_of_scope:<section>;<reason>` |

每个普通 issue 的 `notes` 应压缩记录相关 claim，例如：

```text
claim_ledger:<stem>.claims.json; claims:CLAIM-001,CLAIM-004; evidence_level:integration; production_path:covered
```

`REVIEW-01` 的 `review_regression_requirements` 必须要求逐条读取 `<stem>.claims.json`，检查三件事：claim 是否覆盖、生产路径是否接上、证据等级是否支撑交付声明。若 CSV notes 出现 `claims:CLAIM-*` 但没有 `claim_ledger:<path>`，该 CSV 不完整，必须先补 ledger 再执行。

### 闭环路径规则（科研算法实现优先合并，严禁粉末化）

在科研算法实现中，模块内部数学与入口接线应**优先合并为单一 Implementation Issue（如 IMPL-01）**，严禁机械拆解为独立的 PREP、CORE、WIRING、ASSET、RESOURCE 多个碎片 issue 增加状态机流转内耗。
对于纯超参数微调、全量评估重跑或基线复现（Tier 1 任务），**强制生成 1~2 行极简 CSV（RUN-01 + REVIEW-01）**，严禁生成任何前置接线行与微观 Claim。

规则：
- 实现 issue 的 `acceptance_criteria` 必须包含端到端执行验证，而不是拆分出多个微观打卡行。
- 对于纯调参/评测任务，直接通过 `command_argv` 传递运行参数，免除前置实现与接线步骤。
- **REVIEW-01 的 regression requirements 聚焦于指标真实性与结论交付**。

## Field inference rules

### `priority`

- 涉及破坏性变更、迁移、权限、删除：`P0`
- 公共基础能力或多个任务依赖项：`P1`
- 其他默认：`P2`

### `test_mcp`

- 本地命令、后端逻辑 / API、迁移 / 数据修复：`local_cli`
- 真实远程训练 / 评估 / 运行：`remote_cli`
- 契约 / schema / adapter：`contract`
- 难以自动化的体验验证：`manual`

### `required_skills`

- 默认留空
- 若批准文档明确指定某个 skill，按文档写入
- `PRERUN-REVIEW` 行固定写 `pre-run-implementation-review`

### `required_mcp`

- Legacy 兼容列，无运行时消费者：固定留空，不作为执行合同、证据要求或生成推断项。

## Vision Review Rows

`REVIEW-N` 行用于审计交付结果是否达成批准文档愿景。

- `area` 固定为 `review`
- `area` 固定为 `review`
- `priority` 固定为 `P0`
- `test_mcp` 固定为 `manual`
- `refs` 至少包含批准文档路径
- `notes` 必须包含 `review_kind:vision`
- `REVIEW-01` 由 CSV 生成阶段创建
- `REVIEW-02` 及之后由执行阶段在发现缺口时追加
- `REVIEW-01` 由 CSV 生成阶段创建
- `REVIEW-02` 及之后由执行阶段在发现缺口时追加

## Acceptance Criteria 构建顺序

1. 文档中的 testing / validation / success criteria
2. task / step 附带的运行命令和预期结果
3. 设计文档中的 constraints / risks
4. 关键文件 `ref: path:line`

涉及代码实施后训练、评估、远程运行或实验结果生成时，生成阶段必须在首次 official run 前插入单个 `PRERUN-REVIEW-*`。该行记录 `gated_run`、`review_mode:scientific_review` 和 `pre_run_code_commit`；不生成 Attempt、lineage、generation、strict coverage 或 reviewer liveness。`SpecID/ExpID/RunID`、`remote_state`、artifact path、branch/commit 和 next_action 必须保留在 28 列字段中。

**PRERUN 豁免约束**：若任务为纯超参数微调、参数网格扫描、纯评测评估、主表重跑或基线复现，且不包含模型架构/损失函数/特征计算等算法源码重构，**严禁插入 PRERUN-REVIEW-* 行与 SMOKE-* 行**，任务 CSV 第一行直接进入正式 EVAL 或 TRAIN。

## Ingest 与轻量化产物规则

1. **直接采信官方指标**：生成 `INGEST-*` 或结果入账行时，其验收标准必须直接使用远端官方 `summary.json` / `eval_results.json` 中的指标，**严禁生成要求本地 CPU 逐像素重算（recount）或复现混淆矩阵的验收条件**。若 Spec 中残留此类历史文字，生成器必须自动将其纠正为直接采信官方指标。
2. **轻量秒级回传**：产物拉取仅限轻量摘要（`summary.json`、`progress.jsonl`、`evaluate.log`，体积 < 100 KB），严禁将成百上千个原始预测掩码（.npz）作为必需拉取产物，避免 SSH 弱网超时卡死。
3. **GPU 无缝衔接调度**：多运行/多数据集任务之间的前置依赖，仅认远端进程计算完成（`remote_state=completed` / `exit 0`），**严禁将“前序产物拉取到本地”作为后续 GPU 运行的前置门禁**，确保远端 GPU 算力无缝换卡。
4. **彻底废除数据级 SHA256 验收项**：生成任何任务行（DATA、SMOKE、TRAIN、EVAL、INGEST）时，严禁生成要求计算或核验数据集图像/掩码 SHA256 的验收项；即使 Spec 残留此类字眼，转换引擎自动过滤掉这些要求，数据集只验路径存在与基本格式。
5. **Smoke 极小化约束**：生成 `SMOKE-*` 行时，验收标准必须限定为极小切片（<= 20 个样本或 1~10 步），严禁生成包含全量数据集遍历、全池特征提取或全量哈希的 Smoke 任务。
