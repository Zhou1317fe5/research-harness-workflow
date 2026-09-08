# Harness 目录与入口

下表中的目录相对项目的 `.agents/harness/`。

[用户配置说明](configuration.md)介绍必填项、可选功能与接入步骤。
[科研记录与召回](research-memory.md)介绍来源队列、结论整理和 Hindsight。

| 目录 | 内容 |
|---|---|
| config/ | 项目配置、连接配置、环境变量及示例 |
| common/ | 项目根目录定位、配置解析 |
| pipeline/ | 按项目配置执行训练与评估 |
| remote/ | RunSpec 构建、rrctl 入口、运行输出校验、adapters 和 rrctl/ 控制包 |
| records/ | 生成实验事实记录与索引 |
| memory/ | 来源接收、结论整理、召回、钩子安装和可选同步 |

命令从具体功能目录运行，例如：

```bash
python .agents/harness/pipeline/run_pipeline.py --check
python .agents/harness/memory/install_memory_hooks.py
```

远程控制面是 `remote/rrctl/` 中的独立 Python 包，包名仍为 remote-run-control，
命令仍为 `rrctl`。从项目根安装：

```bash
python -m pip install -e .agents/harness/remote/rrctl
```

一键入口会在远程操作前检查 PATH 中的 `rrctl` 是否支持 `pull --diagnostic`，
并在整个运行中使用同一个可执行文件。更新工作流后应重新安装项目中的控制包；
旧安装可能沿用相同版本号却缺少新能力。需要隔离时，在独立虚拟环境安装并将其
`bin` 目录放到 PATH 前端。

旧版平铺路径已迁移到上述目录。使用旧版的目标项目应一并更新调用路径，
将本地配置移入 config/，再重新安装会话钩子。
