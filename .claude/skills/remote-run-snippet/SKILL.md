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
- 学习率、batch size、训练步数、seed、评估协议等参数在 `.sh` 中维护，不在 TOML、CSV notes 或生成的 RunSpec 中再维护另一份命令。核对实际参数时读取脚本及其调用链。
- 脚本用 `RRCTL_OUTPUT_ROOT` 接收本次输出目录；已有脚本使用位置参数时，可在 argv 中传入 `{output_root}`。`RRCTL_RUN_ID` 可用于结果来源标识。
- 训练输出与评估输入指向同一 checkpoint；`requires` / `outputs` 声明必须存在的文件。
- `adapter` 指向实际的进度与结果文件；文本日志可直接拉取，JSON / JSONL 字段用于自动检查。
- `artifacts` 只声明本次分析所需文件或目录；`records` 映射指标字段。项目配置随代码提交，凭据仅放 `.agents/harness/config/.env`。

脚本中的 Bash 变量由 Bash 解释；配置里的 `{repo_root}`、`{output_root}`、`{run_id}` 由公共执行器替换。命令仍按参数数组执行。单文件训练评估脚本也必须在训练失败或缺少 checkpoint 时停止评估；需要动态选择 checkpoint 时，由项目脚本明确传递路径，不能凭文件时间猜测。

## 执行

1. 从已批准的 Spec / CSV 取得命令、RunID、commit、环境与输出位置，沿用调用方的科学审查和远程授权边界。
2. 本地检查配置：

   ```bash
   python .agents/harness/pipeline/run_pipeline.py --check
   ```

3. 用已有 `mission.rrctl-request.v1` 请求构建 RunSpec，增加 `--project-config .agents/harness/config/project.toml`。配置提供 workload、adapter 和 artifacts 默认值；请求中的显式值优先。首次接入用 `validate_adapter.py` 验证真实输出样例。
4. 生成一键命令：

   ```bash
   python .agents/harness/remote/remote_run.py issues/<stem>/runs/<RunID>/runspec.json
   ```

   运行打印的命令时，公共入口依次调用 rrctl 的 `ready`、`launch`、前台 `wait` 和 `pull`。`.agents/harness/config/profiles.json` 存在时自动使用；也可显式指定 `--profiles`。

5. Mission 执行期间需要及时记录 launch 状态时，分别调用上述 rrctl 子命令，并在 launch 成功后立即更新对应 CSV 行。保持已有的单个前台 `rrctl wait` 等待方式。
6. 失败或健康检查需要介入时，使用 `rrctl pull <RunID> --diagnostic` 拉取诊断快照；它不会终止正在运行的进程。按实际状态更新 CSV，不把诊断快照计作完成结果。
7. 完成后的结果保存在 `remote_artifacts/<ExpID>/<RunID>/`。用 `experiment_records.py build --exp <ExpID>` 读取结果，再更新 `analysis.md` 与后续行动。

恢复已有运行时直接使用 `rrctl inspect/wait/pull`，不重新执行一键入口以免重复 launch。rrctl 不可用或失败时停止当前动作，保持 `fallback_allowed:false`。
