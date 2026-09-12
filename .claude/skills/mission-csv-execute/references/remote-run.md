# 远程运行、rrctl 与长训练恢复

## 远程训练行处理

当 issue 涉及远程 train→eval：

1. 读取 Spec/CSV 中的 train intent、eval intent、required args、branch、commit、artifact path 和 command owner。
2. 查找最近风险路由：`no_prerun/micro_validation/smoke_validation` 绑定 base/candidate；`full_review` 的 smoke row 绑定 candidate 与用户授权，official row 绑定已通过的 `PRERUN-REVIEW-*` 与 `pre_run_code_commit`。
3. 确认运行命令使用 route 所绑定的 candidate commit；存在后续 diff 时重新分类，而不是自动进入完整 PRERUN。
4. 缺少路由所需的 probes/review 时阻止正式运行。`no_prerun` 不要求历史 PRERUN，`micro_validation/smoke_validation` 要求各自探针；`targeted_review/full_review` 要求正式 gate。受限 smoke 和预注册只读 probe 只在各自原有边界内放行。
5. 新启动由公共 `remote_run.py` 调用现有 `remote_route`，核对 command owner、CSV/row 和任务生命周期；独立路由 CLI 仅在需要提前诊断时使用：
   - actionable remote row（尚未启动或失败后 retry）未声明 `command_owner` 时默认进入 rrctl；显式 `command_owner:rrctl` 同样进入 rrctl。
   - 显式 `command_owner:legacy` 只用于已有运行的观察/收尾，不能启动新运行；失败后 retry 必须迁移 rrctl。
   - 已闭环历史行保持只读；有真实远程证据的 `running_remote` legacy run 可按原路径完成；没有真实证据的 `running_remote` fail closed。
   - 禁止从标题、ExpID 或旧运行记录推断 actionable row 继续 legacy；禁止批量改写历史 row。
   - `remote_route.py` 输出的 `fallback_allowed` 必须为 `false`；rrctl 不可用、readiness 失败或 launch 失败时停在当前 row，不得静默回退 legacy。
6. rrctl 路由下使用 `remote-run-snippet` 调用项目 `.sh` 训练与评估脚本；参数在脚本中维护，`.agents/harness/config/project.toml` 登记入口和产物约定。历史 legacy 运行仅按已有、已批准的行内协议收尾，重试迁移 rrctl。
7. 若由用户手动粘贴远程命令：写入 CSV `notes` 和 `issues/<stem>/<stem>.review.md`，标记 `remote_state=running_remote`，在该可恢复暂停点停止。
8. 新正式运行必须经 rrctl process 后端；连接权限不构成绕过控制面的例外。已有外部启动的运行可登记真实身份和恢复方式，但不得据此启动新的 direct SSH/nohup 运行。
9. 恢复时按 command owner 分流：
   - rrctl run 先使用 `rrctl inspect` / `rrctl wait` 读取远端权威 control state；本地 cache 丢失时先 `rrctl resume --profile <profile> --control-path <control-root>`；terminal completed 后使用 `rrctl pull`。
   - legacy run 仅使用当前 row 已批准的项目拉取协议；模板不再提供独立拉取 skill。
   - 任一路由拉回后都先标 `remote_state=artifacts_pulled`，再 ingest 到实验记录并刷新 review.md 顶部客观摘要：关键指标、baseline 差距、日志异常、未验证项、原始产物路径；过程细节追加到底部 `Appendix: Execution Log`。
10. Codex 不在该摘要中给最终科研判断；Claude 后续读取 review.md 和必要原始数据后，再决定 Result/STATE/下一版 Spec。

### rrctl 默认路由

新启动（包括受限 smoke/probe）统一经 `remote_run.py --execute`；下列低层 CLI 用于理解返回值、诊断和恢复，不能替代该入口的绑定检查。`--resume` 核对 CSV、任务生命周期、RunID 与远端 RunSpec digest，仅执行 inspect/wait/pull。paused/cancelled 任务可观察已有绑定运行，不因此恢复 active 或取得新启动权限。

旧 RunSpec 沿原定义恢复；同时缺少 Mission 指针时，仅从 `issues/<stem>/runs/<RunID>/runspec.json` 定位该 Mission 唯一 CSV 和唯一 RunID 行。缺失或有歧义则停止恢复，不补字段、不改 digest。

