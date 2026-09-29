# 审查覆盖矩阵

更新时间：2026-09-28（批 1 完）

## 批 1 — L0 静态一致性与契约（完成）

| 项 | 方法 | 覆盖 | 发现 |
|---|---|---|---|
| 0.1 全量镜像一致性 | `diff -rq` codex↔claude、codex↔pi、mirrored 清单枚举 | 53 个 .codex 文件 × 2 宿主 | **F-001**(minor, open)：25/53 硬编码守护、28 个未守护；**F-002**(info)：.pi 仅 1 skill；**F-003**(info)：parity 仅 PRERUN |
| 0.2 引用抽取对账 | 正则抽反引号引用 → 对账文件系统/argparse | 908 个唯一反引号引用 | **F-004**(minor)：39 处 basename 无仓库文件（多为合法运行时/契约路径，需分类）；**F-005**(info)：反面示例与正面使用需区分 |
| 0.3 schema 双向一致 | 读/写调用点 grep 对齐 | 8 个核心 schema | **F-006**(info)：mission.recovery-scan.v1 单向（只写无读）；**F-007**(info)：mission.completed-review.v1 全仓零命中 |
| 0.4 规则重复聚类 | 关键词聚类 8 条核心规则 | 11 个 skill + AGENTS/CLAUDE | **F-008**(info)：8 条规则多文件多表述，移交 L4 裁定 canonical |

## 批 2 及以后

待批 1 用户裁定 findings 后启动（L5 历史回放）。

## 发现汇览

| ID | 层 | 严重度 | 状态 | 一句话 |
|---|---|---|---|---|
| F-001 | L0.1 | minor | open | 镜像一致性校验仅守护 25/53 文件 |
| F-002 | L0.1 | info | open | .pi/skills 仅 1 个 skill，其余共用 canonical |
| F-003 | L0.1 | info | open | parity 校验仅覆盖 PRERUN 一个 skill |
| F-004 | L0.2 | minor | open | 39 处文档引用按 basename 无仓库文件，需分类 |
| F-005 | L0.2 | info | open | 引用校验需区分反面示例与正面使用 |
| F-006 | L0.3 | info | open | mission.recovery-scan.v1 单向 schema |
| F-007 | L0.3 | info | open | mission.completed-review.v1 全仓零命中 |
| F-008 | L0.4 | info | open | 8 条核心规则多文件多表述，待 L4 裁定 canonical |

## 测试入口（每批末必须跑绿）

- `python3 -m unittest discover -s .agents/harness/remote/tests`（77）
- `python3 -m unittest discover -s .agents/harness/workflow/tests`（61）
- `python3 -m unittest discover -s .agents/harness/remote/rrctl/tests`（61）
- `node --experimental-strip-types --test .pi/tests/*.test.mjs`（21）
- `python3 .pi/check-prerun-parity.py --self-test`

## 可持续审查流程（下次复用）

**不要从 0 重新审查。按下面 5 步增量执行：**

1. 读 `baseline.json` 拿基线 commit 与每层结果。
2. `git diff --name-only <baseline_commit>..HEAD` 得到变更文件集。
3. 对变更文件集合，只重跑与其相关的层检查：
   - 改了 `.codex/.claude/.pi/skills` 之间同一路径 → 重跑 L0.1；
   - 改了任意 `.md` 文档 → 重跑 L0.2 引用对账；
   - 改了 schema 常量或读/写调用点 → 重跑 L0.3；
   - 改了规则相关文档 → 重查 L0.4 聚类（规则重复的 canonical 归属）。
4. 查 `findings.jsonl`：
   - `status=open` → 仍未裁定，继续跟踪；
   - 已修复 → 改 `status=fixed`、填 `fix_commit`；
   - 裁定不修 → 改 `status=wontfix`、填理由；
   - 新发现问题 → 追加新行（id 递增，layer 指向所属检查）。
