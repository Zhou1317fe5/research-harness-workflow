# 科研记录与召回

首次接入先读 [用户配置说明](configuration.md)。本文中的命令从项目根目录运行。

会话钩子保存来源，agent 根据来源整理正式记录。当前决定、实证发现、推断和执行事实分别记账。
本地文件与队列独立工作；Hindsight 是可选的检索副本。

## Skill 与 Hindsight 的分工

`research-memory` skill 指导 agent 读取本地来源、处理待确认项、维护结论及其适用范围。
`.agents/skills` 是指向 `.codex/skills` 的发现入口；Claude 使用同步的 `.claude/skills`。
执行所需步骤已完整写在对应的 SKILL.md 中。

Hindsight 提供远端存取和语义召回。启用后，本地队列负责同步与重试；
决定类型、生效状态和实验依据仍由 agent 按来源整理。

本文件面向用户介绍配置与使用方式，阅读和配置完成后可按需保留。

## 入口与存储

```bash
python .agents/harness/memory/install_memory_hooks.py
python .agents/harness/memory/research_memory.py context
python .agents/harness/memory/research_memory.py pending
python .agents/harness/memory/research_memory.py status
```

安装器只合并当前项目的 `.codex/hooks.json` 和 `.claude/settings.local.json`，
可以用 `--host codex|claude` 选择宿主。重复安装不增加绑定，`--remove` 只移除本工具的回调。
在其他目录启动宿主时，命令仍指向安装时的项目路径；项目移动后重新安装。

