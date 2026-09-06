# 指令优先级

1. 当前会话中用户的明确要求
2. 仓库自身的规则、文档与约定
3. 相关 skill / protocol 的流程定义
4. 本文件的硬门禁与偏好

只做审查、分析、解释或问答时不进入实现流程。命中 skill 时先读对应 `SKILL.md`；**skill 已经规定的执行细节不在本文件重复，以 skill 为准。**

# 工作路由

- `mission <CSV|目录>`：执行合法 CSV；执行态持续到终态，或用户明确暂停、取消、改变边界。
- `mission <approved spec>`：校验已提交且未改动后，由 `mission-approved-doc` 生成 `issues/<stem>/` 并执行。
- `mission <draft spec|Markdown|自然语言>`：由 `mission-spec` 讨论、写 draft、取得明确批准。
- `mission`、`continue`、`resume`、`继续`：由 `mission-recovery` 只扫描 `issues/`。
- 普通任务目标和验收清楚时直接执行；多步任务维护 plan。
- 分析、审查、解释、Q&A 直接回答。

**何时进 mission**：任务产生进入台账的新科研结论（新 ExpID、新指标、baseline 对照）时才进，走 CSV 全账。画图、选样例、论文正文、复用已有结果不进，直接执行并留一份 `result-summary.md`。不允许跑完整 PRERUN 却不建 CSV。

需求未定时先澄清目标、约束和验收。`mission-spec` 每次只问一个仍会改变方案的问题；方案确定后尽快写 draft。未批准的 spec 禁止实现，批准后不再另写 implementation plan。

# 实施纪律

- 改动紧贴批准范围和现有代码模式，不混入无关重构、格式化或调试痕迹。
- 先读即将修改的代码；使用结构化解析器处理 CSV、JSON、TOML 等格式。
- 系统边界校验外部输入；shell、SQL 使用安全参数传递。
- 不用 case 特化、固定答案或输出修补伪装 prompt、模型和测试能力。
- 功能逻辑只写在 canonical 实现文件；兼容 wrapper 只维护向后兼容，不承载行为。

# 验证

**不做 TDD / test-first / RED。** 科研代码的正确性由科学契约与数值证据判定，不由先写测试判定。这一条覆盖 skill 中任何 test-first 表述。

**本地**负责代码正确性、单元测试、编译、参数链路与配置解析；**真实训练启动、step 级验证、GPU 显存、loss/log/checkpoint 必须走远程**。

远程执行统一经 **rrctl** 控制面，`fallback_allowed` 必须为 `false`：rrctl 不可用、readiness 失败或 launch 失败时停在当前 row，**不得回退临时 SSH/tmux 拼接冒充同一控制面**。生命周期、首步 gate、巡检口径与拉取策略见 `mission-csv-execute/references/remote-run.md`。

测试是 commit、push、PR 前的硬门禁。只报告实际运行过的命令、退出码和结果。测试范围分级、`validation_gap` 标注与 claim 终态规则见 `mission-csv-execute`。

# 科研产物布局

**证据与认知分开。**

```text
remote_artifacts/<ExpID>/<RunID>/   原始证据。项目根，不进 Git，不作默认上下文，禁止递归批量读取
research_workspace/
  STATE.md                          当前在做什么，1–3 屏
  CONCLUSIONS.md                    我们现在相信什么。C 编号 + OPEN/SUPPORTED/MIXED/REJECTED/SUPERSEDED
  EXPERIMENTS.csv                   做过哪些实验，由 record.json 自动派生
  experiments/<ExpID>/
    record.json                     单实验机器事实，由 CSV + artifacts 投影，不手工维护
    analysis/analysis.md            唯一结论文件，固定四段 Change / Result / Finding / Next
    analysis/*.md                   诊断附件，不参与结论
```

读取顺序：`STATE.md → CONCLUSIONS.md → EXPERIMENTS.csv → record.json → analysis.md`；只有需要核验具体实验时才读 `remote_artifacts/<ExpID>/`。`record.json` 中路径以项目根解析。

研究产物最低关联：SpecID + ExpID + Branch + Commit；多次远程运行补 RunID。

# 安全与进程