此路由只替代通用远程控制，不替代 Spec/CSV intent、PRERUN gate、项目 adapter、结果 ingest 或科研审查。

多组模块/消融脚本使用命名 pipelines。先通过 `run_pipeline.py --list-pipelines` 确认组合，再在请求或生成入口选择 `--pipeline <名称>`。所选阶段、名称和 SHA 固定到 RunSpec；恢复同一个 RunID 时不能换组合。

1. 请求携带适用的 change manifest 和科学 gate。受限用途在 RunSpec metadata 中沿用 `remote_route` 的同名边界对象：`pre_review_smoke` 或 `preregistered_read_only_probe`，不是 reviewer packet 中的 smoke 证据对象。前者声明 candidate commit、1–100 steps、GPU 数、隔离输出、禁正式指标/ingest、授权、生产命令绑定、碰撞保护和 checkpoint cleanup；后者声明 candidate commit、预注册、base-validation-only、禁参数更新/official access/ingest 等原有只读边界。
2. 公共入口执行 `rrctl --json doctor`，要求 process 后端及 observer-deadline、worker-monitoring、unknown-operation-outcome 能力。不可用时修复安装，不回退临时 SSH/nohup。新运行显式声明 CPU 或 GPU 资源；未绑定不重叠 GPU 的任务使用独占资源。需要诊断连接时使用 `rrctl --json --profiles <profiles.json> doctor --profile <profile>`；本地 doctor 不代表远端已 ready。
3. 从已批准 intent、当前运行行和 route/review evidence 生成 `mission.rrctl-request.v1`；所有用途的 `metadata` 带 `mission_csv`（项目相对路径）与 `mission_row_id`，低风险分流携带现有 `change_manifest`。入口核实际 Git diff、candidate commit 及 `requires_prerun`，不额外要求低风险行有历史 PRERUN。request 通过 stdin 交给 builder，不落盘；命令必须来自批准意图，凭据只传变量名。自定义控制脚本声明沿用 `metadata.custom_control_scripts`，通用 stage/launch/health/cleanup 仍归 rrctl。
4. 直接生成该 RunID 唯一保留的 canonical RunSpec：

   ```bash
   printf '%s' '<mission.rrctl-request.v1 JSON>' \
     | python .agents/harness/remote/build_rrctl_runspec.py - \
         --output issues/<stem>/runs/<RunID>/runspec.json \
         --project-config .agents/harness/config/project.toml \
         --check-rrctl
   ```

   request 默认使用 `artifact_pull_policy:{"mode":"minimal","on_demand":[]}`：顶层 `artifacts` 与 `adapter_contract.artifacts` 只声明结论所需的 summary、必要日志或 manifest；完成校验所需 progress/summary 也属于最小清单。其他逐样本 trace 放在 `on_demand`，失败或真实 gap 时才精确拉取。official 可省略 `local_pull_root`，默认 `<source.repo_root>/remote_artifacts/<ExpID>`，由 rrctl 追加 `<RunID>`。受限 smoke/probe 必须显式使用当前 Mission 目录内的隔离 pull root，远端 output root 的路径组件包含 RunID，不进入正式科研记录。

   builder 通过 stdin 解析本身就是 request JSON/转义检查；解析失败时原地修正输入，不写失败 request 文件。新增或修改 adapter、adapter contract、JSON/JSONL 输出格式或 required fields 时，必须在 GPU launch 前用代表性本地 fixture 跑实际 adapter：

   ```bash
   python .agents/harness/remote/validate_adapter.py \
     issues/<stem>/runs/<RunID>/runspec.json <fixture-output-root>
   ```

   它必须覆盖 `first_step/periodic/completion`，并确认 completion 输出 `complete:true`。这样在远程 mutation 前捕获 JSONL 解析、literal dotted key、缺字段和 adapter 输出协议错误。fixture 不替代真实 smoke；它只阻止控制契约错误进入 GPU。

