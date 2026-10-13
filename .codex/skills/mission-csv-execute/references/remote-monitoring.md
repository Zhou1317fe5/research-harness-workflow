# 远程运行长训练监控与透明度规范 (Remote Monitoring)

长时间远程运行必须保持全程透明，严禁陷入信息黑盒。

## 1. 运行透明度三重机制

1. **启动前时间沙盘（Time Sandbox）**：
   正式全量运行启动前，调用估算工具：
   ```bash
   python3 .agents/harness/remote/remote_progress.py --estimate \
     --total-steps <总步数> --smoke-summary <smoke产物路径>
   ```
   工具会自动解耦冷启动耗时与边际耗时，推算总耗时与预计完成时刻（当地 CST 时区），并在对话层启动汇报中主动告知用户。
2. **30 分钟周期进度心跳（Heartbeat）**：
   公共入口 `remote_run.py` 内置 30 分钟进度心跳（默认 `--heartbeat-seconds 1800`）。在长时间等待期间，每隔 30 分钟在控制台打印一行进度心跳、速度、已用时间、预计剩余时间、预计完成时刻与当前指标。
3. **秒级即时查询探针（Active Query）**：
   当用户在会话中询问“跑到哪了？”或“预计几点跑完？”时，运行：
   ```bash
   python3 .agents/harness/remote/remote_progress.py
   ```
   1 秒内解析远端状态与进度并向用户汇报。

## 2. 等待期唤醒纪律（禁止轮询模型）

- **禁止短周期轮询**：严禁反复调用 shell session 或按固定时间向模型输出“仍在运行”废话。
- **Pi 宿主**：由 extension 调用 `rrctl_event_wait` 在进程内等待，仅在 terminal/attention 时发送唤醒事件。
- **Codex 宿主**：调用 `python3 .agents/harness/remote/agent_event_wait.py start <runspec>` 结束当前 turn，由 RunID 级 relay 通过 `codex queue` 唤醒原 thread。
- **无本地守护进程**：不得创建 systemd user unit、全局 nohup 守护或常驻后台 watcher。rrctl 保持 daemonless，远端由独立会话管理。
