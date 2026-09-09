# 接入与配置教程：装好工具，再接通自己的代码

下面从一个常见情况出发：项目已经能训练、能评估，但命令、数据路径和结果整理还需要自己照看。我们要把这些现成入口接进工作流，让 agent 能按同样的方式运行，并知道去哪里判断进度、读取结果。

文中用 `train.py` 和 `evaluate.py` 举例。你的入口放在 `tools/`、`scripts/` 或其他目录都可以，填写真实路径即可。

整次接入按这个顺序做：

1. 准备环境，把工作流文件放进项目。
2. 补上项目背景，接好训练与评估命令。
3. 填写远程连接，让工作流认识进度和结果。
4. 启用科研记录，再用一次小任务检查整条链。

Hindsight 可以在本地流程跑顺后再接。已有项目的升级和科研记录迁移放在文末。

## 先准备好这些东西

本地需要 Python 3.11+，以及能在项目里工作的 Codex 或 Claude Code。项目也要有 Git 历史；如果还是一个普通目录，先初始化 Git 并保存已有代码，后面才能关联每次实验所用的版本。

远程实验这边，rrctl 的本地控制端和远程主机使用 Linux。远程要有 Python 3.11+、SSH、Git、tmux、conda，以及项目自己的依赖和数据。使用密码登录时，本地还需要 sshpass。

模板带有任务执行相关技能，`humanizer-zh` 需要在 agent 环境中另外安装。代码语义检索与外部资料查询可以按需接入 `fast-context-mcp` 和 `smart-search`；它们是可选工具，不是首次接入的前置条件。

下文的 `npx` 安装命令需要 Node.js / npm；其他运行依赖按所选工具仓库的说明准备。

