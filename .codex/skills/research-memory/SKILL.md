---
name: research-memory
description: Record and recall research decisions, findings, hypotheses, and experiment analyses when a research conversation changes direction, analysis files change, or a new task needs prior conclusions. Use the local research-memory queue; this does not launch experiments or authorize new runs.
---

# Research Memory

使用项目中的 `.agents/harness/memory/research_memory.py`。本地脚本负责来源队列、
结论整理和恢复；配置启用 Hindsight 时，由脚本补充同步与远端召回。

在科研交流出现新决定或推断、实验分析更新，或新任务需要既有结论时，先核对本地上下文与相关待处理来源。
宿主钩子通常已经完成采集；使用队列中的事件 ID，避免把同一条消息再录一份。

根据来源整理结论与处理状态。已有明确授权继续执行；尚不确定的内容留在待确认状态，并写清缺少什么信息。
查当前选型、历史成绩和失败原因时按类型、范围与协议分别检索，沿来源下钻。

## 读取与召回

从项目根运行：

```bash
python .agents/harness/memory/research_memory.py context --query "<当前问题>"
python .agents/harness/memory/research_memory.py pending
python .agents/harness/memory/research_memory.py show <event_id>
```

`context` 会扫描已登记的文件并返回当前决定、状态和相关来源。
`pending --offset 8 --limit 8` 可翻页，`--history` 可查看历史版本。
预览被截断时，按 `show` 返回的来源或 `full_text_ref` 读取需要的内容。

按问题选择召回范围：

```bash
python .agents/harness/memory/research_memory.py recall "<问题>" --kind decision --status ACTIVE --scope model.architecture
python .agents/harness/memory/research_memory.py recall "<问题>" --kind finding --protocol "<协议>"
python .agents/harness/memory/research_memory.py recall "<历史问题>" --history
```

正式记录在 `research_workspace/CONCLUSIONS.md`，进度和当前模型指针在 `STATE.md`，
实验依据在 `experiments/<ExpID>/record.json` 与 `analysis/analysis.md`。
使用条目时核对类型、范围、协议、状态及来源。

## 处理来源

通过 stdin 向 `process` 提交 JSON，一次处理一个事件：

```json
{
  "event_id": "<队列中的事件 ID>",
  "disposition": "recorded",
  "records": [
    {
      "kind": "decision",
      "status": "ACTIVE",
      "scope": "model.architecture",
      "summary": "<用户明确确认的决定>",
      "state_slot": "architecture",
      "supersedes": ["<同范围的旧 C 编号>"]
    }
  ]
}
```

命令为 `python .agents/harness/memory/research_memory.py process`。
无旧条目时省略 `supersedes`；一条来源有多个结论时使用多个 records。
已记入现有分析的内容可用 `references` 关联文件，省略 records。

| disposition | 内容 |
|---|---|
| recorded | 提供 records 或现有记录的 references |
| waiting | 提供 reason，写清待确认的问题 |
| discarded | 提供 reason，说明无需形成科研记录的原因 |

| kind | status |
|---|---|
| decision | ACTIVE、PROPOSED、SUPERSEDED |
| finding | OPEN、SUPPORTED、MIXED、REJECTED、SUPERSEDED |
| hypothesis | OPEN、REJECTED、SUPERSEDED |
| execution | OBSERVED、RETRACTED、SUPERSEDED |

ACTIVE decision 关联明确的用户来源；agent 的建议使用 PROPOSED 或 OPEN hypothesis。
SUPPORTED、MIXED finding 提供 `evidence` 文件引用。scope 与 summary 写单段文本，
需要指定生效时间时使用带时区的 `effective_at`。

`evidence` 与 `references` 可引用 research_workspace、remote_artifacts、issues 或
docs/reviews 中已有的 JSON、CSV、图像等科研文件。引用只检查路径与存在性，不读取
或自动采集文件内容；自动登记来源仍限 Markdown 和 record.json。

`supersedes` 指向相同类型、范围和协议的旧条目。脚本保留旧记录并写入取代关系，
同范围的新生效决定应明确关联已有决定。缺少 Type、Scope 的旧条目先按原始来源补齐。

`state_slot` 控制 STATE 的 Current Model 指针：

- architecture：ACTIVE decision；
- verified_result：SUPPORTED finding，并提供 protocol；
- baseline：ACTIVE decision、SUPPORTED finding 或 OBSERVED execution。

程序为新结论分配 C 编号，写入来源信息并更新相应指针。实验指标与生成的 record.json
继续由实验记录入口维护。

## 文件与恢复

默认采集 STATE、CONCLUSIONS、实验主分析、record.json、research_workspace/analysis/*.md，
以及主分析引用的 Markdown 诊断附件。补充来源时使用：

```bash
python .agents/harness/memory/research_memory.py watch research_workspace/reviews/<文件>.md
python .agents/harness/memory/research_memory.py scan
```

`status` 查看处理数量与未完成事务，`recover` 恢复中断的整理。
发生文档冲突时，按事件、当前文件和本地事务记录核对后继续处理。

控制记录位于独立科研仓库的 Git 元数据中，或 `.agents/harness/.memory/`。
可选配置位于 `.agents/harness/config/research-memory.json`。
需要暂停当前项目的自动采集时，在此配置设置 `"hooks_enabled": false`（默认 `true`）。
已被宿主加载的回调也会立即返回空结果，不采集、扫描或恢复事务；手工 CLI 仍可使用。
Hindsight 默认关闭；启用后 `recall` 返回远端候选，`sync` 推进本地待同步队列。
候选内容仍按本地正式条目的状态、协议和证据范围使用。
