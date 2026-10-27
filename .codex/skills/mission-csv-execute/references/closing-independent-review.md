# Closing Independent Review Specification

当 `final_ready.py` 判定 `decision:independent_review_required` 时（L3/L4 交付无等价独立审查、机械证据冲突、claim 疑似高估或存在未解决 current-scope 疑点），读取并执行本规范。

## 1. 独立审查能力阶梯

按顺序选择第一条可执行路径，并把结果写入 review log 与 CSV notes：

| 优先级 | 模式 | 记录值 | 独立性 | 执行要求 |
|---|---|---|---|---|
| 1 | `scripts/run_vision_review.py` | `review_agent_mode:closing-reviewer-job` | `review_independence:true` | 只读；prompt 不含主代理结论；通过 reviewer_job fresh session 完成 |
| 2 | 主会话按同一 prompt 自审 | `review_agent_mode:self-review` | `review_independence:false` | 仅在 reviewer_job 失败时使用；如实记录 capability failure，自审可闭环 |

### 审查模型铁律（严禁越级）
- 收尾审查模型**必须为当前会话（执行）模型，严禁私自指定或借用高级审查模型**。
- 收尾审查是工程交付与台账核验（Spec 对应关系、CSV 状态、Git 提交对齐、Handoff 审计），不是科学机理审查。科学机理结论已由前序独立 `RESULT-ANALYSIS-01` 敲定，收尾审查严禁越级调用高级模型浪费配额。
- requested/observed 必须如实记录并来自可确证的运行时通道（`session-metadata` / `event-stream` / `parent-runtime`），记录 `unknown` 会被验证器拒绝。

## 2. 脚本调用接口

```bash
python .codex/skills/mission-csv-execute/scripts/run_vision_review.py \
  --csv <csv-path> \
  --source-doc <source-doc-path> \
  --claim-ledger <claim-ledger-json-path> \
  --outcome-contract <outcome-contract-json-path> \
  --deferred-ledger <deferred-ledger-json-path> \
  --review-log <csv-path-without-.csv>.review.md \
  --output reviews/review-01.json \
  --handoff <csv-path-without-.csv>.handoff.md \
  --workdir <repo-root> \
  --backend codex \
  --model <当前会话模型（默认自动探测，严禁传高级审查模型）>
```
兼容 CSV 没有 source doc 时省略 `--source-doc`。脚本失败时显式分类（`quota_error` / `transport_error` / `timeout`）；供应商额度耗尽必须暂停并告知用户，不自动切模型或以自审掩盖额度失败。其他 capability failure 可如实记录后进入 self-review。

### 恢复与材料身份

- runtime 选 CSV 最后一个 REVIEW 行为当前轮；job 目录为 `reviews/closing-reviewer-job-<截短CSV展示名>-<截短轮次展示名>-<完整SHA256>/`。展示名不承担唯一性，完整路径/轮次/材料/任务/模型/backend 一起确定身份。
- 同轮、同输入重跑复用原 job、verdict 和持久化 invocation 记账；`REVIEW-02` 或材料变化生成新 job，不搬移、不删除、不改名旧 job，也不删除 verdict/job.json 来重置预算。
- 材料集合是 CSV、批准文档、指定 ledgers/review log、CSV/ledger/extra 明确引用的证据，以及 Git 列出的实现/配置源码（含未忽略的新源码）。显式引用 RunID 目录时绑定 `artifact_manifest.json` 和其中 `entries[].path` 的实际文件内容（流式 hash）；不能只信 manifest 声称的旧 digest。无 manifest 的目录拒绝，需改为明确文件引用。不递归盲扫数据集、checkpoint、旧实验目录或无关大产物；未列入 manifest 的额外文件不自动纳入，关键证据须在清单或显式引用中。未识别的源码类型也须显式引用。
- 凭据（`.env*`、`profiles.json`、私钥等）不得进入材料；自动发现跳过，显式输入/证据引用拒绝，软链接目标同样检查。只传所需配置说明，不传秘密本体。
- 按工作树内容而非 HEAD/diff 散列：已提交科学代码变化也会使旧结论失效；空提交与暂存动作不会。当前 REVIEW 状态、输出 tags、当前轮 review-log 段、事件、progress/lock 和 reviewer job 不作为本轮新材料，避免结账自失效；其他行与历史审查仍保留。
- reviewer_job 的本地 invocation 上限只约束此 job 启动/恢复次数，不代表供应商剩余额度；新材料可有新 job，但不是额度恢复证据。供应商 quota 错误仍按上面的暂停规则处理。

## 3. Reviewer Prompt 硬要求

- 独立路径的请求模型 = 当前会话（执行）模型；
- 只基于批准文档、CSV、claim/evidence ledger、diff/commit、测试证据与交付声明；
- 不信任主 agent 的结论性总结；
- 交工单正文不自报独立性/模型；最终唯一身份头由 launcher 从结构化 runtime 证据生成，不能把自审稿直接改标为独立审查。
- 不为了找问题而找问题，只有让结论不可信或让声明超出证据的差距才算 gap；
- 必须检查声明与证据等级是否一致，尤其严查 mock 是否冒充通过；
- 结论分类：
  - `vision_met`: 已按批准协议实现并执行（负结果或合法跳过亦可为此值）；
  - `gaps_found`: 存在 current-scope 实施或证据差距；
  - `limited_review`: 证据不足无法判断。
- `scientific_outcome` 单独记录（`hypothesis_supported / hypothesis_not_supported / gate_failed / inconclusive / not_applicable`），严禁将 `gate_failed` 转化为 `gaps_found`。
- 数值精度容差用于判断实现/复现是否在约定误差内，不等于统计显著性；不能据此声称涨点可信，统计结论仍按批准协议与实际重复实验的证据判定。

## 4. Review Log 格式 (`<stem>.review.md`)

- **远端工件绝对地址铁律**：非必要数据严禁拉取到本地。日志中对大型工件、特征切片或原始证据的引用，统一直接记录其在远端服务器上的绝对直连物理地址（格式：`<server_host>:<absolute_path>`，如 `adam:/home/zhoujiale/cdfss-dinov3-workspace/runs/<RunID>/output/...`），严禁使用本地相对路径，严禁为了在文档中引用而将大文件拉到本地。

```markdown
## REVIEW-N
- Source doc: <path>
- Review agent: closing-reviewer-job | self-review
- Review independence: true | false
- Review requested model: <当前执行模型>
- Review observed model: <catalog model id>
- Review model evidence: session-metadata | event-stream | parent-runtime
- Scope checked: <goals/non-goals>
- Evidence checked: <commits/tests/logs>
- Claim coverage: complete | gaps
- Claim/evidence alignment: matched | mismatches found
- Result: vision_met | gaps_found | limited_review
- Scientific outcome: hypothesis_supported | hypothesis_not_supported | gate_failed | inconclusive | not_applicable
- Gaps: <none or bullet list>
- Follow-up issues added: <none or ids>
- Deferred findings: <none or ids>
- Human-required blockers: <none or exact blocker>
```