5. 需要 formal review 的 official RunSpec `source.commit` 等于 gate 的 `pre_run_code_commit`；`prerun.gate-provenance.v2` 保留现有字段，不增加状态。低风险分流使用 route candidate commit。smoke 使用绑定 RunID 的独立输出、`anchors=[]`、无 gate provenance、最小 progress/health 和 `smoke_summary.json`；入口仍检查 CSV/任务、候选 commit、隔离输出、GPU 与 cleanup，不要求先构造 reviewer packet 或 official ingest 材料。
6. 公共入口在 launch 前执行 ready。需要单独诊断时才运行以下命令，不在公共入口前例行重复：

   ```bash
   rrctl --json ready issues/<stem>/runs/<RunID>/runspec.json
   ```

   `ready:false`、非零退出或 remote preflight failure 都是运行前 readiness failure：不得启动、不得写 `running_remote`、不得写成远程 blocker；修复同一 request/环境后重试当前 row。pre-review smoke 的 thin ready 只验证 source commit、production argv、GPU/环境、隔离生命周期根、RunID 输出绑定和 cleanup 边界，不运行 `prerun_ready.py` 或 scientific reviewer。
7. 新正式 Mission 使用带绑定检查的公共入口（内部执行 ready、launch、wait、pull，不再手工重复各阶段）：

   ```bash
   python3 .agents/harness/remote/remote_run.py issues/<stem>/runs/<RunID>/runspec.json --execute
   ```

   `launch` 通过 first-step gate 后才能推进下游步骤。启动后记录 `command_owner:rrctl`、RunID、profile、PID/PGID、control/output root、`pre_run_code_commit` 和恢复命令。若返回 first_step_observer_timeout 且远端进程已启动，记录 `remote_state=running_remote` 与 `first_step_gate:pending`，继续观察同一 RunID，不重新 launch。首步默认预算 600 秒，CLI 的单次观察预算默认 900 秒；整个训练时长交给 wait。

   控制操作返回 `status:unknown` 或 `error.outcome:unknown` 时，只能确认操作结果暂不确定，不能记为训练失败或停止成功。保留原 RunID 与路径，按返回的 `next_actions` 用 inspect/wait/resume 核对；不要自动重发 launch/abort，也不要换 RunID 重启同一实验。
8. periodic unhealthy 或 Stop Trigger 不自动转换为 abort：**健康检查负责报告事实，不自动取得停止权**。**硬故障仅包括**：目标进程确认消失、显存 OOM、最新 progress 出现 NaN/Inf、明确未恢复的 fatal traceback，以及 Spec 明确声明的 Stop Condition；确认命中并记录证据后才显式执行 `rrctl abort <RunID> --yes`。单次低 GPU、单次日志延迟、checkpoint 写盘、旧日志历史错误、身份暂时不可读或一次检查失败只记 `degraded`，不得停止训练；至少连续两次复核仍异常才升级诊断。`rrctl wait` 返回一次结构化 attention，远端 workload 保持运行。停止操作必须核对本次独立进程组的 RunID、control root、PID 启动身份和 boot ID，禁止直接按进程名批量终止。
9. 公共入口在 launch 通过首步 gate 后以前台调用等待终态。若观察超时或需要分步恢复，使用同一 RunID 继续等待；公共入口仍在运行时不要另开重复等待：

   ```bash
   rrctl --json wait <RunID> --poll-seconds 600 --max-wait-seconds 900
   ```

   新运行的首步、周期检查与完成验收由远端 worker 自主执行；`rrctl wait` 只观察已发布的结果，不输出中间日志，多个观察者不会重复执行检查器。结束本地等待或关闭 agent 后，远端检查仍会继续。既有运行保留启动时的 worker，本地升级不会原地迁移或重启它。

   退出码 0 表示 completed，1 表示 failed/aborted，2 表示 attention/控制错误，124 表示观察期限到达且运行保留。外层工具预算需大于观察预算及一次控制请求时间；124 后直接继续同一 RunID 的 wait，不标 failed、不拉诊断、不重复 launch。也可用一键入口的 `--execute --resume` 恢复。若工具只是 yield，继续等待同一工具 session；按宿主要求提供必要进度，不重读 skill 或重复输出未变化状态。completed 后立即 pull/ingest；出现 attention 时按硬/软条件处理，保持 `fallback_allowed=false`。

### 运行类型

