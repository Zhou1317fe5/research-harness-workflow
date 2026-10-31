# Closing Review 与收尾编排规范

`REVIEW-*` 行用于判断整批工作是否按批准文档完成，并把实施结果与科学结果分开记录。同一规则适用于目录化 CSV 与 legacy 平铺 CSV。

## 1. 核心定位与职责分工

- **Closing Review 的核心职责**：回答「当前 Mission 是否已经满足证据、执行状态与交付要求，可以合法结束？」。
- **职责隔离**：
  - `RESULT-ANALYSIS-01` 回答「实验结果在科学机理上说明了什么」（由独立强模型在长训练入账后独占完成）；
  - `Closing Review` 回答「工程实施与台账是否可以合法收工交付」；
  - 收尾阶段不实现新功能、不重新设计方案、不开启第三轮无界实验优化。

## 2. Review 前置条件与机械门禁

在执行 REVIEW 行之前，必须通过以下前置检查：

1. **机械就绪核验（HARD GATE）**：
   运行 `final_ready.py` v2 对 28 列状态、remote terminal state、实验身份、metrics token、provenance、claim 终态、post-run result-analysis 和 scoped git status 做机械检查。它读取一个 `mission.final-ready.v2` 的 JSON request（字段：`csv_path`、`review_row_id`、`review_path`、`handoff_path`、`claims_path`、`expected_run_ids`、`required_review_tokens`、`provenance_paths`、`protected_paths_clean`、`closing_context`），经文件或 `-` 传入：
   ```bash
   python .codex/skills/mission-csv-execute/scripts/final_ready.py - <<'JSON'
   {"schema_version": "mission.final-ready.v2",
    "csv_path": "issues/<stem>/<stem>.csv", "review_row_id": "REVIEW-01",
    "review_path": "issues/<stem>/reviews/review.json",
    "handoff_path": "issues/<stem>/reviews/handoff.md",
    "claims_path": "issues/<stem>/claims.json",
    "expected_run_ids": ["<RUN-ID>"], "required_review_tokens": ["<token>"],
    "provenance_paths": ["issues/<stem>/runs/<RUN-ID>/run_spec.json"],
    "protected_paths_clean": true, "closing_context": {}}
   JSON
   ```
   失败时修当前 REVIEW 行，不创建等待型 REVIEW。
2. **所有前置非 review 行必须闭环**：
   - 对 canonical CSV，所有正式实验行闭环后，优先通过 `evidence-close` 结构化直接入账结项；涉及跨域重要结论争议时，方需运行 `RESULT-ANALYSIS-01`。
3. **前置未完成行与遗留台账清算**：
   - 若前序存在卡住行、更高轮次取代的重试行或预注册门控跳过行阻碍收工，**必须读取并执行 [references/closing-legacy-cleanup.md](closing-legacy-cleanup.md)** 进行清算，严禁死锁。
4. **一次聚合审计（禁止一错一停）**：
   收尾开始与每次修复后，统一运行一次聚合审计，一次性拿到全部门禁错误再批量修复：
   ```bash
   python .codex/skills/mission-csv-execute/scripts/closing_audit.py \
     --csv issues/<stem>/<stem>.csv --workdir . \
     --handoff issues/<stem>/reviews/handoff.md --claims issues/<stem>/<stem>.claims.json
   ```
   它一次覆盖行终态/Git 绑定/ingest/claim/result-analysis、deferred ledger 与 handoff 契约；
   输出按组归类，有错退出码 1。**严禁每发现一个错误就单独重跑一次 `final_ready.py`/`mission_completion.py`**。
5. **台账完备性**：
   - 若输入 CSV 缺少 review 行，运行 `ensure_review_row.py` 合成 `REVIEW-01`；
   - 若 notes 包含 `claims:CLAIM-*` 但缺少可读 `claim_ledger:<path>`，先补齐账本，不得裸跑。

## 3. Closing 路由决策 (Closing Route Decision)

`final_ready.py` 是路由选择的机器权威，按其返回的 `decision` 分流：

1. **`decision:evidence_close`（直接结构化关闭）**：
   - 适用场景：L0–L2 普通任务已完成风险匹配测试；或科研任务的同一 `pre_run_code_commit` 已完成独立 scientific PRERUN 且之后未改变数据流、算子、指标或结果归属。
   - 执行方式：由主会话基于结构化证据直接完成，**不再次调用模型**。
   - 记录值：`review_agent_mode:evidence-close`、`review_independence:false`、三个模型字段记为 `not_applicable`。
