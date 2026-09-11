# Remote Run Control

rrctl 0.4 使用 Linux 独立进程会话承载远端 worker，不依赖 tmux 或常驻的本地服务。
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

新 worker 自主执行首步、周期检查和完成验收。退出本地 wait 或关闭 agent 后，检查继续按 RunSpec 的健康策略运行；成功退出的 workload 先进入 workload_complete，通过原有验收、smoke 清理和必需产物检查，封存报告与 manifest 后才进入 completed。非零退出保留真实退出码。检查器超时与异常单独记录，活任务保持运行；完成检查持续不可用时保留待验收状态并报告 attention。

正常监控只有一个写入者，使用独立的 monitor 锁；状态锁只用于短期发布。心跳约每 30 秒写入一次，不执行 adapter。昂贵检查遵守各阶段间隔，adapter 有独立超时，工作负载退出通过有超时的 process.wait 及时检测。心跳过期阈值覆盖 adapter 的预算和采样时间；失联、过期或损坏的缓存会报告未知，不据此伪造训练失败或完成。

新模式下 inspect、health、wait 只读已发布结果；多个观察者不会放大 adapter 次数。首步通过后不重复推进生命周期，已完成运行使用封存的验收记录，重连不再调用 completion adapter。health 返回的 run_state、health_status、monitor_status 分别描述运行、检查和监控器状态。

| control 文件 | 用途 |
|---|---|
| monitor.json | 协议、固定策略、拥有者、心跳、检查计数和当前告警 |
| health-latest/阶段.json | 原子替换的完整检查结果及采样时间 |
| monitor-events.jsonl | 独立的告警打开与恢复事件，序号单调 |
| health.jsonl | 由 worker 按检查周期保存的检查历史 |
| workload-exit.json | 实际工作负载退出码 |
| completion.json | 绑定运行身份及 manifest 摘要的验收记录 |
| finalization-error.json | 检查或发布不可用的诊断依据 |

既有 status.json 和 events.jsonl 继续保存生命周期及可恢复的转换链。诊断快照包含上述监控证据，保留原大小上限及截断标记。普通拉取仍校验文件内容的大小和 SHA。

wait 的远端 observe 请求最多持续 120 秒，仅读缓存。无事件响应在当前客户端内部续等，不输出给调用者。传输预算按每次请求设置，并随剩余客户端期限缩短。--poll-seconds 表示客户端观察窗口上界，不改变 worker 的采样频率。连接故障最多尝试三次，协议错误不会静默回退为另一套监控。

告警指纹由运行、阶段、原因代码和固定对象组成，不包含时间、step 或即时 GPU 百分比。可重试的检查故障连续出现三次才打开告警；明确硬故障立即报告。同一问题更新计数，恢复后再次发生会形成新的事件轮次。advisory 的 GPU 波动不自动升级为必须干预。

默认会重放仍有效的必要告警。调用者明确传入上一响应中的 monitor.event_cursor，才会跳过已确认的事件：

```bash
rrctl --json wait RUN-ID --max-wait-seconds 0 --after-event RUN_IDENTITY:SEQUENCE
```

游标绑定本次运行，不能跨运行复用，也不承诺跨连接精确一次投递。wait 默认输出状态摘要和证据路径，完整观察响应保存在本机运行索引的 last_observation.json；wait --full-output 可直接输出完整结果。正常持续等待只在终态或必要告警时对外返回一次。

默认 900 秒及显式期限保持原义，到期返回 124；0 仍是已有的持续等待选项。外层 remote_run.py 默认显式转发 900 秒，因此只升级 rrctl 不会消除这类每 15 分钟的返回。可以使用已有 --max-wait-seconds 0 参数；宿主工具的硬超时、模型自主调用和压缩不由 rrctl 控制，程序调用次数也不等于模型 token 使用量。

monitoring 能力及有效策略写入新运行的 binding，RunSpec 摘要格式保持不变。未声明该能力的旧 process worker 继续走 client_compatibility 路径；不覆盖活跃任务的 zipapp，不改写其 SHA，不自动重启或迁移任务。worker 被杀或服务器重启后不会自动恢复监控，重连报告最后状态及失联信息。doctor 显示本机版本、实现路径、源码摘要和监控协议能力，不代表已经检查服务器健康。

一键入口支持从请求生成配置，以及恢复已有运行：

```bash
python3 .agents/harness/remote/remote_run.py runspec.json --request request.json --execute
python3 .agents/harness/remote/remote_run.py runspec.json --execute --resume
```

多组脚本可登记为 `pipelines.<名称>.stages`。用 `run_pipeline.py --list-pipelines` 查看组合，准备请求时增加 `--pipeline module_a`。所选名称和阶段 SHA 固定在 RunSpec 中；恢复运行不能换成另一个模块。

