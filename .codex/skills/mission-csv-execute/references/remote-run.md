# 远程运行、rrctl 与长训练恢复

## Step 6.6：远程训练行处理

当 issue 涉及远程 train→eval：

1. 读取 Spec/CSV 中的 train intent、eval intent、required args、branch、commit、artifact path 和 command owner。
2. 查找最近风险路由：`no_prerun/micro_validation/smoke_validation` 绑定 base/candidate；`full_review` 的 smoke row 绑定 candidate 与用户授权，official row 绑定已通过的 `PRERUN-REVIEW-*` 与 `pre_run_code_commit`。
3. 确认运行命令使用 route 所绑定的 candidate commit；存在后续 diff 时重新分类，而不是自动进入完整 PRERUN。
4. 缺少合法 route/probes/review 时 fail closed；仅 `full_review` 的 `pre_review_smoke` 可在正式 review 前放行，其他 official run 不得绕过 gate。
5. 使用 `scripts/remote_route.py` 解析 command owner 和 lifecycle：
   - actionable remote row（尚未启动或失败后 retry）未声明 `command_owner` 时默认进入 rrctl；显式 `command_owner:rrctl` 同样进入 rrctl。
   - 显式 `command_owner:legacy` 只有同时声明使用原因、迁移期限或 migration issue、责任组件/owner 的完整 legacy 豁免才允许；失败后 retry 必须迁移 rrctl。
   - 已闭环历史行保持只读；有真实远程证据的 `running_remote` legacy run 可按原路径完成；没有真实证据的 `running_remote` fail closed。
   - 禁止从标题、ExpID 或旧运行记录推断 actionable row 继续 legacy；禁止批量改写历史 row。
   - `remote_route.py` 输出的 `fallback_allowed` 必须为 `false`；rrctl 不可用、readiness 失败或 launch 失败时停在当前 row，不得静默回退 legacy。
6. rrctl 路由下使用 `remote-run-snippet` 对接 `.agents/harness/config/project.toml` 中的训练与评估命令。历史 legacy 运行仅按已有、已批准的行内协议收尾，重试迁移 rrctl。
7. 若由用户手动粘贴远程命令：写入 CSV `notes` 和 `issues/<stem>/<stem>.review.md`，标记 `remote_state=running_remote`，在该可恢复暂停点停止。
8. 若具备明确 SSH 权限、远程凭据和用户授权：Codex 可直接连接服务器运行命令；启动后仍需记录 remote session、命令、branch/commit、`pre_run_code_commit`、预期输出路径，并标记 `remote_state=running_remote`。
9. 恢复时按 command owner 分流：
   - rrctl run 先使用 `rrctl inspect` / `rrctl wait` 读取远端权威 control state；本地 cache 丢失时先 `rrctl resume --profile <profile> --control-path <control-root>`；terminal completed 后使用 `rrctl pull`。
   - legacy run 仅使用当前 row 已批准的项目拉取协议；模板不再提供独立拉取 skill。
   - 任一路由拉回后都先标 `remote_state=artifacts_pulled`，再 ingest 到实验记录并刷新 review.md 顶部客观摘要：关键指标、baseline 差距、日志异常、未验证项、原始产物路径；过程细节追加到底部 `Appendix: Execution Log`。
10. Codex 不在该摘要中给最终科研判断；Claude 后续读取 review.md 和必要原始数据后，再决定 Result/STATE/下一版 Spec。

### rrctl 默认路由

进入 RunSpec 构建前先运行 `scripts/remote_route.py`，确认 command owner、scientific gate、remote state 与 artifact lifecycle；随后才可调用 `build_rrctl_runspec.py`。

此路由只替代通用远程控制，不替代 Spec/CSV intent、PRERUN gate、项目 adapter、结果 ingest 或科研审查。

