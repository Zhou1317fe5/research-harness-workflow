# Agent 自主项目适配协议 (Automated Onboarding Protocol)

> **目标受众**：AI Coding Agent (Hermes, Codex, Claude Code, Pi, Cursor)
> **任务目标**：将当前科研项目无侵入、零失误地适配到 `research-harness-workflow` 体系。
> **前置约束**：
> 1. 绝不破坏项目原有代码、超参、数据路径及已有脚本。
> 2. 凭据绝不入库，全部由环境变量与 `.env` 隔离。
> 3. 严格遵循符号链接与独立 Git 仓库规范，完成后执行确定性自检。

---

## Phase 0：目标项目现状探测 (Target Reconnaissance)

Agent 先读取目标项目元数据，提取并记录以下 4 项事实：
1. **启动入口**：
   - 检查 `scripts/`、`train.py`、`main.py`、`eval.py` 等入口。
   - 提取实际启动命令与启动器（Python 原生, `torchrun`, `accelerate` 等）。
2. **权重传递约定**：
   - 检查训练产物输出位置（默认目录或 `--output-dir` 参数）。
   - 评估读取 checkpoint 的路径与参数形式。
3. **指标与输出**：
   - 识别核心指标名（如 `mIoU`, `Accuracy`, `val_loss`）。
   - 确认当前是否有结构化进度文件（如 `progress.jsonl`, `summary.json`, wandb 等）。
4. **宿主环境感知**：
   - 识别当前交互宿主环境（Pi / Codex / Claude Code / 通用终端）。

---

## Phase 1：环境自举与脚手架搭建 (Self-Bootstrapping & Scaffolding)

Agent 负责安装缺少的工具与依赖，无需人类插手。

### 1.1 拉取模板资产
```bash
git clone https://github.com/Zhou1317fe5/research-harness-workflow.git /tmp/research-harness-template
```

### 1.2 依赖自举安装
```bash
# 安装 harness 核心依赖与 rrctl 远程执行工具
if [ -f "/tmp/research-harness-template/.agents/harness/requirements.txt" ]; then
  python3 -m pip install -q -r /tmp/research-harness-template/.agents/harness/requirements.txt
fi
python3 -m pip install -q -e /tmp/research-harness-template/.agents/harness/remote/rrctl
```

### 1.3 核心资产装配（严格维护符号链接与独立仓库）
在当前项目根目录执行：
```bash
# 复制 harness 控制程序与技能树
cp -Rn /tmp/research-harness-template/.agents .
cp -Rn /tmp/research-harness-template/.codex .

# 关键：.claude 与 .agents 下的 skills 必须为软链接，指向 .codex/skills
rm -rf .claude/skills .agents/skills 2>/dev/null || true
mkdir -p .claude .agents
ln -s ../.codex/skills .claude/skills
ln -s ../.codex/skills .agents/skills

# 复制账本、方案与产物目录
cp -Rn /tmp/research-harness-template/issues .
cp -Rn /tmp/research-harness-template/docs/specs docs/ 2>/dev/null || mkdir -p docs/specs
mkdir -p remote_artifacts research_workspace/experiments scripts

# 复制科研记忆与状态骨架
cp -n /tmp/research-harness-template/scripts/memory scripts/memory 2>/dev/null || true
cp -n /tmp/research-harness-template/remote_artifacts/README.md remote_artifacts/
cp -n /tmp/research-harness-template/research_workspace/STATE.md research_workspace/
cp -n /tmp/research-harness-template/research_workspace/CONCLUSIONS.md research_workspace/

# 若宿主为 Pi，复制 Pi 专用配置
if [ -d "$HOME/.pi" ] || command -v pi >/dev/null 2>&1; then
  cp -Rn /tmp/research-harness-template/.pi . 2>/dev/null || true
fi
```

### 1.4 配置 .gitignore 隔离防线
在项目根目录 `.gitignore` 追加以下内容（已存在则跳过）：
```gitignore
# Research Harness Artifacts & Credentials
.agents/harness/config/profiles.json
.agents/harness/config/.env
.agents/harness/config/project.toml
.agents/harness/config/research-memory.json
remote_artifacts/
research_workspace/
issues/.missions.json
```

