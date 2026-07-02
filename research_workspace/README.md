# research_workspace 目录导航

本目录是项目的本地科研工作区，用于保存**当前研究状态、实验台账、实验产物索引、跨实验分析和论文/模块调研材料**。

> 建议把 `research_workspace/` 单独 `git init` 成一个独立仓库，和放代码的主仓库分开管理。好处：代码提交与实验产物提交互不干扰；体量大、更新频繁的实验证据不会撑爆代码仓库历史。

## 默认阅读顺序

1. `STATE.md`：当前真相、当前主线、废弃路径和下一步优先级。
2. `00-实验记录.md`：所有实验的总台账。
3. `experiments/<ExpID>/`：单次实验的证据入口（远程拉回的日志/结果 + 分析）。
4. `experiments/_cross_experiment/<RouteID>/`：跨实验、路线级或 baseline 对比分析。

## 目录结构

```text
research_workspace/
  README.md                 # 本文件
  STATE.md                  # 当前真相（每轮结束更新，下一轮先读它）
  00-实验记录.md            # 实验台账（每次实验的索引 + 关键指标）
  11-模块规划.md            # 模块级规划台账（可选）

  experiments/
    <ExpID>/                # 单次实验绑定的产物
      remote_artifacts/     # 远程拉回的最小证据集（日志/指标/结果/可视化）
      analysis/             # 该实验的分析、诊断、next steps
    _cross_experiment/
      <RouteID>/            # 跨实验、路线级复盘

  module_research/
    search_evidence/        # 模块/论文调研的外部检索证据
  papers/                   # 论文材料
  commands/                 # 远程命令、shell 片段
  archive/                  # 过时草案
```

## 最低关联字段

任何一条实验结论至少要能回指到：`SpecID + ExpID + Branch + Commit`（多次远程运行再补 `RunID`）。回指不到这些字段的结论，不要当成可靠结论。
