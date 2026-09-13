# 完成版实验分析入账

用于交付时准备 Hindsight 入账预览，以及用户提出“预览入账”“确认入账”“继续同步”时的直接办理。
只办理发布时，无需先执行 context、pending、process 或 Mission 恢复。

## 准备本轮预览

本轮更新了已定稿的 `analysis/analysis.md`、项目 `hindsight_enabled` 为 true、且用户未拒绝入账时，
Agent 在交付科研结果时准备一次本地预览。普通代码任务、草稿和运行过程不触发；服务未启用时不反复提醒配置。
以本轮 ExpID 为范围，多个实验合成一批；只有用户要求时才扩大到历史实验。已有对应预览或确认时复用，避免重复提示。

从项目根执行。每次预览或同步前，在同一 shell 中加载项目 `.agents/harness/config/.env`（如存在），
供凭据检查与连接使用，不打印文件或变量值。`scripts/memory` 只转发命令，不自动加载凭据：

```bash
set -a
if [ -f .agents/harness/config/.env ]; then source .agents/harness/config/.env; fi
set +a
./scripts/memory publish-batch --exp-id <ExpID_A> --exp-id <ExpID_B>
```

程序只选择各实验的主分析，不跟随附件。预览不联网、不扫描原始来源、不入同步队列。
它检查敏感信息、草稿、原始对话/日志、按序且非空的 Change / Result / Finding / Next 四段；
符号链接、不可读文件及超过 32 KiB 的主分析被排除。已同步且内容、来源身份未变的版本自动跳过。
`--limit` 只限制终端显示条数；完整清单在返回的 `preview_path` 中。

读取清单及其链接的完整副本，核对分析已定稿、范围正确、无敏感内容；模式检查不能代替审阅。
在对话交付消息中展示实验清单、数量、排除项和预览链接，供用户一次确认。无候选时不请求确认。
用户暂不入账时照常交付；不新增 CSV 等待行，也不改写机器生成的 handoff。

## 确认后完成上传

用户确认所展示的固定清单后，Agent 加载环境并执行：

```bash
./scripts/memory publish-batch --confirm <BATCH_ID> --sync --wait
```

`--confirm` 只用于用户已确认的清单；同批内容不变时不重复询问，也不按“最新批次”猜测授权。
整批预检通过后一次入队，只同步已确认版本。预览副本、主分析或来源身份改变，需要重新预览并确认。
省略 `--sync` 只入队；`--wait` 必须与确认及同步一起使用。

`--wait` 在默认 120 秒预算内查询已提交的同一远端操作；超过 20 份会分轮处理。
它不会在等待中重发失败或结果不明的上传。仅当 `complete: true` 且无错误、无 blocked 时报告整批成功；
入队和 `submitted` 都不代表远端完成。

## 沿原批次继续

预算耗尽但远端仍正常处理时，Agent 沿原批次继续：

```bash
./scripts/memory sync --batch <BATCH_ID> --wait --seconds 120
```

同批续传沿用原确认和已有 operation ID，不重复 publish，不带上其他待同步记录。
服务失败、队列被占用或内容变化时停止当前同步，报告已完成数量、未完成数量及原因；
服务恢复后仍可继续原批次，内容变化则重新预览。用户无需再到终端手工操作。
科研任务已完成时，明确分开报告科研交付和待续的入账状态，不改变科研完成判断。

同步预算支持 `--seconds 1..900`。不带 `--wait` 保留原先单轮检查行为；不带参数的 `publish-batch`
仍可手动预览全部主分析。`sync --limit 4` 涉及整个精选队列，只在用户明确要求该范围时使用。
原 Python 长命令继续兼容。清单与确认沿用本地控制目录 `publications/`，不作为科研产物提交。
