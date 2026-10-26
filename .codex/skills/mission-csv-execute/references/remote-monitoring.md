# 远程运行长训练监控与透明度规范 (Remote Monitoring)

长时间远程运行必须保持全程透明，严禁陷入信息黑盒。

## 1. 运行透明度三重机制

1. **启动前时间沙盘（Time Sandbox）**：
   正式全量运行启动时，`remote_run.py` 会自动根据规模与步频推算并输出时间沙盘；亦可手动调用估算工具：
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
   默认优先读取本地状态快照（毫秒级响应，不挂起 SSH）；强制穿透远端可加 `--remote`。

## 2. 等待期唤醒纪律（禁止轮询模型与自编汇报表格）

- **禁止短周期轮询与自写 sleep 循环**：严禁在终端编写 `sleep 1200`、`nohup` 脚本或通过循环 SSH 抓取进度；严禁按固定时间向模型唤醒输出“仍在运行”废话；必须统一由 `remote_run.py`、`rrctl_event_wait` 或 `agent_event_wait.py` 维持事件监听。
- **进度样式规范**：向用户汇报任务进度时，**必须统一调用并输出 `python3 .agents/harness/remote/remote_progress.py <RunID>` 生成的标准卡片（`[GPU 任务进度]`）**，严禁模型自行捏造 Unicode/ASCII 边框大表格（`┌───┬───┐`）或长篇格式化公文。
- **严禁过程动态均值冒充结论**：评测运行中途的累加指标（如 step 41/600）仅为未收敛的瞬时中间态，**严禁在运行中途将动态均值当作正式结果与基线对比并输出“超基线/低于基线”误导判断**；只有跑满完整集数落盘的官方 `summary.json` 才是唯一权威结论。
- **Pi 宿主**：`rrctl_event_wait` 运行 `--observe-only`；已启动 Run 必须 `resume:true`。心跳显示为用户可见的非唤醒消息，不调用模型。计算到达 terminal/attention 即唤醒，执行器立即继续原 Run 的 pull/诊断/ingest，不等回传才通知，也不等用户说“继续”。
- **Codex 宿主**：`python3 .agents/harness/remote/agent_event_wait.py start <runspec> --resume` 结束当前 turn；relay 同样只观察计算终态。当前 `codex queue` 无非唤醒通知接口，因此仅对真实变化的进度至多每 30 分钟队列通知一次（会唤醒会话），禁止借通知再做周期 shell 轮询；终态独立立即通知并续跑 pull/ingest。
- **回传阶段**：与计算 ETA 分开。pull 的 stderr 报文件数、已传/总字节、速度和单文件有效预算；中断只重试同 Run 的缺项。`pull_busy` 表示已有传输，不能另起同目标 pull 或清空其 staging。
- **先定位再交接**：pull 成功后的第一个动作是把产物发布到该 Run 的规范目录（`remote_artifacts/<ExpID>/<RunID>/`）并落 manifest，然后才把收尾交给 relay 或后台观察。在此之前不得先手工搬动、改名或重排目录——relay 补拉会按 manifest 重建路径，与人工规整互相覆盖，产生“文件突然不存在”。已手工动过产物时，先按 manifest 核验现有位置，只补缺项，不整批重拉。
- **无本地守护进程**：不得创建 systemd user unit、全局 nohup 守护或常驻后台 watcher。rrctl 保持 daemonless，远端由独立会话管理。

## 3. 终态即报纪律（禁止隐瞒指标）

- **评估完成即报**：任何实验一旦评估完成（observer 收到 terminal 唤醒）或拉取到 summary，Agent 的**第一动作必须是在终端对话层输出该 Run 的核心指标**（RunID、评估集数、mIoU、FB-IoU 及对比基线），**先向用户通报指标，然后再去做后续的归档与台账**，绝不能憋到整个 CSV 闭环或收尾审查才说。

## 4. 禁止本地二次计算与轻量拉取原则

- **直接采信官方指标**：模型评估完成落盘的 `summary.json` / `eval_results.json` 即为官方权威指标，入账与分析直接取用，**严禁在本地进行二次计算（如逐像素 recount、重算混淆矩阵）**。
- **核验移至远端**：若确需一致性或完整性核对，必须在远端评估脚本中就地计算，仅回传轻量报告，绝不在拉取后在本地重新计算。
- **轻量拉取（秒级回传）**：日常实验严格使用 `minimal` 模式拉取轻量摘要文件（`summary.json`、`progress.jsonl`、`evaluate.log`，体积 < 100 KB），严禁将数千个 `.npz` 或原始预测掩码拉回本地，彻底杜绝弱网环境下长耗时与 SSH 断连卡死。