### 1.5 初始化独立的 research_workspace Git 仓库
```bash
if [ ! -d "research_workspace/.git" ]; then
  git init -b main research_workspace
  git -C research_workspace add STATE.md CONCLUSIONS.md
  git -C research_workspace commit -m "🎉 init(research): 初始化独立科研记录仓库"
fi
```

---

## Phase 2：适配层合成 (Adapter Synthesis)

基于 Phase 0 提取的信息，Agent 自动生成目标项目的运行包装与配置。

### 2.1 生成标准化脚本 `scripts/train.sh` 与 `scripts/eval.sh`
必须支持 `RRCTL_OUTPUT_ROOT`（由 rrctl 运行时自动注入），保证产物隔离：

* `scripts/train.sh`：
```bash
#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${RRCTL_OUTPUT_ROOT:-$HOME/research-runs/$(basename "$(pwd)")/manual}"
mkdir -p "$OUTPUT_DIR/checkpoints"

echo "==> Starting Training, output dir: $OUTPUT_DIR"
# [Agent 填入探测到的实际训练命令]
# 示例: python train.py --output_dir "$OUTPUT_DIR" "$@"
```

* `scripts/eval.sh`：
```bash
#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${RRCTL_OUTPUT_ROOT:-$HOME/research-runs/$(basename "$(pwd)")/manual}"
CHECKPOINT_PATH="${CHECKPOINT:-$OUTPUT_DIR/checkpoints/best.pt}"

echo "==> Starting Evaluation on: $CHECKPOINT_PATH"
# [Agent 填入探测到的实际评估命令]
# 评估结束后，确保指标写入 $OUTPUT_DIR/summary.json
```
执行赋权：`chmod +x scripts/train.sh scripts/eval.sh`

### 2.2 生成 `.agents/harness/config/project.toml`
写入运行契约与字段映射：
```toml
version = 1

[[pipeline.stages]]
name = "train"
argv = ["bash", "scripts/train.sh"]
log = "train.log"
outputs = ["checkpoints/best.pt"]

[[pipeline.stages]]
name = "evaluate"
argv = ["bash", "scripts/eval.sh"]
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
summary_required_fields = ["<主指标字段名>"]
summary_finite_fields = ["<主指标字段名>"]

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
primary_metric = "<主指标字段名>"
dimensions = []
```

### 2.3 生成 `AGENTS.md` 与 `CLAUDE.md` 并保持逐字一致
1. 将模板根目录的 `AGENTS.md` 同步至目标项目根目录的 `AGENTS.md` 与 `CLAUDE.md`。
2. 在两份文件末尾的“本项目补充”节中，填入 Phase 0 提取的项目事实（研究背景、Baseline、主指标名、数据划分、代码路径）。
3. **强制核验**：确保 `diff -u AGENTS.md CLAUDE.md` 为空。

### 2.4 初始化连接配置骨架
```bash
cp -n .agents/harness/config/profiles.example.json .agents/harness/config/profiles.json
cp -n .agents/harness/config/.env.example .agents/harness/config/.env
chmod 600 .agents/harness/config/profiles.json .agents/harness/config/.env
```

---

## Phase 3：确定性门禁自检 (Verification Gates)

Agent 在交付前必须在终端运行以下自检命令：

```bash
# 1. 验证技能符号链接与镜像一致性 (必须 PASS)
python3 .agents/harness/workflow/check_skill_mirrors.py

# 2. 验证两份主规则文件完全逐字一致 (退出码必须为 0，无 diff)
diff -u AGENTS.md CLAUDE.md

# 3. 验证科研工作区 Git 独立性 (输出必须以 research_workspace 结尾)
git -C research_workspace rev-parse --show-toplevel

# 4. 验证 rrctl 就绪状态
rrctl --json doctor
```

---

## Phase 4：交付总结与接力汇报 (Handoff Report)

完成上述工作后，向用户输出结构化汇报：
1. **适配画像**：识别到的框架、训练入口、评估入口、主指标。
2. **生成资产列表**：列出 `scripts/train.sh`、`project.toml` 等关键路径。
3. **人类接力点 (唯一需要人提供的信息)**：
   - 提示用户在 `.agents/harness/config/profiles.json` 中配置远程 SSH 主机与端口。
   - 提示用户在 `.agents/harness/config/.env` 中配置远程 Conda 环境名、路径与 SSH 密码/密钥。
4. **验证建议**：推荐一条无需 GPU 的本地 dry-run 测试命令验证整条链路。
