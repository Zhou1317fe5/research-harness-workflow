# test_mcp / required_mcp 映射

`test_mcp` 只表示**主验证模式**。
`required_mcp` 才表示**必须实际调用的验证工具**。
两者必须同时看，不把工具名拼进验证模式。

| test_mcp | 主验证目标 | 默认 required_mcp | 典型 runner / 辅助工具 |
|----------|------------|-------------------|--------------------------|
| `local_cli` | 本地单测、API、前端流程或迁移验证 | 按场景填写 | `pytest` / `npm test` / `playwright` / 项目测试运行器 |
| `remote_cli` | 真实远程训练、评估或运行验证 | 按远程执行合同填写 | `rrctl` + 项目 adapter |
| `contract` | Schema/API 契约验证 | 留空 | 项目自定义 contract runner |
| `manual` | 无法自动化的人工验收 | 按场景填写；UI 相关至少 `chrome-devtools` | 在 notes 写 `manual_test:<steps>` |

## 前端 issue 额外规则

对任何 `area=frontend` 或 `area=both` 且改动用户可见 UI 的 issue：

- `required_mcp` **至少**包含 `chrome-devtools`
- 若涉及多步交互流：登录、表单、筛选、弹窗、上传、导航、下单、跨页面状态切换 → 追加 `playwright`
- 若涉及品牌呈现、hero、视觉层级、滚动叙事、动效、hover/transition、响应式观感 → 追加 `screenpipe`
- 若同时满足交互流和视觉体验要求，可组合多个工具，如 `chrome-devtools;playwright;screenpipe`
- 若前端 issue 最终 `required_mcp` 为空，必须在 `notes` 记录原因；否则视为 CSV 拆分不完整

## required_mcp 证据最低要求

| MCP | 最低证据 |
|-----|----------|
| `chrome-devtools` | 至少 1 次页面 snapshot 或截图，并附 console/network 结论摘要 |
| `playwright` | 至少 1 条命名场景，记录通过/失败与关键步骤 |
| `screenpipe` | 至少 1 份与视觉/动效/滚动相关的证据摘要，说明观察到的实际行为 |
