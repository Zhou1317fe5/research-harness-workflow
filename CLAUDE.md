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
- 明确请求 Mission 恢复（包括无参数 `mission`），或已选定 Mission 需要恢复上下文时，由 `mission-recovery` 只扫描 `issues/`。
- 普通任务目标和验收清楚时直接执行；多步任务维护 plan。
- 分析、审查、解释、Q&A 直接回答。

普通任务完成用户要求的实现或产物、适用验证及本次改动引起的问题修复后，再交付结果与限制。已有授权内继续推进；用户要求只分析、先审方案、暂停或不提交时，遵守该边界。

**何时进 mission**：任务产生进入台账的新科研结论（新 ExpID、新指标、baseline 对照）时才进，走 CSV 全账。画图、选样例、论文正文、复用已有结果不进，直接执行并留一份 `result-summary.md`。不允许跑完整 PRERUN 却不建 CSV。

需求未定时先澄清目标、约束和验收。`mission-spec` 每次只问一个仍会改变方案的问题；方案确定后尽快写 draft。未批准的 spec 禁止实现，批准后不再另写 implementation plan。

## 实验阶段（探索 → 定模型 → 论文证据）

科研开销必须按这个顺序投。四个问题不能用同一种规模回答，也不得捆成一条必过条件：

| 问题 | 属于 | 最小规模 |
|------|------|----------|
| 机制到底有没有产生作用？ | **探索** | 对照 + 方法（代码路径不同则加一个“模块关闭”臂）× **单一配置** |
| 增益来自哪个组件？ | 定模型后（消融） | 多臂 × 已选配置 |
| 参数敏不敏感？ | 定模型后 | 多配置 × 少数臂 |
| 换 fold / seed 还成立吗？ | 论文前 | 已选配置 × 多 replicate |

- **先答第一个。** 后三个代价高一个量级，必须在看到信号后再投入。
- **配置网格与多臂消融不得写成进入后续阶段的前置条件。** “完整矩阵通过才能进下一步”会把
  “机制有没有信号”排在“参数扫描跑完”之后，使最有信息量的问题最晚被回答。
- **只跑改动的那一侧。** 基线是复现的已发表模型，不参与搜索、不重复测量；候选 commit、配置、
  新增模块的变化都不使已记录基线失效。
- **网格条目数不是严谨度。** 在同一个未验证的机制上跑多个配置不增加结论强度，只增加开销。
- 机制 screen 的 episode / 配置在跑前定好，不根据表现筛样本。
- **故事与涨点双轨解耦。** 架构设计允许且倡导解耦：叙事模块负责顶会前沿立意（Title / Abstract / Figure 1），保分模块在底层生效兜底指标；不强求单一模块同时兼顾高新颖性与高涨点。
- **大故事保持定力。** 探索期初测指标微亏时，优先通过即插即用的工程与数值 Trick（动态阈值、原型校准、正则惩罚）就地攻坚拉升指标，严禁轻易推翻已确立的核心学术叙事。
- **单域初筛与有界止损。** 探索阶段必须在单一典型数据集上看到坚挺信号（净涨点 ≥1.0）后，方可启动多域或全量基准；初测微亏时最多允许尝试 1~2 个兜底 Trick 补丁，若两轮后仍无有效信号，果断以 `inconclusive` 结项并交接给讨论会话，严禁展开第三轮无意义微调或死磕。

细节见 `docs/specs/TEMPLATE-spec.md` 的 9.0.1 与训练评估计划节。

# 实施纪律

- 改动紧贴批准范围和现有代码模式，不混入无关重构、格式化或调试痕迹。**不为“结构上不可能出错”而增加防御代码**：只在外部输入边界与实际出过问题的路径上校验，其余处保持简单（适用范围见 `systematic-debugging/defense-in-depth.md`）。
- 先读即将修改的代码；使用结构化解析器处理 CSV、JSON、TOML 等格式。
- 系统边界校验外部输入；shell、SQL 使用安全参数传递。
- 功能逻辑只写在 canonical 实现文件；兼容 wrapper 只维护向后兼容，不承载行为。

# 验证

