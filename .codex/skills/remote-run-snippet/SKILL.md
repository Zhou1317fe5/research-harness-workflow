---
name: remote-run-snippet
description: 通过 bash 调用项目训练与评估脚本，用 rrctl 启动、等待和拉取结果；用于已确定命令与运行范围的远程实验。
metadata:
  short-description: 项目脚本驱动的远程训练与评估
---

# 远程训练与评估

公共实现位于项目根 `.agents/harness/`。训练、评估命令及其参数维护在项目 `.sh` 脚本中，通过 `bash` 执行；rrctl 负责远程生命周期、监控和拉取。

## 首次接入

先读取项目已有的训练、评估 `.sh` 脚本。可以分别使用两个脚本，也可以把训练和评估写在一个脚本中；保留原路径、参数与启动方式。没有脚本时参考模板 `scripts/train.sh`、`scripts/eval.sh`，将项目原有命令和科学参数集中到脚本中。

再读取 `.agents/harness/config/project.toml`；尚未创建时，从 `.agents/harness/config/project.example.toml` 复制并填写脚本入口与输出约定。确认：

- `pipeline.stages[].argv` 登记 `['bash', 'scripts/train.sh']` 等脚本调用；单个训练评估脚本只登记一个阶段。目录和文件名沿用项目约定。
- 多组模块/消融脚本使用 `pipelines.<名称>.stages`，`pipeline.default` 指定默认组合。用 `run_pipeline.py --list-pipelines` 查看名称，再在生成请求时选择 `--pipeline <名称>`；一次只运行所选组合。组合可以有训练、评估两个阶段，也可以指向单个训练评估脚本。
- 学习率、batch size、训练步数、seed、评估协议等参数在 `.sh` 中维护，不在 TOML、CSV notes 或生成的 RunSpec 中再维护另一份命令。核对实际参数时读取脚本及其调用链。
- 脚本用 `RRCTL_OUTPUT_ROOT` 接收本次输出目录；已有脚本使用位置参数时，可在 argv 中传入 `{output_root}`。`RRCTL_RUN_ID` 可用于结果来源标识。
- 训练输出与评估输入指向同一 checkpoint；`requires` / `outputs` 声明必须存在的文件。
- `adapter` 指向实际的进度与结果文件；文本日志可直接拉取，JSON / JSONL 字段用于自动检查。
- `artifacts` 只声明本次分析所需文件或目录；`records` 映射指标字段。项目配置随代码提交，凭据仅放 `.agents/harness/config/.env`。
- `resources.device=gpu` 默认独占全部可见 GPU；并行运行必须绑定互不重叠的 GPU。CPU 检查显式使用 `device=cpu`。`environment.required_modules` 与阶段 `check_argv` 在训练启动前检查依赖和输入。

脚本中的 Bash 变量由 Bash 解释；配置里的 `{repo_root}`、`{output_root}`、`{run_id}` 由公共执行器替换。命令仍按参数数组执行。单文件训练评估脚本也必须在训练失败或缺少 checkpoint 时停止评估；需要动态选择 checkpoint 时，由项目脚本明确传递路径，不能凭文件时间猜测。

## 执行

1. 从已批准的 Spec / CSV 取得命令、RunID、commit、环境与输出位置，沿用调用方的科学审查和远程授权边界。
2. 本地检查配置：

   ```bash
   python3 .agents/harness/pipeline/run_pipeline.py --check
   ```

3. 用 `rrctl --json doctor` 确认 process 后端和实际安装路径。用已有 `mission.rrctl-request.v1` 请求构建 RunSpec，增加 `--project-config .agents/harness/config/project.toml`。配置提供 workload、adapter、artifacts、environment、resources 和 health 默认值；请求中的显式值优先。完成校验必需的 progress/summary 必须进入最小清单。首次接入用 `validate_adapter.py` 验证真实输出样例。
4. 生成一键命令：

   ```bash
   python3 .agents/harness/remote/remote_run.py issues/<stem>/runs/<RunID>/runspec.json
   ```

   运行打印的命令时，公共入口依次调用 rrctl 的 `ready`、`launch`、前台 `wait` 和 `pull`。`.agents/harness/config/profiles.json` 存在时自动使用；也可显式指定 `--profiles`。

   也可增加 `--request <request.json|-> --execute` 一次完成生成和执行，`-` 从 stdin 读取。完整响应保存在返回的 `details_path`；默认只输出摘要，必要时使用 `--full-output`。

   多组合项目增加 `--pipeline module_a`，或在 request 中写 `pipeline:module_a`。生成器把名称、阶段定义和 SHA 写入 RunSpec，并绑定 workload 与预检；不要同时指定另一份低层 workload。恢复时保留原组合，改选模块需要新 RunID。不同组合共享顶层产物契约；输出协议不同的实验也可使用独立 `--project-config`。

5. Mission 执行期间需要及时记录 launch 状态时，分别调用上述 rrctl 子命令。进程已启动而首步观察未通过时，记录绑定和 gate pending，沿用同一 RunID 继续观察；不重复 launch。首步通过后再推进下游步骤。
6. 失败或健康检查需要介入时，使用 `rrctl pull <RunID> --diagnostic` 拉取诊断快照；它不会终止正在运行的进程。按实际状态更新 CSV，不把诊断快照计作完成结果。
7. 完成后的结果保存在 `remote_artifacts/<ExpID>/<RunID>/`。用 `experiment_records.py build --exp <ExpID>` 读取结果，再更新 `analysis.md` 与后续行动。

`wait` 默认每 600 秒检查，观察预算为 900 秒。退出码 0 表示 completed，1 表示 failed/aborted，2 表示 attention/错误，124 表示本次观察到期且远端运行保留。工具超时应留出一次控制请求的时间；124 后继续等待同一 RunID，不拉失败诊断、不改失败状态。

恢复已有运行可使用 `rrctl inspect/wait/pull`，或给一键入口增加 `--execute --resume`；它核对摘要后跳过 launch。现有本地索引丢失时，先用 `rrctl resume --profile <profile> --control-path <control-root>` 恢复定位。rrctl 不可用或失败时保持 `fallback_allowed:false`。
