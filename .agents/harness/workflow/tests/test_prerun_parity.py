#!/usr/bin/env python3
"""PRERUN 协议 parity 的常规回归。

复用 `.pi/check-prerun-parity.py` 的检查逻辑，覆盖四种情形：真实副本一致、
单侧正文变化被拒、合法 launcher 差异通过、scripts 必须指向 canonical 实现。
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".pi/check-prerun-parity.py"
CODEX = ROOT / ".codex/skills/pre-run-implementation-review/SKILL.md"
CLAUDE = ROOT / ".claude/skills/pre-run-implementation-review/SKILL.md"
PI = ROOT / ".pi/skills/pre-run-implementation-review/SKILL.md"


def _load_parity_module():
    spec = importlib.util.spec_from_file_location("check_prerun_parity", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load parity checker: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrerunParityTests(unittest.TestCase):
    def setUp(self):
        self.parity = _load_parity_module()
        self.temp = tempfile.TemporaryDirectory(prefix="prerun-parity-")
        self.addCleanup(self.temp.cleanup)

    def _fixture(self) -> tuple[Path, Path]:
        """临时 codex/pi 副本；pi 的 scripts 软链到该 fixture 的 codex scripts。"""
        root = Path(self.temp.name)
        codex = root / "codex" / "SKILL.md"
        pi = root / "pi" / "SKILL.md"
        codex.parent.mkdir()
        pi.parent.mkdir()
        (codex.parent / "scripts").mkdir()
        codex.write_text(CODEX.read_text(encoding="utf-8"), encoding="utf-8")
        pi.write_text(PI.read_text(encoding="utf-8"), encoding="utf-8")
        (pi.parent / "scripts").symlink_to(codex.parent / "scripts", target_is_directory=True)
        return codex, pi

    def test_real_copies_hold_protocol_parity(self):
        self.assertEqual(self.parity.check(CODEX, PI), [])

    def test_claude_body_matches_codex_protocol(self):
        self.assertEqual(
            self.parity.protocol(CLAUDE.read_text(encoding="utf-8")),
            self.parity.protocol(CODEX.read_text(encoding="utf-8")),
        )

    def test_launcher_difference_is_allowed_but_body_drift_is_not(self):
        codex, pi = self._fixture()
        codex_text = codex.read_text(encoding="utf-8")
        pi_text = pi.read_text(encoding="utf-8")
        start, end = self.parity.START, self.parity.END
        self.assertNotEqual(
            codex_text.split(start)[1].split(end)[0],
            pi_text.split(start)[1].split(end)[0],
        )
        self.assertEqual(self.parity.check(codex, pi), [])
        pi.write_text(pi_text + "\nAdditional protocol clause.\n", encoding="utf-8")
        self.assertTrue(self.parity.check(codex, pi))
        codex.write_text(codex_text + "\nAdditional protocol clause.\n", encoding="utf-8")
        self.assertEqual(self.parity.check(codex, pi), [])

    def test_scripts_must_symlink_to_canonical_implementation(self):
        codex, pi = self._fixture()
        (pi.parent / "scripts").unlink()
        (pi.parent / "scripts").mkdir()
        errors = self.parity.check(codex, pi)
        self.assertTrue(any("软链" in error for error in errors), errors)

    def test_missing_marker_is_rejected(self):
        codex, pi = self._fixture()
        pi.write_text(
            PI.read_text(encoding="utf-8").replace(self.parity.END, ""), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            self.parity.check(codex, pi)


if __name__ == "__main__":
    unittest.main()
