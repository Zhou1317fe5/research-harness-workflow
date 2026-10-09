# Agent 自主项目适配协议 (Automated Onboarding Protocol)

> **目标受众**：AI Coding Agent (Hermes, Codex, Claude Code, Pi, Cursor)
> **任务目标**：将当前科研项目无侵入地适配到 `research-harness-workflow` 规范。
> **前置约束**：
> 1. 绝不破坏项目原有代码逻辑、默认超参、数据路径及已有脚本。
> 2. 凭据绝不入库，全部由环境变量与 `.env` 隔离。
> 3. 严格遵循符号链接与独立 Git 仓库规范，完成后执行确定性自检。
> 4. **交互准则（探索优先，选择代替查找）**：用户知道怎么用 Agent 开启会话，但对底层配置细节是初学者。**凡是能由 Agent 自动探索的信息，严禁要求用户手动查找路径或参数**。Agent 必须自动搜索并列出带编号的候选选项，让用户只做单选；唯一需要用户手动编辑的是本地文件中的机密密码。

---

## Phase 0：目标项目现状探测与选项生成 (Target Reconnaissance)

用户经常直接 clone 开源论文的 Baseline 仓库，对其内部细节并不熟悉。**Agent 必须自主进行代码逆向分析，提取并推断运行事实，严禁要求用户手动查找代码路径与参数**：

1. **启动入口逆向推断**：
   - 优先查阅开源仓库的 `README.md`（检索 Usage, Quick Start, Training 章节中的标准启动命令）。
   - 全局搜索包含 `if __name__ == '__main__':` 的入口文件，反查其参数解析逻辑（`argparse`、`Hydra`、`OmegaConf`、`mmengine` 等配置体系），推断主训练与评估命令模板。
   - 若项目完全无入口（从零起步的新项目），直接采用模板标准骨架并生成待实现的占位代码。
   - 若探测到多个任务/配置文件入口，列出带编号的菜单让用户单选，无需用户手动寻找。
2. **权重路径与传递逆向追踪**：
   - 自动搜索代码中的 `torch.save`、`save_checkpoint`、`ModelCheckpoint`，追踪开源代码将权重硬编码保存在何处（如 `./work_dirs/latest.pth`、`./checkpoints/` 等）。
   - 自动搜索代码中的 `load_state_dict`、`--resume`、`--checkpoint`、`--weights`，分析评估阶段如何读取权重。
3. **指标输出逆向追踪**：
   - 自动扫描评估代码中的核心指标计算（如 `mIoU`, `Accuracy`, `val_loss`），识别其是在控制台 stdout 打印还是保存为文件。
   - 若代码仅在控制台打印指标，Agent 在后续包装时自动注入正则提取垫片，无需改动源码。
4. **宿主与已有连接识别**：
   - 识别当前宿主（Codex 或 Pi）。
   - 检查本地 `~/.ssh/config`，提取已配置的 SSH 主机别名作为后续服务器候选项。
5. **外部工具可用性检查**：
   - 检查环境中是否已有 `humanizer-zh`、`fast-context-mcp` 或 `smart-search`。

---

## Phase 1：环境自举与脚手架装配 (Self-Bootstrapping & Scaffolding)

Agent 负责安装依赖并将模板装配进项目。**在复制之前必须先扫描潜在冲突，严禁直接覆盖用户文件，必须安全合并；实在拿不准的，主动向用户出示选项进行询问。**

### 1.1 拉取模板与复制前冲突预检 (Pre-flight Conflict Scan)
```bash
# 1. 拉取模板并移除 .git 与 README.md（绝对禁止覆盖目标项目自身的 Git 历史与原有项目 README）
git clone https://github.com/Zhou1317fe5/research-harness-workflow.git /tmp/research-harness-template
rm -rf /tmp/research-harness-template/.git /tmp/research-harness-template/README.md
```

