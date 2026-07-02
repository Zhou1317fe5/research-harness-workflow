#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _run_git(args: list[str]) -> str | None:
    try:
        out = subprocess.check_output(["git", *args], cwd=REPO_ROOT, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return None


def list_candidates() -> tuple[list[str], list[str]]:
    if not SCRIPTS_DIR.is_dir():
        return ([], [])

    train = []
    evals = []
    for p in sorted(SCRIPTS_DIR.glob("*.sh")):
        name = p.name
        rel = str(p.relative_to(REPO_ROOT))
        if name == "train_eval_shutdown_autodl.sh":
            continue
        if name.startswith("train"):
            train.append(rel)
        elif name.startswith("eval"):
            evals.append(rel)
    return (train, evals)


def _print_candidates(train: list[str], evals: list[str]) -> None:
    print("Train script candidates:")
    if train:
        for s in train:
            print(f"  - {s}")
    else:
        print("  (none found under scripts/)")
    print()
    print("Eval script candidates:")
    if evals:
        for s in evals:
            print(f"  - {s}")
    else:
        print("  (none found under scripts/)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a copy-paste remote run snippet (git checkout/pull + train_eval_shutdown_autodl.sh)."
    )
    parser.add_argument("--branch", type=str, default=None, help="Git branch to checkout on remote (default: local HEAD).")
    parser.add_argument("--train-script", type=str, default=None, help="Training script under scripts/ resolved from intent.")
    parser.add_argument("--eval-script", type=str, default=None, help="Eval script under scripts/ resolved from intent.")
    parser.add_argument(
        "--model-dir-mode",
        type=str,
        default="train",
        choices=["train", "latest-checkpoint"],
        help="Pass-through to scripts/train_eval_shutdown_autodl.sh",
    )
    parser.add_argument(
        "--shutdown-mode",
        type=str,
        default="never",
        choices=["always", "on-success", "never"],
        help="Pass-through to scripts/train_eval_shutdown_autodl.sh",
    )
    parser.add_argument("--eval-gpu", type=str, default=None, help="Optional --eval-gpu value for evaluation.")
    parser.add_argument("--show-output", action="store_true", help="Add --show-output flag.")
    args = parser.parse_args()

    train_candidates, eval_candidates = list_candidates()

    if not args.train_script or not args.eval_script:
        _print_candidates(train_candidates, eval_candidates)
        print()
        print("Resolve one train + one eval script from intent and re-run with:")
        print("  --train-script ... --eval-script ...")
        return 2

    train_script = args.train_script
    eval_script = args.eval_script

    if train_script not in train_candidates:
        print(f"[ERROR] --train-script not found in scripts/ candidates: {train_script}", file=sys.stderr)
        return 2
    if eval_script not in eval_candidates:
        print(f"[ERROR] --eval-script not found in scripts/ candidates: {eval_script}", file=sys.stderr)
        return 2

    branch = args.branch or _run_git(["rev-parse", "--abbrev-ref", "HEAD"]) or "<branch>"

    extra = []
    if args.eval_gpu:
        extra.append(f"  --eval-gpu {args.eval_gpu}")
    if args.show_output:
        extra.append("  --show-output")

    # Build the command lines
    lines = [
        "( \\",
        "set -euo pipefail; \\",
        "git fetch --all --prune; \\",
        f'git checkout "{branch}"; \\',
        "git pull --ff-only; \\",
        "bash scripts/train_eval_shutdown_autodl.sh \\",
        f"  --train-script {train_script} \\",
        f"  --eval-script {eval_script} \\",
        f"  --model-dir-mode {args.model_dir_mode} \\",
        f"  --shutdown-mode {args.shutdown_mode}",
    ]

    # Add extra arguments if present
    if extra:
        # Remove the trailing backslash from shutdown-mode line
        lines[-1] += " \\"
        # Add extra lines
        for i, ex in enumerate(extra):
            if i < len(extra) - 1:
                lines.append(ex + " \\")
            else:
                lines.append(ex + " \\")
    else:
        lines[-1] += " \\"

    # Add closing parenthesis
    lines.append(")")

    print("# Run on remote (tmux is already open on connect)")
    print()
    for line in lines:
        print(line)
    print()
    print("# After it finishes, capture the printed EXP_ROOT for pulling artifacts to local.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
