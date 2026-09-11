# Remote Run Control

rrctl 0.2 使用 Linux 独立进程会话承载远端 worker，不依赖 tmux 或常驻的本地服务。
包名为 `remote-run-control`，Python 模块为 `remote_run_control`。

```bash
python3 -m pip install -e .agents/harness/remote/rrctl
rrctl --json doctor
```

新 RunSpec 使用 `session.backend=process`。`session.name` 只是兼容的显示标签，归属由 RunID、control root、PID、启动时间、boot ID 和进程组决定。旧后端的历史运行可 inspect、resume 和 pull；不会用旧协议启动或停止任务。

`environment.variables` 保存明确的非敏感环境覆盖，`required_modules` 声明导入依赖，`preflight_argv` 可检查已提交的项目入口。Conda 激活前后都清除继承的动态库/Python 路径；需要这些路径的项目在 variables 中显式声明。Python 3.10 的项目配置解析依赖公共 tomli，可用 `.agents/harness/requirements.txt` 安装。

GPU 任务声明 `resources.device=gpu`。`gpu_ids=[]` 独占全部可见 GPU；明确给出不重叠的 GPU 索引或 UUID 才可并行。worker 检查已有 CUDA 进程、空闲显存和同一用户的活动资源登记，再设置 `CUDA_VISIBLE_DEVICES`。CPU 检查用 `device=cpu`。占用检查不会终止其他进程。

```bash
rrctl --json ready runspec.json
rrctl --json launch runspec.json
rrctl --json wait RUN-ID --poll-seconds 600 --max-wait-seconds 900
rrctl --json pull RUN-ID
```

| wait 退出码 | 含义 | 后续动作 |
|---|---|---|
| 0 | completed | pull |
| 1 | failed / aborted | 检查终态原因 |
| 2 | attention 或控制错误 | 读取结构化错误；观察故障不会自动停止任务 |
| 124 | 本次观察期限已到 | 对同一 RunID 继续 wait |

`--max-wait-seconds 0` 可持续等待。外层工具超时应大于观察预算及一次控制请求的时间。首步 gate 默认由生成器设置为 600 秒；它与整个训练时长分开。首步观察未通过但进程已启动时保留绑定，记录 gate pending，不重新 launch。

一键入口支持从请求生成配置，以及恢复已有运行：

```bash
python3 .agents/harness/remote/remote_run.py runspec.json --request request.json --execute
python3 .agents/harness/remote/remote_run.py runspec.json --execute --resume
```

多组脚本可登记为 `pipelines.<名称>.stages`。用 `run_pipeline.py --list-pipelines` 查看组合，准备请求时增加 `--pipeline module_a`。所选名称和阶段 SHA 固定在 RunSpec 中；恢复运行不能换成另一个模块。

输入 `--request -` 从 stdin 读取。入口默认只打印状态、路径和退出码；完整响应存于本机 `.local/state/rrctl/client-results/<RunID>/`，`--full-output` 可输出全文。`--resume` 核对 RunSpec 摘要后只执行 inspect/wait/pull。

完成校验直接依赖的 progress/summary 会进入最小产物清单。pull 返回真实 destination；重复拉取会核对已有文件的大小与 SHA 后复用，内容不同则拒绝覆盖。失败诊断使用 `pull --diagnostic`，其 control/output 目录结构不等同于正式产物根。
