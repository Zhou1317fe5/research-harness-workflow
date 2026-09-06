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

`.agents/harness/` 是唯一公共脚本目录。旧版根目录 `scripts/`、`.claude/harness/`、
`.codex/harness/` 的工作流脚本已迁到这里。项目自己的训练脚本可以继续放在原处。

`.claude/skills/` 和 `.codex/skills/` 保持相同规则；`.agents/skills` 是指向
`.codex/skills` 的相对符号链接，为 Codex 提供项目级发现入口。复制时保留该链接。

## 首次接入：改配置，必要时补少量输出

### 1. 填项目信息

在 `AGENTS.md`、`CLAUDE.md` 末尾填写“研究背景”和“路径”两节。
说明研究目标、baseline、主指标、训练入口、数据与 checkpoint 位置。

### 2. 配置远程连接

```bash
cp .agents/harness/.env.example .agents/harness/.env
cp .agents/harness/profiles.example.json .agents/harness/profiles.json
chmod 600 .agents/harness/profiles.json
```

在 `profiles.json` 的 `ssh_argv` 中填写 SSH 地址、端口或主机别名；`gpu` 是示例 profile 名。
密码认证只配置 `password_env: "SSH_PASSWORD"`，密码值放在本地 `.env`。
使用 SSH key 时删除 `password_env`。两个本地配置文件均被 Git 忽略。

`.env` 使用 shell 语法，带空格或特殊字符的值需加引号。填写远程 conda 环境、
conda 初始化脚本和远程仓库路径；生成 RunSpec 时使用这些实际值。
`remote_run.py` 会在子进程内 source 此文件，不把凭据写进生成命令或 RunSpec。
手工调用 rrctl 时先 source `.env`，并显式传入 `--profiles .agents/harness/profiles.json`。

### 3. 配置训练、评估和 checkpoint 衔接

```bash
cp .agents/harness/project.example.toml .agents/harness/project.toml
```

修改 `project.toml` 中的 `pipeline.stages`：

```toml
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
```

这些是接入示例，参数名要替换为项目真实参数。已有 shell 入口可写成
`argv = ["bash", "tools/train.sh", "..."]`，不要求统一脚本名或目录。
`{repo_root}`、`{output_root}`、`{run_id}` 由执行器替换，参数不经过 shell 拼接。

训练和评估必须约定同一个 checkpoint 路径。程序正常结束返回 0，失败返回非零值；
任一阶段失败或缺少 `requires` / `outputs` 文件时停止。stdout/stderr 同时写入阶段日志和 rrctl 控制台。
动态 checkpoint 命名由项目入口处理；模板不猜测哪个文件代表最佳模型。

已有入口支持输出路径和退出码时通常不用改业务代码。缺少这些接口时，补一个小入口或增加输出参数即可。
`project.toml` 不含凭据，应随项目代码提交，确保远程执行的是同一份配置。

### 4. 对接日志与指标

日志不用搬到某个固定的 `train/logs` 或 `eval/...` 结构。将本次运行的文件写到
rrctl 指定的 `output_root` 下，在配置中填写相对路径；各次运行使用独立目录。

- **只拉日志**：在 `artifacts` 中声明现有文件或子目录即可。
- **自动检查训练状态**：在 `adapter` 中指定进度文件、step 字段及需要检查的数值字段。
- **自动读取指标**：配置结果文件和指标字段。支持 JSON 与 JSONL 最后一条记录，支持嵌套字段名。

示例进度 JSONL 中的一行：

```json
{"step": 120, "loss": 0.42}
```

示例结果摘要：

```json
{"metric": 0.81}
```

有现成的结构化输出时，只需映射路径和字段。只有 tqdm 或纯文本输出时，可以补写一个很小的进度/结果文件，
也可以在 `.agents/harness/rrctl_adapters/` 实现项目适配器，并在 `rrctl_project_adapters.py` 登记。
单纯拉取文本日志不要求改日志格式。每个项目只实现自己需要的适配。

配置中的 `artifacts` 控制成功后拉取哪些文件或目录，`required = false` 表示不存在时允许跳过。
默认例子只拉摘要和两份日志；checkpoint、大型逐样本输出、可视化按分析需要另行声明。
`rrctl pull` 拉取整个声明清单，不会自行挑选有价值的指标，也没有固定 200 MB 下载上限。

