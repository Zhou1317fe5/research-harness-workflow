# 批 6 L3 安全审查报告

- 审计者：security-auditor（read-only）→ 主 Executor 落盘
- 日期：2026-09-29
- 基线：research-harness-workflow main（批 5 后）

## 威胁模型

| 输入源 | 流向下游 | 越权风险面 |
|---|---|---|
| `.env` 中 `RRCTL_SSH_PASSWORD` 等 | `transport.py._base_command` → `sshpass -e` env | `argv` 干净；env 落 `sshpass` 子进程可被同 UID `/proc/*/environ` 读出 |
| `profiles.json`（无密码字面量） | `ssh_argv` → `Popen(list)` | profile 校验 basename 必须是 `ssh`，单 destination 无 remote command |
| Mission CSV（用户/Agent 写） | `csv_state.apply_update` 锁内原子写 | FIELDS/`_validate_single_prerun`/枚举白名单 |
| RunSpec（用户/Agent 写） | `build_rrctl_runspec`，最终写 `rrctl.run.v1` | `_relative` / `_local_absolute` / `contains_secret_data` 拒险 |
| Reviewer 文本响应 | `reviewer_job.parse_json_object` | 严格 JSON Schema + 类型 narrowing |
| 远程拉回 artifact | `controller.pull` → 落 `remote_artifacts/<RunID>` | per-entry `is_symlink()` + size + sha256 |

## 检查项矩阵

| 检查项 | 方法 | 覆盖文件 |
|---|---|---|
| 凭据三通道（env/argv/日志） | 逐调用点审计 secret 是否落 env/argv/日志 | remote_run.py、transport.py、reviewer_job.py、memory/ |
| 注入面（路径穿越） | 检查所有"用户输入路径 → 文件写入"入口 | csv_state.py、compact_artifacts.py、workflow_sync.py、preflight.py |
| 注入面（命令注入） | 检查 shell=True、字符串拼命令、SSH argv 构造 | remote_run.py、controller.py、transport.py、install_memory_hooks.py |
| JSON/配置注入 | json.loads 后未验证 schema 的入口 | reviewer_job.py、memory_hooks.py |
| TOCTOU | pull manifest 验签→落盘、verdict 验签→gate 开门、workflow_sync 读状态→写目标 | controller.py、build_rrctl_runspec.py、workflow_sync.py |
| 故障注入 | kill -9、SSH 断、半写 JSON、磁盘满、编码异常、时钟回拨 → fail-closed | 已由批 5 worker 3 集成测试覆盖 |
| 并发 | 双 Executor 同写 CSV/sidecar/RunID | common/locking.py、csv_state.py |

## 发现（按严重度）

### Critical

无。

### High

**H-1** · `build_rrctl_runspec.py:283-330` —— verdict 文件做内容校验后只把"路径"写进 RunSpec，存在 IO-TOCTOU

证据：
- `_verified_verdict` 在构建时根据路径读 `verdict.json`、校验 schema/commit/reviewer_id，然后把 `verdict_artifact: "<repo 相对路径>"` token 放进 RunSpec。
- `remote_run.py` 后续仅凭 CSV notes 的 tag 值匹配放行，**不再重新哈希 verdict 文件内容**。

利用窗口：恶意/失控 Agent 在 RunSpec 写好之后、`apply_update` 写 PRERUN-REVIEW row 之前替换 `verdict.json`（保持 schema 合法但翻转 `result`/`decision`），本次 launch 仍按"旧 RunSpec 已固定路径"通过 gate。Gate 因此把"一份可变磁盘文件"当作了长效凭据。

修复方向：
1. RunSpec 中冗余保存 `verdict_sha256`（按 canonical JSON 序列化后 sha256），同时保存路径。
2. `csv_state.apply_update` 在写 PRERUN-REVIEW row 时把 verdict sha 作为单值 tag 一起登记。
3. `remote_run.execute` 在 launch 前再次 `read_bytes → canonical → sha256` 比对 RunSpec 中数值；不一致即 `RunSpecBuildError("gate verdict drifted")`。

### High/Medium

**M-1** · `transport.py:260-263` —— `SSHPASS` 通过 env 传给 `sshpass` 子进程，同 UID 进程可读

证据：`environment["SSHPASS"] = password; command = ["sshpass", "-e", *command]`。argv 干净、日志干净（`StreamRedactor` 对 explicit value 也脱敏）；但 Linux 下同 UID 的 `/proc/<pid>/environ` 仍可读出 SSH 密码。

影响：共享开发机上其它 agent/进程能窃取；单机单用户可接受。

修复方向：评估切到 `ssh-agent + 公钥`（`profile.kind="ssh"` 已无 `password_env` 即此路径）；保留 password 路径则补文档说明"假定单用户工作站"。

**M-2** · `workflow_sync.py`（中段省略未直接看到 `write_entry`） —— 公共文件落盘前未在本次读取的证据中确认对 destination 做 `is_symlink()` / `resolve().is_relative_to(target)` 检查

证据：摘录中只看到 `EXCLUDE_PREFIXES/EXCLUDE_PARTS/EXCLUDE_SUFFIXES` 黑名单与白名单，目标端写入是否查 symlink 未能亲眼验证；相关 path（`PUBLIC_DIRS=(".agents/harness", ".agents/skills", ".codex/skills", ".claude/skills", ".pi")`）在测试里被允许是 symlink（`test_workflow_sync_matrix.py:207` 的 `.claude/skills` 是 symlink）。

