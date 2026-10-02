---
name: post-run-result-analysis
description: Use after remote experiment artifacts are ingested to require an independent strong-model analysis before closing a Mission.
---

# Post-run Result Analysis

本 skill 只处理**运行完成并已 ingest 后的科研结果分析**，不启动、恢复或修改远程运行，也不替代 `pre-run-implementation-review` 或 closing review。

## 目标与硬门禁

- `remote_state=ingested` 的每个非空 `(ExpID, RunID)` 都必须出现在 `reviews/result-analysis.json`。
- 每个条目必须绑定最终的 `research_workspace/experiments/<ExpID>/analysis/analysis.md`、其 SHA-256、原始证据引用和科学结果状态。
- 正式分析必须由独立的强模型 reviewer 完成，**唯一支持的通道是 `result-analysis-reviewer-job`**：调用 `.agents/harness/reviewer_job.py --review-kind result-analysis --backend <pi|codex>`，模型取 `.agents/harness/config/review_contract.toml` 中对应宿主的当前值、thinking 级别见同一文件的 `[thinking].level`。
  获批模型值定义在 `.agents/harness/config/review_contract.toml`（项目自有，不参与模板同步）；运行时校验与按宿主推导在 `.agents/harness/review_model.py`。额度耗尽等原因需要切换时改前者（一次显式提交），reviewer_job 与验证器自动跟随，不允许会话内临时传参绕过。
- `advisor` 可以在存在冲突解释或研究方向选择时提供辅助意见，但不能生成正式分析、verdict 或关闭分析行。
- 强模型身份无法从 reviewer_job verdict 核验时，停止在分析行；不得回退到主 Executor、自审或把模型自报名称当作证据。证据仅接受机械可核验的形式：
  - `model_evidence_ref` 必须是 `job:reviews/result-analysis-<ExpID>/verdict.json#verdict`（CSV 相对路径），由验证器读取 verdict、核对其 schema、packet/task/raw-response SHA-256、规范化输出 SHA-256、observed model 与后端 host 已批准身份一致；
  - 不能写 `pending`、`unknown` 或占位字符串。
- 主 Executor 只负责整理事实、保存 reviewer 返回内容和更新状态；不能自行补写科学结论、替换 `scientific_outcome` 或把替代验证包装成结果。

## 输入边界

分析前先读取：

1. approved spec / outcome contract、当前 CSV 和 claim/evidence ledger；
2. 每个目标 RunID 的 RunSpec、manifest、record、summary 和原始 artifact；
3. 对应 baseline、fold、shot、ablation 和预注册 gate；
4. `issues/<stem>/<stem>.review.md` 中的客观运行摘要、异常和 validation gap。

不要把主 Executor 已写的结论、主观 handoff 或 advisor 回复作为事实输入。它们最多作为待核对材料；原始指标和可定位文件优先。

## 强模型调用

使用统一的入口脚本 `run_result_analysis.py`，从其所在 skill 目录运行（`.codex/skills/…` 是 canonical，`.agents/skills/…` 与 `.pi/skills/…` 是各宿主的同步副本）：

```bash
python3 .codex/skills/post-run-result-analysis/scripts/run_result_analysis.py \
  --csv issues/<stem>/<stem>.csv \
  --exp-id <ExpID> \
  --run-ids <RunID-1> <RunID-2> \
  --backend pi \
  --workdir .
```

- `--backend`：在 Pi 宿主用 `pi`（`pi --mode text --print --tools read,grep,find,ls --no-skills --no-context-files`），在 Codex 宿主用 `codex`（`codex exec --json --sandbox read-only`）。
- 脚本会：
  - 从 CSV 校验 `(ExpID, RunID)` 覆盖；
  - 构造 reviewer packet（`post-run.result-analysis.v1`），落到 `issues/<stem>/reviews/result-analysis-<ExpID>/packet.json`；
  - 生成独立 reviewer 任务 task.md（prompt 明确要求独立读证据、不信任主代理总结，输出严格 JSON）；
  - 调 `reviewer_job.py --review-kind result-analysis --backend <…>`，模型由 review_contract 决定——**主会话不能传 `--model` 影响 reviewer 模型**；
  - 写 `verdict.json`（schema `post-run.result-analysis-verdict.v1`）到同一目录。

