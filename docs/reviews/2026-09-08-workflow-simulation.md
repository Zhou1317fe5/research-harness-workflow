# 工作流隔离模拟与修复

模拟已完成。批准文档、任务 CSV、实际 CLI 执行、审查、handoff、提交和恢复走通；rrctl 通过本地传输实际启动进程，完成首步检查、等待、拉取、索引恢复和失败诊断。科研记录、事务恢复及 MCP 同步使用独立副本验证。

证据目录：`/tmp/rhw-workflow-sim-eo061t6d/`。所有 fixture、测试脚本和模拟科研仓库都在该目录。真实论文复现使用另一独立仓库，结果另行记录。

## 验证结果

| 范围 | 实际结果 | 证据入口 |
|---|---|---|
| Spec、CSV、审查、handoff、提交、重复恢复 | 修复后完整重放 44 步到终态，副本工作树干净 | `csv-evidence/after-normal/commands.jsonl` |
| CSV 损坏输入、恢复、远程状态、兼容输入、阶段门、Git 隔离 | 44/44 行为检查符合预期 | `csv-evidence/final-verification-summary.json` |
| 科研来源、状态、事务、记录投影、钩子与本地 MCP | 106/106 行为检查通过，回放命令退出 0 | `memory-evidence/acceptance-commands.json` |
| rrctl 正常生命周期 | ready、launch、wait、pull、resume、inspect 均退出 0 | `evidence/*-sim-normal-01.json` |
| rrctl 失败与取消 | 失败运行拒绝正常 pull；诊断拉取成功；重复 launch 被拒绝；显式 abort 只停止本次任务 | `run_negative_simulation.py`、`evidence/` |
| periodic health 异常 | wait 退出 2 后任务仍在运行，显式 abort 后终止 | `evidence/wait-sim-health-attention.json` |
| smoke JSONL、checkpoint 清理与边界 | 实际清理成功；外部符号链接目标保留；正式运行未启用清理 | `evidence/cleanup-after.json`、`cleanup-boundary.json` |
| 静态与镜像 | 81 个已跟踪 Python 文件解析成功，46 个 skill 文件镜像一致，链接和 diff 检查通过 | `evidence/final-static-validation.json` |

拒绝场景中的非零退出码是预期结果；每个场景同时检查输出和文件后态。测试数量不表示代码覆盖率，也不替代真实 GPU 评测。

## 问题与处理

| 编号 | 问题与影响 | 修复 | 提交 |
|---|---|---|---|
| F01 | Spec 模板含校验器拒绝的元数据，缺少必需章节，并引用旧科研字段 | 对齐 canonical 格式，科研信息放入正文，更新记录与停止条件引用 | `0e37f09` |
| F02 | smoke builder 丢失 JSONL 格式，正常多行进度被拒绝 | 保留 progress_format | `8ad60df` |
| F03 | 小型 .pt checkpoint 未删除，摘要却声称清理完成 | 补齐常见 checkpoint 目录和文件模式，验证实际删除与外部路径边界 | `8ad60df` |
| F04 | rrctl 支持的 GPU 观测策略无法通过 builder 传入 | 传递并校验 required/advisory/disabled | `8ad60df` |
| F05 | 文档和生成器沿用旧验证枚举，更新时强加 BOM | 统一验证枚举，保留输入编码 | `a31232c` |
| F06 | 带额外列的 CSV 在报错前被截成表头 | 完整解析后序列化，再原子替换；错误输入保持原字节 | `a31232c` |
| F07 | 截断 CSV、数组形 ledger/status 触发裸异常 | 共享行宽、ID、JSON 类型检查，返回结构化诊断 | `a31232c` |
| F08 | 损坏 review、未完成 claim、失败 handoff 仍能被恢复器判为完成 | 复用实际 claim、review 和 handoff 合同，核对所有引用 | `a31232c` |
| F09 | ingested 缺产物、仅有 artifact 字样或虚构路径仍可终态 | 要求显式产物引用并核验本地文件或目录 | `a31232c` |
| F10 | 较新的平铺任务抢占目录任务恢复优先级 | 先按目录任务排序，再比较修改时间 | `a31232c` |
| F11 | 显式外部 19 列 CSV 能接收却不能推进 | 现有入口接受兼容表头并保留列数；新建任务仍用 28 列 | `a31232c` |
| F12 | 效果预测被当成停止门，阶段请求不能走 stdin | 显式 effect_prediction 只产生 advisory；停止及正确性约束保留，增加 stdin 入口 | `0e63811` |
| F13 | commit hook 失败后污染 index，重试误报用户冲突 | tracked 文件直接 commit --only；新文件 intent 失败时精确撤回 | `40f71d7` |
| F14 | 处理旧文档事件会覆盖当前版本的同步内容 | 只有当前事件更新文档同步任务 | `bd8bf36` |
| F15 | 不可解码主分析中断全部来源扫描 | 失败来源进入待处理诊断，其他来源继续采集 | `bd8bf36` |
| F16 | 未采纳建议可以取代已生效的用户决定 | ACTIVE 决定只由新的 ACTIVE 用户决定取代 | `bd8bf36` |
| F17 | 结果改判或撤回后 STATE 留下旧指针 | 清除本工具生成且已失效的 Current Model 指针 | `bd8bf36` |
| F18 | 配置的实验维度在记忆投影中丢失 | Run 同时保存 dimensions 对象与兼容字段 | `0eb144b` |
| F19 | 派生索引的 CRLF 阻断科研仓库 diff 检查 | 明确使用 LF | `0eb144b` |
| F20 | 单 Run 的明确指标与协议仍一直留空 | 单 Run 投影指标；协议仅在来源明确一致时投影，多 Run 不猜聚合 | `0eb144b` |

另按实际使用要求增加 `hooks_enabled`。设为 false 后，宿主已经缓存的回调也立即返回空结果；手工 CLI 和其他项目不受影响。本模板仓库已停用自动回调。

各问题的原始失败、修复位置和命令见 `csv-evidence/result-summary.md`、`memory-evidence/result-summary.md`。rrctl 修复前后的原始输出分别保存在 `evidence/smoke-adapter-sim-smoke-before.json`、`smoke-adapter-sim-smoke-after.json`、`cleanup-before.json` 和 `cleanup-after.json`。

## 复验入口与边界

科研记录可用 `python /tmp/rhw-workflow-sim-eo061t6d/memory-evidence/replay_all.py <新label>` 在新副本回放。CSV 重放使用 `csv-evidence/workflow_sim.py`，通过 CSV_SIM_ROOT 指定新 clone，CSV_SIM_EVIDENCE 指定独立证据目录。控制面脚本为 `run_control_simulation.py` 和 `run_negative_simulation.py`，只能使用它们的隔离项目和新 RunID。

十个本地 skill 的入口及分工已核对，`.codex` 与 `.claude` 同步。普通任务、可选调试、风险分流和科学 PRERUN 的边界保留。科学 reviewer 的真实 GPU 输入与正式结果归属将在论文复现中验证。

模拟的 MCP 服务是本地 HTTP fixture，覆盖 JSON/SSE、异步操作、重试和项目隔离。另已通过实际配置读取 Hindsight 工具列表和请求字段；真实内容的同步与召回尚待独立论文项目验证。模拟指标只用于检验记录链路。

后续真实任务采用官方 [RSRM 仓库](https://github.com/Sparkling-Water/RSRM)。该仓库提供评测代码和配置；本轮检索尚未取得论文原始指标表，比较结论需注明这个证据缺口。