影响：目标仓里预埋的 symlink 可能把同步内容写到目标仓外。

修复方向：在 `command_transfer` 将每个 entry 落盘前加：
```python
dest = (target_repo / path).resolve()
if not dest.is_relative_to(target_repo.resolve()): raise SystemExit(...)
if (target_repo / path).is_symlink() and not is_known_canonical_layout(...): raise ...
```
同时把 `.claude/skills` 这种**模板化预期的 symlink**加白名单。

**M-3** · `harness/common/locking.py` —— `fcntl.flock` 不防同进程嵌套重入

证据：docstring 明示"不防同进程嵌套重入。同一进程内对同一锁文件第二次调用 `file_lock` 会因 `fcntl.flock` 阻塞而卡死。"当前所有调用点都是跨进程或单次，**已知调用方安全**；但任何未来在 `csv_state` 之外再嵌套一层锁的工具都会立即死锁，而且不会以显式失败暴露。

修复方向：用 `threading.local()` 持 fd 计数，重入返回同一 holder；或改用 `fcntl.lockf(..., F_OFD_SETLK)` + 进程内记录后 raise。

## Low/Hardening

- **L-1** · `research_memory.py:SECRET` 与 rrctl `security.py:SECRET_ASSIGNMENT` 是两个独立维护的正则，模式语义接近但演化会偏差。建议提一份公共表。
- **L-2** · `monitor.py` 心跳用 `utc_now()`（wall clock），过期判定用 `time.monotonic()`（正确）；wall clock 回拨只影响审计 timestamp，不导致 stale 误判或 gate 误开。**无可利用问题，记录以免误改。**
- **L-3** · `reviewer_job.parse_json_object` 退化路径会循环对每个 `{` 尝试 `decoder.raw_decode(text[index:])`，对 noisy 大响应是 O(n²)。建议先 strip 到末尾非空白 `}`，或限制文本 len（MAX 1 MiB）。
- **L-4** · `compact_artifacts.py:_run_id` 已否定 `/`、`..`、空段，安全。`research_root` 来自 CLI 显式参数，调用方预期就是把 `remote_artifacts` 移到 project root 下，非漏洞。

## Notable Non-Issues

- **`remote_run.py:281`** `["bash","-c",'set -e; set -a; source "$1"; set +a; shift; exec "$@"', "harness", env_file, *argv]`：把凭据来源 `.env` 作为 `$1`、argv 作为剩余参数位置展开，**secrets 不进 argv/日志**。
- **`install_memory_hooks.py:31-38`** 用 `shlex.join(list)` 生成 hook 命令，常量 marker、严格宿主白名单（"codex"/"claude"），路径来自 `Path.resolve()`。
- **`artifacts.build_artifact_manifest`** 对每个文件 `is_symlink()` 严格 raise；`controller.py:1001-1005` pull 时 size+sha256 双校验，rename 目标先 unlink + exclusive create。
- **`jsonutil.atomic_write_bytes`** 用 `mkstemp` + `fchmod` + `fsync` 文件 + `fsync` 目录，是规范的原子小文件写。
- **`output_limits.run_bounded`** 半写 stdout/stderr 用 `selectors` 排空、`process.kill()` 超时强杀，timeout 错误显式 `remote_state_unknown: True`。
- **`hindsight_mcp.py`**：HTTPS 或 localhost-only；禁 redirect；`MAX_RESPONSE_BYTES=4MiB`；`Authorization` 头只在请求构造前临时组装，不打印；HTTPError 不外抛 body。
- **`csv_state.apply_update`**：锁内 `_read_csv → _validate_rows → _validate_single_prerun → 写 sidecar → 原子写 csv`，事件 digest 由 `sha256_canonical(schema,row_id,event)` 决定，sidecar/csv 双写不一致时 event digest 仍能识别。
- **`profiles.py`** `password_env` 必须为合法 shell 变量名；`secure_file_mode(profile_path)` 必须 0600；显式拒绝 `password/api_key` 类型键名直接出现在 profile（防止字面量入仓）。
- **`reviewer_job.py:185 / :193 / :393 / :463`** 全部用 `subprocess.Popen(list_argv)`，**非** shell=True；此前担心的字符串拼 argv 不成立。
- **凭据三通道约定**整体成立：profiles.json 存变量名 + argv 仅变量名引用 + `StreamRedactor` 对显式凭据值做多形式脱敏（raw + JSON 转义 + 私钥段落）。

## 建议

整体姿态良好：rrctl 这条主路径的命令注入、路径限域、原子写都是防御到位的；memory hook 体系对 host/stdin/secret 的过滤也很规整。需要立刻处理的可信根问题只有 **H-1**——把 `verdict.json` 从"路径凭据"升级为"哈希凭据"，并把 hash 写进 RunSpec 与 CSV tag；这条链路不补，前面 `_verified_verdict` 的所有内容校验只是构建时一次性的"建议"。其余 Medium 项（SSHPASS env、workflow_sync 落盘前 symlink 检查、file_lock 重入）按上面顺序择期处理。
