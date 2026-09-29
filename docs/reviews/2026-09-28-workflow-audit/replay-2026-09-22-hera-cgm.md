# 会话回放：2026-09-22 hera-cgm

- 会话文件：`/home/zhou/.pi/agent/sessions/--home-zhou-cdfss-dinov3--/2026-09-22T01-22-52-225Z_01a0c6b5-66c0-7017-8713-afba84b06a51.jsonl`（1472 行）
- 时长：2026-09-22 01:34 → 16:49+ UTC（约 15 小时）
- Mission CSV：`issues/2026-09-21_21-53-04-hera-cgm/2026-09-21_21-53-04-hera-cgm.csv`
- 模型：`cline-pass/deepseek-v4.1-flash`
- 回放者：scout 子代理（批 2），主代理整理落盘

## 事件时间线

| 时间 (UTC) | 事件 | 层级 | 状态 | 说明 |
|---|---|---|---|---|
| 01:34:16 | mission 启动 | — | — | `mission issues/...hera-cgm.csv` |
| 02:09:11 | rrctl attention（probe-off-ref-fss-s5） | L2 | **fixed** | 一次性 attention |
| 02:11:22 / 02:15:48 | **同一 RunID 同一 event (`e1e7d345…`) 4 分 26 秒内推 2 次 attention** | L2 | **fixed-in-current-code** | 与已修复的 relay 去重同模式 |
| 02:19:48 / 02:20:18 / 02:21:26 / 02:22:19 | **同一 RunID (`-r2`) 同 event 3 分钟 4 次推送直至 terminal** | L2 | **fixed-in-current-code** | 重复推送最严重的单次证据 |
| 02:30:49 / 02:32:09 | `-r3` attention → terminal | L2 | — | 正常（同 RunID 一次性） |
| 02:38:09 / 02:38:31 | **`control-fss-s5` 同 event 22 秒内 2 次 attention** | L2 | **fixed-in-current-code** | 同模式第三次出现 |
| 02:38 → 11:01 | （中段无人工消息，agent 长跑） | — | — | OQ：中间是否有 PROMPT 循环/reviewer fetch 未切片精读 |
| 11:01:38 | **人工指令**：先用 post-run-result-analysis 总结、写 review.md、待 gpt6 预分析 | L4 | **open（设计偏离）** | 在独立 reviewer 触发前要求先产出可读摘要——人机协议层"独立 reviewer 触发时机"问题 |
| 14:52:09 / 16:49:12 | 人工两次询问闭环状态 | L4 | — | 人工巡检（正常） |
| ≈16:42 | subagent 调用 `scientific-reviewer` | L4 | — | post-run-result-analysis 独立分析门禁，与"先 review.md"指令形成时序冲突 |

## 分类汇总

- L2：3 组同 event 重复推送（均"fixed-in-current-code"）
- L4：1 个人机协议调整（open；关联 rule-canonical.md 的 `independent-review` 行）
- L0/L1/L3：未发现

## 与现有修复的对照

E1/E2/E3 的"同一 RunID 同一 event 多次推送"正是**已在 commit `4e21a45`/`agent_event_wait.py attention_pending` 修复**的 relay 去重问题（Codex 侧 relay 在 attention 已投递未处理时不另起 relay）。该会话发生于修复之前（9-22），属"已修问题的历史证据"；但需要注意：**当时修复只覆盖 Codex 侧 relay**，Pi 侧 `rrctl-events.ts` 扩展的推送去抖是否同因待 L1/L2 单测确认（见新增发现 F-009）。

## 新增发现（写入 findings.jsonl）

见 findings.jsonl F-009、F-010。
