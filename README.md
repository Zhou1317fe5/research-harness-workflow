# research-harness-workflow

可按项目复制使用的科研工作流模板。用 Spec 明确实验目标，用任务 CSV 跟踪执行；
原始日志放在 `remote_artifacts/`，实验分析与可复用结论放在 `research_workspace/`。

工作流说明见 [面向科研的 harness engineering](docs/workflow/面向科研的harness-engineering.md)。
本仓库基于 [Missions](https://github.com/flowing-water1/Missions) 整理科研执行流程。

## 安装到项目

要求：Python 3.11+；rrctl 的本地与远程环境为 Linux，远程需有 SSH、tmux 和 conda。
Skills 按项目安装。已有同名文件时先合并，保留项目自己的规则和配置。

```bash
git clone https://github.com/Zhou1317fe5/research-harness-workflow.git /tmp/rhw
cd /path/to/your-project
cp -R /tmp/rhw/.agents /tmp/rhw/.claude /tmp/rhw/.codex .
cp -R /tmp/rhw/issues /tmp/rhw/docs /tmp/rhw/remote-run-control .
cp /tmp/rhw/AGENTS.md /tmp/rhw/CLAUDE.md .
mkdir -p research_workspace/experiments remote_artifacts
cp /tmp/rhw/research_workspace/STATE.md /tmp/rhw/research_workspace/CONCLUSIONS.md research_workspace/
cp /tmp/rhw/remote_artifacts/README.md remote_artifacts/
cat /tmp/rhw/.gitignore >> .gitignore
python -m pip install -e remote-run-control
```

`.agents/harness/` 按功能组织公共实现；各子目录的职责见 [目录说明](.agents/harness/README.md)。旧版根目录 `scripts/`、`.claude/harness/`、
`.codex/harness/` 的工作流脚本已迁到这里。项目自己的训练脚本可以继续放在原处。

`.claude/skills/` 和 `.codex/skills/` 保持相同规则；`.agents/skills` 是指向
`.codex/skills` 的相对符号链接，为 Codex 提供项目级发现入口。复制时保留该链接。

### 初始化独立科研仓库

在目标项目中，将 `research_workspace/` 初始化为独立 Git 仓库。代码分支可以切换，
科研仓库持续维护同一份实验记录与跨实验分析；科研记录中的 Branch / Commit 指向对应代码版本。
主仓库的 `.gitignore` 已包含 `/research_workspace/`，这里使用普通嵌套仓库，不添加 submodule。

```bash
git init -b main research_workspace
git -C research_workspace add STATE.md CONCLUSIONS.md
git -C research_workspace commit \
  -m "🎉 init(research): 初始化科研记录" \
  -m "Why: 跨代码分支保留实验与结论" \
  -m "Why this works: 科研目录拥有独立 Git 历史" \
  -m "Remaining: 按需配置科研仓库远端"
git -C research_workspace rev-parse --show-toplevel
```

最后一条应返回目标项目的 `research_workspace` 目录。已有独立科研仓库时沿用它，
只合并缺少的模板。科研仓库自行配置远端、提交与推送，代码仓库的 push 不包含它。
本模板仓库保留已跟踪的空白模板，用于安装到其他项目。

若旧项目已将科研文件提交到代码仓库，先备份并把现有记录提交到独立科研仓库，再在代码仓库停止跟踪：

```bash
git ls-files -- research_workspace
# 上面有输出时执行；只从代码仓库索引移除，保留磁盘上的科研文件。
git rm -r --cached -- research_workspace
git add .gitignore
git diff --cached --stat
```

检查范围后提交这次迁移。仍跟踪科研目录的旧代码分支也需要合入停止跟踪的变更，
再切换使用，避免旧分支恢复过时的科研文件。

`systematic-debugging` 是随模板提供的可选辅助技能，可以不安装或移除；
它不是运行、验收或提交的依赖。

## 用户配置

按 [用户配置说明](.agents/harness/docs/configuration.md) 接入；配置统一放在 `.agents/harness/config/`。

| 功能 | 用户需要填写 |
|---|---|
| 远程训练、评估 | `project.toml` 的真实命令、checkpoint 衔接、进度/结果字段与产物清单 |
| 远程连接 | `profiles.json` 的 SSH 地址、端口、认证方式和 profile 名 |
| 远程环境 | `.env` 的 conda 环境、conda.sh、远程项目路径；密码认证时填写 SSH_PASSWORD |
| 本地科研记忆 | 安装会话钩子即可；`research-memory.json` 可调整来源、项目身份与预算 |
| Hindsight（可选） | 记忆配置中显式启用，并在 `.env` 填写 MCP 端点和凭据变量 |

先在 AGENTS.md、CLAUDE.md 中补齐研究背景与项目路径。已有配置沿用原值；首次接入从
`config/` 的 `.example` 模板复制。`project.toml` 随代码提交，连接配置和凭据留在本地。
完整字段表、复制命令、SSH key 配置、Hindsight 配置和升级步骤均在上述说明中。

## 使用

```text
mission 我想验证 X 方法在 Y 任务上的效果
mission docs/specs/<日期>-<主题>.md
mission issues/<stem>/<stem>.csv
mission
```

自然语言需求先形成待批准的 Spec，批准后由 CSV 跟踪执行。新实验、新指标或新的 baseline 对照使用 Mission；
画图、写论文正文、复用已有结果直接执行并留 `result-summary.md`。

运行前的配置检查：

```bash
python .agents/harness/pipeline/run_pipeline.py --check
```

Agent 从已批准的运行范围构造 `mission.rrctl-request.v1`，通过 stdin 交给 builder，增加：

```text
--project-config .agents/harness/config/project.toml
```

配置提供 workload、adapter 和产物默认值；请求中的显式字段优先，审查和来源校验仍由 builder 执行。
首次接入用 `validate_adapter.py <runspec.json> <fixture-root>` 检查真实输出样例。

已经准备好 RunSpec 时，一键入口为：

```bash
python .agents/harness/remote/remote_run.py issues/<stem>/runs/<RunID>/runspec.json --execute
```

它依次执行 rrctl 的 ready、launch、前台 wait、pull。训练与评估在远端的同一个 workload 内顺序完成。
Mission 需要及时记录启动状态时，Agent 分别调用这些子命令，在 launch 成功后更新 CSV，再等待。
`wait` 进程负责周期检查，完成或异常后返回当前会话，沿用现有等待方式。
恢复已有运行使用 inspect / wait，不要重新执行一键入口。

正常结果与异常诊断均由 rrctl 拉取：

```bash
rrctl --profiles .agents/harness/config/profiles.json pull <RunID>
rrctl --profiles .agents/harness/config/profiles.json pull <RunID> --diagnostic
```

正常文件写入 `remote_artifacts/<ExpID>/<RunID>/`。诊断快照写入其 `diagnostics/<snapshot-id>/`，
可在失败、abort 或运行需要介入时使用，不会终止 workload。每个诊断文本文件最多保留尾部 1 MiB，
文本内容总量最多 16 MiB；`snapshot.json` 记录截断和跳过项。原始完整文件仍留在远端。
诊断快照不能作为成功结果入账。

```bash
python .agents/harness/records/experiment_records.py build --exp <ExpID>
python .agents/harness/records/experiment_records.py derive
```

`record.json` 保存机器事实，`EXPERIMENTS.csv` 从记录生成。无法从证据确定的 parent、评测协议、baseline、
outcome 会保留待补项；分析写入 `analysis/analysis.md`，不通过手工改生成文件伪装成已确定事实。

### 科研记录与召回

安装当前项目的会话钩子：

```bash
python .agents/harness/memory/install_memory_hooks.py
python .agents/harness/memory/research_memory.py status
```

安装器合并 Codex 与 Claude Code 的本地配置，保留其他钩子。Codex 下次启动后，
在 `/hooks` 中检查并信任新定义；这是宿主执行钩子的要求。
也可以用 `--host codex` 或 `--host claude` 只安装一种宿主，用 `--remove` 移除本工具的绑定。

用户消息、agent 最终回复与实验分析更新先进入本地队列。会话开始、恢复、压缩后和新消息到达时，
自动提供当前决定与待处理来源；`research-memory` skill 负责引导 agent 整理来源、维护结论状态。
当前采用架构、已验证结果和对照原点分别引用 `CONCLUSIONS.md` 中的条目。

Hindsight 默认关闭。本地采集、整理与查询只需 Python 标准库；启用后由短任务同步队列，
服务异常仍保留本地来源与重试状态。配置、命令和宿主边界见
[科研记录与召回](.agents/harness/docs/research-memory.md)。

## 目录与阅读顺序

```text
.agents/harness/
  config/                          用户配置与示例
  common/                          路径与配置解析
  pipeline/                        训练、评估执行器
  remote/                          rrctl 入口、RunSpec 与 adapters
  records/                         实验事实与索引
  memory/                          科研记忆与会话钩子
  docs/                            用户配置和使用说明
.claude/skills/  .codex/skills/        两套同步的技能入口
.agents/skills                      Codex 发现入口，链接到 .codex/skills
issues/<stem>/                      任务 CSV、审查与 RunSpec
docs/specs/                         实验方案
remote_artifacts/<ExpID>/<RunID>/    原始证据，Git 忽略
research_workspace/                 目标项目中的独立 Git 仓库
  STATE.md                          当前研究状态
  CONCLUSIONS.md                    跨实验判断与状态
  EXPERIMENTS.csv                   自动生成的实验索引
  experiments/<ExpID>/record.json   单实验机器事实
  experiments/<ExpID>/analysis/analysis.md
remote-run-control/                 rrctl 源码
```

按 `STATE.md → CONCLUSIONS.md → EXPERIMENTS.csv → record.json → analysis.md` 阅读。
只有核验具体问题时才读取对应 RunID 的原始文件。实验结论使用 Change / Result / Finding / Next 四段。

## 从旧版升级

1. 合并新版 `.agents/`、两套 skills、规则文件和 rrctl 源码，保留本地配置与已有实验记录。
2. 按功能子目录更新 harness 调用路径，将旧平铺目录中的本地配置移入 `config/`，重新安装会话钩子。
3. 从两套 skills 中移除旧的 `mission-doc-route`、`mission-long-task`、`exp-analysis-hen`、
   `exp-results-ingest-local`、`autodl-remote-run-snippet`、`autodl-remote-pull-manifest` 和 `remote-pull-manifest`。
4. 用 `project.toml` 对接命令与日志，重新安装 rrctl 并生成后续运行的 RunSpec。旧运行继续使用其已保存的协议。
5. 将旧原始日志逐实验迁到 `remote_artifacts/`，更新相关引用。保留旧实验分析，不重写历史结论。

新增 Git 忽略规则不会删除已经提交的日志，也不会缩小已有 Git 历史；历史清理需单独处理。

## 配置检查

后续修改项目配置时，可以用下列生产入口检查配置结构；它不启动训练：

```bash
python .agents/harness/pipeline/run_pipeline.py --check
```

仓库不再分发测试代码。真实运行的 readiness、首步、健康检查和结果契约由 rrctl 与 adapters 执行。

## License

见 [LICENSE](LICENSE)。
