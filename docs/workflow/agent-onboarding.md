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
6. **科研方向与 Trick 手册领域推断**：
   - 模板自带的 `.codex/skills/mission-csv-execute/references/metric-tricks-arsenal.md` 默认面向计算机视觉。
   - Agent 需分析本项目所处的研究领域（如 NLP/大模型微调、强化学习、多模态、推荐系统、语音、图学习或时间序列等），并在 Phase 2 自动检索并定制该领域的保分 Trick 手册。
7. **训练/评估进度源逆向探测（progress 契约）**：
   - 进度心跳与"累计 ≥N 步后报首个 ETA"依赖适配器从 `[adapter].progress_path`（默认 `progress.jsonl`，格式 `jsonl_last`）按 `progress_count_field`（默认 `step`）读出累计步数；没有该文件就出不了 ETA，且 `periodic` 阶段健康检查会因文件缺失判失败。
   - Agent 必须自动探测本项目是否已有可复用的进度输出：搜索训练循环里的 `tqdm`/`progress`/`step`/`epoch` 打印、`*.jsonl`/`*.csv` 落盘、`callback`/`Logger`，判断能否直接映射出"已处理步数/样本数"及其字段名与文件格式。
   - 若已有 → 在 Phase 2.2 的 `[adapter]` 中把 `progress_path`/`progress_format`/`progress_count_field` 指向它；若没有 → 在 Phase 2.1 用外层"进度垫片"生成 `progress.jsonl`（见 2.1.1），绝不改核心 Python 源码。
8. **其它可选能力的接入探测**：进度/评估之外，完成判定、产物入账、输出隔离、多维度/身份字段、环境自检、GPU 分配、退出码语义、超参透传等能力同样依赖项目侧契约。Agent 须按 **Phase 2.6 可选能力接入清单** 逐项探测，并在安装阶段让用户单选"接入/跳过"，未接入项在交付报告中显式声明不可用。

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

### 2.1.1 进度垫片：生成 `progress.jsonl`（供进度/ETA 心跳消费）

**为什么需要**：`remote_run.py` 的进度心跳、"累计 ≥N 步后报首个 ETA"完全依赖适配器从 `[adapter].progress_path`（默认 `progress.jsonl`，格式 `jsonl_last`）按 `progress_count_field`（默认 `step`）读出累计步数。**项目若不产出该文件，心跳就拿不到步数，ETA 永远出不来**（只会显示"仍在准备/暂无可信 ETA"）；且 `periodic` 阶段适配器要求该文件存在，缺文件会被健康检查判失败。

**按 Phase 0 探测结果二选一，均不侵入核心 Python 源码**：

1. **项目原生已写进度文件** → 直接在 `project.toml` 的 `[adapter]` 映射，无需垫片：
   ```toml
   [adapter]
   progress_path = "<项目原本的进度文件相对路径>"
   progress_format = "jsonl_last"   # 若为单对象 json 则用 json
   progress_count_field = "<该文件里表示已处理步数/样本数的字段名>"
   ```