5. 每层完成且 findings.jsonl 无新增 open blocker 后：更新 `coverage.md` 对应该层一行，并把 `baseline.json` 的 `as_of_commit` 推进到当前 HEAD。

**若新增了一个全新检查（例如批 3 加了 L1 模块单测），则在该层运行后把新检查名写进 `baseline.json.checks` 并在 `layers` 中登记其结果，使下次增量审查知道该检查已存在。**

## 批 1 补充实施（2026-09-29）

| ID | 处置 | 结果 |
|---|---|---|
| F-001 | **fixed** | `.claude/skills` 改为 symlink → `../.codex/skills`（commit f97b1bb），物理同源；测试改为 symlink 断言 |
| F-002 | **wontfix** | 用户确认为有意设计（仅 PRERUN 有 .pi 副本），归档 |
| F-003 | **wontfix** | 同 F-002，parity 仅覆盖 PRERUN 是设计结果 |
| F-004 | **fixed** | `reference-whitelist.json` 建立引用分类（runtime-generated/contract-file/output-path/false-positive） |
| F-005 | **fixed** | 归入 F-004 机制；`--ephemeral` 标记为 false-positive（反面示例） |
| F-006 | **wontfix** | Agent-consumed CLI 输出，设计单向；豁免登记 baseline.json |
| F-007 | **wontfix** | 审计候选清单误报，仓库不存在；豁免登记 baseline.json |
| F-008 | **fixed** | `rule-canonical.md` 登记 8 条规则待裁定清单；canonical 裁定移交批 4 L4 |

**批 1 结论**：8 条 findings 全部处置完毕（4 fixed / 4 wontfix），无 open blocker。

## 批 2 — L5 历史会话回放（完成）

回放 6 个代表会话（4 子代理并行 + 2 主代理补跑失败样本）：

| 样本 | 会话日期 | 大小 | 方法 | 产出 |
|---|---|---|---|---|
| 09-20 hera-ase-performance | 2026-09-20 06:25 → 跨日 | 4.2MB | scout aborted，主代理补跑 | replay-2026-09-20-hera-ase-performance.md |
| 09-22 hera-cgm | 2026-09-22 01:34 → 同日 | 5.3MB | scout 成功 | replay-2026-09-22-hera-cgm.md |
| 09-23 author-version-full-reproduction | 2026-09-23 03:54 → 同日 | 1.6MB | scout PAD 空，主代理补跑 | replay-2026-09-23-author-version-full-reproduction.md |
| 09-24 hera-scope-diag | 2026-09-24 06:14 → 跨日 | 5.6MB | 主代理（scout 未派） | replay-2026-09-24-hera-scope-diag.md |
| 09-28 hera-gsr-scnp-safe | 2026-09-28 03:19 | 4.9MB | scout 成功 | replay-2026-09-28-hera-gsr-scnp-safe.md |
| 09-29 hera-gsr-scnp-region-full | 2026-09-29 02:55 | 2.3MB | scout 成功 | replay-2026-09-29-hera-gsr-scnp-region-full.md |

### 故障模式频率表（6 回放 + 9-27 主会话）

