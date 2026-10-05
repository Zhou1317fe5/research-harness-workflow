#!/usr/bin/env python3
"""科学行为指纹：区分「代码的科学行为变了」与「记账 churn 增加了」。

问题
----
原来所有 gate 用 **commit 精确相等** 判断「已审查/已 smoke 的候选是否还有效」。
这条判据把两类变化混为一谈：

- 改了指标实现、数据流、sink —— 科学行为变了，必须重新审查与重跑；
- 改了一段文档、补了测试、提交了 issues/ 记账 —— 科学行为没变，但 commit 变了，
  于是上一轮 smoke 被判失效，必须重跑。

历史后果：一个 Mission 里出现「改文档 -> 候选 commit 变 -> smoke 作废 -> 重跑
smoke -> 又产生新候选」的自循环（fss phase2 单行 68 个 RunID）。

判据
----
以 **行为指纹**替代 commit 相等：对 commit 下所有「行为相关路径」的 (path, blob)
集合取摘要。任何行为相关路径的增删改都会改变指纹；只动被排除路径则不改。

fail-closed
-----------
默认**一切皆行为相关**，只排除显式 deny-list。判不准的东西算作行为变更，因此
指纹不相等时会退回原来更严的语义（要求重新审查/重跑），不会静默放行。

被排除的路径不含科学语义：文档、任务台账、测试、以及生成物（字节码、日志、
progress/summary 产物）。注意 `docs/specs/` 下的已批准 spec 完整性由
`remote_run.py` 的 source_doc 校验单独负责，不靠本指纹。
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

# 目录前缀：这些子树不承载科学行为。
EXCLUDED_DIR_PREFIXES = (
    "docs/",
    "issues/",
    "tests/",
    "test/",
    "remote_artifacts/",
    "research_workspace/",
    ".agents/",
    ".pi/",
    ".codex/",
    ".claude/",
)

# 文件名后缀：测试、文档、生成物。
EXCLUDED_SUFFIXES = (
    ".md",
    ".rst",
    ".txt",
    ".pyc",
    ".pyo",
    ".log",
    ".jsonl",
    ".tmp",
)

# 文件名通配：Python 缓存与常见生成物。
EXCLUDED_NAME_PARTS = (
    "__pycache__",
    ".pyc",
    ".pyo",
)

# 生成物文件名（不带目录），这些是运行副产品，不是源码身份。
EXCLUDED_FILENAMES = frozenset(
    {
        "progress.jsonl",
        "progress.md",
        "summary.json",
        "smoke_summary.json",
        "artifact-index.json",
    }
)


class FingerprintError(RuntimeError):
    """无法确定某个 commit 的行为指纹。"""


def is_behavior_path(path: str) -> bool:
    """该路径是否承载科学行为。

    默认 True（fail-closed）：只有命中显式排除规则才为 False。
    """
    if not path:
        return False
    normalized = path.replace("\\", "/").lstrip("./")
    name = normalized.rsplit("/", 1)[-1]
    if name in EXCLUDED_FILENAMES:
        return False
    if any(part in normalized for part in EXCLUDED_NAME_PARTS):
        return False
    if normalized.startswith(EXCLUDED_DIR_PREFIXES):
        return False
    if name.startswith(".") and name.endswith((".md", ".jsonl", ".json")):
        return False
    return not normalized.endswith(EXCLUDED_SUFFIXES)


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def commit_entries(repo_root: Path, commit: str) -> dict[str, str]:
    """返回 commit 下 ``{path: blob_sha}``（全部被跟踪文件）。"""
    result = _git(repo_root, "ls-tree", "-r", "-z", commit)
    if result.returncode != 0:
        raise FingerprintError(
            f"cannot read tree of {commit}: {result.stderr.strip() or result.returncode}"
        )
    entries: dict[str, str] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if len(fields) < 3 or not path:
            continue
        # 只取 blob；gitlink/submodule 不参与行为指纹。
        if fields[1] != "blob":
            continue
        entries[path] = fields[2]
    return entries


def behavior_entries(repo_root: Path, commit: str) -> dict[str, str]:
    """commit 下所有行为相关路径及其 blob sha。"""
    return {
        path: sha
        for path, sha in commit_entries(repo_root, commit).items()
        if is_behavior_path(path)
    }


def behavior_fingerprint(repo_root: Path, commit: str) -> str:
    """行为指纹：行为相关 (path, blob) 集合的 sha256。"""
    entries = behavior_entries(repo_root, commit)
    payload = "".join(f"{path}\0{entries[path]}\n" for path in sorted(entries))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def behavior_delta(repo_root: Path, base_commit: str, head_commit: str) -> list[str]:
    """两个 commit 之间行为相关路径的差异（新增/修改/删除），已排序。"""
    base = behavior_entries(repo_root, base_commit)
    head = behavior_entries(repo_root, head_commit)
    return sorted(
        path for path in set(base) | set(head) if base.get(path) != head.get(path)
    )


def same_behavior(repo_root: Path, left: str, right: str) -> bool:
    """两个 commit 的科学行为是否一致。"""
    if left == right:
        return True
    return behavior_fingerprint(repo_root, left) == behavior_fingerprint(repo_root, right)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--compare", help="与之比较的 commit，输出差异路径")
    args = parser.parse_args(argv)
    repo = args.repo.expanduser().resolve()
    try:
        if args.compare:
            payload = {
                "left": args.commit,
                "right": args.compare,
                "same_behavior": same_behavior(repo, args.commit, args.compare),
                "behavior_paths": behavior_delta(repo, args.commit, args.compare),
            }
        else:
            payload = {
                "commit": args.commit,
                "behavior_fingerprint": behavior_fingerprint(repo, args.commit),
                "behavior_paths": sorted(behavior_entries(repo, args.commit)),
            }
    except FingerprintError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
