#!/usr/bin/env bash
# 项目训练入口模板：接入时将原训练命令及参数放在本文件中。
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
cd -- "$repo_root"

# 参数区：按项目实际入口与原有协议填写；额外参数逐项放入 train_args。
python_bin="${PYTHON_BIN:-python}"
train_entry="train.py"
output_root="${RRCTL_OUTPUT_ROOT:-${OUTPUT_ROOT:-"$HOME/research-runs/$(basename -- "$repo_root")/manual"}}"
train_args=(
    --output-dir "$output_root"
    # 在这里加入项目原有的学习率、batch size、训练步数、seed 等参数。
)

if [[ ! -f "$train_entry" ]]; then
    printf '训练入口不存在：%s；请在 scripts/train.sh 中填写项目入口和参数。\n' "$train_entry" >&2
    exit 2
fi
mkdir -p -- "$output_root"
exec "$python_bin" "$train_entry" "${train_args[@]}" "$@"
