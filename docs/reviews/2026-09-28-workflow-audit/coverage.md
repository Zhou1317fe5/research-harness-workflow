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
5. 每层完成且 findings.de 无新增 open blocker 后：更新 `coverage.md` 对应该层一行，并把 `baseline.json` 的 `as_of_commit` 推进到当前 HEAD。

**若新增了一个全新检查（例如批 3 加了 L1 模块单测），则在该层运行后把新检查名写进 `baseline.json.checks` 并在 `layers` 中登记其结果，使下次增量审查知道该检查已存在。**