Agent 在执行任何复制前，必须扫描目标项目根目录，检查是否存在同名冲突文件：
1. **自动安全合并项（无需打扰用户）**：
   - `.gitignore`：**严禁覆盖**，采用末尾追加方式合并模板的隔离规则。
   - `AGENTS.md` / `CLAUDE.md`：若用户已存在，先做备份，在 Phase 2.3 中将用户原有规则与模板核心规则安全融合。
   - `research_workspace/`：若用户已有该目录，保持原有记录，不覆盖已有实验认知。
2. **需要谨慎处理的冲突项（已有脚本）**：
   - `scripts/train.sh` / `scripts/eval.sh`：若用户已有同名训练脚本，**绝对禁止覆盖**！在 Phase 2.1 中仅在其外层注入 `RRCTL_OUTPUT_ROOT` 垫片。
3. **无法判定意图的冲突（主动询问用户）**：
   - 若用户项目中存在与模板中其他重要文件（如 `project.toml`、已有的 `.agents/` 自定义技能）同名且内容差异较大时，Agent 必须停下并向用户单选询问：
     ```text
     检测到您的项目中已存在 [文件名]，且与模板内容不一致：
     [1] 保留您现有的文件（推荐，避免破坏现有配置）
     [2] 安全合并（保留关键参数，引入工作流支持）
     [3] 使用工作流模板版本替换（原有文件将备份为 .bak）
     ```

### 1.2 安全复制与环境合并 (Safe Merge & Copy)
确认冲突处置策略后，执行安全复制与初始化：
```bash
# 安全复制模板资产（不覆盖已确认保留的文件，保持软链接与属性）
cp -an /tmp/research-harness-template/. .
```

1. **合并 .gitignore 隔离防线**（追加模板规则）：
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
2. **符号链接校验**：确保 `.claude/skills` 和 `.agents/skills` 保持为指向 `.codex/skills` 的软链接（`cp -an` 原生保留）。
3. **初始化独立的 research_workspace Git 仓库**：
   ```bash
   if [ ! -d "research_workspace/.git" ]; then
     git init -b main research_workspace
     git -C research_workspace add STATE.md CONCLUSIONS.md
     git -C research_workspace commit -m "🎉 init(research): 初始化独立科研记录仓库"
   fi
   ```

### 1.3 依赖自举安装
```bash
# 安装 harness 核心依赖与 rrctl 远程执行工具
if [ -f ".agents/harness/requirements.txt" ]; then
  python3 -m pip install -q -r .agents/harness/requirements.txt
fi
python3 -m pip install -q -e .agents/harness/remote/rrctl
```

### 1.4 外部工具与可选工具安装

根据环境与用户选择安装配套工具：

