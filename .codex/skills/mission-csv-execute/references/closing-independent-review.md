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
兼容 CSV 没有 source doc 时省略 `--source-doc`。脚本失败时显式分类（`quota_error` / `transport_error` / `timeout`）后进入 self-review。

## 3. Reviewer Prompt 硬要求

- 独立路径的请求模型 = 当前会话（执行）模型；
- 只基于批准文档、CSV、claim/evidence ledger、diff/commit、测试证据与交付声明；
- 不信任主 agent 的结论性总结；
- 不为了找问题而找问题，只有让结论不可信或让声明超出证据的差距才算 gap；
- 必须检查声明与证据等级是否一致，尤其严查 mock 是否冒充通过；
- 结论分类：
  - `vision_met`: 已按批准协议实现并执行（负结果或合法跳过亦可为此值）；
  - `gaps_found`: 存在 current-scope 实施或证据差距；
  - `limited_review`: 证据不足无法判断。
- `scientific_outcome` 单独记录（`hypothesis_supported / hypothesis_not_supported / gate_failed / inconclusive / not_applicable`），严禁将 `gate_failed` 转化为 `gaps_found`。

## 4. Review Log 格式 (`<stem>.review.md`)

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
