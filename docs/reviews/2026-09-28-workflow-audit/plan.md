# 工作流系统性审查方案

- 审计日期：2026-09-28
- 审计基线：`research-harness-workflow` @ `3ef28b1`（main）
- 审计范围：本工作流全部 skill / harness 脚本 / rrctl / Pi 扩展 / 文档契约
- 审计起因：2026-09-27 cdfss-dinov3 会话暴露出若干工作流缺陷，已修复其暴露的部分；本方案是对**整个工作流**的系统性审查，不承诺"零 bug"，承诺"可机器化的一致性全部机器化并加防漂移，不可机器化的规则全部独立通读并登记冲突"。
- **执行纪律**：先记录、由用户决定是否修改。审计期间发现的每个问题先写入 `findings.jsonl`（status=open），未经用户批准不改代码。

## 资产基线（2026-09-28 实测）

| 层 | 模块 | 规模 | 现有测试 |
|---|---|---|---|
| 远程控制 | rrctl（14 模块） | ~7,000 行 | rrctl/tests（61）+ remote/tests（77） |
| Mission 执行 | mission-csv-execute（17 scripts） | ~6,000 行 | workflow/tests（61） |
| 其他 skill 脚本 | pre-run / post-run / mission-spec / recovery / snippet | ~3,000 行 | 部分 |
| 审稿与远程构建 | reviewer_job / review_model / build_rrctl_runspec / remote_run | ~2,900 行 | remote/tests |
| 基础子系统 | common / pipeline / records / memory / workflow | ~3,800 行 | 零散 |
| Pi 扩展 | rrctl-events / research-memory / research-advisor / workflow-output | 647 行 TS | .pi/tests（21） |
| 文档/契约 | 11 skill SKILL.md + references + AGENTS/CLAUDE ×4 宿主镜像 | 53 个 .codex/skills 文件 | parity/mirror/model-sync |

## 分层（L0→L5，自底向上）

### L0 — 静态一致性与契约（机器）
| 项 | 方法 | 判据 |
|---|---|---|
| 0.1 全量镜像一致性 | `test_codex_claude_skill_mirrors_match` 从硬编码 25 条改为自动枚举 `.codex/skills` 全部文件逐字节比对 `.claude`；宿主专属文件显式豁免 | 53/53 受守护 |
| 0.2 引用抽取对账 | 静态解析全部 SKILL.md+references 的反引号引用（命令/flag/路径/函数/字段/schema 字面量）→ 对账到 argparse/文件系统/AST/schema 常量 | 每个引用可解析到唯一存在 |
| 0.3 schema 双向一致 | CSV 28 列、events sidecar、verdict、record.json、result-analysis、outcome/deferred ledger 各 schema 读写字段对齐 | 读写字段集无单向漂移 |
| 0.4 规则重复聚类 | 关键词聚类输出"同一规则 N 文件 N 表述"清单 | 产出《规则 canonical 待裁定清单》→ 移交 L4 |

### L5 — 历史会话回放（人+机器）
- 选 5–6 个代表性 mission 会话（workflow-full-reproduction、author-version-full-reproduction、SCOPE 系列、safe 分支等），逐份回放故障/阻塞/授权事件 → 归类到 L0–L4 → 判已修/未修。
- 汇总《故障模式频率表》，据此重排 L1–L3 优先级。

### L1 — 单元与状态机（机器）
1. rrctl 未测模块：health/readiness/finalization/cleanup/security/output_limits/zipapp_builder（接受/拒绝矩阵）。
2. mission-csv-execute 盲区：stage_flow 图、remote_route 5 类路由矩阵、compact_artifacts、ensure_*_row 幂等、run_vision_review 证据绑定、validate_deferred_ledger。
3. 基础子系统：memory（hindsight 双通道、hook 往返）、pipeline、records、mission_state。
4. 全局状态机迁移表：CSV dev/review/git/remote ∪ remote_state 合法迁移穷举，非法全拒。

### L4 — 文档/流程契约与人机协议（人+advisor）
- 裁定 L0 聚类报告的 canonical 规则；其余改指向 + 加防复述校验。
- reviewer/advisor 通读 11 个 SKILL.md 找规则冲突 → 《规则冲突登记表》。
- 每条硬规则标记【机器 gate】或【人工约定】写回文档。
- goal 协议与各 skill 停止条件一致性核对。

### L2 — 集成与生命周期（机器+临时仓+假后端）
- Mission 端到端 5 类路由各一条（临时 git 仓 + 假 rrctl 后端）。
- 中断恢复矩阵：launch/wait/pull/reviewer_wait/ingest/closing 各阶段 kill -9 后恢复。
- rrctl 生命周期：abort/resume/cleanup/GPU lease/进程归属。
- reviewer_job × gate 端到端 3 verdict × 2 backend 矩阵。
- workflow_sync 全状态机（diverged/deleted/target_ahead/template_only/符号链接/新文件）。

### L3 — 安全与故障注入（机器+威胁建模）
- 凭据三通道（env/argv/日志）逐调用点审计，断言只传变量名。
- 注入面：路径穿越、命令注入、SSH argv。
- TOCTOU：pull manifest 验签→落盘；verdict 验签→gate 开门。
- 故障注入：kill -9、SSH 断、半写 JSON、磁盘满、编码异常、时钟回拨 → 全部 fail-closed。
- 并发：双 Executor 同写 CSV/sidecar/RunID。

## 执行排期

| 批 | 层 | 分包 |
|---|---|---|
| 批 1 | L0 全部 | 主 Executor |
| 批 2 | L5 回放 | 4-5 个 scout/reviewer 子代理并行 |
| 批 3 | L1 全部 | 4 组并行 worker |
| 批 4 | L4 全部 | reviewer + advisor 通读 |
| 批 5 | L2 集成 | 主 Executor 框架 + worker 矩阵 |
| 批 6 | L3 安全 | security-auditor 主导 |

每批结束：跑全部测试入口 + 更新 `findings.jsonl` + `coverage.md`。

## 明确边界

- 不承诺零 bug；承诺可机器化一致性全部机器化、不可机器化规则全部通读并登记、故障模式全部验证 fail-closed。
- 叙事逻辑绝对自洽不在承诺范围（处置=通读+登记+明示人工约定）。
- L2/L3 为抽样矩阵非全状态空间。

## 产出文件

- `plan.md`（本文件）
- `inventory.json`（资产清单，L0 0.1 生成）
- `findings.jsonl`（逐条发现，贯穿全程）
- `coverage.md`（每层覆盖矩阵，批末更新）
- `replay-<date>-<mission>.md`（L5 每会话一份）
- `rule-canonical.md`（L4 规则裁定）
- `rule-conflicts.md`（L4 冲突登记）
