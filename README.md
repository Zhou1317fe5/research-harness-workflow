# 面向科研的 Harness Engineering 工作流

一套用 **Claude Code + Codex** 做深度学习科研实验的轻量工作流骨架：讨论和判断交给 Claude，长任务的代码执行交给 Codex，最终科研判断留给人。

> 完整的思想、工作流讲解和适配指南见 [面向科研的harness-engineering](docs/workflow/面向科研的harness-engineering.md)。
>
> 本工作流在一篇社区实战教程的基础上做了优化，主要针对科研实验场景，并显著降低了 token 消耗（精简挂载的 skill、分层管理上下文）。

## 这套仓库包含什么

```text
.claude/skills/       # Claude Code 的 skills（规划侧：讨论、生成 spec/CSV）
.codex/skills/        # Codex 的 skills（执行侧：跑 CSV、远程运行、拉结果、分析、提交）
AGENTS.md             # 执行侧（Codex）规则
CLAUDE.md             # 规划侧（Claude）路由 + 硬门禁，含 Claude/Codex 职责边界
docs/superpowers/specs/TEMPLATE-spec.md   # 写方案 spec 的模板
issues/TEMPLATE.csv                       # 任务 CSV 的字段模板（状态机 schema）
research_workspace/   # 科研产物工作区的空壳骨架（建议单独 git 成独立仓库）
docs/workflow/        # 工作流教程文章
```

## 快速开始

1. **放进项目**：先给你的项目 `git init`（Codex 每完成一步会自动 commit，没有 git 闭环会断），再把这些文件放进项目根目录。
2. **改项目事实**：打开 `CLAUDE.md` 和 `AGENTS.md`，把末尾的项目事实换成你自己的——研究方向、baseline、主/辅指标、远程环境、artifact 目录、哪些结论必须有证据支撑。路由规则和硬门禁本身不用动。
3. **装两个搜索工具**（可选但推荐）：
   - `fast_context_search`：本地代码语义搜索，安装见 [面向小白的harness engineering实战（科研导向的infra搭建，本质是篇二创） (linux.do)](https://linux.do/t/topic/2260154/1)
   - `smart-search-cli`：可保存证据的外部检索，安装见  [smartsearch](https://github.com/konbakuyomu/smartsearch)
4. **跑一轮**：开 Claude 讨论需求 → 生成 spec → 批准后转成 CSV → 切 Codex `/goal @issues/xxx.csv` 执行。第一轮别挑复杂模块，选个 baseline 复现或简单实验，先验证这条链能不能闭环。

## 一轮实验怎么走

```text
① 收集信息 → ② 讨论方案(Claude/brainstorming) → ③ 转 CSV → ④ 代码实施 + 运行前审查
                                                          │
⑦ 分析 + 下一步(Claude) ← ⑥ 拉回日志/结果 ← ⑤ 推远程训练(tmux) ┘
      └→（更新 STATE.md 当前真相，进入下一轮）
```

## 适配到自己的任务

最需要动的是几个远程/日志相关的 skill——它们里面写死了示例项目的约定（日志路径、指标名、脚本解析、台账格式）。最省事的做法：开一个会话，让 AI 把下面这几个 skill 读一遍，对照你的任务改：

- `autodl-remote-run-snippet`：从 `scripts/` 解析训练/评估脚本、拼远程命令 → 对齐你的脚本名、参数、远程环境。
- `autodl-remote-pull-manifest`：固定的"最小拉取清单" → 改成你代码实际写日志/结果的路径和文件名。
- `exp-results-ingest-local`：解析结果文件里的指标写台账 → 对齐你的指标名和结果 schema。
- `exp-analysis-hen`：基于指标做 H→E→N 分析 → 换成你课题的指标和分析逻辑。

主线：**你的实验代码把日志/指标/结果写到哪、用什么文件名，这几个 skill 就得照着那套路径去拉、去解析。** 两头对齐，自动化才接得上。详见教程的"如何适配"一节。

## ⚠️ 安全

- **不要提交任何真实凭证**。远程连接信息（主机、端口、密码）放本地 `scripts/.env`，已在 `.gitignore` 中。
- skill 示例里的主机名、密码均为占位符（`user@host` / `your_password`），使用时替换成自己的。
