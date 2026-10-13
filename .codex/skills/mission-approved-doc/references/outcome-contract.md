# Outcome Contract Specification

Outcome Contract 回答：面向最终读者，什么结果决定成败、现在不能声称什么。
落盘路径：`issues/<stem>/<stem>.outcomes.json`。

## 分工对比

| 工件 | 回答什么 | 消费者 |
|---|---|---|
| `*.claims.json` | spec 承诺是否被实现和验证 | issue / vision review |
| `*.outcomes.json` | 用户最终要知道什么、什么结果决定成败、现在不能声称什么 | vision review / handoff / 专项报告 |

## 提取要素

依次提取：
1. `artifact_role`：产物在整个研究中的定位；
2. `desired_effects`：预期达成的行为或性能变化；
3. `reader_questions`：面向决策、可以证伪、有证据入口并声明范围的读者问题；
4. `decisive_result`：能够一锤定音判定假说是否成立的结果指标；
5. `blocked_claims`：未达指定门槛前严禁声称的内容。

每个 effect/question/blocked claim 都必须包含 `source_ref`。不得凭空增加源文档未承诺的新能力，不得预填 pass/fail。

## 验证门禁

生成后必须运行机器校验：
```bash
python .codex/skills/mission-csv-execute/scripts/validate_outcome_contract.py issues/<stem>/<stem>.outcomes.json
```
校验失败时必须先修正 contract，严禁生成 CSV 或进入执行。
