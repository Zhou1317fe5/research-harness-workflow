# 会话回放：2026-09-20 hera-ase-performance

- 会话文件：`/home/zhou/.pi/agent/sessions/--home-zhou-cdfss-dinov3--/2026-09-20T06-25-40-849Z_01a0bd7d-e9f0-7528-89aa-fdc5a9c4e4a6.jsonl`（~4.2MB）
- 时长：2026-09-20 06:25 UTC → 2026-09-21 00:39+（跨日 ~18 小时+）
- Mission CSV：`issues/2026-09-20_14-11-14-hera-ase-performance/2026-09-20_14-11-14-hera-ase-performance.csv`
- 回放者：主 Executor（scout 子代理 aborted 的补跑）

## 事件时间线（前 25 条 user 事件，重点归纳）

| 时间 | 事件 | 层级 | 状态 | 说明 |
|---|---|---|---|---|
| 06:45 / 06:48 | **smoke-on-dg-s1 同 event (`50762038…`) 3 分钟内 2 次 attention** | L2 | **fixed-in-current-code** | 本次样本最早的重复推送证据 |
| 06:51 | smoke-on-dg-s1 terminal | L2 | — | 正常 |
| 06:54 / 06:56 | smoke-off-dg-s1 同 event (`e0ae9802…`) 2 分钟内 2 次 attention | L2 | **fixed-in-current-code** | 同模式 |
| 07:09 → 08:05 | ase-code-dg-s1 attention → terminal（约 56 分钟观察期） | L2 | — | 正常观察窗口 |
| 09:21 / 09:21 | xd-probe-off-dg-s1 同 event **60 秒内 2 次 attention** | L2 | **fixed-in-current-code** | 同模式 |
| 09:25 / 09:26 | xd-probe-k1-dg-s1 attention→terminal 1 分钟 | L2 | — | 正常 |
| 09:30 / 09:30 | xd-smoke-k5-dg-s5 同 event **60 秒内 2 次 attention** | L2 | **fixed-in-current-code** | 同模式 |
| 09:44 / 09:45 / 09:45 / 09:45 / 09:49 | **xd-fss-s5 同 event (`600ff788…`) 5 分钟内 4 次 attention** | L2 | **fixed-in-current-code** | 本样本最严重单点，频率 1/分钟级 |
| 09:52 | xd-fss-s1 attention | L2 | — | 新 RunID，正常 |
| 14:06 / 14:09 / 18:01 | xd-fss-s1 terminal；xd-dg-s5 attention；xd-fss-s5 terminal（长间隔） | L2 | — | 长时段正常 |
| 09-21 00:39 | xd-dg-s5-r2 attention（前缀 -r2，in-row 修复的新 RunID） | L2 | — | in-row 修复惯例 |

## 分类汇总

- **L2 rrctl event 重复推送**：**6 组独立证据**，包括一次"5 分钟 4 次"的密集推送——**是全部 6 个回放样本中重复推送频率最高的一个会话**。
- 其它：全部是正常 attention/terminal 事件链；未发现 L0/L1/L3/L4 特有事件。

## 模式判断

重复推送模式 **不是 9-27 主会话前后才出现**——在本样本（9-20，比 9-27 早一周）已存在，且频率更高（6 组）。这印证了该缺陷是 `.pi/extensions/rrctl-events.ts` + `agent_event_wait.py` relay 层的**长期存在 bug**，9-27 的 8 次连续推送只是最易被注意的一次集中爆发；本样本说明该 bug 在 hera 早期 mission 已给用户造成持续打扰。批 1 的 `4e21a45` 修复 closing 这一类问题。

## 新增发现

- 证据并入 F-009（Pi 侧 rrctl event 重复推送）；本样本作为"问题存在已久、频率高"的权重证言。
