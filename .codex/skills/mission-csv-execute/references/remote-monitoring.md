# 远程运行长训练监控与透明度规范 (Remote Monitoring)

长时间远程运行必须保持全程透明，严禁陷入信息黑盒。

## 1. 运行透明度三重机制

1. **启动前时间沙盘（Time Sandbox）**：
   正式全量运行启动前，调用估算工具：
   ```bash
   python3 .agents/harness/remote/remote_progress.py --estimate \
     --total-steps <总步数> --smoke-summary <smoke产物路径>
   ```
   只有 smoke 实际记录了计时字段，才能拆分冷启动与边际耗时。输出会注明估计来源；跨 shot、batch、设备或存储外推须标为近似，不用整次单集 smoke 耗时冒充稳态单步。ETA 仅涵盖计算，不包括回传、核验和审查；没有可靠计时就报告未知，不为凑 ETA 追加 GPU 探针。
2. **30 分钟周期进度心跳（Heartbeat）**：
   公共入口 `remote_run.py` 每 30 分钟输出结构化 stderr 心跳（默认 `--heartbeat-seconds 1800`），包含计数、速度、已用时间、可用的计算 ETA 与指标。后续 ETA 优先用观察区间的实际进展修正；零步数或缺计时仍显示状态，查询失败显式提示而不静默。
3. **秒级即时查询探针（Active Query）**：
   当用户在会话中询问“跑到哪了？”或“预计几点跑完？”时，运行：
   ```bash
   python3 .agents/harness/remote/remote_progress.py
   ```
   使用原 RunID/RunSpec 显式查询并汇报；耗时取决于连接，不承诺弱网下 1 秒返回，也不为查询重新 launch。

## 2. 等待期唤醒纪律（禁止轮询模型）

- **禁止短周期轮询**：严禁反复调用 shell session 或按固定时间向模型输出“仍在运行”废话。
- **Pi 宿主**：`rrctl_event_wait` 运行 `--observe-only`；已启动 Run 必须 `resume:true`。心跳显示为用户可见的非唤醒消息，不调用模型。计算到达 terminal/attention 即唤醒，执行器立即继续原 Run 的 pull/诊断/ingest，不等回传才通知，也不等用户说“继续”。
- **Codex 宿主**：`python3 .agents/harness/remote/agent_event_wait.py start <runspec> --resume` 结束当前 turn；relay 同样只观察计算终态。当前 `codex queue` 无非唤醒通知接口，因此仅对真实变化的进度至多每 30 分钟队列通知一次（会唤醒会话），禁止借通知再做周期 shell 轮询；终态独立立即通知并续跑 pull/ingest。
- **回传阶段**：与计算 ETA 分开。pull 的 stderr 报文件数、已传/总字节、速度和单文件有效预算；中断只重试同 Run 的缺项。`pull_busy` 表示已有传输，不能另起同目标 pull 或清空其 staging。
- **先定位再交接**：pull 成功后的第一个动作是把产物发布到该 Run 的规范目录（`remote_artifacts/<ExpID>/<RunID>/`）并落 manifest，然后才把收尾交给 relay 或后台观察。在此之前不得先手工搬动、改名或重排目录——relay 补拉会按 manifest 重建路径，与人工规整互相覆盖，产生“文件突然不存在”。已手工动过产物时，先按 manifest 核验现有位置，只补缺项，不整批重拉。
- **无本地守护进程**：不得创建 systemd user unit、全局 nohup 守护或常驻后台 watcher。rrctl 保持 daemonless，远端由独立会话管理。