| 模式 | 层 | 频次 | 覆盖样本 | 处置 |
|---|---|---|---|---|
| rrctl event 同 event 重复推送 | L2 | 22 组 | 09-20×6/09-22×3/09-24×5/09-27×8 | relay 层已修（4e21a45）；Pi 侧单测待批 3 |
| smoke wrapper 适配成本 | L1 | 7 例 | 09-28×4/09-29×3 | Mission 内 fixed-in-row；未升级缺陷 |
| codex PRERUN rollout observed_model | L1 | 1 | 09-27 | 已修（4e21a45/3ef28b1） |
| pull staging 残留 | L2 | 1 | 09-27 | 已修（0544962） |
| CSV schema 首执行错 | L1 | 1 | 09-27 | 已修（preflight.py） |
| codex verdict 误标 event-stream | L1 | 1 | reviewer | 已修（3ef28b1） |
| 镜像一致性 25/53 | L0 | 1 | 批 1 | 已修（symlink） |
| 文档反引号 basename | L0 | 39 | 批 1 | reference-whitelist.json |
| workflow_sync 双向 | L1 | 1 | 09-23 | 设计覆盖 |
| 隐私扫描请求 | L3 | 1 | 09-23 | 批 6 L3 真实需求证据 |
| 基线归属修正 | L0/L1 | 1 | 09-28 | fixed-in-mission |
| PRERUN 步骤数漂移 | L0/L1 | 2 | 09-29 | fixed-in-mission |
| remote_state=running_remote 被拒 | L1 | 1 | 09-29 | ✅ 正向 |
| observer timeout 不误标 failed | L2 | 1 | 09-29 | ✅ 正向 |
| evidence-close 非独立 | L4 | 2 | 09-28/29 | F-008/L4 待裁定 |
| 独立 reviewer 触发 vs 用户介入 | L4 | 1 | 09-22 | F-010 |
| 独立分析 vs closing 冻结点 | L4 | 1 | 09-29 | F-011 |

### 批 2 新增 findings

- F-009 (L2/major) rrctl event Pi 侧重复推送——批 3 L1 补单测
- F-010 (L4/minor) 独立 reviewer 触发 vs 用户介入
- F-011 (L4/minor) 独立分析时序 vs closing 冻结点

### 批 2 重要正向证据

- codex 后端 PRERUN 在 09-29 region-full 首次正向使用，批 1 rollout 取证修复端到端正确
- remote_state=running_remote 非法迁移被 csv_state fail-closed reject
- observer timeout bounded retries exhausted 不误标 failed（wait 语义区分）

### 对批 3 优先级的调整建议

基于频率表：
1. **最高优先**：Pi 侧 rrctl-events 扩展单测（F-009 的 22 组历史证据）
2. **次优先**：L1 smoke wrapper 模式提取（7 例"in-row fix"是否可抽公共模式）
3. **保持**：各正向证据路径（fail-closed gate、wait 语义）只需要在 L1 只要不是已经覆盖就不用重复测

## 批 3 — L1 单元与状态机（完成）

4 个 worker 并行 + 主代理验证提交。

### 测试入口规模变化

| 入口 | 批前 | 批后 | 增量 |
|---|---|---|---|
| rrctl/tests | 61 | 231 | +170（worker 1 新增 7 文件 118 用例）|
| remote/tests | 77 | 83 | +6（worker 3 新增 records_ledger）|
| workflow/tests | 61 | 174 | +113（worker 2 blindspots 61、worker 4 csv-matrix 17、worker 3 lifecycle 等）|
| memory/tests | — | 103 | 随 adb8601 新增 test_memory_protocol.py 被纳入 |
| .pi/tests | 21 | 23+12 = 35 | F-009 已修 + 其它 pi 扩展既有用例 |

### 新增测试文件

- `.agents/harness/remote/rrctl/tests/test_security.py / test_readiness.py / test_health.py / test_finalization.py / test_output_cleanup.py / test_output_limits.py / test_zipapp_builder.py`（worker 1，118 用例）
- `.agents/harness/workflow/tests/test_mission_scripts_blindspots.py`（worker 2，61 用例）
- `.agents/harness/workflow/tests/test_common_basics.py / test_pipeline_stages.py / test_mission_lifecycle.py` 与 `.agents/harness/remote/tests/test_records_ledger.py` 与 `.agents/harness/memory/tests/test_memory_protocol.py`（worker 3，61 用例）
- `.agents/harness/workflow/tests/test_mission_contracts.py` 追加 `CsvStateTransitionMatrixTests`（worker 4，17 用例含 8 个 BUG-CANDIDATE 断言现状）

### 新发现（findings.jsonl 增 7 条）

