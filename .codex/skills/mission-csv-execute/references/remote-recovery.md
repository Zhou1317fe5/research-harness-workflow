# 远程运行中断恢复与异常处置规范 (Remote Recovery Protocol)

当远程运行出现等待超时 (124)、本地会话中断、暂态告警或状态未决时，必须严格遵循本规程，严禁擅自重新 launch 或误判任务失败。

## 1. 核心安全铁律

1. **观察超时 (124) 绝不等于远程任务失败**：
   `rrctl wait` 返回 124 仅代表本地观察期限到达（例如前台会话时间限制），远端进程仍在持续运行。**严禁拉取失败诊断、严禁更改为失败状态、严禁重新 launch**。
2. **首步暂态告警不判硬失败**：
   若返回 `first_step_observer_timeout` 或暂时性的 `first_step_attention`（如首个 episode 尚未写出 progress.jsonl 触发的暂态告警，但 inspect 证实 worker 与 workload 正常存活），记录 `remote_state=running_remote` 与 `first_step_gate:pending`，沿用当前 RunID 继续观察。
3. **控制操作未知状态**：
   控制操作返回 `status:unknown` 或 `error.outcome:unknown` 时，只能确认操作结果暂不确定，保留原 RunID 与路径，按返回的 `next_actions` 核对，不要自动重发 launch/abort。

## 2. 会话中断后的恢复三部曲

当会话重启、本地上下文丢失或重新连接时：

```bash
rrctl --json resume --profile <profile> --control-path <control-root>   # 1. 从远端 control state 重建本地索引
rrctl --json inspect <RunID>                                            # 2. 读取权威状态
rrctl --json pull <RunID>                                               # 3. 仅当已达 terminal completed 时拉取
```

- 终态判断以 `inspect` 为准，不以本地日志猜测为准；未达终态就继续等，已达终态才 pull。
- 一键入口恢复：`python3 .agents/harness/remote/remote_run.py issues/<stem>/runs/<RunID>/runspec.json --execute --resume`。

## 3. 记账中断后的安全认领 (Reconcile)

rrctl 已启动但记账未落盘（额度中断、崩溃、上下文丢失）时，CSV 行会停在 `not_applicable`，而远端已有活的 RunID。
**必须使用 `reconcile_remote_binding.py` 以 rrctl inspect 证据核对身份后认领**：

```bash
python3 .agents/skills/mission-csv-execute/scripts/reconcile_remote_binding.py \
  <csv-path> <row_id> <runspec.json> <inspect.json> --next-action "<下一步动作>"
```

- 脚本会逐项比对 `run_id`、`commit`、`run_spec_sha256`，确认 `backend=process` 且各 alive 标志全为 true；
- **严禁直接手工改 `remote_state` 绕过它**，严禁重新启动引发远端撞车。

## 4. 远程硬故障判定与 Abort 标准

健康检查负责报告事实，不自动取得停止权。
**只有确认命中以下硬故障之一，并记录证据后，才显式执行 `rrctl abort <RunID> --yes`**：
- 目标进程确认消失（进程退出且非 completed）；
- 显存发生不可逆 CUDA OOM；
- 最新 progress 出现 NaN / Inf；
- 明确未恢复的 fatal traceback；
- Spec 明确声明的 Stop Condition。

单次低 GPU、单次日志延迟、checkpoint 写盘停顿或一次检查失败只记 `degraded`，严禁停止训练；至少连续两次复核仍异常才升级诊断。
失败或 abort 时使用 `rrctl pull <RunID> --diagnostic` 拉取诊断快照，不进入正式指标入账。
