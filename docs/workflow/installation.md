# 项目接入与配置

把工作流安装进自己的科研仓库，再接入已有的训练、评估和结果输出方式。项目原来的代码目录可以保留，配置指向真实入口即可。功能与流程见[介绍页](README.md)。

本文按接入顺序组织：

- [准备条件](#准备条件)
- [安装工作流](#安装工作流)
- [项目适配](#项目适配)
- [运行配置](#运行配置)
- [远程连接](#远程连接)
- [科研记录](#科研记录)
- [可选 Hindsight](#可选-hindsight)
- [开始第一轮](#开始第一轮)
- [常见问题与更新](#常见问题与更新)

## 准备条件

- 一个可以用 Git 记录代码版本的科研项目。
- 本地 Python 3.11 或更高版本。
- 能在项目中工作的 Codex 或 Claude Code。
- 若要运行远程实验：本地控制端和远程主机使用 Linux；远程具备 Python 3.11+、SSH、Git、tmux、conda，以及项目所需依赖和数据。密码认证还需要本地 sshpass。

只有本地科研记录需求时，可以先接入相关功能。远程环境、数据下载和训练依赖需要按项目实际情况准备。

模板自带任务执行相关技能。中文方案与交付说明还需要在 agent 环境中安装 humanizer-zh；代码语义检索和资料查询分别使用 fast_context_search、smart-search-cli，需要另行配置。接入时可先让 agent 检查这些工具和技能是否可用，再补齐缺少的部分。

使用 Codex 执行长任务时，可以在 Goal 模式中引用准备好的 CSV。先确认当前版本支持 /goal；入口不可用时，按 [OpenAI Docs](https://developers.openai.com/codex/use-cases/follow-goals) 检查版本和功能设置。具体执行方式见[日常使用指南](usage.md#用-goal-模式执行-csv)。

## 安装工作流

以下命令中的项目目录需要替换。已有同名文件时，复制命令保留原文件；随后仍需合并项目规则和配置，跳过复制不代表已经完成适配。

```bash
git clone https://github.com/Zhou1317fe5/research-harness-workflow.git /tmp/research-harness-template
cd /path/to/your-project

cp -Rn /tmp/research-harness-template/.agents /tmp/research-harness-template/.claude /tmp/research-harness-template/.codex .
cp -Rn /tmp/research-harness-template/issues /tmp/research-harness-template/docs .
cp -n /tmp/research-harness-template/AGENTS.md /tmp/research-harness-template/CLAUDE.md .

mkdir -p research_workspace/experiments remote_artifacts
cp -n /tmp/research-harness-template/research_workspace/STATE.md /tmp/research-harness-template/research_workspace/CONCLUSIONS.md research_workspace/
cp -n /tmp/research-harness-template/remote_artifacts/README.md remote_artifacts/
cat /tmp/research-harness-template/.gitignore >> .gitignore

python -m pip install -e .agents/harness/remote/rrctl
```

复制时保留 .agents/skills 的符号链接。项目已有的训练脚本无需移动。

| 复制进来的内容 | 用途 |
|---|---|
| .agents/、.codex/、.claude/ | 工作流程序和 agent 使用的技能 |
| AGENTS.md、CLAUDE.md | 项目规则与研究背景 |
| docs/specs/、issues/ | 实验方案与执行清单模板 |
| docs/workflow/ | 接入、使用和配置教程 |
| research_workspace/ | 科研状态、实验分析和结论 |
| remote_artifacts/ | 远程运行取回的原始证据 |

检查合并后的 .gitignore，确认本地连接配置、凭据和原始产物不会进入代码提交。

## 项目适配

### 需要修改的文件

| 位置 | 需要补齐的内容 |
|---|---|
| AGENTS.md、CLAUDE.md 的“本项目补充” | 研究背景、基线、主指标、评测口径，以及代码和数据位置 |
| .agents/harness/config/project.toml | 训练与评估命令、checkpoint 衔接、进度与结果文件、要收集的产物和指标 |
| .agents/harness/config/profiles.json | 远程主机、端口、登录方式 |
| .agents/harness/config/.env | 远程环境名称、环境激活文件、工作目录；使用密码时在本地填写凭据 |
| 项目自己的训练、评估入口 | 如果还不支持独立输出目录、进度文件或结果摘要，补齐这些输入输出约定 |
| .gitignore | 合并模板规则，让本地凭据、原始运行产物和独立科研仓库保持各自的存放方式 |

科研记忆与可选 Hindsight 的设置见下文。日常接入主要改项目说明、配置和项目自己的入口；公共工作流脚本与技能通常可以沿用。

### 研究背景与路径

两份规则文件中的项目补充保持一致，至少让 agent 能回答：

- 当前研究什么问题，基线是什么，基线代码在哪里？
- 哪个指标决定实验结果，采用什么数据划分和评测口径？
- 训练和评估分别从哪里启动，需要哪些参数？
- 数据、预训练权重和已有 checkpoint 在哪里？
- 可使用哪些计算资源，有没有预算或运行限制？

已有项目规则先合并，保留原有约定。尤其要说明已有的测试方式、科学假设和禁止改变的评测条件。

训练代码可以继续放在原来的目录。模板里的 train.py、evaluate.py 是示例名称，需要替换成项目真实入口，包括影响科学结果的参数。

项目需要提供运行进度、最终结果和必要日志。已有 JSON 或 JSONL 输出时，通常只需配置字段；否则可以让 agent 补充结果摘要或适配现有输出。训练产生的 checkpoint 与评估使用的 checkpoint 应明确对应。

可以先这样让 agent 检查接入情况：

```text
请检查当前项目怎样接入这套科研工作流。
先核对所需工具和技能是否可用，再核对训练、评估命令和结果输出。
补齐项目说明与配置，沿用现有数据划分、指标定义和科学参数。
列出仍缺少的路径或环境信息，暂不启动远程实验。
```

## 运行配置

首次接入时复制：

```bash
cp -n .agents/harness/config/project.example.toml .agents/harness/config/project.toml
```

下面是一份“训练后评估”的配置示例。脚本名、参数、checkpoint 名称和指标字段都要换成项目自己的约定。只评估已有模型时，删除训练阶段，把评估的 checkpoint 参数指向已有权重，并去掉对本次训练输出的 requires；进度配置也要改为评估入口实际产生的内容。

```toml
version = 1

[[pipeline.stages]]
name = "train"
argv = ["python", "train.py", "--output-dir", "{output_root}"]
log = "train.log"
outputs = ["checkpoints/best.pt"]

[[pipeline.stages]]
name = "evaluate"
argv = ["python", "evaluate.py", "--checkpoint", "{output_root}/checkpoints/best.pt", "--output", "{output_root}/summary.json"]
log = "evaluate.log"
requires = ["checkpoints/best.pt"]
outputs = ["summary.json"]

[adapter]
progress_path = "progress.jsonl"
progress_format = "jsonl_last"
progress_count_field = "step"
first_step_min_count = 1
progress_finite_fields = ["loss"]
summary_path = "summary.json"
summary_format = "json"
summary_required_fields = ["metric"]
summary_finite_fields = ["metric"]

[[artifacts]]
path = "summary.json"
required = true

[[artifacts]]
path = "train.log"
required = false

[[artifacts]]
path = "evaluate.log"
required = false

[records]
summary_glob = "summary.json"
primary_metric = "metric"
dimensions = []
```

按下面的用途检查每一部分：

| 配置项 | 如何填写 |
|---|---|
| version | 保持为 1 |
| pipeline.stages | 按实际执行顺序列出阶段，每个 name 唯一 |
| argv | 项目真实命令，每个参数单独一项；脚本入口可以写成 ["bash", "tools/train.sh", "..."] |
| cwd | 可选，阶段的工作目录，相对项目根；省略时为项目根 |
| log | 阶段日志，相对本次输出目录 |
| requires / outputs | 阶段开始前需要、结束后应产生的文件，都相对本次输出目录 |
| adapter 中的 progress_* | 进度文件路径、格式，以及计数和需要检查的数值字段 |
| adapter 中的 summary_* | 最终结果路径、格式，以及必须存在和应为有限数值的字段 |
| artifacts | 需要取回的文件或目录；required=false 表示允许缺失 |
| records.summary_glob | 每次运行取回结果后，在哪个相对路径查找指标摘要 |
| records.primary_metric | 主指标字段，可以使用 metrics.accuracy 这样的嵌套字段名 |
| records.secondary_metric | 可选辅助指标；没有时省略 |
| records.dimensions | 需要保留的维度字段，例如 seed、dataset；没有时填 [] |

命令中的 {repo_root} 是项目根目录，{output_root} 是本次运行的独立输出目录，{run_id} 是本次运行标识。参数本身含有花括号时，用 {{ 和 }} 表示原样的括号。训练输出与评估输入应指向同一个 checkpoint。

上面的示例要求训练持续写入 progress.jsonl，每一行类似：

```json
{"step": 1, "loss": 0.84}
```

评估结束后写入 summary.json，例如：

```json
{"metric": 0.81}
```

这些只是输出格式示例，不是测试成绩。已有字段叫 accuracy 或其他名称时，修改配置去匹配真实输出。进度和结果分别支持 json、jsonl_last；后者读取最后一条记录。需要的字段都应出现在所选记录中。

纯文本日志仍然可以取回。若希望工作流自动识别其中的进度和指标，可以让 agent 为项目补充结构化输出或适配日志格式。项目的指标定义、单位和统计口径应保持一致。

多个预先确定的阶段可以按依赖顺序配置；前一阶段失败时，后续阶段停止。若下一步取决于中间的科学判断，应在任务清单中保留判断环节。

## 远程连接

```bash
cp -n .agents/harness/config/profiles.example.json .agents/harness/config/profiles.json
cp -n .agents/harness/config/.env.example .agents/harness/config/.env
chmod 600 .agents/harness/config/profiles.json .agents/harness/config/.env
```

profiles.json 示例：

```json
{
  "profiles": {
    "gpu": {
      "kind": "ssh",
      "ssh_argv": ["ssh", "-p", "22", "user@host"],
      "password_env": "SSH_PASSWORD"
    }
  }
}
```

将 gpu 换成便于识别的连接名称，将端口和 user@host 换成真实地址。也可以使用已有 SSH 主机别名。使用 SSH key 时移除 password_env；密码认证时，它的值保持为变量名 SSH_PASSWORD。

在本地 .env 中填写：

| 变量 | 何时需要 | 填什么 |
|---|---|---|
| SSH_PASSWORD | 密码认证时 | 登录密码 |
| REMOTE_CONDA_ENV | 远程运行 | 已创建的 conda 环境名称 |
| REMOTE_CONDA_SH | 远程运行 | 远程 conda.sh 的绝对路径 |
| REMOTE_REPO_ROOT | 远程运行 | 远程项目工作目录的绝对路径 |

.env 使用 shell 语法，空格和特殊字符需要正确引用。凭据只填在本地配置中，不写进项目说明、聊天或代码提交。模板的 Git 忽略规则已包含 profiles.json 和 .env。

远程环境应提前安装项目依赖并准备好数据。把数据集和预训练权重的位置写入项目说明，运行命令中的路径也要与之对应。

agent 根据这些信息准备每次实验的运行设置。实验使用的代码版本、GPU 和预算来自当次批准的任务，不需要在通用配置里重复维护。

## 科研记录

### 保存独立的科研历史

代码仓库保存实现，research_workspace 保存实验分析和研究结论。科研目录使用独立 Git 历史，便于在切换代码分支时继续查阅已有研究。

新接入且尚未初始化时运行：

```bash
git init -b main research_workspace
git -C research_workspace add STATE.md CONCLUSIONS.md
git -C research_workspace commit \
  -m "🎉 init(research): 初始化科研记录" \
  -m "Why: 保留实验分析与结论的独立历史" \
  -m "Why this works: 科研记录与代码分别提交" \
  -m "Remaining: 按需配置科研仓库远端"
git -C research_workspace rev-parse --show-toplevel
```

最后一条应返回当前项目的 research_workspace 目录。已有独立科研仓库时沿用它，保留现有记录。它的提交与推送需要单独进行，代码仓库的推送不会带上科研记录。

### 启用记录与召回

在项目根目录运行：

```bash
python .agents/harness/memory/install_memory_hooks.py
python .agents/harness/memory/research_memory.py status
```

安装命令默认配置 Codex 和 Claude Code；只使用一种时，可加 --host codex 或 --host claude。重新启动所用工具后生效。Codex 中还需通过 /hooks 检查并信任新定义，项目本身也需要受信任。

本地功能无需 Hindsight。要调整项目身份或记录范围，再复制可选配置：

```bash
cp -n .agents/harness/config/research-memory.example.json .agents/harness/config/research-memory.json
```

| 设置 | 建议填写方式 |
|---|---|
| project_id | 为项目取一个稳定、独立的名称；启用 Hindsight 时尤其需要区分项目 |
| sources | 默认包含研究状态、结论和实验分析；有额外分析文件时再补充 |
| hooks_enabled | 默认 true；改为 false 可暂停自动收集 |
| hindsight_enabled | 默认 false；需要远端服务时才启用 |
| context_chars / max_items | 默认 6500 字符和 8 条；先沿用，确有需要再调整 |

接入后，可以让 agent 查询当前方案、过去的实验发现和待讨论事项。记录如何参与研究，见[日常使用指南](usage.md#科研记录与召回)。

## 可选 Hindsight

官方安装仓库：[vectorize-io/hindsight](https://github.com/vectorize-io/hindsight)。先按仓库的 [Quick Start](https://github.com/vectorize-io/hindsight#quick-start) 安装并启动服务，再为当前项目准备 memory bank。

服务可用后，在 research-memory.json 中把 hindsight_enabled 设为 true，再在 .env 中填写：

| 变量 | 内容 |
|---|---|
| HINDSIGHT_API_KEY | 服务凭据 |
| HINDSIGHT_MCP_URL | 对应项目 memory bank 的 MCP 地址 |
| HINDSIGHT_API_URL、HINDSIGHT_BANK_ID | 不填写 MCP_URL 时，改用服务基础地址和 bank ID |

两种地址写法选一种。不同项目使用不同 project_id；需要隔离时使用不同 memory bank。未启用远端服务时，本地记录和查询仍可使用。

## 开始第一轮

配置完成后，从项目根目录运行：

```bash
python .agents/harness/pipeline/run_pipeline.py --check
```

这条命令只解析配置，不启动训练。通过后，再让 agent 用真实日志和结果样例检查进度与指标是否能被识别。

第一次实际运行选择一个预算明确、结果容易判断的小任务，按正常工作流完成必要验证和远程执行。检查它能否真实启动、产生有效进展、取回结果，并让分析关联到本次代码与实验依据。配置解析通过不等于训练已经验证通过。

提出需求和查看交付的方法见[日常使用指南](usage.md)。

## 常见问题与更新

| 现象 | 先检查什么 |
|---|---|
| 找不到项目配置 | 是否已将 .example 文件复制为实际配置文件名 |
| 找不到 rrctl，或更新后提示不支持某个操作 | 是否在当前 Python 环境重新安装了项目里的 rrctl |
| 配置解析通过，但训练启动失败 | 命令、数据和环境是否真实存在，项目依赖是否齐全 |
| 实验在跑，但无法识别首步进展 | 进度文件路径、格式、计数字段是否匹配实际输出 |
| 评估找不到 checkpoint | 训练产出与评估输入是否一致，文件是否存在 |
| 结果取回了，但没有主指标 | summary_glob 和 primary_metric 是否匹配真实结果 |
| 科研记录没有自动更新 | 工具是否重启、项目是否受信任、记录入口是否已启用；Codex 还需检查 /hooks |

### 更新已有项目

更新前保存当前代码和科研记录。合并新版工作流文件时保留项目背景、实际配置和已有实验，不用示例文件覆盖它们。

更新后重新安装项目中的 rrctl，并按本文的[科研记录](#科研记录)一节重新安装记录入口。若旧项目的配置仍平铺在 .agents/harness/ 下，将它们移入 config/，保留原值；旧的 remote-run-control 源码目录改用 .agents/harness/remote/rrctl/。

核对配置后先做一次小任务验证。正在进行的实验沿用已有任务记录和远程运行，不因更新而重复启动。

### 迁移已有科研记录

如果旧项目已在代码仓库中跟踪这些文件，先保留备份，并确认现有记录已提交到独立科研仓库。然后将 /research_workspace/ 加入主仓库的 .gitignore，再停止主仓库对它的跟踪：

```bash
git ls-files -- research_workspace
git rm -r --cached -- research_workspace
```

第二条仅在第一条有输出、且备份和独立提交均已确认后使用。它保留磁盘上的文件；提交前检查改动范围。仍跟踪旧科研目录的代码分支也需要合入这次调整。
