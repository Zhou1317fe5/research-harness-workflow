# 指令优先级

1. 当前会话中用户的明确要求
2. 仓库自身的规则、文档与约定
3. 相关 skill / protocol 的流程定义
4. 本 `CLAUDE.md` 的硬门禁与偏好

`# 硬门禁` 一节下的规则无论走哪条路由都必须满足。审查、分析、解释类任务可不进入实现流程，但结论必须可追溯。

---

# 双柱架构

本项目的工作流由两根柱子支撑，CLAUDE.md 是它们之上的路由层与硬约束层。

| 柱子 | 职责 | 触发方式 |
|------|------|----------|
| **mission** | 自包含任务编排与执行引擎（批准文档转 CSV、闭环执行、持久化恢复） | `mission <doc-path\|csv-path\|描述>` |
| **brainstorming** | 需求澄清、方案收敛与 spec 生成流程 | 按本文件路由判断 |

---

# 路由矩阵

```
用户请求
  │
  ├─ 先判断任务复杂度
  │   ├─ 简单明确 / 小修小补 / 明确文案 / 路径整理 / 低风险配置？
  │   │   → 直接执行
  │   └─ 新能力 / breaking / 架构变更 / 复杂科研实验设计 / 需求仍模糊？
  │       → brainstorming → 产出 `docs/superpowers/specs/*.md` 或用户指定路径 → 用户批准 → mission <spec-doc.md> 转 CSV，或按用户要求直接实现
  │
  ├─ 已批准的 design doc / plan doc？
  │   → mission <doc-path.md>
  │
  ├─ 已有 task CSV（`issues/*.csv` 或 `.mission/*.csv`）？
  │   → mission <csv-path>
  │
  ├─ 复杂 bug / 长时 refactor（需持久化/恢复）？
  │   → 轻量根因定位；必要时使用 `systematic-debugging`；可直接修复或交给 `mission <任务描述>`
  │
  ├─ 已有本轮 implementation plan，且用户要求按计划施工？
  │   → 直接按计划执行
  │
  ├─ 功能开发 / bug 修复 / 行为变更？
  │   → 直接实现 + 风险匹配的测试/验证；禁止 TDD/RED/test-first
  │
  ├─ 分析 / 审查 / 解释 / Q&A？
  │   → 直接回答
  │
  └─ 简单明确的小任务？
      → 直接执行 + update_plan

任何代码变更后：相关测试 → 自检 diff/风险/证据 → commit
```

- 用户要求 `continue nonstop` 时持续推进到验收或明确阻塞。
- 分析代码问题和修复 bug 时启用 `sequential-thinking`。

---

# 硬门禁

以下规则无论走哪条路由都不可违反。

## Claude Code 规划交付边界
- Claude Code 只做规划交付：讨论 / 修订 spec、生成 / 修订 / 审查任务 CSV；不自动执行 CSV，也不按 CSV 生成文件（执行与产物生成属 Codex）。已有 `issues/*.csv` 或 `.mission/*.csv` 时只做审查、修订和一致性检查。
- skill 流程不得覆盖上述 Claude/Codex 职责边界（skill 在指令优先级中排在本文件之上，此条为例外收窄）。
- 在 brainstorming / spec / approved-doc 阶段，允许修改 spec 与生成 CSV；禁止创建目录骨架、业务文档、配置模板、源码、测试、报告或数据产物。

## 验证

- commit/push/PR 前必须运行相关测试并如实报告；功能开发、bug 修复、行为变更必须补齐风险匹配的验证证据（禁 TDD/RED/test-first 见路由矩阵）。
- 本地负责代码正确性、单元测试、`compileall`、参数链路与配置解析；真实训练启动、`1 step` / `1000 step`、GPU 显存、loss/log/checkpoint 验证必须走 `scripts/.env` 指向的远程服务器。
- 缺少目标验证时标注 `validation_gap`；不得把替代验证包装成目标通过，不得虚构命令、退出码或验证结果。
- `pre-commit` 是推荐实践，非阻断项（除非用户/仓库明确要求）。

## 训练 / 运行前代码实施审查

- 含代码更改且后续会训练、评估、远程运行或生成实验结果的任务，必须在代码实施和本地轻量验证完成后、首次运行前做一次 pre-run implementation review。
- 该审查只作为运行前门禁：不逐文件、逐小改动、逐 CSV row 审查，也不得变成 TDD/RED/test-first；输入包括需求 / Spec / CSV intent、diff 或代码快照、本地验证证据、待运行命令和关键项目约束。
- 中等及以上代码改动、bug 修复、行为变更、mission/CSV 任务优先用独立 review sub-agent；不可用时记录 `validation_limited:same-model sub-agent unavailable` 并执行独立上下文 fallback review。
- CSV / mission 任务在代码实施 + 本地验证 block 之后、首次运行 block 之前设置 `PRERUN-REVIEW-*`；它记录本次运行使用的 `pre_run_code_commit`，不是整个 CSV 的最终 commit。
- 审查发现 blocker 时不得启动训练 / 运行；先修复、补跑相关轻量验证，并重新通过 pre-run review。

## 安全与进程

- 无用户授权不运行破坏性命令，例如 `git reset`、危险删除等。
- 不硬编码密钥、凭证或 API Key；远程连接信息只读 `scripts/.env`，不回显密码，不写入脚本、CSV、review 或日志。
- 参数化查询，不拼接不可信输入构造 shell / SQL；系统边界校验并清理外部输入。
- 不终止非当前任务启动的进程。
- 长生命周期进程最少新增、优先复用、结束即回收；启动前检查端口占用，启动后确认真实可访问。

## 搜索分工

### 核心原则
代码库内部先语义后精确，代码库外部走可保存证据的搜索链路。

### 使用场景

#### 1. 本地代码语义理解：`fast_context_search`
- 适用：探索性搜索、自然语言定位逻辑、理解业务逻辑 / 调用链路、跨模块查询、新任务开始前的代码调研和中文语义搜索。
- 参数：快速粗查用 `tree_depth=1, max_turns=1`；默认用 `tree_depth=3, max_turns=3`；复杂调用链可用 `max_turns=5`；必要时用 `project_path` 指定项目根目录。

#### 2. 精确字符串定位：`rg`
- 已知函数名、类名、配置项、报错文本或固定 token 时，用 `rg` 精确定位。
- 已知文件路径时直接阅读目标文件，不做额外探索。

#### 3. 外部资料 / 论文 / 工具版本：`smart-search-cli` skill
- 外部资料、论文、工具版本和当前信息统一走 `smart-search-cli` skill；具体命令、fetch、Context7/Exa 路由、Deep Research 细节以该 skill 为准。
- 本项目覆盖规则：默认不做配置前置检查；`search` 返回空或明显无关时最多重试 3 次，仍为空则报告 `search_empty`。
- 需要保存证据时使用 `--output`：单实验写入 `research_workspace/experiments/<ExpID>/analysis/search_evidence/`；路线级写入 `research_workspace/experiments/_cross_experiment/<RouteID>/search_evidence/`；模块 / 论文调研写入 `research_workspace/module_research/search_evidence/`。
- API / SDK / framework / library 文档一律经 `smart-search-cli` skill 路由，不单独调用 Context7 MCP。

## 人读产物语言

- `reports/*.md` 与 `issues/*.review.md` 面向用户的标题、结论、风险、验证说明和剩余工作默认中文优先、中英对照。
- `issues/*.csv`、`.mission/*.csv`、`reports/*.csv`、配置、数据和模型产物属于机器消费层，不作为历史改写目标翻译或重写。
- 英文枚举、命令、路径、模型名、指标保持原 token，例如 `replay_ready`、`python -m compileall`、`DSDM`、`MAE`、`RMSE`。
- 语言改写不得削弱 CSV 状态源语义，不得跳过实现、验证、review、提交闭环。

---

# 提交约定

## 前置条件

- commit/push/PR 前满足硬门禁中的验证要求
- merge 前至少完成自检：diff 范围、风险点、测试证据；重大变更按当前用户要求再决定是否额外 review。

## 提交粒度

- 一个逻辑变更一个提交，边界清晰可审查
- 不混入无关格式化、调试痕迹
- 先 `git status` 确认改动范围，只 add 相关文件

## 双仓库提交

- 主仓库提交包含源码、配置、脚本、测试、`docs/`、`issues/`；`research_workspace/` 由其自身仓库单独提交。
- 远程结果落库或生成实验分析后，按 `research-result-commit` workflow 将当前 ExpID 相关研究产物合并为一个 `research_workspace` commit，并在适用时记录关联主仓库 branch + commit。

## Commit Message

格式：`<emoji> <type>(scope): summary`

| 类型 | Emoji | 说明 |
|------|-------|------|
| init | 🎉 | 项目初始化 |
| feat | ✨ | 新功能 |
| fix | 🐞 | 错误修复 |
| docs | 📃 | 文档变更 |
| style | 🌈 | 代码格式化（不影响逻辑） |
| refactor | 🦄 | 代码重构 |
| perf | 🎈 | 性能优化 |
| test | 🧪 | 测试相关 |
| build | 🔧 | 构建系统或外部依赖 |
| ci | 🐎 | CI 配置 |
| chore | 🐳 | 辅助工具变动 |
| revert | ↩ | 撤销提交 |

- scope 用模块/目录，无明确范围可省略
- summary 中文、动词开头、≤ 50 字、不加句号
- **正文默认必写**，至少覆盖三点：
  - `Why:` 为什么要改
  - `Why this works:` 为什么这样改有效（验证证据 / 设计理由 / 根因修复）
  - `Remaining:` 还剩什么工作、已知限制、后续建议
- 破坏性变更：type 后加 `!` 或正文写 `BREAKING CHANGE: ...`

推荐正文模板：

```text
Why:
- <问题 / 目标>

Why this works:
- <设计理由 / 验证结果 / 根因修复依据>

Remaining:
- <后续 issue / 已知缺口 / 下一步>
```

---

# 沟通偏好

## 语言

- 默认简体中文，可混用英文术语
- 代码标识符英文，代码注释简体中文

## 输出风格

- **执行类任务**：进度优先 — 当前动作、已完成、下一步、风险/阻塞、`path:line` 引用
- **分析类任务**：结论优先 — 核心判断、依据与权衡、实施建议
- **简单查询**：直接回答，不加框架
- 多步任务（≥ 3 步）用 `update_plan` 维护可见任务列表，同一时刻仅一个 `in_progress`，完成即标记
- 复杂内容后附简短总结并突出下一步，不重复输出完整计划

---

# 技能注册表

| 技能 | 用途 |
|------|------|
| `brainstorming` | 新能力、breaking change、架构变更、复杂科研实验设计或需求模糊时，收敛设计与验收口径 |
| `mission` | 批准文档转 CSV、已有 CSV 执行、长任务持久化与恢复 |
| `systematic-debugging` | 复杂 bug、根因不明或跨模块故障的根因定位辅助 |

开始任务前优先判断是否有匹配的 skill。命中则读取其 `SKILL.md` 并按流程执行。

---

# 项目背景

- 基线：<你的 baseline 名称与简述>。
- 方法：在可靠 baseline 上组合可插拔模块。
- 目标：优先提升 **<你的主指标>**；<辅助指标> 只作辅助。

---

# 项目事实

- 科研工作区统一入口为 `research_workspace/`；新建研究文件前先判断归属目录，无法判断时先说明建议路径并等待确认。
- 常用产物：Spec 在 `docs/superpowers/specs/<SpecID>.md`；执行清单在 `issues/<SpecID>.csv`；CSV schema 在 `issues/TEMPLATE.csv`；review 交接在 `issues/<SpecID>.review.md`。
- `research_workspace/` 目录归属：根目录放长期入口和台账（`README.md`、`STATE.md`、`00-实验记录.md`、`11-模块规划.md`）；`experiments/<ExpID-or-RunDir>/` 放单次实验绑定产物，其 `analysis/` 子目录放该实验的分析 / 诊断 / next steps / root cause / 计划草案；`experiments/_cross_experiment/<RouteID>/` 放跨实验或路线级复盘；`commands/` 放远程命令与 shell 片段；`papers/` 放论文材料；`module_research/` 放模块论文 / 源码调研；`archive/` 放过时草案。
- 远程训练 / 评估用 `tmux`；记录非敏感证据：hostname、branch、commit、GPU、conda env、session、EXP_ROOT、脚本与日志路径。
- **Stop Trigger（运行健康检查）**：远程运行必须通过两层检查，任一触发必须立即记录到 CSV `notes` / `next_action` 或 `issues/<SpecID>.review.md`（不新增 `remote_state` 枚举）：
  - **① 首步强制采样**（启动后 30s–2min 内，性质 = go/no-go）：进程存在 + log 出现 `step ≥ 1` + loss 有限值；训练阶段 GPU util 抽样 ≥ 30%；评估阶段单次 inference 耗时 ≤ 预算 × 3；checkpoint 路径已被创建。任一不满足 → 不要走开，先排查；不要标 `running_remote`，标 `dev_state=进行中` + `notes` 写"首步验证未通过"。首步采样归属：首条 remote row 负责"启动 + 首步 30s-2min 观察" + 更新自己的 `notes`（格式：`首步采样：通过 (step=1, loss=0.42, gpu_util=87%) | 未通过 (no step in log)`）+ `remote_state`（通过 → `running_remote`；未通过 → 保持空或标 `启动失败`）。
  - **② 长期周期巡检**（整个生命周期，性质 = abort）：训练 log 出现 NaN / Inf / CUDA OOM；console log 持续 >5min 无增长；训练阶段 GPU util 持续 <30% 超过 10min；评估阶段日志停滞 >5min；nvidia-smi 无目标进程；Spec 定义的 Stop Condition（<你的项目特定停止条件>）。任一触发 → 立即停止追加，记录 `notes` / `review` 触发原因 + 时刻；恢复时先排查根因再决定是否续训。
- 研究产物最低关联：SpecID + ExpID + Branch + Commit；多次远程运行补充 RunID。
- 正式执行入口：`/goal @issues/<SpecID>.csv`；远程训练中的 `remote_state=running_remote` 只是恢复点。
- CSV 状态枚举：`dev_state` 用中文"未开始 / 进行中 / 已完成"（不改英文以避免 schema breaking change）；`remote_state` 用英文 `not_applicable / running_remote / completed / artifacts_pulled` 等。
- 科研声明必须有 artifacts 支撑；没有实际训练 / 评估证据时，不声称 <主指标>、Current Best 或实验完成。
- 触及 <关键模块>、attention、batch size、baseline 回退时，先确认语义和验证路径，再改代码或写结论。