1. 先构造 `mission.remote-route.v1` 请求并运行路由器；official 请求携带必要 formal verdict，pre-review smoke 请求携带 `execution_purpose:pre_review_smoke` 和 commit/1–100 steps/GPU/隔离输出/禁指标/禁 ingest/用户授权/正式命令绑定/fail-on-collision/checkpoint cleanup 合同。只有 `decision:proceed, route:rrctl` 才继续，blocked 禁止回退 legacy。
2. 确认 `remote-run-control` 已按其独立提交安装，`command -v rrctl` 成功；不可用时记录 `rrctl_unavailable` 诊断并停在当前运行 row，不得回退为临时 SSH/tmux 拼接命令冒充同一控制面。
3. 从已批准 intent、当前 gated run row 和 route/review evidence 生成显式 `mission.rrctl-request.v1` request；通过 stdin 直接交给 builder，不把 request 写入 artifact root。禁止生成器推断实验命令，禁止把密码写入 request。`route=rrctl` 时禁止新增承担通用 stage/launch/health/cleanup ownership 的一次性脚本；自定义脚本只允许项目 adapter 或科学语义解析。
4. 直接生成该 RunID 唯一保留的 canonical RunSpec：

   ```bash
   printf '%s' '<mission.rrctl-request.v1 JSON>' \
     | python .agents/harness/remote/build_rrctl_runspec.py - \
         --output issues/<stem>/runs/<RunID>/runspec.json \
         --project-config .agents/harness/config/project.toml \
         --check-rrctl
   ```

   request 默认使用 `artifact_pull_policy:{"mode":"minimal","on_demand":[]}`：顶层 `artifacts` 与 `adapter_contract.artifacts` 都只声明结论所需的 `results.json` / `summary.json` / 必要日志或 manifest；episode/rank trace、逐样本指标和其他 `.jsonl` 放在 `on_demand`，留在远端，只有出现实际失败或 `validation_gap` 才以 `mode:diagnostic` 精确拉取单个诊断文件。official request 可以省略 `local_pull_root`；builder 统一派生为 `<source.repo_root>/remote_artifacts/<ExpID>`，由 rrctl 追加 `<RunID>`。显式值仍兼容。`execution_purpose:pre_review_smoke` 必须显式提供隔离的 `local_pull_root`，禁止进入正式科研结果目录。

   builder 通过 stdin 解析本身就是 request JSON/转义检查；解析失败时原地修正输入，不写失败 request 文件。新增或修改 adapter、adapter contract、JSON/JSONL 输出格式或 required fields 时，必须在 GPU launch 前用代表性本地 fixture 跑实际 adapter：

   ```bash
   python .agents/harness/remote/validate_adapter.py \
     issues/<stem>/runs/<RunID>/runspec.json <fixture-output-root>
   ```

   它必须覆盖 `first_step/periodic/completion`，并确认 completion 输出 `complete:true`。这样在远程 mutation 前捕获 JSONL 解析、literal dotted key、缺字段和 adapter 输出协议错误。fixture 不替代真实 smoke；它只阻止控制契约错误进入 GPU。

5. official RunSpec `source.commit` 等于已通过 gate 的 `pre_run_code_commit`；`prerun.gate-provenance.v2` 只记录 `pre_run_code_commit/review_mode/review_result/reviewer_id/blocker_closure_evidence`，不写 attempt/lineage/generation。smoke RunSpec 等于 route candidate commit，使用绑定 RunID 的独立 fail-on-collision 输出，强制 `anchors=[]`、无 gate provenance、只保留最小 progress/health 和 `smoke_summary.json`，不发布指标、不 ingest；不得构造或检查 coverage manifest、review packet/hash、scientific anchors 或 official artifact completeness。
6. 远端 mutation 前运行：

   ```bash
   rrctl --json ready issues/<stem>/runs/<RunID>/runspec.json
   ```

   `ready:false`、非零退出或 remote preflight failure 都是运行前 readiness failure：不得启动、不得写 `running_remote`、不得写成远程 blocker；修复同一 request/环境后重试当前 row。pre-review smoke 的 thin ready 只验证 source commit、production argv、GPU/环境、隔离生命周期根、RunID 输出绑定和 cleanup 边界，不运行 `prerun_ready.py` 或 scientific reviewer。
7. readiness 通过后才运行：

   ```bash
   rrctl --json launch issues/<stem>/runs/<RunID>/runspec.json
   ```

   `launch` 只有通过 first-step gate 才算成功。成功后立即在 CSV notes 与 review execution log 记录 `command_owner:rrctl`、RunID、RunSpec SHA256、profile 名、session、control root、output root、`pre_run_code_commit`、launch JSON 证据和不含凭据的 resume 命令，并设置 `remote_state=running_remote`。
