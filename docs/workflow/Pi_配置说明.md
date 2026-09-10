# Pi 安装与配置

选择 Pi 使用本工作流时，先完成本页的全局环境配置，再从科研项目根目录启动 Pi。项目文件、远程实验和 Research Memory 的接入步骤见[项目接入与配置](installation.md)。

没有安装 Pi 时，先运行：

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi --version
```

需要安装的包如下。前五个是本工作流的基础配置，后两个是推荐增强。

| 包 | 用途 | 配置入口 |
| --- | --- | --- |
| `pi-mcp-adapter` | 接入 fast-context等通用 MCP | `/mcp setup`、`/mcp` |
| `@juicesharp/rpiv-ask-user-question` | 向用户展示结构化问题与选项 | 安装后默认可用 |
| `@narumitw/pi-goal` | 长任务目标保持与继续执行 | `/goal` |
| `@juicesharp/rpiv-advisor` | 主 Executor 按需咨询更强的模型 | `/advisor` |
| `pi-sub-agent` | 启动独立子代理，包括 Scientific Reviewer | `/sub-agent-settings`；项目 Agent 看 `.pi/agents/` |
| `@ff-labs/pi-fff` | 本地模糊文件搜索、内容搜索和文件补全 | `/fff-health`、`/fff-mode` |
| `@cortexkit/pi-magic-context` | 当前会话的 Historian、压缩与 session-history | 官方 setup 向导、`/ctx-status` |

下面均为全局安装：

```bash
pi install npm:pi-mcp-adapter
pi install npm:@juicesharp/rpiv-ask-user-question
pi install npm:@narumitw/pi-goal
pi install npm:@juicesharp/rpiv-advisor
pi install npm:pi-sub-agent
pi install npm:@ff-labs/pi-fff
pi install npm:@cortexkit/pi-magic-context
```

安装后重启 Pi，用 `pi list` 确认 packages 已注册。

项目侧需要保留模板中的 `.codex/skills`、`.agents/skills` 和 `.pi/`。即使选择 Pi，普通科研 skills 仍通过 `.agents/skills` 读取共享内容。按通用接入教程复制基础文件后，再复制 Pi 项目文件：

```bash
cp -Rn /tmp/research-harness-template/.pi /path/to/your-project/
cd /path/to/your-project
pi
```

已有同名配置时合并需要的字段，保留模板的软链接。后续的 Pi 交互命令都在这个项目会话中执行。

先用 `/login` 配好 provider，再用 `/model` 选择主 Executor。日常执行可以选择便宜或高额度模型。使用 `newapi` 之类的自定义 provider 时，先按 [Pi 模型配置文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md) 配置 `~/.pi/agent/models.json`，确认模型出现在 `/model` 列表中。密钥使用环境变量引用，不写入项目文件。

`pi-mcp-adapter` 安装后还需要 MCP 服务配置。在 Pi 中运行：

```text
/mcp setup
/mcp
```

如果 Codex 已经配好了 fast-context，可在 setup 中导入现有 Codex 配置；不需要重新填写一份凭据。新环境按 [fast-context 官方仓库](https://github.com/SammySnake-d/fast-context-mcp)安装服务并填写启动参数。用 `/mcp` 查看服务，再让 Pi 调用 fast-context 查找一个项目文件，确认实际可用。

Hindsight 的正式接入由项目 Research Memory 管理，不需要通过这里新增全局 Hindsight 工具。已有的全局 Hindsight MCP 可以保留作手动诊断，但不作为科研记忆的正式写入入口。

`rpiv-ask-user-question` 无需单独配置模型、服务或 API Key，重启后即可使用 `ask_user_question`。默认快捷键是 `Ctrl+]`，用于收起或展开问题窗口。如果键盘布局不方便使用，可在 `~/.config/rpiv-ask-user-question/config.json` 中设置：

```json
{
  "collapseKey": "alt+o"
}
```

`pi-goal` 直接使用主 Executor 的模型。输入 `/goal` 打开管理界面，或用下面的方式开始一个明确的长任务：

```text
/goal 完成当前功能修改，并通过相关验证
/goal status
/goal pause
/goal resume
```

先保留默认配置即可：自动工作上限 25 次响应、连续 3 次无进展时暂停、Managed run RPC 关闭。需要调整时，在 `/goal` 的 Settings 中修改。配置文件为 `~/.pi/agent/pi-goal.json`，缺少这个文件时包会直接使用默认值。

`rpiv-advisor` 安装后，需要运行一次 `/advisor`，选择更强的 Advisor 模型以及适用的 reasoning effort；支持时可选 `high`。选择会保存在 `~/.config/rpiv-advisor/advisor.json`。

普通软件项目使用包原始策略；本科研工作流会自动使用科研专用触发策略。因此不要把科研 guidance 写到全局 `advisor.json`。Advisor 用于方案、冲突、困难 Debug 和实验解释等决策，Scientific Reviewer 仍通过独立子代理执行。

配置 Scientific Reviewer 时，直接编辑 `.pi/agents/scientific-reviewer.md` 的 YAML frontmatter，填写所需的模型和 thinking level。未填写时分别继承主 Pi 会话的模型和 thinking level。例如：

```yaml
---
name: scientific-reviewer
description: Read-only isolated execution of the supplied review task
tools: read, grep, find, ls
# 可选：不填写则继承主 Pi 会话配置
# model: provider/model-id
# thinking: high
---
```

项目已经提供只读 profile 和 Reviewer task 的传递规则，不需要另写一份科研审查 prompt。

`pi-fff` 建议保持默认 `tools-and-ui` 模式。它增加 `fffind`、`ffgrep`、`fff-multi-grep` 和 FFF 文件补全，同时保留 Pi 原有工具。需要显式指定时，可以这样启动：

```bash
pi --fff-mode tools-and-ui
```

在项目里用 `/fff-health` 查看索引状态，用 `/fff-rescan` 重新扫描。通常不需要另配数据库路径。FFF 负责本地文件与内容搜索；理解科研实现和跨模块调用链时，继续使用 fast-context。

Magic Context 按“只管理当前会话上下文”的方式配置。全局安装 package 后运行官方向导：

```bash
npx @cortexkit/magic-context@latest setup --harness pi
```

向导中为 Historian 选择便宜或高额度模型，例如 `gpt-5.6-luna`。这是 Pi 中已配置的 `provider/model` 示例，可以换成自己可用的模型。

| 向导项目 | 选择 |
| --- | --- |
| Historian | 便宜或高额度模型，例如 `gpt-5.6-luna` |
| Dreamer | No |
| Sidekick | No |
| Embedding | 可以先任选一项，下一步统一关闭 |

然后编辑全局配置：

```bash
nano ~/.config/cortexkit/magic-context.jsonc
```

使用以下配置：

```json
{
  "$schema": "https://raw.githubusercontent.com/cortexkit/magic-context/master/assets/magic-context.schema.json",

  "enabled": true,

  "historian": {
    "pi": {
      "model": "gpt-5.6-luna"
    }
  },

  "compaction": {
    "enabled": true
  },

  "execute_threshold_percentage": 70,
  "history_budget_percentage": 0.15,

  "commit_cluster_trigger": {
    "enabled": false
  },

  "system_prompt_injection": {
    "enabled": true
  },

  "memory": {
    "enabled": false,
    "auto_search": {
      "enabled": false
    },
    "git_commit_indexing": {
      "enabled": false
    }
  },

  "embedding": {
    "provider": "off"
  },

  "dreamer": {
    "disable": true
  },

  "sidekick": {
    "disable": true
  },

  "todowrite": {
    "enabled": false
  }
}
```

这套配置启用 Magic Context 自己的压缩，触发阈值设为 70，历史预算为可用上下文的 15%。跨会话 Memory、Auto Search、Git memory indexing、Embedding、Dreamer、Sidekick、commit-cluster trigger 和 todowrite 全部关闭。如果项目已有 `.cortexkit/magic-context.jsonc`，也要检查它没有重新开启这些功能。

Magic Context 配好后，在使用它的项目中关闭 Pi 原生自动压缩。调整项目的 `.pi/settings.json`，它会覆盖全局设置，其中应有：

```json
{
  "compaction": {
    "enabled": false
  }
}
```

模板另有 `.pi/settings.magic-context.example.json` 供参考，它不会自动关闭未安装 Magic Context 的项目的原生压缩。只合并这个字段，不要覆盖整个 settings 文件。从项目根运行以下命令，保留其他配置，只修改 compaction：

```bash
python3 - <<'PY'
import json
from pathlib import Path