- `pre_review_smoke`：通用 1–100 step 运行可达性验证，必须清理 checkpoint。
- `pilot`：可选的通用小预算科学阶段；只有 Spec 明确要求预注册 gate 时才创建，不是每个 Mission 的固定步骤。
- `official`：正式训练或评估，可发布最终指标。
- `preregistered_read_only_probe`：不改模型状态的预注册只读探针。

Pilot RunSpec 使用 `execution_purpose:pilot`；不得用 `official` 表示 `pilot_only` 运行。
10. 公共入口在 official run 返回 terminal completed 后自动 pull；只有分步恢复时才单独执行 `rrctl pull`。只拉 RunSpec `artifacts` 中的最小结果集，`artifact_pull_policy.on_demand` 不会被默认拉取。pull 的 manifest、size 与原子目标验证通过后才设置 `remote_state=artifacts_pulled`。pre-review smoke 不进入 official artifact ingest：成功、失败或 abort 后都必须由 RunSpec `output_cleanup` 删除绑定 output root 内的 checkpoint/optimizer/scheduler/大型文件，保留 control root 的 `console.log/status.json` 与 output root 的 `smoke_summary.json`，并验证 `checkpoint_cleanup_completed:true`、`checkpoint_paths_remaining:[]` 后才写 smoke evidence。
失败、abort 或 periodic attention 时可执行 `rrctl pull <RunID> --diagnostic`。快照位于该 RunID 的 `diagnostics/<snapshot-id>/`，只包含受大小限制的日志、状态和已有进度；不标记正式结果已拉取，也不进入指标入账。

11. **禁止为"等跑完"建立本地常驻进程**：不得创建 systemd user unit、nohup 守护、后台 `rrctl wait` 包装或任何本地 watcher 去跨会话等待远端终态。rrctl 是 daemonless 设计，`rrctl wait` 是当前会话内的前台阻塞调用；远端 worker 拥有独立进程会话，stdio 与 SSH 分离；结束本地观察不会改变远端归属。
12. **会话中断后的恢复流程**（不需要任何常驻进程，也不做自动拉取）：

    ```bash
    rrctl --json resume --profile <profile> --control-path <control-root>   # 从远端 control state 重建本地索引
    rrctl --json inspect <RunID>                                            # 读权威状态
    rrctl --json pull <RunID>                                               # 仅当已达 terminal completed
    ```

    终态判断以 `inspect` 为准，不以本地日志或旧 CSV 估算为准。未达终态就继续等，回来再查；已达终态才 pull。

### 多阶段 ExecutionPlan 推进

仅当前任务明确存在 ExecutionPlan 时读取以下规则。

1. 多阶段计划在每个新代码 commit 首次运行前先执行 change route；仅 `targeted_review/full_review` 完成正式 PRERUN，`no_prerun/micro_validation/smoke_validation` 直接继承或用 probe evidence 推进。retry 和后续 stage 不重复审查同一变更。
2. CSV 仍是 Mission 状态唯一来源。状态保存 `implementation_reviewed_commit`，推进时与当前 plan 的 `reviewed_commit`、已拉取 rrctl manifest 和可用的效果型 scientific gate 结果一起传给 `stage_flow.py`；默认不重复比较冻结 contract。
3. `stage_flow.py` 是无状态推进建议器：
   - `ready`：只实例化下一个已审查 RunSpec；Mission 在 rrctl launch 成功后自行更新 CSV。
   - `retry`：使用当前 materialized binding 重试，`requires_new_prerun:false`、`reviewer_delta:0`；binding 漂移写 advisory。
   - `ready` + advisory：同一已审查 commit 下，效果型 scientific gate false/pending、RunID/paths/profile/transport/monitoring、contract 或非安全 conformance 漂移仍推进。
   - `implementation_review_required`：当前 `reviewed_commit` 不等于 `implementation_reviewed_commit`；先审查新代码快照。
   - `blocked`：manifest identity/status、late-binding provenance 或状态合同无效；不得启动。
