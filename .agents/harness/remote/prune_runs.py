#!/usr/bin/env python3
"""清理远端 run 目录中的可重建大件（默认只删 repo/ 与 stage/）。

远端 ``<runs_root>/RUN-*/`` 的构成：
- ``output/``  —— 科学产物与摘要（summary.json / progress.jsonl / evaluate.log / manifest），
  **永远保留**，是 CSV 与 claims 的取证来源；
- ``control/`` —— 控制面状态，**永远保留**；
- ``repo/``    —— 远端代码快照，可由 git 重建（单个 run 可达 70MB+）；
- ``stage/``   —— 打包暂存与中间态（可达 15MB+）。

本工具通过 ssh 在远端就地清理，按 ``--stem`` / ``--keep`` / ``--older-than-days`` 选择目标，
只删 ``--targets`` 指定目录，并在删除前后报告释放体积。默认 dry-run，需 ``--apply`` 才落地。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_TARGETS = ("repo", "stage")
FORBIDDEN_TARGETS = {"", ".", "/", "output", "control"}


def validate_targets(raw: str) -> list[str]:
    targets = [item.strip().strip("/") for item in raw.split(",") if item.strip()]
    if not targets:
        raise ValueError("targets_empty")
    for target in targets:
        if target in FORBIDDEN_TARGETS:
            raise ValueError(f"target_forbidden:{target}")
        if "/" in target or target.startswith(".."):
            raise ValueError(f"target_must_be_top_level_dir:{target}")
    return targets


def build_list_script(root: str, targets: list[str]) -> str:
    """远端列出 RUN 目录、mtime、终态与各 target 体积（KB）。

    state 用 grep/sed 从 control/status.json 提取——内嵌 python3 -c 的引号转义经两层 shell
    必碎（实测全部 unknown），单引号包 grep/sed 模式最稳。
    """
    lines = ["set -u", f"ROOT={shlex.quote(root)}", 'for d in "$ROOT"/RUN-*; do',
             '  [ -d "$d" ] || continue', '  id=$(basename "$d")',
             '  mtime=$(stat -c %Y "$d" 2>/dev/null || echo 0)',
             # grep -o '"state" : "..."'
             "  state=$(grep -o '\"state\"[[:space:]]*:[[:space:]]*\"[^\"]*\"' \"$d/control/status.json\" 2>/dev/null | head -1 | sed 's/.*:[[:space:]]*\"//;s/\"$//')",
             '  state=${state:-unknown}',
             '  sizes=""']
    for target in targets:
        lines.append(f'  kb=$(du -sk "$d"/{shlex.quote(target)} 2>/dev/null | cut -f1); sizes="$sizes ${{kb:-0}}"')
    lines += ['  echo "RUNINFO $id $mtime $state$sizes"', "done"]
    return "\n".join(lines) + "\n"


FAILED_STATES = frozenset({"failed", "aborted", "cancelled", "stopped"})


def build_delete_script(root: str, targets: list[str], run_ids: list[str]) -> str:
    lines = ["set -u", f"ROOT={shlex.quote(root)}"]
    for run_id in run_ids:
        quoted = shlex.quote(run_id)
        for target in targets:
            lines.append(f'rm -rf "$ROOT"/{quoted}/{shlex.quote(target)}')
        lines.append(f'echo "PRUNED ${{ROOT}}/{run_id}"')
    return "\n".join(lines) + "\n"


def build_remove_script(root: str, run_ids: list[str]) -> str:
    """失败 run 整目录删除（--failed 模式；output 无证据价值）。"""
    lines = ["set -u", f"ROOT={shlex.quote(root)}"]
    for run_id in run_ids:
        lines.append(f'rm -rf "$ROOT"/{shlex.quote(run_id)}')
        lines.append(f'echo "PRUNED {shlex.quote(run_id)}"')
    return "\n".join(lines) + "\n"


def parse_list_output(stdout: str, target_count: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = line.split()
        if not parts or parts[0] != "RUNINFO" or len(parts) != 4 + target_count:
            continue
        try:
            mtime = int(parts[2])
            sizes = [int(value) for value in parts[4:]]
        except ValueError:
            continue
        entries.append({"run_id": parts[1], "mtime": mtime, "state": parts[3], "sizes_kb": sizes})
    return entries


def select(entries: list[dict[str, Any]], *, stem: str | None, keep: int | None,
           older_than_days: float | None, now: float) -> list[dict[str, Any]]:
    candidates = [e for e in entries if not stem or stem in e["run_id"]]
    if older_than_days is not None:
        cutoff = now - older_than_days * 86400
        candidates = [e for e in candidates if e["mtime"] <= cutoff]
    if keep is not None and keep >= 0:
        candidates.sort(key=lambda e: e["mtime"], reverse=True)
        candidates = candidates[keep:]
    return sorted(candidates, key=lambda e: e["run_id"])


def run_remote(host: str, script: str, *, timeout: float) -> str:
    completed = subprocess.run(
        ["ssh", "-C", "-o", "BatchMode=yes", host, "bash", "-s"],
        input=script, capture_output=True, text=True, timeout=timeout, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"remote_failed:{completed.returncode}:{completed.stderr.strip()[:300]}")
    return completed.stdout


def prune_local_state(run_ids: list[str], *, apply: bool) -> list[str]:
    """清理本地 rrctl 运行时索引（~/.local/state/rrctl/{runs,client-results,specs}/<RunID>）。

    安全网拦截内联 rm 的场景：失败 run 重跑前的本地索引残留（run_already_indexed 锁）。
    只删精确 RunID 名下的三个子目录，绝不动 specs 之外的其他内容。
    """
    import shutil
    state_root = Path(os.environ.get("RRCTL_STATE_ROOT", Path.home() / ".local/state/rrctl"))
    removed: list[str] = []
    for run_id in run_ids:
        for sub in ("runs", "client-results", "specs"):
            target = state_root / sub / run_id
            if not target.exists():
                continue
            resolved = target.resolve()
            if state_root.resolve() not in resolved.parents:
                continue  # 越界防御：只删 state_root 一层子目录
            if apply:
                shutil.rmtree(resolved)
            removed.append(str(resolved))
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理远端 run 目录的可重建大件（保留 output/control）")
    parser.add_argument("--host", default=None, help="ssh 主机别名（如 eve）；--local-state 模式可省略")
    parser.add_argument("--root", default=None, help="远端 runs 根目录（RUN-* 的父目录）；--local-state 模式可省略")
    parser.add_argument("--stem", default=None, help="只处理 run_id 含该子串的 run")
    parser.add_argument("--keep", type=int, default=None, help="按 mtime 保留最新 N 个，其余纳入清理")
    parser.add_argument("--older-than-days", type=float, default=None, help="只清理早于 N 天的 run")
    parser.add_argument("--targets", default=",".join(DEFAULT_TARGETS), help="要删除的顶层子目录，默认 repo,stage")
    parser.add_argument("--failed", action="store_true",
                        help="失败/中止的 run 整目录删除（含 output/，因其无证据价值）；成功 run 仍只清 targets")
    parser.add_argument("--local-state", action="store_true",
                        help="清理本地 ~/.local/state/rrctl/{runs,client-results,specs}/<RunID> 索引"
                             "（失败 run 重跑前 run_already_indexed 锁的解法）；与 --run-ids 搭配")
    parser.add_argument("--run-ids", default=None, help="逗号分隔的 RunID（--local-state 模式必填）")
    parser.add_argument("--apply", action="store_true", help="真正删除；缺省仅列出计划")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = parser.parse_args(argv)

    if args.local_state:
        if not args.run_ids:
            print("prune rejected: --local-state 需要 --run-ids", file=sys.stderr)
            return 2
        run_ids = [item.strip() for item in args.run_ids.split(",") if item.strip()]
        if not all(re.fullmatch(r"RUN-[A-Za-z0-9._\-]+", item) for item in run_ids):
            print("prune rejected: run_ids_must_be_RUN_idents", file=sys.stderr)
            return 2
        removed = prune_local_state(run_ids, apply=args.apply)
        mode = "APPLIED" if args.apply else "DRY-RUN"
        for item in removed:
            print(f"  {item}")
        print(f"{mode}: 本地索引清理 {len(removed)} 项（runs/client-results/specs 下精确 RunID）")
        return 0
    if not args.host or not args.root:
        print("prune rejected: 远端模式需要 --host 与 --root（或改用 --local-state）", file=sys.stderr)
        return 2

    try:
        targets = validate_targets(args.targets)
    except ValueError as exc:
        print(f"prune rejected: {exc}", file=sys.stderr)
        return 2
    if args.keep is not None and args.keep < 0:
        print("prune rejected: keep_must_be_non_negative", file=sys.stderr)
        return 2

    import time as _time
    listing = parse_list_output(
        run_remote(args.host, build_list_script(args.root, targets), timeout=args.timeout),
        len(targets),
    )
    if args.failed:
        # 失败/中止 run：整目录删除（含 output/，无证据价值）；未到终态的 run 不动。
        selected = [e for e in select(listing, stem=args.stem, keep=args.keep,
                                      older_than_days=args.older_than_days, now=_time.time())
                    if e["state"] in FAILED_STATES]
    else:
        selected = select(listing, stem=args.stem, keep=args.keep,
                          older_than_days=args.older_than_days, now=_time.time())
    freed_kb = sum(sum(entry["sizes_kb"]) for entry in selected)
    plan = [{"run_id": e["run_id"], "freed_kb": sum(e["sizes_kb"])} for e in selected]

    if args.apply and selected:
        if args.failed:
            run_remote(
                args.host,
                build_remove_script(args.root, [e["run_id"] for e in selected]),
                timeout=args.timeout,
            )
        else:
            run_remote(
                args.host,
                build_delete_script(args.root, targets, [e["run_id"] for e in selected]),
                timeout=args.timeout,
            )

    if args.json:
        print(json.dumps({
            "host": args.host, "root": args.root, "targets": targets,
            "applied": bool(args.apply), "runs": plan, "freed_mb": round(freed_kb / 1024, 1),
        }, ensure_ascii=False))
    else:
        mode = "APPLIED" if args.apply else "DRY-RUN"
        for item in plan:
            print(f"  {item['run_id']}  释放 {item['freed_kb']/1024:.1f} MB")
        print(f"{mode}: {len(plan)} 个 run，共 {freed_kb/1024:.1f} MB（targets={','.join(targets)}）")
        if not args.apply:
            print("  加 --apply 才会真正删除；output/ 与 control/ 永远保留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
