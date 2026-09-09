# Pi 适配验收记录

验收日期：2026-09-09。运行环境为当前 Linux / WSL，Pi 0.85.1、Node 22.20.0、smart-search 0.1.17。类型检查使用 npm exec 临时工具缓存中的 TypeScript 7.0.2，没有新增项目 npm 依赖。

实现包括科研 Advisor guidance、Pi PRERUN skill 与只读 profile、Memory lifecycle adapter，以及缺失的项目级 Context7 MCP 入口。普通 skills 继续来自 canonical 目录。公共文件只修改三处：PRERUN launcher 的归属、Codex launcher 标记块，以及 Memory CLI 的 `pi` host 枚举。

以下命令均已实际运行，表内为最终结果。它们覆盖 24 项本地测试及独立的 CLI / SDK / 网络联调。一次性验收脚本所在的 `.agents/harness/tests` 已清理，旧路径仅保留作历史记录；长期防漂移检查移至 `.pi/check-prerun-parity.py`，可运行 `python3 .pi/check-prerun-parity.py --self-test`，迁移后已通过。

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| `pi --version` | 0 | 0.85.1 |
| `smart-search --version` | 0 | 0.1.17 |
| `python3 -m py_compile .agents/harness/memory/research_memory.py` | 0 | 修改后的 Python 入口可编译 |
| `python3 /home/zhou/.codex/skills/.system/skill-creator/scripts/quick_validate.py .pi/skills/pre-run-implementation-review` | 0 | Skill is valid |
| `python3 .agents/harness/tests/check_pi_prerun_parity.py` | 0 | 除 launcher 外协议一致，脚本软链接指向 canonical |
| `python3 -m unittest discover -s .agents/harness/tests -p 'test_pi_*.py' -v` | 0 | 7 项通过 |
| `python3 -m unittest discover -s .agents/harness/memory/tests -v` | 0 | 8 项原有 Memory 回归通过 |
| `node --experimental-strip-types --test .agents/harness/tests/pi_adapters.test.mjs` | 0 | 9 项通过 |
| `node --experimental-strip-types .agents/harness/tests/pi_runtime_probe.mjs --typecheck` | 0 | 原生资源加载、策略替换、工具白名单和类型检查通过 |
| `node --experimental-strip-types .agents/harness/tests/pi_runtime_probe.mjs --live-subagent` | 0 | 一个真实 Pi 子会话，实际调用 `read` 读取随机证据标识，文件未改变 |
| `node --experimental-strip-types .agents/harness/tests/pi_mcp_probe.mjs` | 0 | fast-context 与 Context7 均经 Pi MCP adapter 调用成功 |
| `python3 .pi/skills/pre-run-implementation-review/scripts/prerun_route.py --help` | 0 | Pi 路径可调用 canonical 路由脚本 |
| `python3 .pi/skills/pre-run-implementation-review/scripts/prerun_ready.py --help` | 0 | Pi 路径可调用 canonical readiness 脚本 |
| `git diff --check` | 0 | 无空白错误 |

Hindsight 联调命令：

```bash
bash -c 'set -a; source .agents/harness/config/.env; set +a; exec python3 .agents/harness/tests/pi_memory_live_probe.py'
```

该命令退出码为 0。隔离项目 `pi-adapter-check-e27c76a169dc4718` 通过真实 Python CLI 和 Pi TS adapter 完成 Codex→Pi→Codex hooks 切换，随后使用既有 `hindsight_mcp.py` 向同一配置 bank 同步 4 份测试文档。远端文档均核对了测试项目身份，测试结束后 4 次 `delete_document` 调用全部成功。隔离项目的正式状态文件未改变，当前项目的配置和正式科研记录未参与写入。

原生 Pi 资源加载结果是：`pre-run-implementation-review` 选中 `.pi/skills` 中的版本，另外 9 个项目 skills 均来自 `.agents/skills`，项目 `AGENTS.md` 正常加载。已安装的五个 Pi packages 均为全局注册：

| Package | 版本 |
| --- | --- |
| `pi-mcp-adapter` | 2.32.1 |
| `@juicesharp/rpiv-ask-user-question` | 2.9.0 |
| `@narumitw/pi-goal` | 0.54.4 |
| `@juicesharp/rpiv-advisor` | 2.9.0 |
| `pi-sub-agent` | 0.1.5 |

Advisor 检查使用已安装包的真实工具元数据和 Pi 的 system prompt builder。科研提示中原有的固定阶段调用规则被替换，普通软件提示保持原样，其他扩展和项目原文得到保留。五类科研触发条件、hard-stop 和正式 Reviewer 的职责边界由项目 guidance 表达；它们是 Executor 的判断规则，不是新增自动审批 gate。

Reviewer 检查验证了 fresh SDK context、CLI 工具白名单以及真实子进程读取。即使尝试重新激活，`advisor`、shell、写入与委派工具也不能进入受限 registry。Reviewer 子会话不注册两个科研扩展的 hooks。此次只验证 launcher 和隔离能力，没有执行正式科学审查或训练实验。

Memory 测试覆盖用户输入去重、运行中 steering、最终回答、压缩恢复、工具 stdout 排除、子会话跳过、hooks 关闭以及长输入产生的 EPIPE。Codex 默认 host 和 Claude host 的原有入口仍可工作。Parity 测试在临时副本中分别单改两侧协议，均得到失败；同步修改后通过，没有修改真实协议来制造测试结果。

通用 MCP 联调中，fast-context 找到了实际的 `research-advisor.ts`，Context7 完成 `pi-mcp-adapter` 的库名解析和配置文档查询，没有调用全局 Hindsight 工具。

验收过程中修正了两处测试假设：SDK 测试进程需向 `pi-sub-agent` 提供真正的 Pi CLI 入口；Context7 的库名检索不保证 Pi 主仓库出现在短结果列表，因此改用本任务实际安装的 `pi-mcp-adapter` 验证解析与查询接口。没有修改上游 package 来迎合测试。

当前项目原有 `hooks_enabled:false` 和 `hindsight_enabled:false` 已保留。因此代码与隔离链路已通过验收，当前项目仍按原配置关闭自动科研记忆。安装与配置方法见 [Pi 配置说明](Pi_配置说明.md)。后续推荐的 FFF 与 Magic Context 未包含在本记录的五包联调中。
