#!/usr/bin/env python3
"""校验各宿主读到的 skill 副本一致，且差异只落在被显式允许的宿主块内。

背景
----
`.codex/skills/` 是 canonical 源；`.agents/skills` 与 `.claude/skills` 是指向它的
符号链接，用于宿主发现，不是独立副本。Pi 不走符号链接：它按路径读
`.pi/skills/<name>/SKILL.md`。因此在 Pi 上，一个 skill 可能同时存在两份文本
（canonical 与 `.pi/` 副本），而**没有任何机器检查能发现两侧漂移**。

历史事故形态：任务执行中同步一次 `.pi/skills`，同一个 Mission 的前半段与后半段
因此遵循两套规则，事后无法复现当时的判定标准。

检查项
------
1. `.agents/skills` 与 `.claude/skills` 必须解析到 `.codex/skills`（或与其逐字节一致）。
2. `.pi/skills/<name>/` 存在时：
   - `scripts` 必须是软链接并指向 canonical 同名目录；
   - `SKILL.md` 与 canonical 的差异，去除允许的宿主块后必须为空。
3. 允许的宿主块由 skill 自己在文本中声明：`<!-- host-block:start --> ... <!-- host-block:end -->`。
   现有 `pre-run-implementation-review` 使用的 `reviewer-launcher` 块被等价识别，
   不再需要改造既有文本。

本脚本只读；不修改任何 skill 文件。
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CANONICAL = ".codex/skills"
MIRROR_DIRS = (".agents/skills", ".claude/skills")
PI_DIR = ".pi/skills"

# 既有的宿主专属块名；新 skill 若要声明差异，请用统一的 host-block 标记。
HOST_BLOCK_NAMES = ("host-block", "reviewer-launcher")

_BLOCK_RE = {
    name: (re.compile(rf"<!--\s*{name}:start\s*-->"), re.compile(rf"<!--\s*{name}:end\s*-->"))
    for name in HOST_BLOCK_NAMES
}


class ParityError(Exception):
    """配置或输入不满足检查前提。"""


def strip_host_blocks(text: str, name: str) -> str:
    """把宿主块内容替换为占位符，保留块的存在性与前后文。"""
    start_re, end_re = _BLOCK_RE[name]
    if len(start_re.findall(text)) != 1 or len(end_re.findall(text)) != 1:
        raise ParityError(f"必须存在且只存在一个 {name} 块")
    before, remainder = start_re.split(text)
    block, after = end_re.split(remainder)
    if not block.strip():
        raise ParityError(f"{name} 块不能为空")
    return before + f"<{name}>" + after


def strip_all_host_blocks(text: str) -> str:
    present = [name for name in HOST_BLOCK_NAMES if _BLOCK_RE[name][0].search(text)]
    for name in present:
        text = strip_host_blocks(text, name)
    return text.replace("\r\n", "\n")


def check_mirror_dir(root: Path, mirror: Path) -> list[str]:
    """镜像目录必须解析到 canonical，内容不得自行漂移。"""
    canonical = root / CANONICAL
    errors: list[str] = []
    if not mirror.exists():
        errors.append(f"{mirror.relative_to(root)} 缺失")
        return errors
    target_root = mirror.resolve()
    if target_root == canonical.resolve():
        return errors
    canonical_files = {
        path.relative_to(canonical).as_posix(): path
        for path in canonical.rglob("*")
        if path.is_file()
    }
    for relative, source in sorted(canonical_files.items()):
        target = mirror / relative
        if not target.is_file():
            errors.append(f"{mirror.relative_to(root)}/{relative} 缺失（canonical 有该文件）")
        elif target.read_bytes() != source.read_bytes():
            errors.append(f"{mirror.relative_to(root)}/{relative} 与 canonical 内容不一致")
    return errors


def check_pi_skill(root: Path, name: str, canonical_skill: Path, pi_skill: Path) -> list[str]:
    errors: list[str] = []
    label = pi_skill.relative_to(root).as_posix()
    scripts = pi_skill / "scripts"
    canonical_scripts = canonical_skill / "scripts"
    if scripts.exists() or canonical_scripts.exists():
        if not scripts.is_symlink():
            errors.append(f"{label}/scripts 必须是软链接到 canonical scripts")
        elif scripts.resolve() != canonical_scripts.resolve():
            errors.append(f"{label}/scripts 指向 {scripts.resolve()}，不是 canonical {canonical_scripts.resolve()}")
    skill_file = pi_skill / "SKILL.md"
    canonical_file = canonical_skill / "SKILL.md"
    if not skill_file.is_file():
        errors.append(f"{label}/SKILL.md 缺失")
        return errors
    try:
        canonical_text = strip_all_host_blocks(canonical_file.read_text(encoding="utf-8"))
        pi_text = strip_all_host_blocks(skill_file.read_text(encoding="utf-8"))
    except ParityError as error:
        errors.append(f"{label}: {error}")
        return errors
    if canonical_text != pi_text:
        errors.append(
            f"{label}/SKILL.md 在宿主块之外与 canonical 不一致\n"
            + "".join(
                difflib.unified_diff(
                    canonical_text.splitlines(True),
                    pi_text.splitlines(True),
                    fromfile=f"canonical {name}",
                    tofile=f"pi {name}",
                )
            )
        )
    return errors


def check(root: Path) -> list[str]:
    canonical = root / CANONICAL
    if not canonical.is_dir():
        raise ParityError(f"canonical skill 目录不存在: {canonical}")
    errors: list[str] = []
    for mirror in MIRROR_DIRS:
        errors.extend(check_mirror_dir(root, root / mirror))
    pi_dir = root / PI_DIR
    if pi_dir.is_dir():
        for entry in sorted(pi_dir.iterdir()):
            if not entry.is_dir():
                continue
            canonical_skill = canonical / entry.name
            if not canonical_skill.is_dir():
                errors.append(
                    f"{entry.relative_to(root)} 没有对应的 canonical skill {CANONICAL}/{entry.name}"
                )
                continue
            errors.extend(check_pi_skill(root, entry.name, canonical_skill, entry))
    return errors


def self_test(root: Path) -> None:
    """在临时副本上验证：单侧漂移会被发现，仅宿主块不同时通过。"""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory(prefix="skill-mirror-parity-") as directory:
        sandbox = Path(directory) / "repo"
        shutil.copytree(root / CANONICAL, sandbox / CANONICAL, symlinks=True)
        (sandbox / PI_DIR).mkdir(parents=True)
        name = "pre-run-implementation-review"
        source = sandbox / CANONICAL / name
        destination = sandbox / PI_DIR / name
        destination.mkdir()
        (destination / "scripts").symlink_to(Path("../../../") / CANONICAL / name / "scripts")
        canonical_text = (source / "SKILL.md").read_text(encoding="utf-8")
        (destination / "SKILL.md").write_text(canonical_text, encoding="utf-8")
        (sandbox / MIRROR_DIRS[0]).parent.mkdir(parents=True, exist_ok=True)
        (sandbox / MIRROR_DIRS[0]).symlink_to(Path("../") / CANONICAL)
        (sandbox / MIRROR_DIRS[1]).parent.mkdir(parents=True, exist_ok=True)
        (sandbox / MIRROR_DIRS[1]).symlink_to(Path("../") / CANONICAL)
        if check(sandbox):
            raise ParityError("self-test：两侧一字不差时不应报错")

        # 宿主块内不同 -> 通过
        before, remainder = canonical_text.split("<!-- reviewer-launcher:start -->")
        _block, after = remainder.split("<!-- reviewer-launcher:end -->")
        (destination / "SKILL.md").write_text(
            before + "<!-- reviewer-launcher:start -->\n<different launcher>\n"
            "<!-- reviewer-launcher:end -->" + after,
            encoding="utf-8",
        )
        if check(sandbox):
            raise ParityError("self-test：仅宿主块不同不应报错")

        # 宿主块外不同 -> 必须发现
        (destination / "SKILL.md").write_text(
            before + "<!-- reviewer-launcher:start -->\n<different launcher>\n"
            "<!-- reviewer-launcher:end -->" + after + "\nAdditional scientific rule.\n",
            encoding="utf-8",
        )
        if not check(sandbox):
            raise ParityError("self-test：宿主块之外的漂移未被发现")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help="仓库根，默认本脚本所在仓库")
    parser.add_argument("--self-test", action="store_true", help="在临时副本中验证检查本身有效")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    try:
        errors = check(root)
        if args.self_test and not errors:
            self_test(root)
    except ParityError as error:
        print(str(error))
        return 1
    if errors:
        print("\n".join(errors))
        return 1
    print("PASS: skill 镜像与宿主副本一致，差异仅限允许的宿主块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
