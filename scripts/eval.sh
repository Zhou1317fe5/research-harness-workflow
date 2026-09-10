#!/usr/bin/env bash
# 项目评估入口模板：接入时将原评估命令及参数放在本文件中。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
cd -- "$repo_root"

# 参数区：评估协议、数据划分、seed 等参数均在本脚本中维护。
python_bin="${PYTHON_BIN:-python}"
eval_entry="evaluate.py"
output_root="${RRCTL_OUTPUT_ROOT:-${OUTPUT_ROOT:-"$HOME/research-runs/$(basename -- "$repo_root")/manual"}}"
checkpoint="${CHECKPOINT:-"$output_root/checkpoints/best.pt"}"
eval_args=(
    --checkpoint "$checkpoint"
    --output "$output_root/summary.json"
    # 在这里加入项目原有的评估参数。
)

if [[ ! -f "$eval_entry" ]]; then
    printf '评估入口不存在：%s；请在 scripts/eval.sh 中填写项目入口和参数。\n' "$eval_entry" >&2
    exit 2
fi
if [[ ! -f "$checkpoint" ]]; then
    printf '评估 checkpoint 不存在：%s\n' "$checkpoint" >&2
    exit 2
fi
mkdir -p -- "$output_root"
exec "$python_bin" "$eval_entry" "${eval_args[@]}" "$@"
