# 入账证据边界（completion / recovery）

触发：记录含失败/非零退出、摘要显式 `complete:false`、缺少正常 completion，
或需要保留恢复资产时，在提交科研结果前读本页。
机器权威：`.agents/harness/records/experiment_records.py:load_run_provenance`；
record build 与 Mission ingest 共用它。跳过本页不能绕过该校验。

## 正常路径不是“有指标即完成”

- RunSpec 身份、manifest provenance 和必需产物哈希仍必须一致。
- 保存在当前 Mission 或该 Run 产物目录中的 rrctl status / inspect / terminal /
  workload-exit 证据按 RunID 核对。明确失败、停止或非零退出不能被本地补写的
  provenance 覆盖；不把 observer 超时返回码或 CSV 的 `ingested` 当成远端进程退出。
- 原 RunSpec 的 `adapter_contract.summary_path/summary_format` 决定控制摘要位置。
  摘要明确 `complete/completed/healthy/success/completion_contract_passed:false`、
  `status/state:failed/failure/error/stopped/cancelled/aborted` 或非零 `exit_code`
  时，正常 completion 拒绝入账；按 records 设置选出的其他结果摘要也执行同一检查。
- **不要求所有任务都有 `complete` 字段**。现有 generic_json 契约不要求它，
  未声明标量指标的控制/训练摘要仍可保留 `metric:null`。科学负结果
  `hypothesis_not_supported` / `inconclusive` 不是进程失败，仍可正常分析和结项。
- 每条新派生 Run 显式带 `evidence_mode`、`terminal`、`completion_contract_passed`。
  没有终态观测就写 null；只有已核对 RunID、RunSpec digest、manifest 摘要、exit0、
  completion health 的 `rrctl.completion.v1` receipt 才将 contract passed 投影为 true。
  兼容旧 provenance 的健康入账不等于证明远端 completion 已通过：无 receipt 时为 null。

## 通用恢复入口（不设项目/scope 白名单）

只接受 `rrctl recover-evidence` 原恢复清单及其 `output/` 文件布局。
有 `recovery_id` 的清单始终走恢复分支，即使有人又补了正常 provenance；
不得重建“正常 completion 清单”来避开恢复绑定。

在当前 Mission 的 `validation/recovery-binding.json` 增加绑定，结构为：

```json
{
  "schema": "research.mission-recovery-binding.v1",
  "bindings": [{
    "run_id": "<original RunID>",
    "run_spec_sha256": "<original canonical RunSpec digest>",
    "manifest_sha256": "<SHA-256 of original recovery manifest bytes>",
    "recovery_id": "<original recovery_id>",
    "reason": "<why these assets remain useful; limitations>",
    "status_evidence": {"path": "validation/<retained inspect>.json", "sha256": "<file SHA-256>"},
    "coverage_report": {"path": "validation/<coverage report>.json", "sha256": "<file SHA-256>"}
  }]
}
```

路径相对 Mission 目录，必须留在项目内且没有 symlink。
`status_evidence` 是原 rrctl inspect JSON（`result.status` 或 `status`），也可为原始
status 对象；必须匹配 RunID、已结束状态和整数 exit code，与保留的最终状态不矛盾。
允许 `failed` / 非零退出的**资产保全**，不把它变成成功执行。
本入口不会修改 rrctl 的生命周期；`recover-evidence` 命令自身的退出码限制仍适用，
需要另行保全的失败资产不能通过篡改原始状态绕开该限制。

覆盖核验报告必须由实际重算/资产核验产生，不得仅抄指标或自写一个 passed。
通用结构：

```json
{
  "schema": "research.recovery-coverage.v1",
  "status": "passed",
  "run_id": "<original RunID>",
  "run_spec_sha256": "<same original RunSpec digest>",
  "manifest_sha256": "<same original manifest byte hash>",
  "required_artifacts": ["<all required RunSpec artifact paths, sorted>"],
  "verified_entries": ["<exact original manifest entries: path/size/sha256 objects, original order>"],
  "count_field": "<original contract progress_count_field, if count declared>",
  "verified_count": "<integer independently checked count, if count declared>"
}
```

示意占位符须替换成真实对象/整数，不是字面 schema 值。报告可附加领域核验细节，
通用 reader 不内置 episode 文件名或项目 scope。机器重新哈希全部恢复文件，要求
原 RunSpec 全部 required artifacts 齐全；原合同有 min/exact count 时同时核对报告
和恢复 progress 的计数。无领域日志豁免、无科学必需产物缺失豁免；不完整快照保留
诊断/待补状态，不冒充正式可复用证据。哈希绑定提供一致性，不是远端真实性签名；
原始 inspect 与重算报告仍须有真实工具来源，不能靠本地自写 JSON 获得信任。

通过后只在内存中规范化 manifest 路径/身份，record 保留 `recovery_id`、原因、
绑定/报告路径以及原远端状态，且固定 `evidence_mode:recovery`、
`completion_contract_passed:false`。资产/指标的可复用范围由后续科学审查裁定，
“恢复入账”本身不证明 pipeline 成功或假设成立。

## 修复历史派生账本

先保留原 manifest、summary、RunSpec、唯一日志及历史 record，不覆盖历史证据。
模板同步后单独审查受影响 ExpID 的派生记录、EXPERIMENTS.csv、STATE、analysis 与
claim ledger：原来将 failed Run 写成 completion=true 的表述应标为待纠错，
必要时从已核验原证据生成新版本派生结果，并重新绑定科学审查/outcome。
不得为了刷新 record 而改原始 summary、RunSpec digest 或补写正常 completion provenance。
旧项目专有 recovery-binding 不自动迁移；保留原文件，另经明确审查建立通用绑定。
