# 远程运行与执行编排规范 (Remote Run Orchestrator)

当 CSV issue 涉及远程 train→eval 时，读取并严格遵循本规范。

## 1. 核心定位与执行栈边界

- **核心职责**：在满足前置审查与路由资格的前提下，通过公共入口安全地 launch、observe 并拉取结果入账。所有状态以控制面结构化工件为准，**严禁从控制台日志文本正则匹配猜测状态**。
- **执行栈与本地离线评测边界**：
  - 只有需要真实 GPU 计算（前向/反向、特征提取、预测生成、显存与 loss 验证）或远端专用环境依赖的运行才走远程 rrctl；
  - **纯 CPU 离线指标聚合默认本地执行**，不为聚合再启动 GPU 实验或把已有本地数据传回远端。大数据工件（如特征图、切片数组）遵循远端就地核验与免拉取原则：在原证据所在宿主只读核验并回传轻量报告，报告绑定 RunID、原 RunSpec/manifest 摘要与全量覆盖结论。在远端就地核验通过并拉取 KB 级 summary 摘要后，记录 `remote_state=artifacts_pulled` 或直接推进至 `ingested` 均为完全合规操作（绝非伪写/伪标）；最终交付的 `review.md` / `handoff.md` 直接记录工件的远端绝对物理直连地址（`<server_host>:<absolute_path>`），严禁拖拽大文件，严禁因此阻滞收尾。

## 2. 正常运行 Happy Path

```text
核验 Route 与 Commit
       ↓
构建 canonical RunSpec
       ↓
启动前时间沙盘推算 (remote_progress.py --estimate)
       ↓
公共一键入口启动 (--execute)
       ↓
观察至终态 (completed)
       ↓
拉取最小产物集 (artifacts_pulled)
       ↓
生成实验台账 (ingested) 推进至 RESULT-ANALYSIS
```

### Step 1: 路由与 Commit 行为一致性校验
1. 读取 Spec/CSV 中的 train/eval intent、commit、command owner 与输出路径；
2. 确认 candidate commit 与审查所绑定的 commit **科学行为一致**（由 `.agents/harness/remote/change_fingerprint.py` 判定，补文档/测试不使审查失效）；
3. 检查 gate 状态：`no_prerun` 不要求历史审查；`targeted_review/full_review` 必须具备合法的 `PRERUN-REVIEW-*` 通过证据（或符合条件的用户授权例外）。
4. `remote_route.py` 输出的 `fallback_allowed` 必须为 `false`；rrctl 不可用或 launch 失败时停在当前 row，**严禁静默回退 direct SSH 或 legacy**。

### Step 2: 构建 RunSpec 与公共一键入口启动
1. 首次接入项目训练脚本与配置时，遵循 `remote-run-snippet` 规范（在 `.agents/harness/config/project.toml` 登记 stages 与产物契约）；
2. 生成该 RunID 唯一保留的 canonical RunSpec：`issues/<stem>/runs/<RunID>/runspec.json`；
3. 新正式 Mission 统一经带绑定检查的公共入口执行：
   ```bash
   python3 .agents/harness/remote/remote_run.py issues/<stem>/runs/<RunID>/runspec.json --execute
   ```
   公共入口使用仓库内 `.agents/harness/remote/rrctl/rrctl`，不依赖 PATH 指向另一个项目的可编辑安装。普通执行依次 ready、launch、wait、pull；宿主事件等待使用 `--observe-only`，计算终态即交回执行器继续 pull/ingest。二者不改变最终验收要求。

### Step 3: 长运行监控与透明度
- **时间沙盘 / 心跳 / 等待纪律**：统一遵循 [references/remote-monitoring.md](remote-monitoring.md)（启动前沙盘估算、累计 ≥10 步首个 ETA 与 30 分钟心跳、秒级 `remote_progress.py` 探针、禁止轮询与盲等）。

