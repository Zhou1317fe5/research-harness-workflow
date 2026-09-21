---
name: post-run-result-analysis
description: Use after remote experiment artifacts are ingested to require an independent strong-model analysis before closing a Mission.
---

# Post-run Result Analysis

本 skill 只处理**运行完成并已 ingest 后的科研结果分析**，不启动、恢复或修改远程运行，也不替代 `pre-run-implementation-review` 或 closing review。

## 目标与硬门禁

- `remote_state=ingested` 的每个非空 `(ExpID, RunID)` 都必须出现在 `reviews/result-analysis.json`。
- 每个条目必须绑定最终的 `research_workspace/experiments/<ExpID>/analysis/analysis.md`、其 SHA-256、原始证据引用和科学结果状态。
- 正式分析必须由项目 `scientific-reviewer` sub-agent 完成；当前注册模型为 `openai-codex/gpt-5.6-sol`、thinking `high`。
- `advisor` 可以在存在冲突解释或研究方向选择时提供辅助意见，但不能生成正式分析、verdict 或关闭分析行。
- 强模型身份无法从 host/session metadata 核验时，停止在分析行；不得回退到主 Executor、自审或把模型自报名称当作证据。当前 Pi 门禁只接受 parent session 记录：`model_evidence_ref` 必须绑定真实 session UUID 和 `subagent` tool-call ID（`session:<uuid>#tool:<tool-call-id>`），并由验证器核对 `scientific-reviewer`、实际模型、成功终态和输出 hash；不能写 `pending`、`unknown` 或占位字符串。
- 主 Executor 只负责整理事实、保存 sub-agent 返回内容和更新状态；不能自行补写科学结论、替换 `scientific_outcome` 或把替代验证包装成结果。

## 输入边界

分析前先读取：

1. approved spec / outcome contract、当前 CSV 和 claim/evidence ledger；
2. 每个目标 RunID 的 RunSpec、manifest、record、summary 和原始 artifact；
3. 对应 baseline、fold、shot、ablation 和预注册 gate；
4. `issues/<stem>/<stem>.review.md` 中的客观运行摘要、异常和 validation gap。

不要把主 Executor 已写的结论、主观 handoff 或 advisor 回复作为事实输入。它们最多作为待核对材料；原始指标和可定位文件优先。

## 强模型调用

为每个 ExpID 启动一次全新的 `scientific-reviewer` sub-agent；调用必须显式携带当前仓库 `cwd`，例如 `subagent(agent="scientific-reviewer", agentScope="project", cwd="<repo>", task="...")`，以便 parent session 证据可配对核验：

- 只读、不得调用 advisor、不得再次委派；
- 请求模型 `openai-codex/gpt-5.6-sol`、thinking `high`；
- prompt 明确要求独立读取证据，不信任主代理总结；
- 要求严格输出以下四段，标题和顺序不能改变：

```markdown
## Change
...

## Result
...

## Finding
...

## Next
...
```

同时输出机器可记录的 `scientific_outcome`：
`hypothesis_supported`、`hypothesis_not_supported`、`gate_failed`、`inconclusive` 或 `not_applicable`。

## 持久化

1. 将 sub-agent 的四段正文原样保存为：

   ```text
   research_workspace/experiments/<ExpID>/analysis/analysis.md
   ```

   只允许补充文件头、证据路径或 Markdown 空白，不得改变科学语义。

2. 将索引保存为：

   ```text
   issues/<stem>/reviews/result-analysis.json
   ```

   最小结构：

   ```json
   {
     "schema_version": "post-run.result-analysis.v1",
     "status": "complete",
     "analysis_agent_mode": "scientific-reviewer-subagent",
     "analysis_independence": true,
     "requested_model": "openai-codex/gpt-5.6-sol",
     "observed_model": "openai-codex/gpt-5.6-sol",
     "model_evidence": "session-metadata",
     "model_evidence_ref": "session:<actual-session-uuid>#tool:<subagent-tool-call-id>",
     "entries": [
       {
         "exp_id": "<ExpID>",
         "run_id": "<RunID>",
         "analysis_path": "research_workspace/experiments/<ExpID>/analysis/analysis.md",
         "analysis_sha256": "<sha256>",
         "scientific_outcome": "inconclusive",
         "review_evidence_ref": "session:<actual-session-uuid>#tool:<subagent-tool-call-id>",
         "review_output_sha256": "<sha256-of-normalized-final-reviewer-output>",
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
- index schema、状态、强模型身份、独立性和 parent-session 模型证据完整；
- 每个 entry 的 reviewer session/tool-call 可定位到真实 `scientific-reviewer` 结果，实际模型精确匹配、退出码为 0、输出 SHA-256 一致，且 `analysis.md` 内容来自该输出；
- analysis 文件存在、在工作区内、四段顺序正确且 SHA-256 一致；
- evidence refs 可解析到工作区内的文件/目录；
- scientific outcome 属于固定枚举；
- `status=not_applicable` 只能在没有已 ingest 正式结果且有明确 reason 时使用。

验证通过后，才可把 `RESULT-ANALYSIS-01` 的四状态置为完成；随后才进入 `REVIEW-*`。closing review 必须消费该索引和 analysis 文件，但不能代替本阶段。