**门槛匹配声称，不追求内部完美。** 验证强度按“你要声称什么”确定，不按“内部能不能更完美”确定：

- 要写进论文的结论 → 证据必须支持该结论，不夸大、不冒充；
- 已知的小偏差、近似、工程债 → 允许保留，写清它们不影响结论即可；
- 唯一必须处理的是**可能翻转结论的未解决疑点**（如近似误差大到改变候选排序、或泄漏使对比不成立）；
- 不为“让实现与理论精确一致”付出与结论无关的成本。

本文件与各 skill 里所有验证、证据、门禁条款都按这一条解释：它们防的是**结论不可信**与**声明与证据不符**，不是要求实现无瑕疵。论文读者看的是数字与结论，不会验证实现是否与理论每位小数一致。

**不做 TDD / test-first / RED。** 科研代码的正确性由科学契约与数值证据判定，不由先写测试判定。这一条覆盖 skill 中任何 test-first 表述。

**执行栈分工**：代码正确性、单元测试、编译、参数链路与配置解析可在本地或远程 CPU 完成；**真实训练启动、step 级验证、GPU 显存、loss/log/checkpoint 必须在远程真实运行中验证**。验证地点按便利与成本选择——**不强制在本地**，远程 CPU 同样可用，能用 GPU 显著加速时也可用 GPU。验证范围按改动影响面确定，不追求“把能跑的都跑一遍”。**离线指标统计与结果汇总（从已拉回的预测/产物计算混淆矩阵、mIoU、准确率等纯 CPU 聚合分析，耗时通常仅数秒）属于本地轻量处理，直接在本地环境执行，严禁将预测数据打包传回远端 CPU 额外启动 rrctl 运行**。只有生成模型预测、提取特征或真实训练需要 GPU/专用计算依赖时才走远端。

远程执行统一经 **rrctl 的 process 后端**，`fallback_allowed` 必须为 `false`：rrctl 不可用、readiness 失败或 launch 失败时停在当前 row，**不得回退临时 SSH/nohup 拼接冒充同一控制面**。GPU 运行取得资源归属后才启动；观察超时沿用原 RunID 恢复。生命周期、首步 gate、巡检口径与拉取策略见 `mission-csv-execute/references/remote-run.md`。

**PRERUN 单次审查与 Blocker 闭环**：运行前科学审查只执行单次定点审查，坚决遏制审查标准发散与无限循环。审查发现缺陷或探针缺失时，必须列为具有明确证伪条件的 Blockers；主代理在原实施行完成修复或补齐运行证据，经 `closure.json` 机器校验闭合后即自动放行启动，严禁发起无边界的二次 full review。`not_evaluable` 仅限 Spec 逻辑自相矛盾等不可判定情形；运行期日志或梯度等价性证据缺失一律按可证伪 Blocker 处理。

测试是 commit、push、PR 前的门禁，**范围按改动影响面与将要声称的结论确定**；不设测试数量配额，也不因翻页或进入下一步机械重跑。只报告实际运行过的命令、退出码和结果。测试范围分级、`validation_gap` 标注与 claim 终态规则见 `mission-csv-execute`。

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

需要科研历史或恢复科研任务时，按 `STATE.md → CONCLUSIONS.md → EXPERIMENTS.csv` 定位相关实验，再读对应 `record.json → analysis.md`。普通代码或文档任务先读待改文件及必要调用方。已读且仍在当前上下文中的未变化内容可复用；内容变化、上下文丢失或出现新疑点时再定向补读。只有核验具体实验时才读对应 `remote_artifacts/<ExpID>/`；`record.json` 中路径以项目根解析。

研究产物最低关联：SpecID + ExpID + Branch + Commit；多次远程运行补 RunID。

# 安全与进程