`records` 控制实验索引的字段映射：

```toml
[records]
summary_glob = "summary.json"  # 相对于每个 RunID 目录
primary_metric = "metric"     # 例如 metrics.accuracy
# secondary_metric = "metric_aux"  # 没有辅助指标时省略
# dimensions = ["seed", "dataset"]
dimensions = []
```

无需修改 `experiment_records.py`。一个实验有多 seed、多 fold 结果时保留各次运行，
脚本不会自行决定平均方式、baseline 或科研结论。

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
python .agents/harness/run_pipeline.py --check
```

Agent 从已批准的运行范围构造 `mission.rrctl-request.v1`，通过 stdin 交给 builder，增加：

```text
--project-config .agents/harness/project.toml
```

配置提供 workload、adapter 和产物默认值；请求中的显式字段优先，审查和来源校验仍由 builder 执行。
首次接入用 `validate_rrctl_adapter_fixture.py <runspec.json> <fixture-root>` 检查真实输出样例。

已经准备好 RunSpec 时，一键入口为：

```bash
python .agents/harness/remote_run.py issues/<stem>/runs/<RunID>/runspec.json --execute
```

它依次执行 rrctl 的 ready、launch、前台 wait、pull。训练与评估在远端的同一个 workload 内顺序完成。
Mission 需要及时记录启动状态时，Agent 分别调用这些子命令，在 launch 成功后更新 CSV，再等待。
`wait` 进程负责周期检查，完成或异常后返回当前会话，沿用现有等待方式。
恢复已有运行使用 inspect / wait，不要重新执行一键入口。

正常结果与异常诊断均由 rrctl 拉取：

```bash
rrctl --profiles .agents/harness/profiles.json pull <RunID>
rrctl --profiles .agents/harness/profiles.json pull <RunID> --diagnostic
```

正常文件写入 `remote_artifacts/<ExpID>/<RunID>/`。诊断快照写入其 `diagnostics/<snapshot-id>/`，
可在失败、abort 或运行需要介入时使用，不会终止 workload。每个诊断文本文件最多保留尾部 1 MiB，
文本内容总量最多 16 MiB；`snapshot.json` 记录截断和跳过项。原始完整文件仍留在远端。
诊断快照不能作为成功结果入账。

```bash
python .agents/harness/experiment_records.py build --exp <ExpID>
python .agents/harness/experiment_records.py derive
```

`record.json` 保存机器事实，`EXPERIMENTS.csv` 从记录生成。无法从证据确定的 parent、评测协议、baseline、
outcome 会保留待补项；分析写入 `analysis/analysis.md`，不通过手工改生成文件伪装成已确定事实。

## 目录与阅读顺序

```text
.agents/harness/                     公共脚本、项目配置模板
.claude/skills/  .codex/skills/        两套同步的技能入口
.agents/skills                      Codex 发现入口，链接到 .codex/skills
issues/<stem>/                      任务 CSV、审查与 RunSpec
docs/specs/                         实验方案
remote_artifacts/<ExpID>/<RunID>/    原始证据，Git 忽略
research_workspace/
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
2. 工作流公共脚本迁到 `.agents/harness/`；项目自己的 `scripts/` 保留。
3. 从两套 skills 中移除旧的 `mission-doc-route`、`mission-long-task`、`exp-analysis-hen`、
   `exp-results-ingest-local`、`autodl-remote-run-snippet`、`autodl-remote-pull-manifest` 和 `remote-pull-manifest`。
4. 用 `project.toml` 对接命令与日志，重新安装 rrctl 并生成后续运行的 RunSpec。旧运行继续使用其已保存的协议。
5. 将旧原始日志逐实验迁到 `remote_artifacts/`，更新相关引用。保留旧实验分析，不重写历史结论。

新增 Git 忽略规则不会删除已经提交的日志，也不会缩小已有 Git 历史；历史清理需单独处理。

## 本地验证

```bash
PYTHONPATH=remote-run-control/src python -m pytest .agents/harness/tests remote-run-control/tests
```

显式指定本仓库源码，避免环境中其他 rrctl 安装影响结果。验证使用临时文件和假 workload，
不训练模型。真实训练、GPU 显存和数值验证在远端进行。

## License

见 [LICENSE](LICENSE)。
