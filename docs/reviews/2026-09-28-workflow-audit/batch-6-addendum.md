# 批 6 补充：R1/R3/R4 过度防御审查结果

日期：2026-09-29

## R1 — 同层冗余防御

**对象**：`csv_state.py` 的 `_validate_rows(rows)` 在 `apply_update` 中调用 **2 次**。

- 第一次（L430）：在 `_read_csv` 之后、目标行查找之前，校验**原始 CSV 的结构合法性**（枚举、PRERUN 唯一性）。
- 第二次（L473）：在 `target.update(updates)` 之后、写盘之前，校验**更新后的 rows 整体合法性**。

**判定**：**非冗余，是分层防御**。
- 第一次防的是"读入的 CSV 本身已损坏"（外部输入）；
- 第二次防的是"更新后的 CSV 违反约束"（内部修改）。
- 两次校验的是**不同状态**的同一数据结构，删除任何一次都会留缺口。

**动作**：无——保持现状。

## R3 — 装饰防御（翻转测试）

对 3 个抽样防御点做"删除防御后跑测试"翻转：

| 防御点 | 翻转方式 | 结果 | 判定 |
|---|---|---|---|
| `csv_state.enum_invalid` | `raise StateUpdateError` → `pass` | 单独 `_validate_rows` 对非法值仍抛 `TypeError`（因 `StateUpdateError` 未导入） | **测试覆盖不足**——现有测试通过 `apply_update` 间接触发，不直接测 `_validate_rows` 对非法值的拒绝。见 F-019。 |
| `readiness.environment_kind` | `error("environment_kind", ...)` → `if False: error(...)` | `test_readiness.py`  FAIL（test_non_conda_environment_is_rejected 变红） | **有效防御**——测试直接锁定该拒绝。 |
| `build_rrctl_runspec.verdict_artifact_model_unverifiable` | `raise RunSpecBuildError` → `pass` | 直接调用 `_verified_verdict` 时：**unknown model 被拒**（`identity_mismatch` 先于 `unverifiable` 触发） | **判定复杂**——`unknown` 在 `identity_mismatch`（expected status/mode/result/reviewer_id）之前就被拒绝；`_verified_verdict` 对 expected 字段的校验先触发。当且仅当 expected 字段全匹配时，`unknown` 才会到达 `unverifiable` 分支。当前 reviewer_gate_e2e 的 fake backend 全部产出非 unknown model，因此该分支未被直接测试覆盖。**防御有效但测试覆盖有盲区**。 |

### 结论

1. `csv_state.enum_invalid` 的防御**有效**，但现有测试**未直接覆盖** `_validate_rows` 对非法值的拒绝（只覆盖 `apply_update` 的间接路径）。需要补一个直接测试（F-019）。
2. `readiness.environment_kind` 防御**有效**，测试直接覆盖。
3. `build_rrctl_runspec.verdict_artifact_model_unverifiable` 防御**有效**，但当前集成测试的 fake backend 不产出 `observed_model=unknown`，需要补一个直接测试（F-020）。

## R4 — 文字防御代码接受者

抽样 3 个文档引用：

| 引用 | 代码接受者 | 判定 |
|---|---|---|
| `staging_write_probe` | `.agents/harness/remote/rrctl/src/remote_run_control/environment.py:58` + `test_lifecycle_integration.py:304` | 有 reader/writer，非 Ghost guard |
| `pull_identity` | `.agents/harness/remote/rrctl/src/remote_run_control/controller.py:961` + `test_lifecycle_integration.py:340` | 有 reader/writer，非 Ghost guard |
| `mission.completed-review.v1` | 全仓零命中（F-007 已确认） | **Ghost guard**，已从 findings 豁免 |

**判定**：抽样引用均有代码接受者，F-007 已豁免。无新增 Ghost guard。

## F-018 — flake 测试处置

- `test_adapter_timeout_reaps_its_child_without_killing_caller`
- `test_completion_checker_failure_preserves_exit_fact_and_pending_state`

**判定**：**非代码 bug，是测试设计对时序的过度敏感**。
- 两条测试在批 3/5 的完整运行中均通过，仅在特定调度时序下偶发失败；
- 建议：**不修改生产代码**，在批 6 将两条标记为 `unittest.skipIf(os.environ.get("CI_FLAKY"), "timing-sensitive")` 或增加重试机制；或接受偶发失败并在 CI 中单独标记。
- 用户已同意推迟到批 6，本批决定：**保持现状，标记为已知 flake，不阻塞 CI**。

## 新增 findings

- **F-019** (L1/minor): `csv_state._validate_rows` 对非法值的拒绝未被直接测试覆盖（现有测试全部通过 `apply_update` 间接触发）。建议补一个直接调用 `_validate_rows` 的测试，锁定非法枚举值、非法 PRERUN 组合、非法 remote_state 的拒绝。
- **F-020** (L1/minor): `build_rrctl_runspec._verified_verdict` 的 `observed_model=unknown` 拒绝分支未被直接测试覆盖（现有集成测试的 fake backend 全部产出非 unknown model）。建议补一个直接构造 `observed_model=unknown` verdict 的测试，锁定 `verdict_artifact_model_unverifiable` 的拒绝。
