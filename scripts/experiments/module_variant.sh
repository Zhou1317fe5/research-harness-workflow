#!/usr/bin/env bash
# 模块/消融实验的组合脚本示例：在参数区填写项目已有入口支持的参数。
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)

train_args=(
    # 在这里填写该模块的训练参数。
)
eval_args=(
    # 在这里填写该模块的评估参数。
)

bash "$repo_root/scripts/train.sh" "${train_args[@]}" "$@"
exec bash "$repo_root/scripts/eval.sh" "${eval_args[@]}"
