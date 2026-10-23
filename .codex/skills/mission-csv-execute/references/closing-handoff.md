# Human Handoff 施工交工单规范

REVIEW 行执行后，必须产出一份面向人类决策者的交接文档 `<csv-path-without-.csv>.handoff.md`，与 CSV 同目录同前缀。

## 1. 定位与分工

handoff 是**施工交工单**——用户隔一段时间回来打开它，能还原"这轮干了什么、干成什么样、还剩什么"的完整画面，不需要翻 CSV、不需要翻 claims.json、不需要翻 git log。

| 文件 | 读者 | 目的 | 写法 |
|---|---|---|---|
| `review.md` | reviewer / agent | 证明审过什么、有没有 gap | 结构化审计日志，使用内部编号 |
| `handoff.md` | 人类（决策者） | 跨 session 一眼看懂做了什么、没做什么、下一步 | 跟 spec 同风格的大白话叙事 |

## 2. 内容结构（必须按此顺序）

1. **第一层：先看结论（3 秒读完）**：
   - 包含：对应哪篇 spec、整体判定（`pass / fail / partial / unknown / not_run`）、支撑判定的决定性结果与最重要的一条 blocked claim。
2. **第二层：这份交工单告诉你什么**：
   - 说明证据输入、读者与边界：解释 spec、代码、测试、trace，但不生产运行结果，不把“代码完成”写成“能力已证明”。
3. **第三层：你现在可以确定什么（30 秒浏览）**：
   - 逐条呈现 Outcome Contract 的 reader questions（不暴露 `OUTCOME-*` id，不改写问题文本）；
   - 表格包含：你关心的问题、判定、直接答案、关键证据、可信度、结论边界、下一步；
   - 由 review JSON 机械渲染，必须逐字段一致。没有证据时写 `unknown` 或 `not_run`，不能猜。
4. **第四层：决定整体状态的结果**：
   - 说明成功条件、到达的最后正常阶段与第一处失败/证据缺口。必须分开实现状态、验证状态和能力结论。
5. **第五层：目前仍不能声称什么**：
   - 逐条呈现 Outcome Contract 的 blocked claims（不能声称的结论、原因、解除条件），`partial/unknown/not_run` 不得合并成 pass。
6. **第六层：spec 目标逐条对账**：
   - 以 spec 定义的能力/目标为单位（表格：spec 目标、状态、实际效果、备注）。从产品/用户视角写实际效果，不从代码视角写。
7. **第七层：施工细节（按模块组织）**：
   - 改了什么、具体用户行为场景（你在前端做 X → 系统会 Y → 以前是 Z）、踩了什么坑、做了什么决策、质量判断、集成影响。
   - **数据流/架构变更必须配 Mermaid 图**：
     - 当数据流跨多层改变、调用关系增删改向或架构边界调整时，必须配图（最多两张：改之前、改之后）；
     - 只画本轮变动的节点与边，虚线标出新增边；纯文案/单函数逻辑不强制画图；语法失败时降级为 ASCII 流程图，不阻断任务闭环。
8. **第八层：验证情况**：
   - 精简说明跑了什么测试、哪些验证降级，不喧宾夺主。
9. **第九层：后续可操作**：
   - 还剩什么、阻塞/配置、怎么复现、去哪看。
   - **开放 Deferred Findings**：若存在 `status=open` 的记录，在“后续可操作”前增加独立的 `## 待讨论` 章节，每条前保留隐藏标记 `<!-- deferred:DF-001 -->` 供机械覆盖检查，正文大白话叙述，不出现内部代码标识。

## 3. 风格硬规则

- **以 spec 目标为锚**：结构跟着 spec 走，不跟着 CSV 行号走，不跟着 CLAIM 编号走；
- **自包含**：所有信息内联展开，不引用内部编号让读者跳转查找；
- **跟 spec 同风格**：先说结论再说细节，第一次出现的术语用括号解释；
- **大白话优先**：先说产品效果，再说技术路径；
- **不写审计套话**：禁止出现 "scope checked" / "vision_met" 等模板空话；
- **详细但不冗余**：数据源薄就如实写薄，不编造篇幅；
- **诚实标注不确定性**：降级就说降级，没验证就说没验证。

## 4. 机械验收与生成降级

1. **生成规则**：
   - 走 reviewer 脚本时传 `--handoff <path>`；正文不预写独立性头，由 launcher 按 runtime requested/observed model 与 evidence 生成唯一的 `独立性` 结构化头，和 review JSON 一致。旧稿身份矛盾时拒绝发布，不能改标签冒充独立审查。
   - 走 self-review 时 review JSON 必须为 `review_agent_mode:self-review` / `review_independence:false`，据此生成结构化头，并保留 `WARNING: self-review only, NOT independently verified`；不得同时出现独立审查头。
   - 生成后在 REVIEW 行 notes 追加 `handoff:<path>`，开放项回填 `deferred_coverage:<covered>/<open>`。
   - 审查恢复、轮次与材料身份以 [独立审查规范](closing-independent-review.md#恢复与材料身份) 为准；同输入复用，新轮/新材料新建，不搬删旧 job。交工单必须挂到最后 REVIEW 行，JSON 若含 `review_row_id` 必须匹配该行。
2. **机械合同验收门禁（HARD）**：
   ```bash
   python .codex/skills/mission-csv-execute/scripts/check_handoff_contract.py <handoff-path> --csv <csv-path>
   ```
   - 退出码 0 = 通过：用 `set_note_tags` 更新 REVIEW 行 `handoff_contract:passed`；
   - 退出码 1 = 不合格：修正后重生成；重试后仍失败则记 `handoff_contract:failed <缺项>`，允许代码交付收口，但不得声称 handoff 完成。
   - 无 Outcome Contract 的兼容任务也校验 JSON、CSV 身份与唯一结构化头的一致性；Outcome answers 和 blocked claims 仍由对应合同逐字段校验。
3. **生成失败降级**：
   - 若脚本/reviewer 未能产出 markdown，主代理用 review JSON + CSV 手动渲染兜底稿，顶部标 `WARNING: auto-generation failed, rendered by main agent as fallback`，随后必须跑通过 `check_handoff_contract.py`。
   - 数值结果落在精度容差内，只证明对应数值验收通过，不证明统计显著性或科学假设成立；没有重复实验/协议证据时明确写结论边界。