输入 `--request -` 从 stdin 读取。入口默认只打印状态、路径和退出码；完整响应存于本机 `.local/state/rrctl/client-results/<RunID>/`，`--full-output` 可输出全文。`--resume` 核对 RunSpec 摘要后只执行 inspect/wait/pull。

完成校验直接依赖的 progress/summary 会进入最小产物清单。pull 返回真实 destination；重复拉取会核对已有文件的大小与 SHA 后复用，内容不同则拒绝覆盖。失败诊断使用 `pull --diagnostic`，其 control/output 目录结构不等同于正式产物根。

0.4 为所有操作提供 `rrctl.cli.v1` 外层结果。`operation` 表示当前操作，`status` 为 succeeded、failed、unknown 或 attention，`run_id` 在适用时给出。原有阶段结果保存在 `result`，错误保存在 `error`。无结果或无错误时使用空对象，兼容旧 wrapper 的 `.get()` 调用；ready 和 doctor 原有的顶层发现字段也继续保留。

```json
{
  "schema_version": "rrctl.cli.v1",
  "operation": "inspect",
  "status": "succeeded",
  "run_id": "RUN-ID",
  "ok": true,
  "result": {"status": {"state": "running"}},
  "error": {}
}
```

外层描述操作结果，运行状态仍在原结果中。例如 inspect 成功读到 failed 运行时，查询本身仍为 succeeded；成功的 abort 返回 aborted 运行状态。`ok` 保留旧接口含义，健康查询或观察期限到期可以同时为 ok=true、status=attention。退出码继续有效：wait 保持 0/1/2/124，ready 校验不通过为 1，控制错误为 2，未处理程序错误为 3。

launch、abort 或其他远端副作用请求发出后，如果连接中断、响应丢失或无法解析完整响应，返回 status=unknown、error.outcome=unknown 和 retryable=false。客户端不自动重发；错误包含原 RunID 及 inspect/wait/resume 的 next_actions。当前不确定操作写入已有本机运行索引的 operation-latest.json，供恢复时查阅。已登记的 RunID 再次 launch 会先报错，提示观察原运行。若连接预检就失败，结果明确说明 launch_dispatched=false。

unknown 不表示训练失败，也不能用来判断 abort 是否已经生效。首步观察和正常 wait 到期仍保持原观察语义；只读观察继续使用有界重试。Source/manifest 的 SHA 校验和重复目录保护不放宽。

SSH/本地控制请求的 stdout 和 stderr 各使用统一的 256 KiB 预算，边读取边保留头尾；超限后返回截断标记、完整读取时的 original_bytes 或中断时的 observed_bytes。先执行跨块脱敏，再进入有界缓存，覆盖口令、JSON 转义表示、凭据字段及私钥文本。正常小型 JSON 的阶段字段保持原样；截断的 JSON 不冒充完整成功响应。

文件下载的二进制 stdout 不套用这项小上限，仍按 manifest 的大小和 SHA 校验。控制诊断的 stderr 保持有界。`--full-output` 仍用于查看完整结构化结果，不绕过控制通道预算；完整实验日志通过 artifact 规则单独拉取。

不带 profile 的 doctor 继续只报告安装信息。连接诊断沿用同一命令：

```bash
rrctl --json --profiles .agents/harness/config/profiles.json doctor --profile PROFILE_NAME
```

先按项目约定在调用环境中提供凭据和 Conda 变量。doctor 只显示变量名及 set/unset，不自动读取或 source `.env`。默认检查 profiles.json 同目录的 `.env` 权限；`--env-file PATH` 可指定需要检查的文件。`--offline` 只做本地检查，这两个选项都与 `--profile` 一起使用。

profile 可以增加以下可选诊断配置；省略时使用示例中的默认值：

```json
{
  "diagnostics": {
    "python": "/usr/bin/python3",
    "conda_env_var": "REMOTE_CONDA_ENV",
    "conda_sh_var": "REMOTE_CONDA_SH",
    "directories": ["~"]
  }
}
```

`password_env` 和两个 Conda 配置项都只保存环境变量名。初始化脚本路径通过指定变量提供，需为绝对路径。`ssh_argv` 登记 SSH 可执行文件、选项和一个目标，远端命令由 rrctl 提供；现有 SSH 选项原样传递。诊断配置不覆盖 RunSpec 中实际运行的环境设置。

doctor 检查 profile/JSON、工具、适用的文件权限和变量状态，然后只读验证登录、远程 Python、指定目录可访问性和 Conda 激活。每项失败都有原因代码和修复提示。诊断不创建 RunID、staging 或运行目录，不上传源文件、申请 GPU、启动训练或停止任务；它也不代替具体 RunSpec 的 ready 检查。
