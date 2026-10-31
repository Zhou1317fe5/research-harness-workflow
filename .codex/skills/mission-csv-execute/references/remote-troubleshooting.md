# 远程高级协议、例外授权与诊断速查 (Remote Troubleshooting)

## 1. 用户授权审查例外（pre_run_exception）

正式运行要求一个通过的 scientific verdict。若独立科学审查**两次有界尝试均拿不到 verdict**（审查服务不可用、API 失败等），且当前为 `execution_purpose:official`：
可在用户明确授权的前提下，声明例外放行运行：

```json
{
  "schema_version": "mission.pre-run-exception.v1",
  "kind": "review_service_unavailable",
  "candidate_commit": "<40位commit>",
  "review_mode": "scientific_review",
  "review_result": "not_evaluable",
  "user_authorized": true,
  "authorization_ref": "user_authorization_turn:<用户原话>",
  "reason_code": "review_service_failure_two_attempts",
  "evidence_paths": ["issues/<stem>/prerun/prerun.scientific-review.json"]
}
```

- **两层校验**：路由层核验字段全且必填，台账层核验 CSV notes 对应标签；
- **例外只解锁启动，不解锁结论**：`fallback_allowed` 仍为 false，结果分析必须在 limitations 中显式保留该限制。

## 2. 多阶段 ExecutionPlan 推进 (stage_flow.py)

若任务存在正式多阶段 ExecutionPlan，使用无状态建议器：
```bash
printf '%s' '<request JSON>' | python .codex/skills/mission-csv-execute/scripts/stage_flow.py -
```
- 纯效果预测（`kind=effect_prediction`）未达标只记 advisory，不阻断推进；
- 预注册停止门（`preregistered_stop`）、正确性（`correctness`）或安全门（`safety`）未达标则严格阻断。

## 3. 常用底层诊断入口速查

| 诊断目的 | 命令 |
|---|---|
| 查看远端权威状态 | `rrctl --json inspect <RunID>` |
| 远端环境就绪检查 | `rrctl --json doctor --profile <profile>` |
| 拉取失败诊断快照 | `rrctl pull <RunID> --diagnostic` |
| 本地适配器契约自检 | `python3 .agents/harness/remote/fixture_contract.py <runspec.json>` |
| 行为指纹对比 | `python3 .agents/harness/remote/change_fingerprint.py <commit> --compare <commit> --repo .` |
| 离线完成产物校验 | `python3 .agents/harness/remote/validate_adapter.py <runspec> <pull目录> --phase completion` |