| 工具 | 性质 | 用途 | 项目仓库地址 |
|---|---|---|---|
| **Humanizer-zh** | 本项目要求必装 | 整理中文方案与交付说明（避免机翻感） | [op7418/Humanizer-zh](https://github.com/op7418/Humanizer-zh) |
| **fast-context-mcp** | 可选 MCP | 快速定位代码模块、理清跨文件调用链 | [SammySnake-d/fast-context-mcp](https://github.com/SammySnake-d/fast-context-mcp) |
| **smart-search** | 可选 CLI | 搜索学术论文、工具文档与官方来源 | [blxzer77/smart-search](https://github.com/blxzer77/smart-search) |

#### 安装执行流程

1. **Humanizer-zh 安装（必装）**：
   Agent 在终端直接执行安装：
   ```bash
   npx skills add https://github.com/op7418/Humanizer-zh.git
   ```

2. **可选增强工具（通俗介绍作用并由用户单选）**：
   Agent 主动介绍工具的具体作用与适用场景，列出菜单供用户轻松决策，绝不让用户自己去查安装文档：
   ```text
   是否需要为您配置以下可选增强工具？（由 Agent 自动协助安装与配置）：

   • fast-context-mcp（代码语义检索）：
     - 作用：帮助 Agent 跨文件快速读懂复杂的大型开源项目，理清跨模块调用链（例如：迅速定位数据流、损失函数或数据增强是在哪个底层文件实现的）。
     - 建议：如果您的项目代码文件较多（如复杂的开源 Baseline、多个子模块），建议安装；若项目结构简单，可不装。

   • smart-search（文献与技术资料检索）：
     - 作用：让 Agent 能够联网精准搜索与阅读 arXiv 学术论文、开源库官方技术文档及报错资料，辅助实验方案设计。
     - 建议：如果希望在方案讨论时让 Agent 查阅最新论文或官方文档，建议安装；若只做纯本地代码实验，可不装。

   请选择：
   [1] 仅安装 fast-context-mcp（适合代码较复杂的大型开源库）
   [2] 仅安装 smart-search（适合需要查阅论文/文档的场景）
   [3] 全选安装 (fast-context-mcp + smart-search)
   [4] 暂不安装，保持最小化精简配置 (推荐)
   ```
   - 用户选择后，若包含 `fast-context-mcp`，Agent 自动协助在对应宿主（Codex `config.json` 或 Pi `mcp.json`）中写入配置并测试连通；
   - 若包含 `smart-search`，Agent 协助运行 `smart-search setup` 向导并运行 `doctor` 验证。

### 1.5 宿主环境特化配置 (Codex / Pi)

根据 Phase 0 识别的宿主环境，应用对应配置：

#### A. 如果宿主为 Codex
1. **技能读取**：Codex 原生直接读取根目录 `.codex/skills/`，无需特殊符号链接。
2. **审查契约**：科研审查（PRERUN 与 post-run 分析）的模型与 thinking 级别配置在 `.agents/harness/config/review_contract.toml`；closing review 强制使用当前执行模型。

#### B. 如果宿主为 Pi
1. **项目配置装配**：复制模板中的 `.pi/` 目录至项目根目录：
   ```bash
   cp -Rn /tmp/research-harness-template/.pi .
   ```
2. **技能路由规则**：Pi 会话经 `.agents/skills` 链接读取 `.codex/skills/`；仅 `pre-run-implementation-review` 读 `.pi/skills/` 下的真副本。
3. **推荐全局 packages 安装**（按需自检安装）：
   ```bash
   pi install npm:pi-mcp-adapter                      # MCP 适配
   pi install npm:@juicesharp/rpiv-ask-user-question   # 结构化用户提问界面
   pi install npm:@juicesharp/rpiv-advisor             # 架构与方案顾问
   pi install npm:pi-sub-agent                         # 独立审查子代理运行支持
   pi install npm:@ff-labs/pi-fff                      # 本地模糊文件与内容搜索
   pi install npm:@cortexkit/pi-magic-context          # 长会话压缩与 Historian
   ```
4. **独立审查机制**：PRERUN、post-run 和 closing review 由 `.agents/harness/reviewer_job.py` 启动 fresh 只读 Pi 会话执行，自动隔离主会话偏见。

---

## Phase 2：适配层合成 (Adapter Synthesis)

基于 Phase 0 提取的信息，Agent 自动生成目标项目的运行包装与配置。

### 2.1 生成标准化脚本 `scripts/train.sh` 与 `scripts/eval.sh`（胶水垫片机制）

**核心原则：绝不侵入修改开源项目的 Python 源码，所有路径与格式差异均通过外层 Shell 胶水垫片（Shim Wrapper）无侵入抹平。**

脚本必须支持 `RRCTL_OUTPUT_ROOT`（由 rrctl 运行时自动注入），保证产物隔离：

* `scripts/train.sh`（训练包装与权重搬运垫片）：
```bash
#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${RRCTL_OUTPUT_ROOT:-$HOME/research-runs/$(basename "$(pwd)")/manual}"
mkdir -p "$OUTPUT_DIR/checkpoints"

echo "==> Starting Training, output dir: $OUTPUT_DIR"

# 1. 运行开源代码原生启动命令
# 若开源库支持 --output-dir / --work-dir，直接传入：
# python tools/train.py --output_dir "$OUTPUT_DIR" "$@"
# 若开源代码原生命令采用配置文件或无输出参数：
python tools/train.py --config configs/base.yaml "$@"

# 2. 权重自动搬运垫片（处理开源库硬编码保存路径的情况）
# 若 Phase 0 探测到开源库硬编码保存在 work_dirs/latest.pth：
if [ -f "work_dirs/latest.pth" ]; then
  cp work_dirs/latest.pth "$OUTPUT_DIR/checkpoints/best.pt"
fi
```

* `scripts/eval.sh`（评估包装与指标自动脱水垫片）：
```bash
#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${RRCTL_OUTPUT_ROOT:-$HOME/research-runs/$(basename "$(pwd)")/manual}"
CHECKPOINT_PATH="${CHECKPOINT:-$OUTPUT_DIR/checkpoints/best.pt}"

echo "==> Starting Evaluation on: $CHECKPOINT_PATH"

# 1. 运行开源代码原生评估命令（并将权重传入或软链接至期望位置）
python tools/eval.py --weights "$CHECKPOINT_PATH" "$@" | tee "$OUTPUT_DIR/evaluate.log"

# 2. 指标自动脱水垫片（若开源库仅在终端打印指标，自动提取并写入 summary.json）
python3 -c "
import re, json, sys, os
log_path = '$OUTPUT_DIR/evaluate.log'
if os.path.exists(log_path):
    log = open(log_path, encoding='utf-8', errors='ignore').read()
    # 根据 Phase 0 探测到的指标名提取数值
    m = re.search(r'(mIoU|Accuracy|val_loss|metric)[\s:=]+([0-9.]+)', log, re.IGNORECASE)
    metric = float(m.group(2)) if m else 0.0
    with open('$OUTPUT_DIR/summary.json', 'w') as f:
        json.dump({'metric': metric}, f, indent=2)
"
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

### 2.3 合成 `AGENTS.md` 与 `CLAUDE.md` 并保持逐字一致 (Rules Merging)

若目标项目原本已有 `AGENTS.md` 或 `CLAUDE.md`（见 1.1 备份的 `.orig.bak`）：
1. **提取原有自定义规则**：检查原有文件中是否包含用户自定义规则或旧的“本项目补充”，予以保留；
2. **合入模板核心规则**：以模板的最新工作流引擎规则（Mission 路由、PRERUN 门禁、rrctl 远程执行规范等）为主体；
3. **沉淀至“本项目补充”**：在两份文件末尾的“本项目补充”节中，合并写入 Phase 0 提取的项目事实（研究背景、Baseline、主指标名、数据划分、代码路径）及原有自定义规则；
4. **清理备份并强制核验**：
   ```bash
   rm -f AGENTS.md.orig.bak CLAUDE.md.orig.bak
   diff -u AGENTS.md CLAUDE.md
   ```
   **两份文件必须逐字完全一致**（diff 必须无任何输出），严禁前后规则漂移。

### 2.4 远程连接环境自动探索与选择 (Interactive Remote Configuration)

**核心准则：能由 Agent 自动探索的信息，严禁要求用户手动查找；所有配置项全列成带编号的选项让用户单选。**

#### 1. 远程服务器选择（优先探测本地已有的 SSH 配置）
Agent 先自动读取本地 `~/.ssh/config`：
- **若检测到已有主机别名**，直接列出供用户单选：
  ```text
  检测到本地已配置以下 SSH 主机，请选择用于本项目的远程服务器：
  [1] gpu-server (100.127.x.x:22, 用户 zhou)
  [2] dev-node   (192.168.1.50:22, 用户 ubuntu)
  [3] 输入新的服务器 IP、端口与用户名
  ```
- 若无已有配置或用户选 [3]，再询问服务器 IP、端口（默认 22）、用户名与认证方式（免密 Key 还是密码）。

#### 2. Agent 自动初始化配置并引导密码填写
Agent 代用户初始化配置并写入所选主机信息：
```bash
cp -n .agents/harness/config/profiles.example.json .agents/harness/config/profiles.json
cp -n .agents/harness/config/.env.example .agents/harness/config/.env
chmod 600 .agents/harness/config/profiles.json .agents/harness/config/.env
```
- 若用户使用密钥免密登录，在 `profiles.json` 中移除 `"password_env": "SSH_PASSWORD"`。
- **若使用密码登录**，Agent 详细引导用户在本地填写，**绝不要求在对话中发送**：
  ```text
  ⚠️ 敏感凭据填写指引（请在本地完成，切勿发在对话中）：
  请打开本地文件填入密码：
  1. 文件位置：.agents/harness/config/.env
  2. 修改项：找到第 2 行的 SSH_PASSWORD
  3. 填写示例：SSH_PASSWORD="您的真实服务器密码"

  填写保存后，请回复「已填写」，我将自动连接远端为您探测项目路径与 Conda 环境。
  ```

#### 3. Agent 远程自动搜索项目路径（用户单选确认）
连接建立后，**远程项目路径由 Agent 自动在远端搜索，无需用户手动拼写**：
- Agent 远程执行搜索（在远端 `$HOME` 下搜索与当前项目同名或包含相似代码的目录）；
- 搜索到后列出候选路径供用户选择：
  ```text
  已在远程服务器探测到以下候选项目路径，请选择：
  [1] /home/ubuntu/my-project (推荐，检测到与本地项目同名)
  [2] /data/workspace/my-project
  [3] 手动输入其他自定义路径
  ```
- 用户单选确认后，Agent 自动将 `REMOTE_REPO_ROOT` 写入 `.env`。

#### 4. Agent 远程自动搜索 Conda 环境（用户单选确认）
**Conda 虚拟环境与路径同样由 Agent 全自动探测，无需用户查找**：
1. Agent 远程自动执行探针：
   - 自动运行 `conda info --base` 拼出 `REMOTE_CONDA_SH`（`${CONDA_BASE}/etc/profile.d/conda.sh`）；
   - 自动运行 `conda env list` 获取全部可用虚拟环境列表。
2. Agent 将探测到的环境以菜单形式列出供用户单选：
   ```text
   探测到远程服务器存在以下可用 Conda 环境，请选择：
   [1] dinov3 (/home/ubuntu/miniconda3/envs/dinov3)
   [2] torch21 (/home/ubuntu/miniconda3/envs/torch21)
   [3] py310   (/home/ubuntu/miniconda3/envs/py310)
   ```
3. 用户输入编号后，Agent 自动将选定的 `REMOTE_CONDA_ENV` 与 `REMOTE_CONDA_SH` 写回 `.env`。全套远程环境装配完成。

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

# 4. 验证 rrctl 就绪状态与远程连接连通性
rrctl --json doctor
```

---

## Phase 4：交付总结与接力汇报 (Handoff Report)

完成上述工作后，向用户输出结构化汇报：
1. **适配画像**：识别到的框架、宿主环境（Codex/Pi）、训练入口、评估入口、主指标。
2. **已生成资产列表**：列出 `scripts/train.sh`、`scripts/eval.sh`、`project.toml`、`profiles.json`、`.env` 等路径。
3. **工具与依赖状态**：
   - `Humanizer-zh` 安装状态；
   - 可选工具（`fast-context-mcp` / `smart-search`）接入情况（若未安装，提示用户如需安装 Agent 可协助配置）。
4. **远程环境与凭据状态**：
   - 远程主机连接与 Conda 环境已配置就绪；
   - 确认 `.agents/harness/config/.env` 中的 `SSH_PASSWORD` 本地填写状态。
5. **验证建议**：推荐一条无需 GPU 的本地 dry-run 测试命令验证整条链路。
6. **下一步起跑建议**：告知用户配置已全部就绪，引导用户可直接按照 `docs/workflow/usage.md`，在会话中发送第一条指令开始方案讨论（例如：「我想在现有基线上验证...」）。
