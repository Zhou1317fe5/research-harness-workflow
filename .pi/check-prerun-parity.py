#!/usr/bin/env python3
"""校验 PRERUN skill 各宿主副本仅启动块不同；脚本资源仍指向 canonical 实现。"""
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


def check(codex: Path, pi: Path, agents: Path | None = None) -> list[str]:
    canonical = protocol(codex.read_text())
    errors = []
    other = protocol(pi.read_text())
    if canonical != other:
        errors.append("PRERUN protocol drift：除 launcher 外的内容必须保持一致\n" + "".join(
            difflib.unified_diff(canonical.splitlines(True), other.splitlines(True),
                                 fromfile="Codex protocol", tofile="Pi protocol")))
    helpers = pi.parent / "scripts"
    if not helpers.is_symlink() or helpers.resolve() != (codex.parent / "scripts").resolve():
        errors.append("Pi scripts 必须软链接到 canonical PRERUN scripts")
    # .agents/skills 是 canonical 同步源（通常为指向 .codex/skills 的链接）：
    # 其 launcher 必须与 codex 运行副本逐字一致，否则经该路径读到 skill 的宿主
    # 会拿到错误的 reviewer 后端。
    if agents is not None:
        agents_path = agents
        if not agents_path.exists():
            errors.append(f".agents PRERUN skill 缺失: {agents_path}")
        else:
            agents_text = agents_path.read_text()
            codex_text = codex.read_text()
            if agents_path.resolve() != codex.resolve() and agents_text != codex_text:
                errors.append(
                    ".agents PRERUN skill 必须与 canonical codex 副本逐字一致（含 launcher）\n" + "".join(
                        difflib.unified_diff(codex_text.splitlines(True), agents_text.splitlines(True),
                                             fromfile="Codex SKILL.md", tofile=".agents SKILL.md")))
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
    parser.add_argument("--agents", type=Path,
                        default=ROOT / ".agents/skills/pre-run-implementation-review/SKILL.md")
    parser.add_argument("--self-test", action="store_true", help="额外在临时副本中验证防漂移检查")
    args = parser.parse_args()
    try:
        errors = check(args.codex, args.pi, args.agents)
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
