---
name: mission-recovery
description: Use when the user wants to continue, resume, or recover interrupted mission work and the agent needs to locate unfinished CSV artifacts before execution resumes.
---

你现在是「任务恢复扫描器」。

# 目标

找到未完成的 mission 工件，选择恢复对象，委托给对应的执行 skill。

# 触发条件

- `mission` 无参数调用
- 用户说 "continue" / "resume" / "继续" / "接着做"
- 检测到上下文丢失（compaction / 会话重启）

# 扫描顺序

只扫描 `issues/`。运行确定性扫描器；它复用 closing 的四状态、remote terminal、review contract 与 artifact 判定：

```bash
python <skill-dir>/scripts/scan_recovery.py --repo-root <repo-root>
```

扫描器先检查目录化 CSV，再检查平铺 CSV，找到未完成的 CSV：

先使用 `issues/.missions.json` 中的当前任务指针。`resume_target.kind=csv` 时恢复该 CSV，
即使其他 CSV 的 mtime 更新；`kind=spec` 时沿已登记路径交给 `mission` 校验并路由，
不扫描 docs/specs。`kind=paused` 不自动推进；当前用户明确要求继续该任务时，先用
`mission_state.py transition` 恢复 active（已有 CSV）或 preparing（尚无 CSV），再恢复。
cancelled/superseded/completed 不进入候选；当前指针损坏或缺文件时报告具体错误，不回退旧任务。

```
扫描: issues/*/*.csv
条件: 任一行 NOT 同时满足
  dev_state=已完成 AND review_initial_state=已完成
  AND review_regression_state=已完成 AND git_state=已提交
  AND remote_state in {not_applicable, completed, ingested}
或最终 REVIEW 行缺少非 pending 的 review result / scientific outcome / claim coverage、review JSON、handoff 或 claim ledger
优先级: 最近修改的文件优先

扫描: issues/*.csv
排除: issues/TEMPLATE.csv（schema/template authority，不是任务工件）
条件: 同上
优先级: 若 `issues/*/*.csv` 无可恢复项，再按最近修改时间排序；若同 stem 同时存在目录化和平铺 CSV，优先目录化 CSV

```

恢复器不扫描其他目录。用户显式提供的任意合法外部 CSV 仍由 `mission` 直接路由到 `mission-csv-execute`。

## 无可恢复内容

```
输出: "没有找到可恢复的任务。使用 mission 提供 spec、CSV 路径或任务描述开始新任务。"
停止。
```

# 恢复宣告格式

```
上下文已恢复
- 来源: <csv-path>
- 任务: <doc-title 或 task-name>
- 进度: X/Y issues 已完成
- 恢复点: [<id>] <title>
- 上次完成: [<prev-id>] <prev-title>
- 已知问题: <阻塞项，如有>
```

# 状态校验

定位 CSV 后：

1. 从磁盘读取 CSV
2. 使用扫描器验证表头与 `issues/TEMPLATE.csv` 的科研 28 列 schema 完全一致，并复用 closing terminal predicate
3. 交叉检查状态一致性：
   - `git_state=已提交` 但代码实际未提交？→ 重置为 `未提交`
   - `dev_state=已完成` 但 `review_*` 还是 `未开始`？→ 从 review 阶段恢复
   - `dev_state=进行中` → 从实现阶段恢复（先重新收集上下文）
4. 发现不一致则修正 CSV 状态后再恢复

# 多个可恢复任务

只有没有适用的当前指针，且当前会话也未明确选定任务时，多个未完成 CSV 才需要选择：

```
找到多个可恢复任务：
1. issues/2026-03-20_10-00-00-add-auth/2026-03-20_10-00-00-add-auth.csv (3/7 已完成)
2. issues/2026-04-27-agent-loop-cleanup/2026-04-27-agent-loop-cleanup.csv (5/10 已完成)
恢复哪个？（输入序号或 "all" 按顺序执行）
```

等待用户选择后恢复。

已有当前任务或本轮明确选择时直接继续，不因扫描出其他旧 CSV 重复询问。

# 委托执行

找到恢复对象后：
- 委托给 `mission-csv-execute`

恢复器自己不执行 issue loop，只负责**找到并转发**。