8. periodic unhealthy 或 Stop Trigger 不自动转换为 abort：**健康检查负责报告事实，不自动取得停止权**。**硬故障仅包括**：目标进程确认消失、显存 OOM、最新 progress 出现 NaN/Inf、明确未恢复的 fatal traceback，以及 Spec 明确声明的 Stop Condition；确认命中并记录证据后才显式执行 `rrctl abort <RunID> --yes`。单次低 GPU、单次日志延迟、checkpoint 写盘、旧日志历史错误、PID/cmdline 漂移、tmux 短暂不可见或一次检查失败只记 `degraded`，不得停止训练；至少连续两次复核仍异常才升级诊断。`rrctl wait` 返回一次结构化 attention，远端 workload 保持运行。禁止直接 `pkill`、`pgrep -f | kill` 或仅按 tmux 名终止。
9. launch 通过首步 gate 后，长训练在当前会话使用**一个前台阻塞调用**等待终态：

   ```bash
   rrctl --json wait <RunID> --poll-seconds 600
   ```

   `rrctl wait` 在进程内部每 10 分钟执行 authoritative inspect + periodic health，期间不输出中间日志，因此不会按巡检次数消耗模型 token。预期超过 6 小时且 Stop Trigger 允许时可放宽到 900–1800 秒；不得超过当前任务最短故障检测预算。若执行工具暂时 yield 出 session，只对**同一 session**使用空输入和工具允许的最长等待时间；pending/yield 不是状态变化，后续动作必须直接是工具等待，禁止插入任何用户可见 commentary、进度复述、ETA、skill 重读或额外 `inspect`。命令返回 terminal JSON 后，同一 turn 立即继续：`completed` → pull/ingest/后续 CSV；`failed/aborted` → 记录终态故障；`periodic_health` attention → 保留 workload，按硬/软条件判断是否继续 wait 或显式 abort。

### 运行类型

- `pre_review_smoke`：通用 1–100 step 运行可达性验证，必须清理 checkpoint。
- `pilot`：可选的通用小预算科学阶段；只有 Spec 明确要求预注册 gate 时才创建，不是每个 Mission 的固定步骤。
- `official`：正式训练或评估，可发布最终指标。
- `preregistered_read_only_probe`：不改模型状态的预注册只读探针。

Pilot RunSpec 使用 `execution_purpose:pilot`；不得用 `official` 表示 `pilot_only` 运行。
10. official run 在前台 `rrctl wait` 返回 terminal completed 后执行 `rrctl pull`；只拉 RunSpec `artifacts` 中的最小结果集，`artifact_pull_policy.on_demand` 不会被默认拉取。pull 的 manifest、size、SHA 与原子目标验证通过后才设置 `remote_state=artifacts_pulled`。pre-review smoke 不进入 official artifact ingest：成功、失败或 abort 后都必须由 RunSpec `output_cleanup` 删除绑定 output root 内的 checkpoint/optimizer/scheduler/大型文件，保留 control root 的 `console.log/status.json` 与 output root 的 `smoke_summary.json`，并验证 `checkpoint_cleanup_completed:true`、`checkpoint_paths_remaining:[]` 后才写 smoke evidence。
失败、abort 或 periodic attention 时可执行 `rrctl pull <RunID> --diagnostic`。快照位于该 RunID 的 `diagnostics/<snapshot-id>/`，只包含受大小限制的日志、状态和已有进度；不标记正式结果已拉取，也不进入指标入账。

11. **禁止为"等跑完"建立本地常驻进程**：不得创建 systemd user unit、nohup 守护、后台 `rrctl wait` 包装或任何本地 watcher 去跨会话等待远端终态。rrctl 是 daemonless 设计，`rrctl wait` 是当前会话内的前台阻塞调用；远端已由 tmux 承载，会话结束不影响它。本地常驻只增加故障面，不增加可靠性。
12. **会话中断后的恢复流程**（不需要任何常驻进程，也不做自动拉取）：

    ```bash
    rrctl --json resume --profile <profile> --control-path <control-root>   # 从远端 control state 重建本地索引
    rrctl --json inspect <RunID>                                            # 读权威状态
    rrctl --json pull <RunID>                                               # 仅当已达 terminal completed
    ```

    终态判断以 `inspect` 为准，不以本地日志或旧 CSV 估算为准。未达终态就继续等，回来再查；已达终态才 pull。

