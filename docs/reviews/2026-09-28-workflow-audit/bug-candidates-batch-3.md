# 批 3 新增 BUG-CANDIDATE 清单（待裁定）

> 来源：批 3 L1 四个 worker 的接受/拒绝矩阵测试。**所有条目仅报告未修改代码。**
> 按方案 0.4 约定：先记录，由主代理与用户裁定后才改。

## 迁移方向性（worker 4，csv_state.py）

### BC-1：`git_state: 已提交 → 未提交`（reopen）
- **危害**：抹掉源码冻结事实，撤销 `git_evidence_invalid` 的历史校验。
- **证据**：`test_bug_candidate_reopen_stage1_git_commit_unrestricted`。
- **建议**：加单字段 git 单调性约束（已提交不可回未提交）；如确需 reopen 应走特殊审计 note。

### BC-2：`remote_state: failed → running_remote` 直跳
- **危害**：跳过 failed→completed→artifacts_pulled 的证据链。
- **建议**：加 `failed` 出度白名单（仅允许 →failed/aborted，需显式 resume 记录）。

### BC-3：`remote_state: failed → completed` 直跳
- **危害**：同上，跳过 artifacts_pulled/ingested 证据。
- **建议**：加 `failed` 迁移方向表。

### BC-4：`remote_state: ∅ → completed`（兼容跳）
- **危害**：19 列兼容 CSV 无证据位也可"完成"。
- **建议**：加 ∅→其它迁移白名单。

### BC-5：`remote_state: not_applicable → running_remote`
- **危害**：not_applicable 是"本项目无远程"终态声明。
- **建议**：加 not_applicable 出度白名单。

### BC-6（junction）：`git=已提交 + review_initial=已完成` 之后 `review_initial → 未开始`
- **危害**：提交 + 评审完成之后再回退评审，绕过 review_required_tags。
- **建议**：junction 约束——git=已提交 时 review_initial 只能前向。

### BC-7（junction）：`git=已提交 + dev_state=未开始`
- **危害**：提交时开发尚未开始，违背因果。
- **建议**：junction 约束——git=已提交 要求 dev ≥ 进行中。

### BC-8（junction）：`git=已提交 + remote_state=running_remote`
- **危害**：源码已冻结却远程跑，RunSpec pre_run_code_commit 应当固定。
- **建议**：junction 约束——git=已提交 时 remote 仅取 {completed, artifacts_pulled, ingested} 子集。

## 子系统（worker 3，无修）

### BC-9：`memory/tests/test_lifecycle.py` 预存在失败
- **位置**：`test_loaded_legacy_pi_extension_is_fail_safe_until_reload`、`test_process_then_hook_refreshes_without_new_file_scan`。
- **现象**：带 `PI_SUB_AGENT_DEPTH`/`MAGIC_CONTEXT_PI_SUBAGENT` 环境变量时，`handle_hook` 在 `internal_host_process()` 短路返回 `snapshotRevision="disabled"`，与断言不符。
- **裁定方向**：(a) `LifecycleTests.setUp` 用 `patch.dict(os.environ, {})` 清两变量；或 (b) `internal_host_process()` 应区分子代理与前台。当前两种环境跑都 OK，但**在子代理内会失败**（测试设计缺陷）。

### BC-10：`file_lock` 不防同进程嵌套重入
- **现象**：同一进程两个独立 `file_lock(path)` 上下文会死锁（flock 对同一文件独立 OFD 不互斥）。
- **裁定方向**：如果"同进程允许 re-enter"是约定，需要在 file_lock 内部记录 `threading.local` 或 fd 计数。

### BC-11：`mission_state.transition("paused")` 不清空 `current_task`
- **现象**：active→paused 保留 current_task 指针；恢复工具是否应将"current+paused"视为不可启动？
- **裁定方向**：明确约定或修改；当前测试锁定现状。

### BC-12：cancelled/superseded/completed 任务持有 CSV 唯一绑定
- **现象**：cancelled 任务的 CSV 不能注册到新 task。
- **裁定方向**：是设计意图（tombstone 持有工件）还是要支持"替换 CSV 重跑"场景？

### BC-13：`install_memory_hooks.install(hosts=...)` 对未知 host 静默 0 变更
- **现象**：`install(hosts=("pi",))` 不报错也不安装。
- **裁定方向**：加 `if set(hosts) - {"codex","claude"}: raise ValueError`。

### BC-14：`install_memory_hooks.owned()` 弱启发式
- **现象**：手工打过 `--binding research-memory-v1 extra` 形状的 hook 会被 remove() 遗漏。
- **裁定方向**：把 owned() 改为 shlex 拆词后判定 token 序列，而非子串。

### BC-15：`hindsight_memory.verify_source` 的"工作树 dirty"被归入 unverified 而非 changed
- **现象**：`committed_bytes` 在 dirty 时抛 MemorySyncError 被 except 捕获返回 `"unverified"`；只有干净状态且 sha 不一致才返回 `"changed"`。语义上 dirty≠changed，但用户难以区分"根本没核过"与"被改过"。
- **裁定方向**：单独 catch MemorySyncError 返回 `"stale"`，或文档说明。

## 已遗留的潜在 flake（worker 1 注记）

- `test_adapter_timeout_reaps_its_child_without_killing_caller`：时序敏感，偶发；本次重跑通过。
- `test_completion_checker_failure_preserves_exit_fact_and_pending_state`：轮次间不稳定；本次重跑通过。

## 处置流程（按审计方案 0.4）

1. 主代理将上述条目抄入 `findings.jsonl`（severity/major|minor、status=open）。
2. 用户逐条裁定：修（指派批次）/不修（wontfix）/推迟（defer）。
3. 修改时翻转 BUG-CANDIDATE 测试断言（worker 4 的 8 条 BC 测试已按现状=允许写成，修代码时翻转即得守卫）。