- 未获授权不运行破坏性命令，不覆盖或丢弃用户改动，不使用 `git reset --hard`。（`git reset --hard`、`git clean -f`、`git checkout -- <path>`、`git push --force`、越界的 `rm -rf`，以及 `~/.ssh`、`~/.aws` 读取，已由全局 cc-safety-net 的语义分析拦截，**无需在此重复**；本节只保留它拦不住的部分。）
- **凭据只传变量名或变量引用，永不传值**：`set -a; source <env file>; set +a` 后引用变量；控制面用 `password_env` 传变量名。不硬编码、提交或输出密钥、凭证、API Key。
- **禁止整体打印含凭据的文件**（`cat`/`head`/`tail`/`sed -n`/`nl`）。**实测 cc-safety-net 不拦**直接读取凭据文件、也不拦回显密钥变量值（`echo "$SSH_PASSWORD"`）——它拦的是 `~/.ssh`、`~/.aws` 这类敏感路径，所以这条必须自己守。会话记录把 stdout 永久落盘，一次打印即等于永久泄露；自制脱敏不算防护。确认存在性用 `echo "KEY=${KEY:+set}"`，看结构用 `grep -oE '^[A-Za-z_]+='`。
- 非交互 SSH 下不假设 `python` / `conda` 在 `PATH`，远程 Python 命令必须显式激活环境。
- 不终止非当前任务启动的进程。长生命周期进程尽量少开，启动前检查可复用实例，结束即回收。
- **命令超时优先用工具自带参数**：`ssh -o ConnectTimeout=`、`rrctl --max-wait-seconds`、工具内部预算；shell 层 `timeout` 只是额外一层，不改变权限判定。解释器内联代码里的删除 API（如 `shutil` 的 `rmtree`）由本地 `pi-interpreter-guard` 扩展按内容拦截；cc-safety-net 在 `standard` 级别放行它们，而改用其 `paranoid_interpreters` 会误伤大量正常内联命令。

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

命中 skill 时按宿主读取对应副本，不读 canonical 源：Pi 会话存在 `.pi/skills/<name>/` 时只读该份，Codex/Claude 会话读各自目录；`.agents/skills/`（链接到 `.codex/skills/`）是 canonical 同步源，不是任何宿主的运行副本。两边仅允许在标记的宿主专属块（如 reviewer launcher）内不同，其余内容必须逐字一致。

- `mission`：spec、CSV、执行与恢复的统一入口。
- `mission-spec`：需求讨论、canonical spec 和批准边界。
- `mission-approved-doc`：approved spec 到 issues 工件。
- `mission-csv-execute`：CSV 闭环执行、证据、review 与 handoff。
- `mission-recovery`：只扫描 `issues/` 的恢复入口。
- `pre-run-implementation-review`：运行前科学实施审查、风险分流与等价性探针。
- `systematic-debugging`：可选的故障定位辅助；根因不明或跨模块排查时按需使用，不是执行或提交的前置条件。
- `humanizer-zh`：必装的自然语言处理 skill。
- `smart-search-cli`：外部资料、论文与文档检索。
- `research-memory`：科研结论、决定与状态槽的登记与召回（STATE / CONCLUSIONS / record）。
- `remote-run-snippet`：从 intent 解析远程 train/eval 命令。
- `research-result-commit`：当前 ExpID 研究产物的合并提交。
- `post-run-result-analysis`：运行后由独立 reviewer 读原始证据产出 `RESULT-ANALYSIS-01` 结论。
- `lite-arch` / `lite-arch-recall`：建议安装的 ADR 记录与召回 skills。

---

# 本项目补充

> 以下两节是**每个项目必须自行填写**的部分，模板不预设内容。
> 通用规则（上方全部章节）不需要改动。

## 研究背景

<一到两句：研究方向、基线方法、主指标。例如「X 模型 + Y 任务。基线为 Z。目标是在官方 evaluation setup 下提升 <主指标>，<辅助指标> 只作参考。」>

## 路径

- 远程连接使用 `.agents/harness/config/profiles.json`，凭据和环境只读 `.agents/harness/config/.env`（键：`SSH_PASSWORD` / `REMOTE_CONDA_ENV` / `REMOTE_CONDA_SH`）；
  远程 Python 须显式激活环境。
- <项目训练/评估脚本位置，如 `scripts/train_*.sh` / `scripts/eval_*.sh`>
- <项目数据集路径、checkpoint 路径等>
- <其他项目专有目录与工具>
