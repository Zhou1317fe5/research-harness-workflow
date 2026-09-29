# 会话回放：2026-09-24 hera-scope-diag

- 会话文件：`/home/zhou/.pi/agent/sessions/--home-zhou-cdfss-dinov3--/2026-09-24T06-14-47-203Z_01a0d20d-60a3-71ba-a465-275005498dec.jsonl`
- 时长：2026-09-24 06:14 → 2026-09-25 03:06 UTC（约 21 小时）
- Mission CSV：`issues/2026-09-24_13-52-00-hera-scope-diag/2026-09-24_13-52-00-hera-scope-diag.csv`
- 回放者：主 Executor（scout 子代理的空输出/失败样本的补跑）

## 事件时间线

| 时间 (UTC) | 事件 | 层级 | 状态 | 说明 |
|---|---|---|---|---|
| 08:04 / 08:10 | **同一 RunID (`scope-diag-smoke`) 同一 event (`9b5ea986…`) 6 分钟内 2 次 attention** | L2 | **fixed-in-current-code** | rrctl event 重复推送的最早证据之一 |
| 08:16 | smoke-r2 attention | L2 | — | 新 RunID，正常 |
| 08:33 / 08:34 | **smoke-r3 同 event (`7cb829dc…`) 1 分钟内 2 次 attention** | L2 | **fixed-in-current-code** | 同模式 |
| 08:36 / 08:40 | **smoke-r4 同 event (`2cd1a843…`) 4 分钟内 2 次 attention** | L2 | **fixed-in-current-code** | 同模式 |
| 08:50 → 09:52 | smoke-r5/r6/r7/r8 attention→terminal 链 | L2 | — | 多个独立 RunID 正常 |
| 09-25 01:30 / 01:31 / 01:41 / 02:27 | smoke-r9 同 event (`e7d5e0d2…`) **3 次推送**：1:30、1:31（重复 attention）、**1:41 人工回复"smoke已完成"后**、2:27 terminal | L2+L4 | **fixed-in-current-code**；**人工干扰 open** | 重复推送后**用户被迫手动介入**声明"smoke已完成"，随后才 terminal——重复推送对用户造成明显打扰，且人工回插可能破坏 agent 的 RunID 状态机 |
| 02:27 | smoke-r10 terminal | L2 | — | 正常 |
| 03:00 / 03:01 / 03:06 | replay-s1 terminal；**replay-s5 同 event (`880ebe49…`) 5 分钟内 2 次 attention** | L2 | **fixed-in-current-code** | 同模式最后一次出现 |

## 分类汇总

- **L2（rrctl event 重复推送）**：5 组独立证据（smoke、smoke-r3、smoke-r4、smoke-r9、replay-s5），每次同 event 推 2-3 次；**本批样本中重复推送频率最高的会话**。
- **L4（人机协议）**：1 处——smoke-r9 的 3 次推送迫使用户人工插入"smoke已完成"，人工打断 agent 的 RunID 状态机。
- L0/L1/L3：未发现。

## 关键模式

**重复推送在 hera 系列 smoke/replay 上是常态而非偶发**：本样本仅 21 小时就命中 5 组，比 9-22 cgm（15 小时 3 组）更密。该现象在 9-27 gsr-scnp 主会话（8 次连续同 event attention 重复）的严重版本中已被识别，并于 `4e21a45`（Codex 侧）+ `agent_event_wait.py` attention_pending**修复 relay 层**；本回放的证据说明 Pi 侧扩展 `.pi/extensions/rrctl-events.ts` 的去抖与 relay 的去重共同覆盖了该模式，**后续 batch 3 L1 需要为 Pi 侧扩展补"同一 event digest 已 settle 后不再重复唤醒"的单元测试**。

## 新增发现

- 证据并入 F-009（Pi 侧 rrctl event 重复推送）；F-010 增补一例"人工插入打断 agent 状态机"作为人机协议待裁定项。
