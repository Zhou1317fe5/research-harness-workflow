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
# 注意：**不**把 .agents/、.pi/、.codex/ 整体排除——其中存在真实的项目 adapter、
# 结果归属与安全相关代码，排除它们会放过真实行为变更。那里面的非行为文件由
# 后缀与路径组件规则覆盖（如 SKILL.md / tests/ / __pycache__）。
EXCLUDED_DIR_PREFIXES = (
    "docs/",
    "issues/",
    "remote_artifacts/",
    "research_workspace/",
)

# 路径组件：出现以下任一组件的路径不承载科学行为。
EXCLUDED_PATH_SEGMENTS = frozenset(
    {
        "__pycache__",
        "test",
        "tests",
    }
)

# 文件后缀：仅排除明确为散文、文档或编译产物的格式。
# **不**排除 .txt / .jsonl / .log：它们完全可能是 prompt、数据清单或输入，
# 按 fail-closed 应算作行为相关。
EXCLUDED_SUFFIXES = (
    ".md",
    ".rst",
    ".pyc",
    ".pyo",
)


class FingerprintError(RuntimeError):
    """无法确定某个 commit 的行为指纹。"""


def normalize_path(path: str) -> str:
    """仓库相对路径规范化。

    只去掉真正的当前目录前缀 "`./`"，**不**用 `lstrip`：后者会把 `.env`、`.gitignore`
    这类隐藏路径的前导点也吃掉。
    """
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_behavior_path(path: str) -> bool:
    """该路径是否承载科学行为。

    默认 True（fail-closed）：只有命中显式排除规则才为 False。
    """
    if not path:
        return False
    normalized = normalize_path(path)
    if not normalized:
        return False
    name = normalized.rsplit("/", 1)[-1]
    if not name or name in {".", ".."}:
        return False
    if normalized.startswith(EXCLUDED_DIR_PREFIXES):
        return False
    segments = normalized.split("/")
    if any(segment in EXCLUDED_PATH_SEGMENTS for segment in segments[:-1]):
        return False
    # 末尾的 tests/test 目录（如 src/tests）同样是测试代码。
    if len(segments) > 1 and segments[-2] in EXCLUDED_PATH_SEGMENTS:
        return False
    return not name.endswith(EXCLUDED_SUFFIXES)


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def commit_entries(repo_root: Path, commit: str) -> dict[str, str]:
    """返回 commit 下的 ``{path: identity}``。

    identity 包含 blob 哈希与文件模式：**可执行位变化也是行为变化**。子模块
    （gitlink）不忽略，其 identity 记录目标提交，因此子模块指针移动会被正确识别。
    """
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
        mode, object_type, sha = fields[0], fields[1], fields[2]
        if object_type == "blob":
            entries[path] = f"{mode}:{sha}"
        elif object_type == "commit":
            # 子模块：目标提交变了就是行为变了。
            entries[path] = f"{mode}:commit:{sha}"
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


def coverage_note() -> str:
    """指纹能保证什么的准确描述，供错误信息与文档引用。

    它证明的是「这些路径的文件内容与模式一致」，**不**等于「科学行为必然相同」：
    行为还可能依赖环境、数据、外部服务与未纳入版本控制的状态。
    """
    return (
        "行为指纹只保证被覆盖路径的文件内容与模式一致，"
        "不保证科学行为必然相同（环境、数据与外部状态不在其中）"
    )


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
