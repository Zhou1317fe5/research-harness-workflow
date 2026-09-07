---
name: research-memory
description: Record and recall research decisions, findings, hypotheses, and experiment analyses when a research conversation changes direction, analysis files change, or a new task needs prior conclusions. Use the local research-memory queue; this does not launch experiments or authorize new runs.
---

# Research Memory

使用 [科研记录与召回规则](../../../.agents/harness/docs/research-memory.md)。

在科研交流出现新决定或推断、实验分析更新，或新任务需要既有结论时，先核对本地上下文与相关待处理来源。
宿主钩子通常已经完成采集；使用队列中的事件 ID，避免把同一条消息再录一份。

根据来源整理结论与处理状态。已有明确授权继续执行；尚不确定的内容留在待确认状态，并写清缺少什么信息。
查当前选型、历史成绩和失败原因时按类型、范围与协议分别检索，沿来源下钻。

程序接口、文件格式和恢复方式均以该规则文件与 `.agents/harness/` 的实现为准。