4. rrctl 只报告 control/manifest 事实，不预测科研效果，也不触发 scientific reviewer。在已批准 plan 的 `scientific_gates.<name>.kind` 中将纯效果预测明确标为 `effect_prediction`；这类 gate false/pending 只记 advisory，以最终官方指标判断效果。预注册停止门使用 `preregistered_stop`，正确性、安全和归属门分别使用 `correctness`、`safety`、`attribution`，继续保留阻止或跳过后续阶段的约束。未声明 kind 的旧门按 `preregistered_stop` 兼容，未知 kind 报错，不根据描述文本猜测是否可放行。
5. source commit 在首次传输与结果归属时核对。不要把 rrctl binding 变化当 launch gate。

`stage_flow.py -` 从 stdin 接收 request；无需在 artifact root 落盘阶段请求。
6. `stage_flow.py` 返回建议而不写 CSV、plan 或 manifest。只有 Mission 执行器应用 state patch，因此 CSV/plan/rrctl/review 四类状态职责保持分离。

### Schema-aware CSV 状态更新

所有新的 actionable 状态更新使用：

```bash
printf '%s' '<mission.csv-state-update.v1 JSON>' \
  | python <mission-skill-dir>/scripts/csv_state.py <csv-path> -
```

- request 使用 `mission.csv-state-update.v1`，指定 `row_id`、`set`、`append_notes`、可选 `set_note_tags`、详细 `event` 和 `commit_boundary`。一次调用同时落 row、events sidecar 与全部校验，不要拆成多次调用。
- `-` 表示从 stdin 读取；这是正常路径，不生成一次性 `state-*.json`。仅在调试失败输入或需要人工转交未应用请求时使用兼容的 `<request.json>` 文件参数。
- CLI 校验 canonical 28 列或显式外部兼容 19 列、唯一 row id、状态枚举、notes 与 claim 引用；写已提交或 ingested 时同时核验适用事实。单文件使用同目录临时文件 + `os.replace` 原子替换；CSV 与 events 不构成跨文件事务。
- **写入成功的返回值就是权威后态**：`ok/row/rows_total/columns` 已包含写入后的整行、总行数与列数。禁止在 `ok:true` 之后再 `cat`/`grep`/`DictReader` 读回同一份 CSV 去"确认写进去了"——这是记账调用量最大的单一来源。只有写入失败或需要读取**其他行**时才重新读文件。
- **进度快照不写 CSV**：`X/44`、episode 计数、GPU 利用率、ETA 的权威源是 `rrctl inspect` 与远端 `matrix_status.json`，写进 CSV 既不增加可恢复性又立刻过时。CSV 只记状态**转换**（`dev_state` 变化、`remote_state` 变化、首步 gate 结论、终态证据）。周期巡检默认不写 CSV，除非触发 Stop Trigger 或状态转换。
- 详细事件进入 `<csv-stem>.events.json` sidecar，CSV notes 只保留 event digest。不要把重复日志或长审查文本塞入 notes。
- `commit_boundary:none` 用于 readiness retry、poll、reviewer wait 和 unchanged inspect，输出 `git_commit_recommended:false`；仅 implementation/review/launch/terminal/final_review 阶段边界建议提交。
- 已关闭历史 CSV 不迁移；此规则只约束新写入。

### 常用公共入口

接口不确定时读取对应 `--help`；不要在会话内临时导入私有模块、猜状态常量或重新拼运行目录。

| 动作 | 调用 |
|---|---|
| 生成并启动 | `python3 .agents/harness/remote/remote_run.py <runspec> --request - --execute`，请求从 stdin 输入 |
| 恢复观察与拉取 | `python3 .agents/harness/remote/remote_run.py <runspec> --execute --resume` |
| 更新 CSV | `python3 .agents/skills/mission-csv-execute/scripts/csv_state.py <csv> -`，请求从 stdin 输入 |
| 生成实验记录 | `python3 .agents/harness/records/experiment_records.py build --exp <ExpID>` |
| 查看记忆状态 | `python3 .agents/harness/memory/research_memory.py --repo-root . status` |
| 离线完成复查 | `python3 .agents/harness/remote/validate_adapter.py <runspec> <pull 返回的 destination> --phase completion` |

正式 pull 的 destination 直接包含声明的文件；只有 diagnostic snapshot 才包含 control/output 子目录。默认摘要中的 details_path 指向完整响应，可按字段或行读取，不把整段日志反复放入会话。