p = Path(".pi/settings.json")
d = json.loads(p.read_text()) if p.exists() else {}
if not isinstance(d, dict):
    raise SystemExit("Pi settings 必须是 JSON 对象")
compaction = d.setdefault("compaction", {})
if not isinstance(compaction, dict):
    raise SystemExit("compaction 必须是 JSON 对象")
compaction["enabled"] = False
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
print(json.dumps({"compaction": d["compaction"]}, indent=2, ensure_ascii=False))
PY
```

注意两处 `compaction.enabled` 的归属：Magic Context 配置中是 `true`，Pi settings 中是 `false`。

最后运行官方检查：

```bash
npx @cortexkit/magic-context@latest doctor --harness pi
```

然后重启 Pi，在项目中运行 `/ctx-status`。确认 Historian 使用预期模型、当前会话压缩已启用，并核对关闭项：

| 能力 | 状态 |
| --- | --- |
| Historian / 当前会话压缩 | 开启 |
| session-history | 保留 |
| Cross-session Memory | 关闭 |
| Auto Search | 关闭 |
| Git memory indexing | 关闭 |
| Embedding | 关闭 |
| Dreamer | 关闭 |
| Sidekick | 关闭 |

长期科研记忆继续按[项目接入与配置](installation.md)启用 Research Memory 和 Hindsight。项目身份、bank、来源范围和凭据仍由项目配置管理。关闭 Magic Context 的 memory 不会代替或关闭这条科研记忆链。

项目 Research Memory 同时识别 `PI_SUB_AGENT_DEPTH` 和 `MAGIC_CONTEXT_PI_SUBAGENT`，后台 Historian 与 Reviewer 的提示词不会作为用户来源采集。历史快照放在真实会话之前；工具完成后按版本刷新，同一批工具只扫描一次。Hindsight 默认手工同步精选内容，普通消息不全量上传。

更新项目扩展后，在现有 Pi 会话中执行 `/reload`，或开启新会话，使新的 TS 注入逻辑生效。Python 会先清除旧版扩展的危险注入，保留本地采集；新版扩展握手后恢复历史快照展示。身份隔离和停用设置在下次回调就生效，不需要终止正在运行的实验。

```text
当前 Pi 会话上下文
→ Magic Context

长期科研方向 / 决策 / 结论
→ Research Memory + Hindsight
```

smart-search 按[官方仓库](https://github.com/blxzer77/smart-search)安装成 CLI，在启动 Pi 的同一环境确认 `smart-search --version` 可执行即可。本工作流不要求为它再安装 Pi skill、Codex skill 或 Extension。

包的后续配置可查阅：[Pi](https://pi.dev/)、[MCP adapter](https://github.com/nicobailon/pi-mcp-adapter)、[Ask User Question](https://github.com/juicesharp/rpiv-mono/tree/main/packages/rpiv-ask-user-question)、[Advisor](https://github.com/juicesharp/rpiv-mono/tree/main/packages/rpiv-advisor)、[pi-goal](https://www.npmjs.com/package/@narumitw/pi-goal)、[pi-sub-agent](https://github.com/HamdiMaz/pi-sub-agent)、[FFF](https://github.com/dmtrKovalenko/fff/tree/main/packages/pi-fff)、[Magic Context](https://github.com/cortexkit/magic-context)。