- 未获授权不运行破坏性命令，不覆盖或丢弃用户改动，不使用 `git reset --hard`。
- **凭据只传变量名或变量引用，永不传值**：`set -a; source <env file>; set +a` 后引用变量；控制面用 `password_env` 传变量名。不硬编码、提交或输出密钥、凭证、API Key。
- **禁止整体打印含凭据的文件**（`cat`/`head`/`tail`/`sed -n`/`nl`）。会话记录把 stdout 永久落盘，一次打印即等于永久泄露；自制脱敏不算防护。确认存在性用 `echo "KEY=${KEY:+set}"`，看结构用 `grep -oE '^[A-Za-z_]+='`。
- 非交互 SSH 下不假设 `python` / `conda` 在 `PATH`，远程 Python 命令必须显式激活环境。
- 不终止非当前任务启动的进程。长生命周期进程尽量少开，启动前检查可复用实例，结束即回收。

# 搜索分工

- 本地代码语义理解、探索性定位、跨模块调用链：`fast_context_search`。
- 已知函数名、类名、配置项、报错文本：`rg` 精确定位；已知路径直接读文件。
- 外部资料、论文、工具版本、API/SDK 文档：`smart-search-cli`。

# Git 与提交

- 开始任务先记录 `git status` 和已暂存 patch，只 add 本任务路径。
- 同一路径有用户已暂存 patch，或 index delta 无法精确隔离时，停止提交并报告 blocker。
- 一个逻辑变更一个提交。提交前运行相关验证、检查 `git diff --check`、确认 staged 范围。
- 使用 `<emoji> <type>(scope): 中文摘要`（≤50 字、动词开头、不加句号），正文写 `Why`、`Why this works`、`Remaining`。emoji：init 🎉 / feat ✨ / fix 🐞 / docs 📃 / style 🌈 / refactor 🦄 / perf 🎈 / test 🧪 / build 🔧 / ci 🐎 / chore 🐳 / revert ↩。
- **代码仓库与科研工作区分别提交。**
- merge 前完成 review；push/PR 前再次确认测试证据和工作树边界。

# 沟通

- 默认简体中文，可混用英文术语；代码标识符英文，注释中文。
- 执行任务优先报告当前动作、已完成、下一步和阻塞；分析任务先给结论，再给依据和权衡。
- 多步任务维护可见计划，同一时刻只保留一个 `in_progress`。

# Skills

- `mission`：spec、CSV、执行与恢复的统一入口。
- `mission-spec`：需求讨论、canonical spec 和批准边界。
- `mission-approved-doc`：approved spec 到 issues 工件。
- `mission-csv-execute`：CSV 闭环执行、证据、review 与 handoff。
- `mission-recovery`：只扫描 `issues/` 的恢复入口。
- `pre-run-implementation-review`：运行前科学实施审查、风险分流与等价性探针。
- `systematic-debugging`：根因不明或跨模块故障的定位辅助。
- `humanizer-zh`：必装的自然语言处理 skill。
- `smart-search-cli`：外部资料、论文与文档检索。
- `remote-run-snippet`：从 intent 解析远程 train/eval 命令。
- `research-result-commit`：当前 ExpID 研究产物的合并提交。
- `lite-arch` / `lite-arch-recall`：建议安装的 ADR 记录与召回 skills。

---

# 本项目补充

> 以下两节是**每个项目必须自行填写**的部分，模板不预设内容。
> 通用规则（上方全部章节）不需要改动。

## 研究背景

<一到两句：研究方向、基线方法、主指标。例如「X 模型 + Y 任务。基线为 Z。目标是在官方 evaluation setup 下提升 <主指标>，<辅助指标> 只作参考。」>

## 路径

- 远程连接使用 `.agents/harness/profiles.json`，凭据和环境只读 `.agents/harness/.env`（键：`SSH_PASSWORD` / `REMOTE_CONDA_ENV` / `REMOTE_CONDA_SH`）；
  远程 Python 须显式激活环境。
- <项目训练/评估脚本位置，如 `scripts/train_*.sh` / `scripts/eval_*.sh`>
- <项目数据集路径、checkpoint 路径等>
- <其他项目专有目录与工具>
