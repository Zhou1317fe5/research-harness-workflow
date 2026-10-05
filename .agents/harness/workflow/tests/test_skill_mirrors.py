#!/usr/bin/env python3
"""skill 镜像与宿主副本一致性检查的回归。

覆盖：符号链接镜像被接受、镜像内容漂移被发现、.pi 副本仅在宿主块内不同通过、
宿主块之外漂移被发现、.pi 里出现无 canonical 对应的目录被发现。
"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / ".agents/harness/workflow/check_skill_mirrors.py"
CANONICAL = ROOT / ".codex/skills"
NAME = "pre-run-implementation-review"
START = "<!-- reviewer-launcher:start -->"
END = "<!-- reviewer-launcher:end -->"


def _load():
    spec = importlib.util.spec_from_file_location("check_skill_mirrors", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SkillMirrorTests(unittest.TestCase):
    def setUp(self):
        self.module = _load()
        self.temp = tempfile.TemporaryDirectory(prefix="skill-mirrors-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        shutil.copytree(CANONICAL, self.root / ".codex/skills", symlinks=True)
        (self.root / ".pi/skills").mkdir(parents=True)
        for mirror in (".agents/skills", ".claude/skills"):
            (self.root / mirror).parent.mkdir(parents=True, exist_ok=True)
            (self.root / mirror).symlink_to(Path("../.codex/skills"))
        self.skill = self.root / ".pi/skills" / NAME
        (self.skill / "scripts").parent.mkdir(parents=True)
        (self.skill / "scripts").symlink_to(Path("../../../.codex/skills") / NAME / "scripts")
        self.canonical_text = (self.root / ".codex/skills" / NAME / "SKILL.md").read_text(encoding="utf-8")
        (self.skill / "SKILL.md").write_text(self.canonical_text, encoding="utf-8")

    def _write_pi(self, text: str) -> None:
        (self.skill / "SKILL.md").write_text(text, encoding="utf-8")

    def test_identical_copies_pass(self):
        self.assertEqual(self.module.check(self.root), [])

    def test_host_block_difference_passes(self):
        before, remainder = self.canonical_text.split(START)
        _block, after = remainder.split(END)
        self._write_pi(before + START + "\n<pi launcher>\n" + END + after)
        self.assertEqual(self.module.check(self.root), [])

    def test_body_drift_is_reported(self):
        self._write_pi(self.canonical_text + "\nAdditional scientific rule.\n")
        errors = self.module.check(self.root)
        self.assertTrue(errors, "宿主块之外的漂移必须被发现")
        self.assertIn("SKILL.md", errors[0])

    def test_missing_scripts_symlink_is_reported(self):
        scripts = self.skill / "scripts"
        scripts.unlink()
        scripts.mkdir()
        (scripts / "prerun_route.py").write_text("# copy\n", encoding="utf-8")
        errors = self.module.check(self.root)
        self.assertTrue(any("scripts" in error for error in errors), errors)

    def test_mirror_content_drift_is_reported(self):
        mirror = self.root / ".agents/skills"
        mirror.unlink()
        shutil.copytree(self.root / ".codex/skills", mirror, symlinks=True)
        (mirror / NAME / "SKILL.md").write_text("drifted\n", encoding="utf-8")
        errors = self.module.check(self.root)
        self.assertTrue(any(".agents/skills" in error for error in errors), errors)

    def test_pi_skill_without_canonical_is_reported(self):
        orphan = self.root / ".pi/skills/orphan-skill"
        orphan.mkdir()
        (orphan / "SKILL.md").write_text("orphan\n", encoding="utf-8")
        errors = self.module.check(self.root)
        self.assertTrue(any("orphan-skill" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