2. **项目只在终端打印进度、不落盘** → 在 `scripts/train.sh` 外层加"进度垫片"：把训练输出 `tee` 到 `train.log`，后台按正则增量解析步数与 loss，逐行追加 `progress.jsonl`：
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   OUTPUT_DIR="${RRCTL_OUTPUT_ROOT:-$HOME/research-runs/$(basename "$(pwd)")/manual}"
   mkdir -p "$OUTPUT_DIR/checkpoints"
   : > "$OUTPUT_DIR/train.log"
   : > "$OUTPUT_DIR/progress.jsonl"

   # 后台进度垫片：PROGRESS_RE 必须由 Agent 依 Phase 0 探测到的真实日志格式填写（须能取到 step，最好也能取到 total/loss）
   (
     tail -n +1 -F "$OUTPUT_DIR/train.log" 2>/dev/null | python3 -u -c '
   import re, json, sys
   # 占位示例：匹配形如 "step 123/600 ... loss 0.45"；实际须按本项目日志格式改写
   pat = re.compile(r"step[=: ]+(?P<step>\d+)(?:/(?P<total>\d+))?.*?loss[=: ]+(?P<loss>[0-9.eE+-]+)", re.I)
   for line in sys.stdin:
       m = pat.search(line)
       if not m:
           continue
       rec = {"step": int(m.group("step")), "loss": float(m.group("loss"))}
       if m.group("total"):
           rec["total"] = int(m.group("total"))
       print(json.dumps(rec), flush=True)
   ' >> "$OUTPUT_DIR/progress.jsonl"
   ) &
   TAIL_PID=$!
   trap 'kill "$TAIL_PID" 2>/dev/null || true' EXIT

   # 原生启动命令，输出同时落 train.log 供垫片解析
   python tools/train.py --config configs/base.yaml "$@" 2>&1 | tee -a "$OUTPUT_DIR/train.log"

   # <保留 2.1 的权重搬运垫片…>
   ```
   - `[adapter]` 相应设为 `progress_path="progress.jsonl"`、`progress_format="jsonl_last"`、`progress_count_field="step"`；日志里能解析出总数就一并写入 `total`（心跳的完成百分比与 ETA 需要它，缺失时 ETA 不可算）。
   - 正则与字段名必须依 Phase 0 实测日志填写，**禁止保留占位正则上生产**。
   - 训练进程须行缓冲/实时刷写（`python -u` 或 `flush=True`）；若用 tqdm，建议加 `newline=True` 或对 `\r` 单独处理——否则 `tail` 拿不到整行，进度滞后甚至触发 console stale。

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

### 2.5 领域特化科研 Trick 手册自动定制 (Domain-Specific Tricks Customization)

模板默认自带的 `.codex/skills/mission-csv-execute/references/metric-tricks-arsenal.md` 是专为**计算机视觉**设计的工程保分技巧库。

**Agent 在接入新项目时，必须根据 Phase 0 识别的学科方向自主分析并定制该手册**：
1. **方向对齐**：
   - 若项目确实是计算机视觉方向，保留原手册；
   - 若项目属于其他领域（如 NLP/大模型微调、强化学习、推荐系统、语音、图神经网络、时间序列等），Agent 需自行检索该领域顶会中常用的即插即用涨点与数值稳定 Trick（例如：NLP 中的对比学习温度调节、长序列动态掩码、LoRA rank/scale 补偿；RL 中的优势归一化、Reward 截断平滑；推荐中的负采样温度调节等）；
2. **重写手册**：
   将检索提炼的实用 Trick（包含原理说明、触发场景与紧凑 Python 代码片段）写回目标项目的 `.codex/skills/mission-csv-execute/references/metric-tricks-arsenal.md`；
3. **目的**：确保后续任务在探索期初测指标微亏时，Agent 能够根据专属于该研究方向的保分军火库就地攻坚拉升指标，而不必推翻已确立的核心学术叙事。

### 2.6 可选能力接入清单（安装阶段按需接入，未接入则该能力不可用）

工作流的多数能力**并非开箱即用**，而是依赖项目侧产出特定契约（进度文件、评估摘要、产物/阶段输入输出路径、输入资产、环境变量与模块、CLI 语义、退出码等）。这些能力彼此独立、**可按需启用**：Agent 在 Phase 0 逐项探测，在安装阶段用菜单让用户单选"接入/跳过"，接入一律走**配置映射（映射到项目已有产物）**或**外层垫片（不改源码地生成/搬运产物）**两条路，绝不侵入核心算法代码。

| 能力 | 依赖项目提供的契约 | 未适配的后果 | 可选接入方式（均不改源码） |
|---|---|---|---|
| 进度 / ETA 心跳 | `progress_path` 可解析进度文件，含 `progress_count_field`（步数）、可选 `total` 与有限 `loss` | 无进度、无 ETA（心跳只剩"设备+已用"） | 映射已有进度文件 / 2.1.1 垫片 |
| 进度总数命名 | progress 里表示总量的字段名须落在 generic 识别集（`episodes`/`total_steps`/`max_steps`/`total_epochs`/`epochs`/`total`）之一 | 拿不到 total → 无百分比/无 ETA | 改字段名，或改用 `completion_exact_count` |
| 进度数值有限性 | `progress_finite_fields`（如 loss）不得为 NaN/Inf | 健康检查判失败 | 修训练稳定性，或调整字段声明 |
| 完成判定（completion，仅当声明计数阈值） | 若设了 `completion_exact_count` / `completion_min_count`，进度最终 count 须达标（默认无门槛） | 声明了阈值却没跑够 → completion 判失败 | 让项目写出最终 count；或把阈值调成与真实规模一致 |
| 首步就绪（first_step） | 首条进度即含 `count ≥ first_step_min_count`（默认 1） | 首步健康检查不过，反复等待/告警 | 确保首条进度带 count，或调 `first_step_min_count` |
| 评估摘要入册 | `summary_path`（且 `records.summary_glob` 指向同一文件）含 `primary_metric` 有限值；**纯训练任务也须在收尾写 summary.json**（可仅含 `complete`/final loss） | CSV 记账失败 / 无指标；纯训练缺 summary 则 completion 阶段失败 | eval.sh 指标脱水垫片写 summary.json；纯训练在收尾写一份最小 summary |
| 摘要必填/有限字段（仅当声明） | 若声明 `summary_required_fields`（可含 `complete`/`method_contract` 等）与 `summary_finite_fields`，则须存在且有限（默认不要求） | completion 阶段 `AdapterContractError` | 在 summary 中补齐这些字段，或不在契约里声明 |
| 辅助指标（secondary） | 若声明 `secondary_metric` 则必须存在（未声明则缺失属合法） | 记账字段缺失报错 | 写该字段，或从 `[records]` 去掉 |
| 多维度记录（仅当配置） | 若在 `[records]` 配置了 `dimensions`/`protocol_field`/`checkpoint_field`/`steps_field`，summary 须含对应字段（默认 `dimensions` 为空、不要求） | 配了维度但 summary 缺字段 → 维度为空/报错 | 在 summary 回显，或在 `[records]` 映射到真实字段；不配则无要求 |
| 身份一致性 | 若填了 `summary_identity_fields`/`progress_identity_fields`，摘要/进度须回显同值 | `identity_mismatch` 直接判失败 | 通用项目建议**留空**；确需回显才写这些字段 |
| 流水线阶段输入/产出 | `pipeline.stages[].requires`（阶段前须存在，如 eval 依赖 `checkpoints/best.pt`）、`outputs`（阶段后须存在）、可选 `check_argv`（前置输入检查） | `required input/output missing`，阶段直接失败 | train.sh/eval.sh 把产物写到声明的相对路径，并给出前置检查 |
| 产物入账（manifest） | `[[artifacts]]` 声明的相对路径文件完成时真实存在，且在输出根内；**不得是符号链接、不得越出输出根**（`confined_path` 会拒） | manifest 校验失败 / 拉取缺件 / 符号链接被拒 | 用 train.sh/eval.sh 把真实文件搬到规范相对路径 |
| 输出隔离 | 所有产物写在 `RRCTL_OUTPUT_ROOT`（pipeline 已自动注入该变量）之下 | 拉取与记账拿不到产物 | shim 把它当输出目录，或完成时搬运到该根目录 |
| 输入资产供给（配置项，通常不改代码） | **输入**资产：数据集、预训练骨干权重、用于微调/恢复的起始 checkpoint（注意：训练产出的权重不在此列，走 `pipeline.outputs`/`[[artifacts]]`）；要么远端已具备且路径与代码一致，要么登记 `anchors`（`local_path`+`sha256`+`remote_path`，`remote_path` 不得与生命周期根重叠）由 rrctl 上传落位 | 远端缺该输入文件 → 运行时 FileNotFound | 远端预置，或在 RunSpec/project 里建 `anchors` 清单分发（**不改代码**） |
| 环境变量 | 代码依赖的 env（数据集根、开关等）须经 `environment.variables` 注入 | 读不到关键 env → 行为错误或崩溃 | 项目配置或 RunSpec 提供 `variables` |
| 环境自检（ready，仅当声明） | 若声明 `environment.required_modules`，远端 conda 环境须可 import 这些模块；可选 `preflight_argv`（默认不设则不检查模块） | ready preflight 失败，任务起不来 | 选对 conda 环境；补齐 `required_modules`/`preflight_argv`，或不声明 |
| GPU 分配（通常无需改动） | rrctl 启动时自动注入 `CUDA_VISIBLE_DEVICES=<分配卡>`（`worker.py:397`），进程内 `cuda:0` 即映射到分配卡 | 仅当代码硬编码物理卡号（`torch.cuda.set_device(3)`/`device='cuda:3'` 且无视该变量）时才冲突/OOM | 通常不改；个别硬编码卡号的代码改成读 `CUDA_VISIBLE_DEVICES` 或 CLI 参数 |
| GPU 利用率门禁 | 若开 `gpu_utilization_policy=required`（配 `gpu_min_percent`/`low_gpu_limit_seconds`），项目须真在卡上有足够利用率 | 纯 CPU 跑 / GPU 利用率低 → 健康检查报警或判失败 | CPU 或低占用任务把 policy 调为 `advisory`/`disabled`，或调阈值 |
| 健康 stale 阈值 | 若配置了 `console_stale_seconds`/`progress_stale_seconds`，长时间无 console/进度输出会被判 stale | 被误报 unhealthy / 打断观察 | 定期输出心跳或进度，或放宽/不设 stale 阈值 |
| 致命关键字日志 | `fatal_patterns` 命中的文本（如 OOM/Traceback 关键字）会判失败 | 正常日志里出现被配置的"致命词"被误杀 | 调整 `fatal_patterns`，避免在日志中误打印触发词 |
| 前台常驻进程语义 | 训练/评估必须**前台阻塞到结束**（`run_pipeline` 以子进程 wait 判定）；不得自我 daemon/后台化后立即返回 | 提前判完成、输出缺失（`required output missing`）或假成功 | train.sh/eval.sh 前台跑，不 `nohup … &` |
| 输出实时刷写 | 子进程 stdout 需行缓冲/实时刷（如 `python -u`、`flush=True`）；避免 tqdm 仅 `\r` 不换行 | 进度垫片 tail 不到行、console 被判 stale | 用 `-u`/`flush`，tqdm 加 `newline` 或改按 `\r` 解析 |
| 退出码语义 | 成功跑完 `exit 0`，异常非零退出 | 误判失败 / 吞异常导致假成功 | 修 shim 正确传播退出码，不掩盖异常 |
| 超参透传 | 学习率/步数等超参走命令行参数，不硬编码默认值 | RunSpec 参数不生效 / 形成"假变更" | 一律用 CLI 传参，不改源码默认值 |
| 代码提交 / Git 溯源 | 项目须是 git 仓库，参与运行的代码已 **commit**（`base`/`candidate` commit 存在）；未提交改动不进 bundle | 无 commit 无法构建/launch；远端会跑旧代码；PRERUN 变更指纹漏检未提交改动 | 运行前 `git commit`；改动用 commit 固定，再用该 commit 触发 |

> **按改动量分三档（避免误读为"全都要改代码"）**：
> - **A. 配置 / 环境层，通常不改代码**：进度与摘要的路径/字段名/阈值、identity 留空、`anchors` 输入资产分发、`environment.variables`、`required_modules`、GPU 分配（rrctl 自动注入 `CUDA_VISIBLE_DEVICES`）、`[[artifacts]]`/`pipeline.stages` 声明、health stale/policy、git 提交。
> - **B. 需项目行为配合，可用外层垫片（不必改核心源码）**：生成 `progress.jsonl`、生成 `summary.json`、前台常驻、输出实时刷写、退出码正确、超参走 CLI、产物写到 `RRCTL_OUTPUT_ROOT`。
> - **C. 极少数才需改源码**：硬编码物理卡号、硬编码超参默认值、进度字段名不可配置。

> **强制 vs 可选（默认 generic adapter 时，重要）**：真正**永远必须**的只有——`progress_path` 与 `summary_path` 两个文件在 completion 时必须存在且可解析（`confined_path` 的 `must_exist`；纯训练也要写出一份对象 summary），外加"退出码 0 / 前台常驻 / 产物落在 `RRCTL_OUTPUT_ROOT`"这几条运行语义。其余（completion 精确计数、first_step 阈值、`summary_required_fields`/`summary_finite_fields`、多维度、secondary、identity 回显、GPU 利用率门禁、stale、fatal_patterns、额外 artifacts、`environment.variables`、`required_modules`）**都只在 Agent 显式声明/启用时才成为约束**，默认不设/关闭（completion 计数默认无门槛、`gpu_utilization_policy` 默认 `advisory`、stale 默认 `null`、`fatal_patterns` 默认空、`required_modules` 默认空、`dimensions` 默认空、`identity` 默认空）。**不要把可选约束硬加给用户当"必须适配"。**

**安装阶段交互（一次多选，逐项确认）**：
```text
以下工作流能力依赖项目侧适配，可分别选择"接入"或"跳过"（跳过的能力在交付报告中会标注为不可用）：
[1] 进度/ETA 心跳        （映射进度文件 / 加进度垫片）
[2] 评估指标入册        （eval.sh 指标脱水写 summary.json）
[3] 多维度与身份字段    （在 summary 回显 / 或留空）
[4] 产物入账与输出隔离  （产物搬到 RRCTL_OUTPUT_ROOT 规范路径）
[5] 环境预检与 GPU 分配 （对齐 conda 环境 / 读取分配卡）
[6] 全部接入（推荐，逐项按上表方式落地）
可多选，如 "1,2,4,5"。
```
- **默认推荐**：低成本项（进度垫片、指标脱水、产物搬运）默认接入；身份强校验（`*_identity_fields`）默认留空，除非项目天然带这些标识。
- **诚实原则**：任一项未接入，必须在 Phase 4 交付报告中显式标注"该能力不可用"及原因，严禁默认它可用、更不得伪造对应产物。

**不依赖项目代码、无需适配的能力（供 Agent 判定边界，不必向用户索要适配）**：PRERUN/Review 路由与 reviewer、`gate_provenance`、Mission CSV 状态机与 `preflight.py`、closing/handoff 校验（`mission_completion`/`final_ready`/`lint_handoff`/`check_handoff_contract`）、`research-memory`、`workflow_sync`、控制面（`remote_run`/`agent_event_wait`/`rrctl` monitor/transport）等，全部由工作流内部或 Spec/CSV/文档驱动，不需要用户改项目代码。它们唯一与项目相关的前置是"项目是 git 仓库且改动已提交"（见上表"代码提交 / Git 溯源"）与"评估摘要可供记账"（见上表"评估摘要入册"）。

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

# 5. 验证进度合同可被心跳消费：progress.jsonl 最后一行能解析出 count(step)
python3 -c "
import json, sys
lines = [l for l in open('progress.jsonl', encoding='utf-8') if l.strip()]
assert lines, 'progress.jsonl 为空或不存在：本项目的进度/ETA 心跳将不可用'
rec = json.loads(lines[-1]); assert isinstance(rec.get('step'), int), 'progress.jsonl 缺少整数 step 字段'
print('progress ok, last step =', rec['step'])
"
```
> 若本项目为确实无法产出可步进进度的纯离线任务，第 5 项无法通过时，必须在 Phase 4 交付报告中**显式声明"该项目的进度/ETA 心跳不可用"**，并说明原因，不得默认它可用。
> 若在 Phase 2.6 中接入了"评估指标入册/产物入账"等能力，Phase 3 还须一并验证：`summary.json` 能解析出 `primary_metric`（有限值）、`[[artifacts]]` 声明的文件真实存在且落在输出根内；任何在 2.6 中选择"跳过"的能力，都不得在 Phase 4 报告中标为可用。

