---
spec_id: <ModuleName>-v<N>
title: <方案标题>
status: draft
module: <模块名>
csv: issues/<SpecID>.csv
created: <YYYY-MM-DD>
experiment_type: <baseline_reproduction | probe | method | ablation | seed_audit | reproducibility>
---

# <方案标题>

## 使用原则

本模板是 checklist-style scaffold（查漏脚手架），不是 rigid format（固定版式）。

- Claude 可以先按问题本身自由组织 Spec，再用本模板查漏。
- 不强制每个 Spec 使用完全相同的章节顺序或篇幅。
- 普通轻量模块可以写短 Spec，只要关键事实、风险和验收标准清楚。
- 高风险模块必须覆盖 `CLAUDE.md` 中的分级讨论必检项。
- 不适用的条目写 `not_applicable` 或 `NOT RUN locally`，不要为了填满模板而制造内容。
- `REVIEW-01` 负责检查硬约束是否遗漏，不负责要求所有章节都写满。
- `experiment_type` 在 frontmatter 强制声明，枚举值见 §9.3 Claim Boundary 的对应说明；不同类型决定本轮可以声称什么、不能声称什么。

## 1. 问题锚点

- **STATE.md 关联**：<引用 `research_workspace/STATE.md` 活跃假设 Pn 的精确文字；若属新路线起点，写"无（新路线起点）"并说明理由（不属于任何活跃假设、不在已废弃路径列表内）>
- **触发证据**：<ExpID / artifact 路径 / per-class 信号 / 失败 log 行号 / 跨领域出处 paper（URL 或 bib 条目）>
- **本轮要回答的问题**：<在指定评测协议下，改动 X 能否将主指标从 a 提到 b，或减少某类可量化误差>
- **非目标**：<本轮明确不做的内容；越具体越好>

## 2. Idea Source

- **来源类型**：<① 模型搜索 / ② 文献+Baseline / ③ 跨领域移植 / ④ Baseline 极致优化 / ⑤ 实验信号 / ⑥ 二次调研>
- **证据链**：<论文 URL / ExpID / 实验失败 log / baseline 复现 commit / 跨领域实践来源；与来源类型一一对应>
- **评审记录**：<若经过 brainstorming Idea Card 隔离评审，填写"经 Idea Card 隔离评审，得分 X / 结论 go|revise"；日常 bug、续 spec、明显小迭代填"无（直接进入）">

> **填写约定**：① 模型搜索 与 ② 文献+Baseline 的 idea 必须经过 ⑥ 二次调研校验后才进入实施；③ 跨领域移植 与 ⑤ 实验信号 是相对原创的来源，证据链给出原始观察或论文出处即可。

## 3. 机制分析

- 核心机制：<模块或流程的 technical idea>
- 与 baseline 的关系：<复用/替换/旁路的部分>
- 预期收益：<为什么可能提升 <主指标>>

## 4. 接入点

| 位置 | 文件/入口 | 说明 |
|------|-----------|------|
| 训练 | <path> | <改动点> |
| 推理 | <path> | <改动点> |
| 配置/脚本 | <path> | <新增参数或开关> |

## 5. 风险与回滚

| 风险 | 影响 | 缓解/回滚 |
|------|------|-----------|
| <风险项> | <影响范围> | <验证或回滚方式> |

## 6. 实施步骤

1. <步骤一>
2. <步骤二>
3. <步骤三>

## 7. 训练与评估计划

- Dataset / Evaluation Protocol：<数据来源、划分及评测口径；项目专用维度按实际配置填写>
- Train：<脚本、checkpoint、关键参数>
- Eval：<脚本、checkpoint、关键参数>
- 记录：ExpID / RunID / Commit / Branch 必须写入 CSV 和实验记录。

## 8. 远程训练配置

| 参数 | 值 |
|------|-----|
| Train Intent | NOT RUN locally |
| Eval Intent | NOT RUN locally |
| Project Config | .agents/harness/config/project.toml |
| Expected Runtime | NOT RUN locally |
| Artifact Path | remote_artifacts/<ExpID>/<RunID>/ |
| Required Args | NOT RUN locally |
| Command Owner | 执行 agent |