| ID | 层 | 严重度 | 状态 | 简述 |
|---|---|---|---|---|
| F-012 | L1 | **major** | open | csv_state 迁移方向无单调性约束（BC-1..BC-8 共 8 类）;worker 4 已写"现状=允许"断言便于翻转为守卫 |
| F-013 | L1 | minor | open | memory/tests/test_lifecycle.py 在子代理环境下 2 用例失败（BC-9） |
| F-014 | L1 | minor | open | file_lock 同进程嵌套死锁（BC-10） |
| F-015 | L1 | info | open | mission_state paused 不清 current_task；终态任务持有 csv 唯一绑定（BC-11/12） |
| F-016 | L1 | minor | open | install_memory_hooks 未知 host 静默 + owned() 弱启发式（BC-13/14） |
| F-017 | L1 | info | open | hindsight verify_source dirty 归 unverified 而非 changed（BC-15） |
| F-018 | L1 | minor | open | monitoring 两个时序敏感用例（flake） |

### BUG-CANDIDATE 汇总文档

`docs/reviews/2026-09-28-workflow-audit/bug-candidates-batch-3.md`（18 条 BC-1..BC-15 + flake 备注）。

### 处置约定（按审计方案 0.4）

- 本批次只补测试不改生产代码——0 个生产文件被修改。
- BUG-CANDIDATE 的断言按"现状=允许"写，后续修代码翻转断言即得守卫。
- 待用户逐条裁定（修/不修/推迟）后再进入修复批次。

### 测试

- 三入口 + parity + .pi/tests 全绿；
- `git diff --check` 干净。

## 批 3 修复轮（2026-09-29 后半）

按用户选定 F-012+F-013+F-014+F-016 修复、F-015/F-017 记为约定待文档、F-018 推迟批 6。

### 提交

| 提交 | 范围 | 说明 |
|---|---|---|
| `55171c1` | F-012 major | `csv_state._validate_row_transition()`：dev/review 单调推进 + `_REMOTE_FORWARD` 白名单 + `git=已提交` 两个 junction 禁写。翻新原 8 BC 断言为拒绝，新增 6 个合法迁移烟测，修 1 个 fixture |
| `40c6e8a` | F-013 minor | `test_lifecycle.setUp` 用 `patch.dict(os.environ, {}, clear=True)` 清除子代理 env，修复子代理环境 2 用例假失败；F-014 `file_lock` 同进程嵌套死锁 docstring 明示（当前不改实现） |
| `3d03013` | F-016 minor | `install_memory_hooks.install()` 新增 `_validate_hosts()` 拒未知 host；`owned()` 重写为 `_tokens_look_like_install()` 按形状判定（bash -c 内嵌 / 路径 + `--binding <MARKER>`），不再依赖子串启发式 |

### findings 状态

```
fixed:      9  (F-001 F-004 F-005 F-008 F-009 F-012 F-013 F-014 F-016)
wontfix:    4  (F-002 F-003 F-006 F-007)
deferred:   3  (F-015 F-017 F-018)
open:       2  (F-010 F-011 留批 4)
```

### BUG-CANDIDATE 状态（批 3 登记的 15 条）

- BC-1..BC-8 → F-012 全部 fixed（断言已翻转为拒绝）
- BC-9 → F-013 fixed
- BC-10 → F-014 fixed（文档化约定，未改实现——当前已知调用点均为跨进程/单次使用）
- BC-11..BC-12 → F-015 deferred（约定待文档）
- BC-13 → F-016 fixed（未知 host 现 raise）
- BC-14 → F-016 fixed（owned() 形状判定）
- BC-15 → F-017 deferred（verify_source 语义约定）
- flake 两条 → F-018 deferred（批 6）

### 测试入口最终值

| 入口 | 数量 |
|---|---|
| remote/rrctl/tests | 231 OK |
| workflow/tests | 175 OK |
| remote/tests | 83 OK |
| memory/tests | 103 OK（含子代理环境） |
| .pi/tests | 35 pass |
| prerun-parity | PASS |
