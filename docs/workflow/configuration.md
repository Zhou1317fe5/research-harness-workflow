# 用户配置说明

所有配置集中在 `.agents/harness/config/`。按实际使用的功能填写：

| 使用方式 | 需要配置 |
|---|---|
| 本地科研记录与召回 | 可直接安装记忆钩子；research-memory.json 可选 |
| 远程训练与评估 | project.toml、profiles.json、.env，以及项目研究背景 |
| 自动生成实验指标索引 | project.toml 中的 records 字段 |
| Hindsight 同步与语义召回 | research-memory.json 和 .env 中的 Hindsight 变量 |

使用整个 harness 时，本地与远程 Python 均需 3.11 或更高版本。本地记忆功能使用标准库；
远程执行前在本地安装控制面：

```bash
python -m pip install -e .agents/harness/remote/rrctl
```

远程主机应具备 SSH、Git、tmux 和可显式激活的 conda 环境。
科研工作区的独立 Git 初始化见 [安装说明](installation.md#初始化独立科研仓库)。

## 1. 填写项目背景

在项目根的 AGENTS.md、CLAUDE.md 中填写“研究背景”和“路径”：
研究任务、当前 baseline、主指标、训练和评估入口、数据集位置、checkpoint 位置。
这些信息帮助 agent 构造实验方案；实际执行命令由 project.toml 提供。

## 2. 配置训练、评估与输出

首次接入时复制模板；已有配置则编辑现有文件：

```bash
cp -n .agents/harness/config/project.example.toml .agents/harness/config/project.toml
```

project.toml 不放凭据，应随代码提交。需要修改的字段如下：

| 字段 | 如何填写 |
|---|---|
| version | 保持为 1 |
| pipeline.stages[].name | 唯一的阶段名，如 train、evaluate |
| pipeline.stages[].argv | 项目真实命令的参数数组，替换示例脚本名与参数 |
| pipeline.stages[].cwd | 可选，工作目录相对项目根，默认 . |
| pipeline.stages[].log | 本阶段日志相对本次输出目录的位置，默认为阶段名.log |
| pipeline.stages[].requires | 本阶段开始前必须存在的输入文件，相对本次输出目录 |
| pipeline.stages[].outputs | 本阶段结束后必须存在的输出文件，相对本次输出目录 |
| adapter.progress_path / progress_format | 真实进度文件的位置，以及 json 或 jsonl_last |
| adapter.progress_count_field | 进度计数字段，如 step；可使用嵌套字段名 |
| adapter.first_step_min_count | 首步检查的最小计数，通常为 1 |
| adapter.progress_finite_fields | 需要检查为有限数值的进度字段，如 loss |
| adapter.summary_path / summary_format | 最终结果文件及格式 |
| adapter.summary_required_fields | 最终结果必须包含的字段 |
| adapter.summary_finite_fields | 最终结果必须为有限数值的字段 |
| artifacts[].path / required | 要拉回的文件或目录；required=false 表示允许缺失 |
| records.summary_glob | 每个 RunID 下的结果文件匹配路径，默认 summary.json |
| records.primary_metric | 主指标字段，如 metric 或 metrics.accuracy |
| records.secondary_metric | 可选辅助指标；没有时省略 |
| records.dimensions | 需要保留的维度字段，如 seed、dataset；没有时填 [] |

示例阶段：

```toml
[[pipeline.stages]]
name = "train"
argv = ["python", "train.py", "--output-dir", "{output_root}"]
log = "train.log"
outputs = ["checkpoints/best.pt"]

[[pipeline.stages]]
name = "evaluate"
argv = ["python", "evaluate.py", "--checkpoint", "{output_root}/checkpoints/best.pt", "--output", "{output_root}/summary.json"]
requires = ["checkpoints/best.pt"]
outputs = ["summary.json"]
```

将脚本名和参数替换为项目已有入口；shell 入口可写成 `["bash", "tools/train.sh", "..."]`。
`{repo_root}`、`{output_root}`、`{run_id}` 由执行器替换。
argv 不经过 shell 拼接；参数本身需要花括号时，用 `{{`、`}}` 表示字面量。
训练输出与评估输入必须指向同一个 checkpoint。路径动态变化时由项目入口明确传递。

默认 adapter 读取 JSON 或 JSONL 最后一条记录。例如进度包含 `step`、`loss`，
结果包含 `metric`。只有纯文本日志时，可补写少量结构化字段，或在
`remote/adapters/` 实现专属解析器，并在 `remote/project_adapters.py` 登记。
文本日志仍可通过 artifacts 原样拉取。

以后修改配置时，可以只解析配置，不启动训练：

```bash
python .agents/harness/pipeline/run_pipeline.py --check
```

这条命令检查配置结构，不证明模型命令可运行，也不验证 GPU、loss 或 checkpoint。
真实训练和首步检查由 rrctl 在远程完成。

## 3. 配置远程连接和环境

```bash
cp -n .agents/harness/config/profiles.example.json .agents/harness/config/profiles.json
cp -n .agents/harness/config/.env.example .agents/harness/config/.env
chmod 600 .agents/harness/config/profiles.json .agents/harness/config/.env
```

profiles.json 使用以下结构。将 gpu、端口和 user@host 换成实际内容：

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

SSH 配置中的主机别名也可以放在 ssh_argv 中。使用 SSH key 时移除 password_env；
密码认证时保持其值为变量名 SSH_PASSWORD。

在本地 config/.env 中填写：

| 变量 | 是否需要 | 内容 |
|---|---|---|
| SSH_PASSWORD | 仅密码认证 | SSH 密码 |
| REMOTE_CONDA_ENV | 远程运行必填 | 已创建的远程 conda 环境名称 |
| REMOTE_CONDA_SH | 远程运行必填 | 远程 conda.sh 的绝对路径 |
| REMOTE_REPO_ROOT | 远程运行必填 | 远程项目工作区的绝对路径，供生成 RunSpec 使用 |

.env 使用 shell 语法，特殊字符和空格需要正确引用；实际凭据只保存在本地文件中。
profiles.json 和 .env 均被 Git 忽略。远程环境必须安装项目自己的训练依赖；
模板不会自动安装框架、下载数据或创建 conda 环境。

`remote/remote_run.py` 会在子进程内加载 .env，并默认使用 config/profiles.json。
手工调用 rrctl 时先加载环境，再显式指定连接配置：

```bash
set -a
source .agents/harness/config/.env
set +a
rrctl --profiles .agents/harness/config/profiles.json --json ready issues/<stem>/runs/<RunID>/runspec.json
```

ready 会访问远程主机，应在已经生成并确认 RunSpec 后运行。
环境变量不会直接被 builder 自动展开：agent 构造请求时读取这些变量，
将实际 conda 环境与远程路径写入请求；密码始终只通过 password_env 引用。
每次运行的 SpecID、ExpID、RunID、代码 commit、GPU、输出目录和科学审查信息由已批准的任务提供，
不需要在这些通用配置文件中维护第二份台账。

## 4. 配置本地科研记忆

只使用本地记忆时，无需配置 Hindsight 或安装 SDK：

```bash
python .agents/harness/memory/install_memory_hooks.py
```

默认合并 Codex 和 Claude Code 的项目配置；只用一种宿主时加 `--host codex` 或 `--host claude`。
重新启动宿主。Codex 中通过 `/hooks` 检查并信任新定义后，宿主才会执行这些钩子。
安装器不修改信任记录。用 `--remove` 移除本工具的绑定。

需要调整项目身份、来源或预算时，再创建配置：

```bash
cp -n .agents/harness/config/research-memory.example.json .agents/harness/config/research-memory.json
```

| 字段 | 默认值与填写方法 |
|---|---|
| project_id | 未配置时由项目目录名生成；启用远端同步时填写稳定且独立的项目标识 |
| sources | STATE、CONCLUSIONS、实验分析、record.json、research_workspace/analysis/*.md |
| context_chars | 默认 6500，可设 1000–16000 |
| max_items | 默认 8，可设 1–30 |
| hindsight_enabled | 默认 false，仅使用本地功能时保持关闭 |

会话开始、恢复、压缩后和新消息到达时加载本地上下文。文件变化在下一次生命周期回调或 scan 时采集。
控制队列位于独立科研 Git 元数据中，或 `.agents/harness/.memory/`；目录迁移不移动已有队列。
语义整理由 agent 根据来源完成，待确认项保持可见。详细命令见 [科研记录与召回](research-memory.md)。

## 5. 可选 Hindsight

确认要同步来源后，将 research-memory.json 的 hindsight_enabled 改为 true，
并在 config/.env 配置以下变量：

| 变量 | 说明 |
|---|---|
| HINDSIGHT_API_KEY | 服务凭据 |
| HINDSIGHT_MCP_URL | 推荐直接填写对应 memory bank 的 MCP 端点 |
| HINDSIGHT_API_URL、HINDSIGHT_BANK_ID | 不填 MCP_URL 时，用服务基础地址和 bank ID 组合出端点 |

填写 MCP_URL 或 API_URL + BANK_ID 其中一种端点配置即可。使用不同项目标识区分来源；
有隔离要求时使用不同 memory bank。默认 MCP 路径使用标准库，不需要安装 hindsight-client。
`memory/hindsight_memory.py` 保留旧的手工 SDK 入口，仅使用该旧入口时才需要
`memory/requirements-hindsight.txt` 中的依赖。

钩子先将来源写入本地，再安排短同步任务。服务异常保留队列和重试状态。
手工同步时，先按上一节加载 .env，再运行
`python .agents/harness/memory/research_memory.py sync`。

## 从平铺目录升级

旧版的 project.toml、profiles.json、.env、research-memory.json 移到 config/；
保留现有值，不用示例覆盖它们。按本说明更新脚本路径，再运行新的钩子安装入口。
research_workspace 与已有 .memory 队列保持原位置。

旧根目录中的 remote-run-control 移到 `.agents/harness/remote/rrctl/`。
如果当前 Python 环境以 editable 方式指向旧目录，需要按本文开头的新路径重新安装。
包名 remote-run-control、Python 模块 remote_run_control 和 rrctl 命令均保持不变。
连接配置仍在 config/，已保存运行的控制路径与恢复索引不随源码目录移动。

若钩子没有运行，先检查宿主是否重启、项目是否受信任、Codex 的 /hooks 是否已批准新定义。
若提示配置不存在，检查 config/ 中是否使用了实际文件名，而不是仅保留 .example 模板。
若没有实验指标，检查 records.summary_glob 与 primary_metric 是否匹配已经拉回的结果。