- 若本轮不训练，保留 `NOT RUN locally` 并说明原因。
- 训练、评估命令及参数从项目配置的 `pipeline.stages` 解析，填写实际入口与约束。
- 执行 agent 通过 rrctl 构造远程命令，并把命令写回 CSV `notes` 或 review log。
- intent 无法唯一映射到项目入口时，记录缺失配置或澄清运行范围。

## 9. Research Contract（实验前冻结，开始后不改）

> 本节是本轮实验的判据冻结。Hypothesis / Success Signal / Failure Signal / Metric & Split / Stop Condition 在首次远程运行启动前定稿，运行结束后不改写、不重新定义，避免"事后造判据"。Claim Boundary 由 frontmatter `experiment_type` 直接决定。

### 9.1 功能验收（本地可判定）

- <开关是否生效 / 模块是否被正确导入 / 关键参数链路是否打通 / 是否对未启用路径无副作用 / 关键单元回归>

### 9.2 实验假设与信号（远程证据可判定）

- **Hypothesis**：<可被反驳的机制判断，例如改动 X 减少指定误差，从而改善主指标；写明预期证据>
- **Success Signal**：<具体指标、阈值和评测范围，例如验证集主指标 ≥ baseline + 预定增量，seed 0>
- **Failure Signal**：<**独立定义**，不是"未达到 Success"的反面。示例：NaN / OOM / <指标反向恶化阈值> / <持续低于预期阈值>。必须事先想清楚"什么样的现象会让你判定方法失败"。>
- **Metric & Split**：<主指标、辅助指标、数据划分、评测协议与重复运行设置>
- **Stop Condition**：
  - 默认承继：CLAUDE.md `项目事实` 节的 Stop Trigger（首步采样 + 长期巡检），无需在本节复述。
  - 本轮特定信号：<如 <指标持续低于阈值>、<指标恶化超过阈值>、特定 ablation loss 持续上升等；没有写"无"。>

### 9.3 Claim Boundary（由 frontmatter `experiment_type` 决定）

| experiment_type | 可声明 | 禁止声明 |
|-----------------|--------|----------|
| `baseline_reproduction` | "对齐论文数字"或"复现失败 + 偏差量化" | 不声明 <主指标> 提升、不进 STATE Current Best |
| `probe` | tentative 趋势、需 follow-up | 不声明 <主指标> / Current Best；标 "tentative / 待 seed audit" |
| `method`（单 seed） | tentative <主指标>，标注 seed | 不进 STATE Current Best |
| `method`（多 seed） | <主指标> ± std，可进 STATE Current Best | 不声称"超过 SOTA"除非完整对齐对照协议 |
| `ablation` | 组件贡献度，如 "+1.2 <主指标> from X module" | 不声称新 SOTA、不替换 baseline Current Best |
| `seed_audit` | 稳定性结论（std / 区间） | 单 seed 不进 STATE Current Best |
| `reproducibility` | 自家方法复现自检结论 | 不声称新结果 |

### 9.4 文档验收

- CSV：所有 row 状态闭环，`notes` / `next_action` 与本节信号对齐。
- STATE.md：根据 Claim Boundary 决定是否更新 Current Best / 活跃假设 / 已废弃路径。
- 实验记录：`research_workspace/EXPERIMENTS.csv / record.json` 索引行 + `research_workspace/experiments/<ExpID>/` 详细解读。

> **PRERUN-REVIEW 对照基准范围**：`pre-run-implementation-review` skill 在做代码实施一致性审查时，从本节抽取 **Hypothesis / Success Signal / Failure Signal / Metric & Split / Stop Condition** 五项的**代码可验证子集**作为对照基准；Claim Boundary 不审（属于报告纪律，非代码事实）。skill 仍读整个 spec.md，但这 5 项是核心对照重点。

## 10. 结果分析模板

> 本节只保留空模板；实验完成后的真实分析写入 `research_workspace/experiments/<ExpID>/analysis/analysis.md`。

- Change：<待填>
- Result：<待填>
- Finding：<待填>
- Next：<待填>
