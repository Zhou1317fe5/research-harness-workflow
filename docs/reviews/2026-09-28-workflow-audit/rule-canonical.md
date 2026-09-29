# 核心规则 canonical 待裁定清单

{
  "schema_version": "audit.rule-canonical.v1",
  "generated_at": "2026-09-29",
  "status": "pending-L4-adjudication",
  "rules": [
    {
      "id": "fallback_allowed=false",
      "files": 4,
      "locations": [
        "remote-run.md",
        "remote-run-snippet/SKILL.md",
        "AGENTS.md",
        "CLAUDE.md"
      ],
      "phrasings": [
        "fallback_allowed 必须为 false",
        "fallback_allowed 保持 false",
        "route 仍是 rrctl process、fallback_allowed 保持 false"
      ],
      "question": "保持 false 是否包含初始默认 false 且不得改？"
    },
    {
      "id": "preregistered-gate",
      "files": 6,
      "locations": [
        "remote-run.md",
        "mission-csv-execute/SKILL.md",
        "mission-approved-doc/SKILL.md",
        "remote-run-snippet/SKILL.md",
        "closing-review.md",
        "post-run-result-analysis/SKILL.md"
      ],
      "phrasings": [
        "预注册门限",
        "preregistered gate",
        "预注册"
      ],
      "question": "预注册的门限值与跳过条件是否在所有文件一致？"
    },
    {
      "id": "independent-review",
      "files": 6,
      "locations": [
        "closing-review.md",
        "pre-run-implementation-review/SKILL.md",
        "csv-schema.md",
        "mission-approved-doc/SKILL.md",
        "post-run-result-analysis/SKILL.md",
        "mission-csv-execute/SKILL.md"
      ],
      "phrasings": [
        "独立 review",
        "独立科学审查",
        "independent"
      ],
      "question": "独立性的判定标准（模型/会话/工具隔离）是否一致？"
    },
    {
      "id": "no-mock-as-real",
      "files": 6,
      "locations": [
        "mission-csv-execute/SKILL.md",
        "remote-run.md",
        "post-run-result-analysis/SKILL.md",
        "mission-approved-doc/SKILL.md",
        "AGENTS.md",
        "CLAUDE.md"
      ],
      "phrasings": [
        "不得用 mock、fixture、stub、dry-run、字符串检查、静态验证或脚手架证据包装成真实集成"
      ],
      "question": "哪些验证手段算'真实'，边界在哪？"
    },
    {
      "id": "stop-at-blocker",
      "files": 6,
      "locations": [
        "mission-csv-execute/SKILL.md",
        "remote-run.md",
        "post-run-result-analysis/SKILL.md",
        "mission-approved-doc/SKILL.md",
        "AGENTS.md",
        "CLAUDE.md"
      ],
      "phrasings": [
        "停在当前 row",
        "fail-closed",
        "失败即停"
      ],
      "question": "哪些失败算 blocker，哪些可继续？"
    },
    {
      "id": "single-prerun-row",
      "files": 0,
      "locations": [],
      "phrasings": [
        "唯一一个 scientific PRERUN row",
        "不创建 FIX/PRERUN 行"
      ],
      "question": "唯一性的边界（同一 packet/同一 commit/同一 mission）？"
    },
    {
      "id": "observed-model-gate",
      "files": 4,
      "locations": [
        "pre-run-implementation-review/SKILL.md",
        "csv-schema.md",
        "post-run-result-analysis/SKILL.md",
        "mission-approved-doc/SKILL.md"
      ],
      "phrasings": [
        "observed_model 为 unknown 拒绝",
        "记录型 identity 必须匹配"
      ],
      "question": "unknown 的边界与例外（归一化 verdict）是否一致？"
    },
    {
      "id": "no-relaunch",
      "files": 2,
      "locations": [
        "remote-run.md",
        "remote-run-snippet/SKILL.md"
      ],
      "phrasings": [
        "不重复 launch",
        "沿用同一 RunID"
      ],
      "question": "RunID 复用的边界（超时/失败/用户取消后）？"
    }
  ],
  "next_step": "批 4 L4 由 reviewer/advisor 逐条裁定 canonical 表述，其余文件改为指向并加防复述校验"
}