### Step 4: 终态确认与最小产物拉取
1. **终态即报第一铁律**：计算完成唤醒或拉取到 summary 后，执行器的**第一优先级是在终端对话中向用户直接通报核心指标（RunID、评估集数、mIoU、FB-IoU 及对比基线涨跌）**，严禁憋到 CSV 闭环或收尾审查才汇报；
2. **轻量拉取与禁止二次重算**：公共入口在 official run 返回 terminal completed 后自动 pull 最小结果集（仅 summary.json、progress.jsonl、evaluate.log 等轻量文件，单文件 ≤10MB，秒级完成）；**直接采信评测落盘指标，严禁在本地进行二次计算（如逐像素 recount、重算混淆矩阵）**；核验需求一律置于远端就地完成；严禁拉取海量 .npz 或原始预测掩码，严禁编写 scp 循环将远端工件目录拉回本地做行数统计或排查；非必要数据严禁拉取到本地；`review.md` 与 `handoff.md` 对大工件一律直接记录 `<server_host>:<absolute_path>` 远端绝对物理直连地址；杜绝弱网连接卡死；
2. 校验 pull manifest 与文件存在性后，通过 `csv_state.py` 记录 `remote_state=artifacts_pulled`；
3. pre-review smoke 必须完成终端 checkpoint 清理，保留 `console.log/status.json/smoke_summary.json`，验证 `checkpoint_cleanup_completed:true` 后方可作为 PRERUN 证据。

### Step 4.1 采集/工件核验：分层廉价 + 一次缓存

当任务需要"独立回读采集契约/工件完整性"证据时（如补丁式完整采集任务），严格按本规约，杜绝全量重读拖死收尾：

1. **一次核验、收尾复用**：每个 Run 在它自己完工（terminal completed）时就完成工件核验并落盘 `capture-contract-verification.json`（或等价报告）；收尾聚合只**引用**该报告，禁止对全部 Run 批量重跑核验；仅对缺失/失效的 Run 补验。
2. **分层走便宜路径**（严禁对数据载荷全量解码）：
   - 结构 / dtype / 逐类覆盖：只读 `.npy` / `.npz` 成员头部（shape、dtype），不解压数组；
   - 完整性 / 身份：核 `control/artifact_manifest.json` 里已有的每文件 `sha256` + `size` + `manifest_sha256`，不重读字节；
   - 有限值：用 `summary.json` 的官方指标 + 抽样 K 集（如 K=32）代替全量 `np.isfinite`。
3. **不制造无人消费的存证**：核验报告与远端引用唯一的必需消费者是 CSV 的 `artifact_path`（形如 `<server_host>:<absolute_path>`）与轻量摘要；`mission_completion.py` 只认该字符串，**不读 `remote-reference.json`**。除非确有无下游读者的用途，否则禁止为收尾额外生成 `remote-reference.json` 之类的仪式文件。

### Step 5: 结果入账 (Ingest) 与生命周期流转
1. 执行台账刷新命令：
   ```bash
   python3 .agents/harness/records/experiment_records.py build --exp <ExpID>
   ```
2. 刷新 `research_workspace/EXPERIMENTS.csv` 与 `issues/<stem>/<stem>.review.md` 顶部客观摘要；
3. 更新 CSV `remote_state=ingested`；
4. **结果边界铁律**：`remote_state=completed` 或 `artifacts_pulled` 仅代表远程进程结束，**绝不等于 Mission 完成**。必须完成入账并推进至 `RESULT-ANALYSIS-01` 独立分析。

## 3. 条件分支与异常处置

| 异常/特殊场景 | 处置规约与参考模块 |
|---|---|
| **观察超时 (124)、断线、首步暂态告警或状态未决** | **读取并严格执行 [references/remote-recovery.md](remote-recovery.md)**。<br>- **超时 124 绝不等于任务失败**；<br>- 首步暂态告警不判硬失败；<br>- 会话中断使用 `rrctl resume` → `inspect` → `pull` 恢复；<br>- 记账中断使用 `reconcile_remote_binding.py` 安全认领。 |
| **长训练监控、心跳与等待纪律** | **读取 [references/remote-monitoring.md](remote-monitoring.md)**。 |
| **审查服务挂起需例外放行、多阶段推进或底层诊断** | **读取 [references/remote-troubleshooting.md](remote-troubleshooting.md)**。<br>- 经两次失败且用户明确授权可声明 `pre_run_exception`；<br>- 多阶段计划使用 `stage_flow.py`。 |