reviewer_job 的 verdict 是唯一的可核验证据，包含：

- `backend` / `review_kind`（必须是 `result-analysis`）；
- `requested_model`（launcher 实际传给 backend 的调用名，含 Pi thinking 后缀）；
- `observed_model`（来自后端 session/event 流的可信身份）；
- `model_evidence`（由 reviewer_job 从 session metadata 或 event stream 提取，`session-metadata` 或 `event-stream`）；
- `packet_sha256` / `task_sha256` / `raw_response_sha256` / `review_output_sha256`；
- `exp_id` 与 `run_ids`，必须与 CSV 已 ingest todo 完全一致；
- `review_output`：reviewer 的规范化 JSON 字符串，逐字段为 `exp_id, run_ids, analysis_markdown, scientific_outcome, limitations, validation_gaps`（无代码围栏、无前后散文）。

quota、launcher 失败与静默都是 review-service failure，修好服务后用同一命令重跑——reviewer_job 的幂等逻辑会复用同一 `verdict.json`，不会产生第二次科学意见。

## 持久化

1. 将 reviewer 输出的 `analysis_markdown` 原样保存为：

   ```text
   research_workspace/experiments/<ExpID>/analysis/analysis.md
   ```

   `analysis.md` 必须逐字保存 JSON 的 `analysis_markdown`（仅允许换行规范化），不得补写或改写科学语义。

2. 将索引保存为：

   ```text
   issues/<stem>/reviews/result-analysis.json
   ```

   最小结构：

   ```json
   {
     "schema_version": "post-run.result-analysis.v1",
     "status": "complete",
     "analysis_agent_mode": "result-analysis-reviewer-job",
     "analysis_independence": true,
     "requested_model": "<job verdict 的 requested_model，含 thinking 后缀>",
     "observed_model": "<job verdict 的 observed_model>",
     "model_evidence": "job-verdict",
     "model_evidence_ref": "job:reviews/result-analysis-<ExpID>/verdict.json#verdict",
     "entries": [
       {
         "exp_id": "<ExpID>",
         "run_id": "<RunID>",
         "analysis_path": "research_workspace/experiments/<ExpID>/analysis/analysis.md",
         "analysis_sha256": "<sha256>",
         "scientific_outcome": "inconclusive",
         "review_evidence_ref": "job:reviews/result-analysis-<ExpID>/verdict.json#verdict",
         "review_output_sha256": "<sha256-of-normalized-final-reviewer-json>",
         "evidence_refs": ["remote_artifacts/<ExpID>/<RunID>/..."],
         "limitations": [],
         "validation_gaps": []
       }
     ]
   }
   ```

3. 对当前 ExpID 的 `research_workspace` 产物使用 `research-result-commit` 单独提交。分析 CSV 行使用 `git_repo:research_workspace`、`commit_hash:<nested repo commit>`、`refs` 指向 `analysis.md` 和索引。

## 机械验证

在关闭 CSV 前执行：

```bash
python3 .codex/skills/post-run-result-analysis/scripts/validate_result_analysis.py \
  --csv issues/<stem>/<stem>.csv \
  --index issues/<stem>/reviews/result-analysis.json \
  --workdir .
```

验证器必须 fail-closed 检查：

- canonical CSV 中所有已 ingest 的 `(ExpID, RunID)` 均被覆盖，且无重复或额外条目；
- index schema、状态、强模型身份、独立性和模型证据完整（reviewer_job verdict 是唯一可核验的证据形式）；
- 每个 entry 的 reviewer 证据可定位到真实评审输出：解析 `job:<path>#verdict` 并核对 verdict schema、`review_kind`、`status`、`backend`、`requested_model`（与索引层一致且属于对应宿主的已批准身份）、`observed_model`、`review_output_sha256` 一致；`analysis.md` 内容来自该输出；
- analysis 文件存在、在工作区内、四段顺序正确且 SHA-256 一致；
- evidence refs 可解析到工作区内的文件/目录；
- scientific outcome 属于固定枚举；
- `status=not_applicable` 只能在没有已 ingest 正式结果且有明确 reason 时使用。

验证通过后，才可把 `RESULT-ANALYSIS-01` 的四状态置为完成；随后才进入 `REVIEW-*`。closing review 必须消费该索引和 analysis 文件，但不能代替本阶段。
