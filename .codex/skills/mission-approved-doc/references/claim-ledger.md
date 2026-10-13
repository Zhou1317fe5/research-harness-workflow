# Claim / Evidence Ledger Specification

在生成 issue 前，必须从 `execution_scope` 中抽取一张可验证的 claim/evidence 账本，并随 CSV 同目录落盘为 `issues/<stem>/<stem>.claims.json`。

## 字段规范

| 字段 | 含义 |
|---|---|
| `claim_id` | `CLAIM-001` 递增编号 |
| `source_ref` | 源文档 `path:line` |
| `promise` | 可验证承诺本体（WHEN/MUST/SHALL、状态枚举、字段定义、启动行为、消费关系、错误处理、API 契约、真实副作用、显式 non-goal 边界） |
| `production_path_required` | 是否要求启动注册、API、worker、agent tool、consumer、真实 provider、E2E 等生产路径 |
| `covered_by` | 负责实现/验证的 issue id 数组，生成后回填 |
| `evidence_required` | `real_e2e` / `integration` / `unit` / `static` / `mock_allowed` / `limited_allowed` |
| `status` | 生成时写 `pending`（或文档排除时写 `out_of_scope`）；终态为 `verified / not_run_by_preregistered_gate / failed / validation_gap / out_of_scope` |

- `evidence_required=real_e2e` 的 claim 只有真实端到端运行后才能写 `verified`，并附 `evidence_refs`。
- 若预注册门限决定后续阶段不运行，写 `not_run_by_preregistered_gate` 和非空 `gate_evidence`（表示按协议终止，不表示该能力通过）。

## JSON Schema

```json
{
  "source_doc": "<doc-path>",
  "csv": "<csv-file-name>",
  "execution_scope": "<scope>",
  "claim_coverage": {"covered": 0, "total": 0, "status": "pending"},
  "claims": [
    {
      "claim_id": "CLAIM-001",
      "source_ref": "<doc-path>:<line>",
      "promise": "<可验证承诺原文或忠实改写>",
      "covered_by": ["ISSUE-01"],
      "evidence_required": "integration",
      "production_path_required": true,
      "status": "pending"
    }
  ]
}
```

## CSV Notes 压缩引用格式

```text
claim_ledger:<stem>.claims.json; claims:CLAIM-001,CLAIM-002; claim_coverage:X/Y; claim_coverage_status:pending; evidence_level:integration; production_path:covered
```
