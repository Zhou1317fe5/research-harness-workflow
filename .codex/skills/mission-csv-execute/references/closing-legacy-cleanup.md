# Closing Legacy & Deferred Cleanup Protocol

在进入收尾审查时，若前序存在卡住、重试废弃或门限跳过的行阻碍收工，按本规程清算后方可顺利通过 `final_ready.py` 机械检查，严禁死锁。

## 1. 前置未完成行清算协议

1. **被取代的历史重试行**：
   - 凡已有更高轮次替代的历史 smoke/probe 行（如已有 r2 替代 r1），自动使用 `scripts/supersede_remote_binding.py` 或更新为 `remote_state:not_applicable`、`artifact_policy:pre_review_smoke_only` 并结项。
2. **预注册门控合法跳过行**：
   - 因未达单域晋级阈值、探索期无信号（Rule 33）或命中预算上限而合法跳过的后续训练/消融行，统一更新为四项完成 + `remote_state:not_applicable`，并在 notes 标注 `scientific_outcome:gate_failed`（或 `not_run_by_preregistered_gate`）；
   - 关联的所有 Claim 必须在 `<stem>.claims.json` 中同步标记为 `status:not_run_by_preregistered_gate` 并附注决策证据，严禁遗留 `pending`。
3. **已知次要缺陷与工程债务**：
   - 不阻塞本轮核心结论的次要工程缺陷，统一转入 `<stem>.deferred.json`（分配 `DF-NNN` 编号）并在 handoff 隐式引用，其关联 claim 标记为 `status:out_of_scope`，在 closing notes 记录 `deferred_coverage`，不阻止当前 CSV 机械闭环。

## 2. 探索阶段工程追溯类缺口处理纪律

在探索阶段（`experiment_type: probe / baseline_reproduction` 或 Spec 声明为探索期/机制 screen 时）：
- 只要核心科学证据（机制是否起作用、主指标增减）已有明确事实（即使是 `inconclusive` 或 `no_signal`），任何**非核心计算逻辑的工程追溯项**（如未来日志 digest 记录、离线分析 CLI 打印单次运行的格式小瑕疵、辅助字段落盘）**一律归入 `deferred_improvement` 写入 `<stem>.deferred.json`**；
- **严禁在探索阶段的 closing review 中将此类非核心工程修饰项判定为 `current-scope gap` 并追加 follow-up issue 和第二轮 `REVIEW-(N+1)`**；
- 必须直接在 `REVIEW-01` 以 `evidence-close` 交付闭环收工，避免收尾阶段膨胀。

## 3. 发现问题的分类与处置

| 分类 | 判断标准 | 处置动作 |
|---|---|---|
| `current-scope gap` | 违反 source doc、当前 issue acceptance criteria、核心科学数据流或声明-证据合同 | 现在修；无法在原行修完时追加正式 follow-up issue 和下一轮 REVIEW，继续执行 |
| `human-required blocker` | 只有用户或外部主体能提供授权、凭证、付费/业务决定或不可逆操作 | 记录 blocker，继续其他可推进行；全部剩余项都属于此类时才停 |
| `deferred_improvement` | 当前承诺已满足，但观察到范围外的质量、代码追溯或辅助工具改进 | 写入 deferred ledger，不追加 issue |
| `future_decision` | 当前承诺已满足，但后续产品或架构存在真实取舍需用户决定 | 写入 deferred ledger，不追加 issue |

- 只有 `current-scope gap` 转为后续执行工作（追加新 issue + `REVIEW-(N+1)`）；
- `deferred_improvement` 与 `future_decision` 进入 `<stem>.deferred.json` 并运行 `python scripts/validate_deferred_ledger.py <csv-path>`。

## 4. Human-Required Blockers 最小停滞边界

只有绝对不可替代的人类操作才允许停下：
- 需要破坏性或不可逆授权（删除用户数据、重置数据库、覆盖未提交工作）；
- 缺少外部凭证、账号、Token、付费授权；
- 必需的外部物理动作；
- 继续执行会要求伪造证据或凭证。

普通架构分歧、范围细节模糊或不影响结论的实现取舍，在 review log 记录 `Assumption / Risk` 后选择最小可逆路径继续推进，严禁原地停滞。
