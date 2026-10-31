#!/usr/bin/env python3
"""收尾一次性聚合审计：一次跑完所有机械门禁，返回全部错误分组。

替代收尾阶段“发现一个错误→修→重跑”的排雷循环。覆盖：
- 行终态 / Git 绑定 / ingest / claim / result-analysis（evidence_completion_errors）；
- deferred ledger 结构与覆盖（load_csv_deferred）；
- handoff 契约（check_handoff_contract.check_contract，可选）；
- 显式 claim ledger 终态（validate_claim_ledger，可选）。

只读，不写任何状态。有错误时退出码 1，否则 0。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mission_completion import evidence_completion_errors, read_mission_csv  # noqa: E402
from validate_deferred_ledger import load_csv_deferred  # noqa: E402


def audit(
    csv_path: Path,
    *,
    workdir: Path,
    skip_row_ids: frozenset[str] = frozenset(),
    handoff_path: Path | None = None,
    claims_path: Path | None = None,
    stage: str = "closing",
) -> dict[str, list[str]]:
    """stage=closing 跑全部门禁；stage=ingest 只跑入账相关（供 INGEST 行即时自检）。"""
    groups: dict[str, list[str]] = {}
    csv_path = Path(csv_path)
    workdir = Path(workdir)
    try:
        result = read_mission_csv(csv_path)
    except Exception as exc:  # noqa: BLE001 - 统一转成审计输出
        return {"csv": [f"CSV read failed: {exc}"]}
    rows: list[dict[str, str]] = list(result[1]) if result[1] else []
    if stage == "ingest":
        from mission_completion import claim_completion_errors, ingest_completion_errors
        from result_analysis import result_analysis_completion_errors

        groups["ingest"] = list(ingest_completion_errors(csv_path, rows, workdir=workdir))
        groups["claims"] = list(claim_completion_errors(csv_path, rows, workdir=workdir))
        groups["analysis"] = list(result_analysis_completion_errors(csv_path, rows, workdir=workdir))
        groups["layout"] = layout_errors(workdir)
        return {name: items for name, items in groups.items() if items}
    groups["evidence"] = evidence_completion_errors(
        csv_path, rows, workdir=workdir, skip_row_ids=skip_row_ids
    )
    _, _, deferred_errors, _ = load_csv_deferred(csv_path.resolve(), workdir.resolve())
    groups["deferred"] = list(deferred_errors)
    groups["layout"] = layout_errors(workdir)
    if handoff_path is not None:
        from check_handoff_contract import check_contract

        groups["handoff"] = list(check_contract(Path(handoff_path), csv_path, workdir=workdir))
    if claims_path is not None:
        from validate_claim_ledger import validate_ledger

        groups["claims"] = list(
            validate_ledger(Path(claims_path), csv_path, set(), require_terminal=True, workdir=workdir)
        )
    return {name: items for name, items in groups.items() if items}


def layout_errors(workdir: Path) -> list[str]:
    """工作树内嵌套 research_workspace 仓库会让 git_repo 溯源失效，必须提前拦。"""
    errors: list[str] = []
    workspace = Path(workdir) / "research_workspace"
    if not workspace.is_dir() or workspace.is_symlink():
        return errors
    nested_git = workspace / ".git"
    if nested_git.exists():
        errors.append(
            "nested_research_workspace_repo: research_workspace 自带 .git，会让 "
            "git_repo:research_workspace 溯源失效；请复用主仓的 research_workspace（软链或直接引用），"
            "不要在工作树内新建独立仓库。"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一次性聚合收尾机械门禁")
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--skip-rows", default="", help="逗号分隔的行 ID（如仍在进行的 REVIEW 行）")
    parser.add_argument("--handoff", type=Path, default=None)
    parser.add_argument("--claims", type=Path, default=None)
    parser.add_argument("--stage", choices=("closing", "ingest"), default="closing",
                        help="ingest=入账即时自检（只跑 ingest/claims/analysis/layout）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    skip = frozenset(item.strip() for item in args.skip_rows.split(",") if item.strip())
    groups = audit(
        args.csv,
        workdir=args.workdir,
        skip_row_ids=skip,
        handoff_path=args.handoff,
        claims_path=args.claims,
        stage=args.stage,
    )
    total = sum(len(items) for items in groups.values())
    if args.json:
        print(json.dumps({"ok": total == 0, "total": total, "groups": groups}, ensure_ascii=False))
    else:
        if not groups:
            print("closing audit: OK（无门禁错误）")
        for name, items in groups.items():
            print(f"[{name}] {len(items)} 项")
            for item in items:
                print(f"  - {item}")
        print(f"closing audit: {'OK' if total == 0 else f'{total} 项错误'}（一次性修完再重跑即可）")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