---

## Phase 4：交付总结与接力汇报 (Handoff Report)

完成上述工作后，向用户输出结构化汇报：
1. **适配画像**：识别到的框架、宿主环境（Codex/Pi）、训练入口、评估入口、主指标。
2. **已生成资产列表**：列出 `scripts/train.sh`、`scripts/eval.sh`、`project.toml`、`profiles.json`、`.env` 等路径，以及**进度源配置（`progress.jsonl` 的映射或垫片，及所用 `progress_count_field`）**、定制后的 `metric-tricks-arsenal.md`（注明所适配的研究领域）。
3. **工具与依赖状态**：
   - `Humanizer-zh` 安装状态；
   - 可选工具（`fast-context-mcp` / `smart-search`）接入情况（若未安装，提示用户如需安装 Agent 可协助配置）。
4. **远程环境与凭据状态**：
   - 远程主机连接与 Conda 环境已配置就绪；
   - 确认 `.agents/harness/config/.env` 中的 `SSH_PASSWORD` 本地填写状态。
5. **验证建议**：推荐一条无需 GPU 的本地 dry-run 测试命令验证整条链路。
6. **下一步起跑建议**：告知用户配置已全部就绪，引导用户可直接按照 `docs/workflow/usage.md`，在会话中发送第一条指令开始方案讨论（例如：「我想在现有基线上验证...」）。
