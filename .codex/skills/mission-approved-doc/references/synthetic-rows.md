# Synthetic Rows Specification

在普通执行 issue 之后，CSV 必须包含必要的科研分析行与愿景收尾行。

## 1. RESULT-ANALYSIS-01 行规则

当任务包含正式远程实验结果（或产生科研 ExpID）时，在普通 issue 之后、`REVIEW-01` 之前必须插入唯一的 `RESULT-ANALYSIS-01` 行：
- `id`: `RESULT-ANALYSIS-01`
- `phase`: `analysis`
- `area`: `analysis`
- `priority`: `P0`
- `required_skills`: `post-run-result-analysis`
- `remote_state`: `not_applicable`
- `notes`: 引用 `result_analysis:reviews/result-analysis.json`，并配置 analysis 初始状态（具体执行由 `post-run-result-analysis` 自身负责）。

若 CSV 缺少该行，可通过脚本自动补齐：
```bash
python .codex/skills/mission-csv-execute/scripts/ensure_result_analysis_row.py <csv-path>
```

## 2. REVIEW-01 愿景收尾行规则

每个 CSV 必须追加唯一的 `REVIEW-01` 作为首轮愿景验收审计行：
- `id`: `REVIEW-01`
- `priority`: `P0`
- `phase`: 最后阶段序号
- `area`: `review`
- `title`: `Review documented vision against delivered work`
- `acceptance_criteria`: 验证前序所有非 review 行闭环，执行机械检查与风险分流；
- `test_mcp`: `manual`
- `required_skills`: 留空
- `review_regression_requirements`: 包含 2~4 条来自源文档的任务专属检查项，并要求逐条核验 claim/evidence ledger；
- `notes`: 至少包含 `review_kind:vision`，并引用 `source_doc`, `claim_ledger`, `outcome_contract`, `deferred_ledger`, `result_analysis` 与 `review_json`。

若 CSV 缺少该行，可通过脚本自动补齐：
```bash
python .codex/skills/mission-csv-execute/scripts/ensure_review_row.py <csv-path>
```