使用 Codex 跑长任务时，再确认当前版本可以使用 `/goal`。入口没有显示的话，按 [OpenAI Docs](https://developers.openai.com/codex/use-cases/follow-goals) 检查版本和功能设置。

## 安装清单

先把模板自带的部分和外部工具分开。Mission、rrctl、科研记录程序会随项目模板一起复制；下面列出的外部工具，再按对应仓库安装。

| 工具 | 是否需要另装 | 在这里做什么 | 安装入口 |
|---|---|---|---|
| Mission 及配套技能 | 随模板提供 | 讨论方案、生成 CSV、执行与验收 | 按下一节复制本工作流模板 |
| rrctl | 源码随模板提供，需要安装 Python 包 | 启动、等待和收集远程实验结果 | `python -m pip install -e .agents/harness/remote/rrctl` |
| Humanizer-zh | 本项目要求准备的外部 skill | 整理中文方案和交付说明 | [op7418/Humanizer-zh](https://github.com/op7418/Humanizer-zh) |
| fast-context-mcp | 可选 MCP | 找相关模块、理解已有实现与调用链 | [SammySnake-d/fast-context-mcp](https://github.com/SammySnake-d/fast-context-mcp) |
| smart-search | 可选 CLI 和 skill | 搜索论文、工具文档并获取来源 | [konbakuyomu/smartsearch](https://github.com/konbakuyomu/smartsearch) |
| lite-arch / lite-arch-recall | 可选 skill | 记录架构取舍，修改设计前召回已有决定 | [flowing-water1/lite-arch](https://github.com/flowing-water1/lite-arch) |
| Hindsight | 可选服务 | 辅助检索历史科研材料 | [vectorize-io/hindsight](https://github.com/vectorize-io/hindsight) |

选用的外部工具按各自 README 安装，确认当前 agent 能发现对应的 skill 或 MCP。只使用一种 agent 时，安装到它能读取的位置；两种都用时，分别检查可用性。

### 外部工具装好后，还要配什么

Humanizer-zh 的仓库提供了安装命令：

```bash
npx skills add https://github.com/op7418/Humanizer-zh.git
```

按安装器提示选择使用的 agent。安装后，可以让 agent 用 `humanizer-zh` 整理一小段文字，确认技能能被读取。

选用 fast-context-mcp 时，在所用 agent 的 MCP 设置中登记。启动命令、参数和认证方式按[所选仓库](https://github.com/SammySnake-d/fast-context-mcp)的 README 填写。然后让 agent 用 `fast_context_search` 查一个已知功能的位置，确认能返回当前项目的相关文件。

选用 smart-search 时，安装后使用它自己的向导配置搜索服务，并安装 `smart-search-cli` skill：

```bash
smart-search setup
smart-search doctor --format json
```

向导中填写所选服务的地址、凭据和模型等信息，具体选项以[仓库说明](https://github.com/konbakuyomu/smartsearch)为准。`doctor` 用来检查当前配置是否可用。搜索服务的设置由 smart-search 管理，与项目的 GPU 连接配置分开。

lite-arch 是可选项。按[仓库说明](https://github.com/flowing-water1/lite-arch)安装 `lite-arch` 和 `lite-arch-recall` 两个技能目录，重启 agent 后检查是否可用。还没有架构记录的新项目，首次召回没有结果是正常的；后续有明确的架构决定时再记录。

Hindsight 需要先启动服务，再填写项目的连接信息，步骤见[可选 Hindsight](#可选-hindsight)。这些外部工具的凭据都留在各自的本地配置中。

## 项目适配清单

准备工具之后，真正需要你按项目修改的是这些地方：

| 位置 | 需要修改什么 | 完成后检查什么 |
|---|---|---|
| `AGENTS.md`、`CLAUDE.md` 的“本项目补充” | 研究背景、基线、指标、数据与代码路径 | 两份说明一致，路径与当前项目相符 |
| `.agents/harness/config/project.toml` | 真实运行命令、checkpoint 衔接、进度与结果字段、产物清单 | 配置能解析，并能对应项目实际输出 |
| `.agents/harness/config/profiles.json` | 主机、端口、登录方式和连接名称 | 能使用约定方式连接正确的远端 |
| `.agents/harness/config/.env` | 远程环境与工作目录、需要的本地凭据 | 环境和路径存在，凭据没有进入提交 |
| 项目自己的训练、评估入口 | 输出目录参数、进度文件、结果摘要、失败退出行为 | 正常运行能识别进度和结果，失败不会被报告成成功 |
| `.agents/harness/config/research-memory.json` | 按需调整项目身份、来源范围和可选服务 | 本地记录可用，启用远端服务后能找到正确项目的来源 |
| `.gitignore` | 合并模板的忽略规则 | 本地凭据、原始产物和独立科研仓库没有混入代码提交 |

已有入口满足下面的约定时，配置对应路径和字段即可。缺少的能力可以补在项目自己的入口或适配脚本里。

| 代码需要提供的能力 | 如何适配 |
|---|---|
| 指定本次输出目录 | 训练和评估接受输出路径参数，每次运行写入各自的目录 |
| 输出真实进度 | 写出 JSON 或 JSONL 进度，包含实际计数及需要检查的数值，例如 step、loss |
| 明确 checkpoint 的传递 | 训练写出的文件与评估读取的文件一致；动态文件名由项目入口明确传递 |
| 输出最终结果 | 评估结束后保存真实指标摘要，字段名与 `adapter`、`records` 的配置对应 |
| 正确报告失败 | 失败时返回非零退出码，或保留可被检查出的失败证据；不写伪造的成功结果 |

下面逐项说明这些文件怎样填。

## 1. 把工作流文件放进项目

先拉取模板，再进入自己的项目目录：

```bash
git clone https://github.com/Zhou1317fe5/research-harness-workflow.git /tmp/research-harness-template
cd /path/to/your-project
```

下面这些文件负责工作流和项目规则。复制命令会保留已有的同名文件；已有规则仍需要合并，不能把“没有覆盖”当成“已经接好”。

```bash
cp -Rn /tmp/research-harness-template/.agents /tmp/research-harness-template/.claude /tmp/research-harness-template/.codex .
cp -Rn /tmp/research-harness-template/issues /tmp/research-harness-template/docs .
cp -n /tmp/research-harness-template/AGENTS.md /tmp/research-harness-template/CLAUDE.md .

mkdir -p research_workspace/experiments remote_artifacts
cp -n /tmp/research-harness-template/research_workspace/STATE.md /tmp/research-harness-template/research_workspace/CONCLUSIONS.md research_workspace/
cp -n /tmp/research-harness-template/remote_artifacts/README.md remote_artifacts/
cat /tmp/research-harness-template/.gitignore >> .gitignore

python -m pip install -e .agents/harness/remote/rrctl
```

复制时保留 `.agents/skills` 的符号链接。原有训练代码继续放在原处。

这一步结束后，先认识几个以后会经常看到的位置：

```text
AGENTS.md / CLAUDE.md    项目规则和研究背景
.agents/harness/config/ 项目的运行与连接配置
docs/specs/             讨论形成的实验方案
issues/                 任务 CSV 和交付说明
research_workspace/     实验分析与研究结论
remote_artifacts/       从远端取回的原始证据
```

模板的忽略规则会排除本地凭据和原始产物。合并之后看一眼 `.gitignore`，确认没有被项目原来的规则抵消。

## 2. 先把项目背景告诉 agent

最容易被略过的是 `AGENTS.md` 和 `CLAUDE.md` 末尾的“本项目补充”。

如果这里只写“这是一个深度学习项目”，agent 每次都要重新找训练入口、猜哪个数据划分正在使用。把固定信息写清楚，可以省去这类重复确认。

两份文件保持一致，补上这些内容：

- 当前研究问题、基线和主指标。
- 数据划分与评测口径，哪些约定需要沿用。
- 训练、评估入口，以及影响结果的必要参数。
- 数据、预训练权重和已有 checkpoint 的位置。
- 可用的计算资源和项目中的运行限制。

已有规则先合并。项目自己的科学假设、验证方式和路径都保留下来。

如果不想手工整理，可以在项目中给 agent 这段话：

```text
请读取当前项目，帮我接入这套科研工作流。
先检查所需工具和技能，再核对训练、评估入口和结果输出。
补齐 AGENTS.md、CLAUDE.md 的项目说明，以及工作流配置。
沿用现有数据划分、指标定义和科学参数。
缺少的路径或环境信息请列出来。本阶段完成接入与本地配置检查，暂不启动远程实验。
```

后面的命令和配置，就是这次接入需要完成的内容。你可以自己填，也可以用它们核对 agent 的改动。

## 3. 让工作流调用项目原来的命令

先复制配置模板：

```bash
cp -n .agents/harness/config/project.example.toml .agents/harness/config/project.toml
```

这份 `project.toml` 主要回答两个问题：按什么顺序运行，运行后去哪里找输出。

下面是“训练后评估”的完整示例：

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

从上往下读就能看出这条链：先训练，得到 `checkpoints/best.pt`；再把这个文件交给评估，生成 `summary.json`。

先改 `argv`，把示例脚本名和参数换成项目真实命令。每个参数单独一项，shell 脚本入口也可以写成 `["bash", "tools/train.sh", "..."]`。阶段需要在其他目录执行时，用 `cwd` 指定相对项目根的目录。

然后对齐 checkpoint。`outputs` 写这一阶段应产生的文件，`requires` 写下一阶段开始前需要的文件。两者都相对本次输出目录，评估读到的应当就是这次训练生成的模型。

`{output_root}` 会替换成这次运行的独立输出目录。`{repo_root}` 和 `{run_id}` 分别表示项目根和运行标识；参数本身需要花括号时，用 `{{`、`}}` 表示字面量。

**配置里写了进度文件，并不会自动让训练代码产生它。** 这一步需要核对项目实际输出。

上面的配置约定，训练向 `progress.jsonl` 持续写入记录，例如：

```json
{"step": 1, "loss": 0.84}
```

评估结束后，向 `summary.json` 写入结果，例如：

```json
{"metric": 0.81}
```

这些数值只是格式示例。已有结果字段叫 `accuracy` 时，就修改配置去读取 `accuracy`；嵌套字段也可以写成 `metrics.accuracy`。原来的指标单位和统计方式要保留。

`adapter` 这一段告诉工作流怎样读取进度和结果。`json` 读取一个 JSON 对象，`jsonl_last` 读取最后一条记录。计数和需要检查的字段应出现在相应记录中。只有文本日志的项目，可以让 agent 补充结构化摘要或适配现有格式。

`artifacts` 决定取回哪些文件。通常先保留结果摘要和必要日志，大型 checkpoint、逐样本输出是否取回，按本次分析需要决定。`required = false` 表示文件允许缺失。

`records` 决定怎样整理指标：`summary_glob` 指向取回的结果文件，`primary_metric` 选择主指标。有辅助指标再加 `secondary_metric`；想在记录中保留 seed、dataset 等信息，就把真实字段名放进 `dimensions`。

需要回头查参数时，可以用这张表对照：

| 参数 | 填写内容 |
|---|---|
| `version` | 保持为 1 |
| `pipeline.stages[].name / argv` | 唯一的阶段名，以及项目真实命令的参数数组 |
| `pipeline.stages[].cwd / log` | 相对项目根的工作目录，以及相对本次输出目录的日志路径 |
| `pipeline.stages[].requires / outputs` | 阶段开始前需要、结束后应产生的文件，路径相对本次输出目录 |
| `adapter.progress_path / progress_format` | 进度文件及其格式：`json` 或 `jsonl_last` |
| `adapter.progress_count_field / first_step_min_count` | 实际计数字段，以及首步检查需要达到的计数 |
| `adapter.progress_finite_fields` | 应为有限数值的进度字段，例如 loss |
| `adapter.summary_path / summary_format` | 最终结果文件及格式 |
| `adapter.summary_required_fields / summary_finite_fields` | 结果必须包含的字段，以及需要检查为有限数值的字段 |
| `artifacts[].path / required` | 要取回的文件或目录，以及是否必须存在 |
| `records.summary_glob` | 相对单次运行产物目录的结果文件匹配路径 |
| `records.primary_metric / secondary_metric` | 主指标与可选辅助指标字段 |
| `records.dimensions` | 需要保留的实际维度字段，没有时填空数组 |

只评估已有模型时，删除训练阶段，把评估参数指向已有权重，并去掉对本次训练输出的 `requires`。进度配置也要对应评估实际写出的内容，不能继续等一个不存在的训练 loss。

多个预先确定的阶段可以按顺序配置，前一阶段失败时后续阶段停止。如果下一步需要先判断实验结果，就把这个判断留在任务清单中。

## 4. 填远程连接，确认路径真的对得上

运行命令接好之后，再告诉工作流去哪里运行：

```bash
cp -n .agents/harness/config/profiles.example.json .agents/harness/config/profiles.json
cp -n .agents/harness/config/.env.example .agents/harness/config/.env
chmod 600 .agents/harness/config/profiles.json .agents/harness/config/.env
```

`profiles.json` 填登录方式：

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

把主机、端口和用户替换成真实信息，也可以使用已有的 SSH 主机别名。`gpu` 是这条连接的名称。使用 SSH key 时移除 `password_env`；使用密码时，它的值保留为变量名 `SSH_PASSWORD`。

实际环境信息放在本地 `.env`：

| 变量 | 填什么 |
|---|---|
| SSH_PASSWORD | 密码认证时填写登录密码 |
| REMOTE_CONDA_ENV | 已创建的远程 conda 环境名称 |
| REMOTE_CONDA_SH | 远程 conda.sh 的绝对路径 |
| REMOTE_REPO_ROOT | 远程项目工作目录的绝对路径 |

`.env` 使用 shell 语法，空格和特殊字符需要正确引用。凭据留在本地文件中，不贴进对话，也不写进项目说明。模板会忽略 `profiles.json` 和 `.env`。

这里容易出现的是路径没对齐：项目说明写了一份数据位置，运行参数却还指向旧目录；或者 conda 环境能激活，但项目依赖没有装齐。接入时把这些信息一起核对，后面的启动才有依据。

每次实验用哪些 GPU、跑多久、采用哪个代码版本，由当次任务确定，不需要在这些通用配置里再维护一份实验清单。

## 5. 给科研记录留一个独立的位置

代码可能经常切分支，但我们仍然需要查到之前的实验分析。因此 `research_workspace` 使用独立的 Git 历史，代码仓库忽略这个目录。

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

最后一条应返回当前项目的 `research_workspace` 目录。已有独立科研仓库就继续使用它，保留原来的记录。代码和科研仓库分别提交、分别推送。

接着安装记录入口：

```bash
python .agents/harness/memory/install_memory_hooks.py
python .agents/harness/memory/research_memory.py status
```

默认会配置 Codex 和 Claude Code。只用一种时，加 `--host codex` 或 `--host claude`。安装后重启所用工具；Codex 还需要在 `/hooks` 中检查并信任新定义，项目本身也要受信任。

到这里，本地科研记录就具备接入条件了。想调整项目名称或收集范围时，再复制可选配置：

```bash
cp -n .agents/harness/config/research-memory.example.json .agents/harness/config/research-memory.json
```

先给 `project_id` 填一个稳定的项目名称。`sources` 默认包含研究状态、结论和实验分析，有其他需要记录的分析文件再补。`context_chars` 和 `max_items` 控制每次提供的上下文量，初次接入可以沿用 6500 字符和 8 条。

`hooks_enabled` 默认是 true，改为 false 可以暂停自动收集。`hindsight_enabled` 默认是 false，只用本地功能时保持这个设置。

| 记忆配置参数 | 填写方式 |
|---|---|
| `project_id` | 稳定且独立的项目名称 |
| `sources` | 要纳入的研究文件，先沿用模板，再补充项目自己的分析文件 |
| `context_chars / max_items` | 每次提供的上下文上限，默认 6500 字符和 8 条 |
| `hooks_enabled` | 是否自动收集，默认 true |
| `hindsight_enabled` | 是否使用 Hindsight，默认 false |

这些记录怎样帮助下一轮研究，见[使用指南](usage.md#科研记录与召回)。

## 可选 Hindsight

实验记录积累多了以后，可以加上 Hindsight 辅助检索。先把服务装好，再把当前项目连过去。

**官方安装仓库：[vectorize-io/hindsight](https://github.com/vectorize-io/hindsight)**

仓库的 [Quick Start](https://github.com/vectorize-io/hindsight#quick-start) 提供安装与启动步骤。服务可用后，为项目准备 memory bank，再把 `research-memory.json` 中的 `hindsight_enabled` 改成 true。

连接信息仍填在本地 `.env`：

| 变量 | 填什么 |
|---|---|
| HINDSIGHT_API_KEY | 服务凭据 |
| HINDSIGHT_MCP_URL | 对应 memory bank 的 MCP 地址 |
| HINDSIGHT_API_URL、HINDSIGHT_BANK_ID | 不使用 MCP_URL 时，填写服务基础地址和 bank ID |

两种地址写法选一种。不同项目使用不同的 `project_id`；需要隔离时使用不同 memory bank。不开启这项服务，本地记录和查询也能继续使用。

## 6. 用一次小任务检查接入结果

先在项目根目录解析配置：

```bash
python .agents/harness/pipeline/run_pipeline.py --check
```

通过以后，再让 agent 拿项目真实的日志和结果样例，核对进度、指标能不能被识别。这一步只检查对接关系，真实训练仍然要到远程验证。

第一次可以选一个熟悉的基线或已有评估任务，把预算写清楚，按正常流程运行。观察它能不能真正开始、产生有效进展、取回结果，并把分析关联到这次的代码和实验方案。

如果卡住了，先看失败发生在哪一步。启动失败通常要核对命令、路径和环境；找不到进度就核对输出文件与字段；结果已经取回却没有指标，则检查 `summary_glob` 和 `primary_metric` 是否读到了真实结果。

接下来按[日常使用指南](usage.md)走一轮，接入过程里还有哪些遗漏会更容易看清。

## 已有项目怎么更新

更新前保存当前代码和科研记录。合并新版文件时，保留项目背景、实际配置和已有实验，随后重新安装项目里的 rrctl 和记录入口。

旧配置如果还平铺在 `.agents/harness/` 下，把它们移到 `config/`，保留原值。旧的 `remote-run-control` 源码目录改用 `.agents/harness/remote/rrctl/`。正在运行的实验继续沿用已有记录，更新工作流本身不需要再启动一次训练。

如果科研文件原来被代码仓库跟踪，先保留备份，并确认它们已提交到独立科研仓库。然后在代码仓库的 `.gitignore` 加入 `/research_workspace/`，再检查：

```bash
git ls-files -- research_workspace
```

有输出、且备份与独立提交已经确认后，才停止代码仓库对它的跟踪：

```bash
git rm -r --cached -- research_workspace
```

这条命令保留磁盘上的文件。提交前检查范围；仍跟踪旧科研目录的代码分支也需要合入这次调整。
