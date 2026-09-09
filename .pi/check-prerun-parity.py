#!/usr/bin/env python3
"""校验两个 PRERUN skill 仅启动块不同；脚本资源仍指向 canonical 实现。"""
from __future__ import annotations

import argparse
import difflib
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- reviewer-launcher:start -->"
END = "<!-- reviewer-launcher:end -->"


def protocol(text: str) -> str:
    text = text.replace("\r\n", "\n")
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("必须存在且只存在一个 reviewer-launcher 标记块")
    before, remainder = text.split(START)
    launcher, after = remainder.split(END)
    if not launcher.strip():
        raise ValueError("reviewer launcher 不能为空")
    return before + START + "\n<launcher>\n" + END + after


def check(codex: Path, pi: Path) -> list[str]:
    first, second = protocol(codex.read_text()), protocol(pi.read_text())
    errors = []
    if first != second:
        errors.append("PRERUN protocol drift：除 launcher 外的内容必须保持一致\n" + "".join(
            difflib.unified_diff(first.splitlines(True), second.splitlines(True),
                                 fromfile="Codex protocol", tofile="Pi protocol")))
    helpers = pi.parent / "scripts"
    if not helpers.is_symlink() or helpers.resolve() != (codex.parent / "scripts").resolve():
        errors.append("Pi scripts 必须软链接到 canonical PRERUN scripts")
    return errors


def self_test(codex: Path, pi: Path) -> None:
    """只改临时副本，验证单侧协议变化会失败、同步后会通过。"""
    originals = (codex.read_text(), pi.read_text())
    with tempfile.TemporaryDirectory(prefix="pi-prerun-parity-") as directory:
        first, second = (Path(directory) / name / "SKILL.md" for name in ("codex", "pi"))
        first.parent.mkdir()
        second.parent.mkdir()
        (first.parent / "scripts").mkdir()
        (second.parent / "scripts").symlink_to(first.parent / "scripts", target_is_directory=True)
        for changed, other in ((first, second), (second, first)):
            first.write_text(originals[0])
            second.write_text(originals[1])
            drift = "\nAdditional scientific review constraint.\n"
            changed.write_text(changed.read_text() + drift)
            if not check(first, second):
                raise ValueError("parity self-test 未发现单侧协议变化")
            other.write_text(other.read_text() + drift)
            if check(first, second):
                raise ValueError("parity self-test 未接受同步后的协议")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path,
                        default=ROOT / ".codex/skills/pre-run-implementation-review/SKILL.md")
    parser.add_argument("--pi", type=Path,
                        default=ROOT / ".pi/skills/pre-run-implementation-review/SKILL.md")
    parser.add_argument("--self-test", action="store_true", help="额外在临时副本中验证防漂移检查")
    args = parser.parse_args()
    try:
        errors = check(args.codex, args.pi)
        if args.self_test and not errors:
            self_test(args.codex, args.pi)
    except (ValueError, OSError) as error:
        print(str(error))
        return 1
    if errors:
        print("\n".join(errors))
        return 1
    print("PASS: PRERUN protocol parity; canonical scripts shared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
