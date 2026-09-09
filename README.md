# research-harness-workflow

可按项目复制使用的科研工作流模板。用 Spec 明确实验目标，用任务 CSV 跟踪执行，
用本地科研记录与可选 Hindsight 保存和召回结论。

## 文档

[docs/workflow](docs/workflow/README.md) 介绍工作流与宿主配置：

- [功能、理念与流程](docs/workflow/README.md)
- [项目接入与配置](docs/workflow/installation.md)
- [Pi 安装与配置](docs/workflow/Pi_配置说明.md)
- [日常使用指南](docs/workflow/usage.md)

## 项目组成

| 位置 | 内容 |
|---|---|
| .codex/skills/、.claude/skills/ | 两套同步的技能与执行协议 |
| .agents/skills | 指向 .codex/skills 的发现入口 |
| .pi/ | Pi 项目配置、扩展与 Reviewer 入口 |
| .agents/harness/ | 程序实现、配置与模板 |
| issues/、docs/specs/ | 任务台账与实验方案 |
| research_workspace/ | 科研状态、结论和实验分析 |
| remote_artifacts/ | 原始运行证据 |

本仓库基于 [Missions](https://github.com/flowing-water1/Missions) 整理科研执行流程。
