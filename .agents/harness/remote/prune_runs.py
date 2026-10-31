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
import shlex
import subprocess
import sys
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
    """远端列出 RUN 目录、mtime 与各 target 体积（KB）。"""
    lines = ["set -u", f"ROOT={shlex.quote(root)}", 'for d in "$ROOT"/RUN-*; do',
             '  [ -d "$d" ] || continue', '  id=$(basename "$d")',
             '  mtime=$(stat -c %Y "$d" 2>/dev/null || echo 0)',
             '  sizes=""']
    for target in targets:
        lines.append(f'  kb=$(du -sk "$d"/{shlex.quote(target)} 2>/dev/null | cut -f1); sizes="$sizes ${{kb:-0}}"')
    lines += ['  echo "RUNINFO $id $mtime$sizes"', "done"]
    return "\n".join(lines) + "\n"


def build_delete_script(root: str, targets: list[str], run_ids: list[str]) -> str:
    lines = ["set -u", f"ROOT={shlex.quote(root)}"]
    for run_id in run_ids:
        quoted = shlex.quote(run_id)
        for target in targets:
            lines.append(f'rm -rf "$ROOT"/{quoted}/{shlex.quote(target)}')
        lines.append(f'echo "PRUNED ${{ROOT}}/{run_id}"')
    return "\n".join(lines) + "\n"


def parse_list_output(stdout: str, target_count: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = line.split()
        if not parts or parts[0] != "RUNINFO" or len(parts) != 3 + target_count:
            continue
        try:
            mtime = int(parts[2])
            sizes = [int(value) for value in parts[3:]]
        except ValueError:
            continue
        entries.append({"run_id": parts[1], "mtime": mtime, "sizes_kb": sizes})
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理远端 run 目录的可重建大件（保留 output/control）")
    parser.add_argument("--host", required=True, help="ssh 主机别名（如 eve）")
    parser.add_argument("--root", required=True, help="远端 runs 根目录（RUN-* 的父目录）")
    parser.add_argument("--stem", default=None, help="只处理 run_id 含该子串的 run")
    parser.add_argument("--keep", type=int, default=None, help="按 mtime 保留最新 N 个，其余纳入清理")
    parser.add_argument("--older-than-days", type=float, default=None, help="只清理早于 N 天的 run")
    parser.add_argument("--targets", default=",".join(DEFAULT_TARGETS), help="要删除的顶层子目录，默认 repo,stage")
    parser.add_argument("--apply", action="store_true", help="真正删除；缺省仅列出计划")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = parser.parse_args(argv)

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
    selected = select(listing, stem=args.stem, keep=args.keep,
                      older_than_days=args.older_than_days, now=_time.time())
    freed_kb = sum(sum(entry["sizes_kb"]) for entry in selected)
    plan = [{"run_id": e["run_id"], "freed_kb": sum(e["sizes_kb"])} for e in selected]

    if args.apply and selected:
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