### 多阶段 ExecutionPlan 推进

1. 多阶段计划在每个新代码 commit 首次运行前先执行 change route；仅 `targeted_review/full_review` 完成正式 PRERUN，`no_prerun/micro_validation/smoke_validation` 直接继承或用 probe evidence 推进。retry 和后续 stage 不重复审查同一变更。
2. CSV 仍是 Mission 状态唯一来源。状态保存 `implementation_reviewed_commit`，推进时与当前 plan 的 `reviewed_commit`、已拉取 rrctl manifest 和可用的效果型 scientific gate 结果一起传给 `stage_flow.py`；默认不重复比较冻结 contract/hash。
3. `stage_flow.py` 是无状态推进建议器：
   - `ready`：只实例化下一个已审查 RunSpec；Mission 在 rrctl launch 成功后自行更新 CSV。
   - `retry`：使用当前 materialized binding 重试，`requires_new_prerun:false`、`reviewer_delta:0`；binding 漂移写 advisory。
   - `ready` + advisory：同一已审查 commit 下，效果型 scientific gate false/pending、RunID/paths/profile/transport/monitoring、contract/hash 或非安全 conformance 漂移仍推进。
   - `implementation_review_required`：当前 `reviewed_commit` 不等于 `implementation_reviewed_commit`；先审查新代码快照。
   - `blocked`：manifest identity/status/SHA、late-binding provenance 或状态合同无效；不得启动。
4. rrctl 只报告 control/manifest 事实，不预测科研效果，也不触发 scientific reviewer。效果型 gate false/pending 只记 advisory，以最终官方指标判断效果；correctness/安全/归属检查不降级。
5. source commit 在首次传输与结果归属时核对；artifact SHA 在 pull/ingest 时核对。不要在每个 stage/retry 重算三摘要或把 rrctl binding 变化当 launch gate。
6. `stage_flow.py` 返回建议而不写 CSV、plan 或 manifest。只有 Mission 执行器应用 state patch，因此 CSV/plan/rrctl/review 四类状态职责保持分离。

### Schema-aware CSV 状态更新

所有新的 actionable 状态更新使用：

```bash
printf '%s' '<mission.csv-state-update.v1 JSON>' \
  | python <mission-skill-dir>/scripts/csv_state.py <csv-path> -
```

- request 使用 `mission.csv-state-update.v1`，指定 `row_id`、`set`、`append_notes`、可选详细 `event` 和 `commit_boundary`。一次调用同时落 row、events sidecar 与全部校验，不要拆成多次调用。
- `-` 表示从 stdin 读取；这是正常路径，不生成一次性 `state-*.json`。仅在调试失败输入或需要人工转交未应用请求时使用兼容的 `<request.json>` 文件参数。
- CLI 在写入前验证固定 28 列、唯一 row id、状态枚举和 claim ledger 引用，并用同目录临时文件 + `os.replace` 原子替换；失败时原 CSV 字节不变。
- **写入成功的返回值就是权威后态**：`ok/row/rows_total/columns` 已包含写入后的整行、总行数与列数。禁止在 `ok:true` 之后再 `cat`/`grep`/`DictReader` 读回同一份 CSV 去"确认写进去了"——这是记账调用量最大的单一来源。只有写入失败或需要读取**其他行**时才重新读文件。
- **进度快照不写 CSV**：`X/44`、episode 计数、GPU 利用率、ETA 的权威源是 `rrctl inspect` 与远端 `matrix_status.json`，写进 CSV 既不增加可恢复性又立刻过时。CSV 只记状态**转换**（`dev_state` 变化、`remote_state` 变化、首步 gate 结论、终态证据）。周期巡检默认不写 CSV，除非触发 Stop Trigger 或状态转换。
- 详细事件进入 `<csv-stem>.events.json` sidecar，CSV notes 只保留 event digest。不要把重复日志或长审查文本塞入 notes。
- `commit_boundary:none` 用于 readiness retry、poll、reviewer wait 和 unchanged inspect，输出 `git_commit_recommended:false`；仅 implementation/review/launch/terminal/final_review 阶段边界建议提交。
- 已关闭历史 CSV 不迁移；此规则只约束新写入。