Codex 要求新建或修改的钩子通过 `/hooks` 信任检查，项目的配置层也须受信任。
安装器不修改宿主的信任记录。两种宿主的具体入口见
[Codex hooks](https://developers.openai.com/codex/hooks) 和
[Claude Code hooks](https://code.claude.com/docs/en/hooks)。

| 宿主事件 | 动作 |
|---|---|
| SessionStart（包括恢复和 compact） | 恢复未完成整理，扫描来源，加载当前决定与相关队列 |
| UserPromptSubmit | 保存本条用户消息，再提供本地上下文 |
| PostToolUse | 检查登记文件；有更新时提供来源摘要，不采集工具输出 |
| PreCompact | 将已发生的文件变化落盘 |
| Stop | 保存宿主提供的最终回复，安排可选同步 |

控制目录优先使用独立科研仓库的 Git common dir 下的 `research-memory/`。
没有独立科研仓库时使用 `.agents/harness/.memory/`；已有队列的位置保持不变。
其中的 `events/` 保存来源快照，`index.json` 保存处理状态、整理事务和同步状态，
`outbox/` 保存待同步版本。它们不进入 Git，也不作为整库上下文加载。

正式记录仍在 `research_workspace/CONCLUSIONS.md`、`STATE.md` 和单实验分析中。
程序不会手工补造 `record.json`、实验指标或新的 ExpID。

## 整理来源

收到新的科研决定、分析更新或相关待处理提示后，先用 `show <事件 ID>` 查看来源及当前处理状态。
处理已经读过、与本轮任务有关的来源。不要凭预览中的一句话为整份长文下结论。
启动摘要显示总待处理数和当前版本数；历史未处理版本用 `pending --history` 查看，
较多条目用 `--offset 8 --limit 8` 翻页。

| 类型 | 状态与使用边界 |
|---|---|
| decision | ACTIVE 必须来自用户的明确决定；建议或候选用 PROPOSED |
| finding | SUPPORTED、MIXED 必须附证据；OPEN、REJECTED 保留不确定项与失败教训 |
| hypothesis | OPEN 表示待验证；不能仅凭 agent 的解释升为已验证发现 |
| execution | OBSERVED、RETRACTED 描述执行事实，不代替科学判断 |

每个来源的处理结果为 `recorded`、`waiting` 或 `discarded`。
`waiting` 和 `discarded` 必须有原因；已经体现在现有分析中的来源可以用 `references` 关联该文件。
不确定用户是否作出决定时先记 `waiting`，写清缺少的确认。已经明确的决定不重复索取授权。
这项整理工作沿用当前任务边界，不自动启动新的实验。

使用以下请求结构，把事件 ID、结论与证据替换成已核对的内容：

```json
{
  "event_id": "E000000000000000000000000",
  "disposition": "recorded",
  "records": [
    {
      "kind": "decision",
      "status": "ACTIVE",
      "scope": "model.architecture",
      "summary": "当前采用已由用户确认的模型。",
      "state_slot": "architecture",
      "supersedes": ["C01"]
    }
  ]
}
```

通过 stdin 交给 `python .agents/harness/memory/research_memory.py process`。
没有旧条目时省略 `supersedes`。若已有同范围的生效决定，必须明确取代谁。
程序沿用 C 编号的位数；旧条目保留，标记 SUPERSEDED 并链接新条目。
旧格式的加粗 Status 可以读取；缺少 Type 或 Scope 的旧条目需先按原始来源补齐，不能猜其身份。

`scope` 说明对象、任务和适用边界。`effective_at` 可提供带时区的 ISO 时间；
省略时使用来源发生时间。未来才生效的决定先保留为待处理或待确认来源，不提前切换当前选型。
来源发生时间、接收时间、Git commit 和决定生效时间分别保留。
实验分析从相邻 `record.json` 引用 SpecID、ExpID、Branch、Commit、RunID 和协议；缺失值保留待补状态。

`state_slot` 有三个位置：`architecture`、`verified_result`、`baseline`。
前两者分别要求 ACTIVE decision 和 SUPPORTED finding。
写入已验证结果还必须明确 `protocol`；不同协议分别保留，不互相取代。
STATE 只更新 Current Model 区域，引用对应 C 条目，其他进度与约束保留。

来源先以原子文件落盘，再更新索引。整理时先保存包含预期文件哈希的写入计划，再更新正式文件。
中断后的下一次读取会自动补完；也可以运行 `recover` 查看恢复结果。
如果中断期间有人编辑了待写文件，事务保留在 `status.interrupted`，程序不覆盖这些改动。
核对事件、当前文件与控制目录中的事务后，恢复原预期版本再重试；不要删除队列来掩盖冲突。

## 文件更新与查询

默认检查：

- STATE.md、CONCLUSIONS.md；
- `experiments/*/analysis/analysis.md` 和 `experiments/*/record.json`；
- `research_workspace/analysis/*.md`；
- 主分析引用的科研工作区内 Markdown 诊断附件。

另一个跨实验分析或审查需要纳入时，明确登记：

```bash
python .agents/harness/memory/research_memory.py watch research_workspace/reviews/method-review.md
python .agents/harness/memory/research_memory.py watch docs/reviews/method-review.md
```

文件检查发生在宿主生命周期回调和显式 `scan` 时。外部程序更新的文件会在下一次检查时进入队列，
不依赖常驻监视进程。文件删除、解析失败和超出采集上限也留下状态；恢复后可以重试。
文件未提交时记录工作副本及哈希，提交后补来源 commit。

`record.json` 只投影身份、协议、对照、指标和有限运行摘要。原始日志、checkpoint 和大型逐样本输出
保持原位置；`evidence` 可以引用存在的 `remote_artifacts/` 文件，但采集器不读取它们。

```bash
python .agents/harness/memory/research_memory.py recall "当前模型" --kind decision --status ACTIVE --scope model.architecture
python .agents/harness/memory/research_memory.py recall "某评测协议" --kind finding --protocol "协议标识"
python .agents/harness/memory/research_memory.py recall "输入缺失 重试条件"
python .agents/harness/memory/research_memory.py recall "旧模型" --history
```

召回同时返回正式条目和来源候选，保持类型、协议与状态。默认排除被取代的条目，
历史用途加 `--history`。不同范围的发现不可直接合并成一个成绩或架构描述。
原文比预览长时会标记截断；按事件 ID 或文件引用继续读。

## 可选 Hindsight

本地模式不需要 SDK 或远端服务。要启用同步，复制 `config/research-memory.example.json` 为
`config/research-memory.json`，填写稳定、独立的 `project_id`，将 `hindsight_enabled` 设为 `true`。
默认来源与预算也可在这个文件中调整。

凭据由既有环境或 `.agents/harness/config/.env` 提供，只使用变量名：
`HINDSIGHT_MCP_URL` 与 `HINDSIGHT_API_KEY`；
也可用 `HINDSIGHT_API_URL`、`HINDSIGHT_BANK_ID` 组合出端点。
宿主回调在子进程中加载环境，不将凭据写入宿主配置。

同步先写本地队列，由持有独占锁的短任务调用 MCP。Codex 的 async 标记目前不执行异步逻辑，
因此绑定只调用一个本地回调，由回调在落盘后安排同步任务。网络返回不占用前台上下文入口。
`sync --limit 4` 可以显式重试；它需要在已加载上述变量的环境中运行。

同一逻辑来源沿用 document_id，每个版本保存内容哈希。接收请求和完成操作分别记账；
超时保留发送意图，先确认旧版本，再发送更新。失败不清空队列。
`recall` 可以补充远端候选，返回项目、来源角色和本地版本核对结果。
`snapshot_matches` 只说明候选对应已知快照；是否仍生效、是否有科学证据仍须核对正式条目。

## 预算与已知边界

默认上下文预算为 6500 字符，每次来源列表最多 8 条。消息输入上限为 4 MiB，
文件采集上限为 1 MiB，事件预览快照上限为 32 KiB；较长的已接收消息另存完整本地来源。
疑似凭据内容仅留哈希与来源引用，进入待确认状态，不复制或同步其正文。
识别依靠常见凭据格式与已加载的凭据变量；不要把原始凭据当作科研来源。

正常采集使用宿主提供的 turn_id 或 prompt_id。旧宿主没有这些标识时按会话轮次记录，
但无法严格区分同一未结束轮次中完全相同的两次表态与重复投递；事件会注明 session_sequence。
缺少最终回复正文时，只有限读取 transcript 中可确认的最终回复，无法确认则保留 capture_gap。
transcript 格式不是稳定接口；正常路径始终优先使用宿主的 last_assistant_message。

自动接收不等于自动确认结论。语义整理由本轮 agent 根据来源完成，未完成内容持续留在队列。