2. **`decision:independent_review_required`（独立审查阶梯）**：
   - 适用场景：L3/L4 交付无等价独立审查、机械证据冲突、claim/evidence 疑似高估或发现 current-scope gap。
   - 执行方式：**读取并严格遵循 [references/closing-independent-review.md](closing-independent-review.md)**（执行审查模型铁律：必须使用当前会话执行模型，严禁私自越级借用高级审查模型；优先 `closing-reviewer-job`，失败降级 `self-review`）。
   - **reviewer 不可用 -> 有界降级终态**：若 `closing-reviewer-job` 的 `job.json` 为 `capability_gap`，或 stderr 明确为供应商 4xx/429（如 `unknown field "role"`、额度耗尽），在 `closing_context` 置 `reviewer_unavailable: true`。此时 `final_ready.py` 直接返回 `evidence_close`（reason 含 `reviewer_unavailable_self_review`），由主会话按 self-review 完成一次收尾，**不得重复拉起 reviewer，也不得因 gaps 未当场闭合而反复重跑 final_ready**。gaps 一律如实登记为 deferred follow-up。

## 3.1 收尾执行护栏（Closing Loop Guard，强制）

收尾阶段严禁陷入“核验 -> 再核验 -> 请求完成裁决”的空转循环：

- **每类机械检查最多执行一次**：`final_ready.py`、`check_handoff_contract.py`、`validate_claim_ledger.py`、`csv_completion_errors()`、`preflight.py` 在收尾期内各自只跑一次；只有在**修改了对应输入文件之后**才允许重跑该检查，禁止无改动重跑。
- **reviewer 只尝试一次**：`closing-reviewer-job` 失败后不得重试第二次；按上条降级 self-review。
- **advisor 不用于重复确认**：advisor 不得用于“再问一次是否已可收尾”；同一收尾议题至多调用一次。
- **合法终态包含“有 deferred 缺口”**：实验与入账完成、收尾证据存在 deferred follow-up 时，按实记录并结束，**严禁**为凑“全部闭环”伪造 claim/gap 或新增补救实验。
- 判断完成标准：收尾期内每个检查脚本的调用次数不超过“改动次数 + 1”，且最终只输出一次收尾汇报。

## 4. Human Handoff 施工交工单与机械验收

REVIEW 行执行后，必须产出面向人类决策者的施工交工单 `<csv-path-without-.csv>.handoff.md`：
1. **结构与规范**：**读取并遵循 [references/closing-handoff.md](closing-handoff.md)**（严格遵循大白话叙事九层结构、Outcome 表格逐字段对齐、架构变更 Mermaid 配图标准）；
2. **机械合同验收门禁（HARD GATE）**：
   落盘后必须运行机器验收检查：
   ```bash
   python .codex/skills/mission-csv-execute/scripts/check_handoff_contract.py <handoff-path> --csv <csv-path>
   ```
   退出码 0 时方可在 REVIEW 行 notes 记录 `handoff_contract:passed`。未通过严禁标记 `vision_met`。

## 5. Review 行闭环与最终交付

`REVIEW-N` 只有在 review log 已写入、handoff.md 已生成且 handoff contract 检查通过后方可完成：

- **若 `vision_met`**：
  - 必须同时满足 `handoff_contract:passed`；
  - 标记 `REVIEW-N` 完成；若无其他未完成行，运行 `csv_completion_errors()` 核验后结束 CSV；
  - **终端对话层最终汇报（严格禁问令）**：
    终端对话层汇报只包含客观三要素（完成状态、关键科学指标对比、交接文件路径）。**严禁在终端对话层输出任何疑问句、决策请示或向用户发问**；所有实现疑虑、架构讨论、后续实验建议一律沉淀至 `handoff.md` 与 `findings.jsonl`，交由后续方案讨论会话读取：
    ```text
    交接文件路径：
    - 方案讨论交接层：issues/<stem>/<stem>.review.md
    - 人类交付交工单：issues/<stem>/<stem>.handoff.md
    ```
- **若 `gaps_found`**：
  - 将 current-scope gaps 追加为 follow-up issues 并追加 `REVIEW-(N+1)`；标记当前 REVIEW 完成并继续执行。
- **若科研结果为负或无信号**：
  - `gate_failed`（预注册门控跳过）或 `inconclusive`（探索期两轮止损无信号）属于**合规正常科研终态**，直接在 `scientific_outcome` 记录并正常闭环收工，严禁陷入无界优化循环。
- **若存在 Human-required blocker**：
  - 参照 [references/closing-legacy-cleanup.md](closing-legacy-cleanup.md) 记录最小诉求，保持 `git_state=未提交`，停止并请求用户输入。
