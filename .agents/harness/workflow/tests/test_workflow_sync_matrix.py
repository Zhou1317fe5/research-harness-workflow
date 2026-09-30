#!/usr/bin/env python3
"""场景 B：workflow_sync 全状态机回归。

pull/push 两方向各覆盖：
diverged / deleted / target_ahead / template_only / symlink / 新文件。

模板仓与目标仓都是真实 GitRepo，`workflow_sync.py` 作为工具本身直接用
`--template/--target/--ref` 参数走 CLI 子进程测试（真实调用，不 mock）。
"""
from __future__ import annotations

import json
import os
import shutil  # used for .rmtree in _reseed_target (unused after the swap below, kept for lint).
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SYNC = ROOT / ".agents/harness/workflow/workflow_sync.py"


def _git(repo: Path, *args: str, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check, cwd=cwd,
    )


class WorkflowSyncMatrixTests(unittest.TestCase):
    """每个状态都走 pull/push 一遍；状态经 collect + CLI 验证。"""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="workflow-sync-matrix-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # 模板仓（template）与目标仓（target）均建在 tmp 下；
        # 维护各自的 公共文件种子（.agents/skills、.codex/skills、.claude/skills、.pi、AGENTS.md、CLAUDE.md）
        self.template = self.root / "template"
        self.target = self.root / "target"
        self.template.mkdir()
        self.target.mkdir()
        self._init_repo(self.template)
        self._init_repo(self.target)
        # F-001 修复回归：.claude/skills 是 .codex/skills 的 symlink，保持本源
        # 用 codex 作为 canonical ，claude 作为 symlink（荐用_codex 唯一写入端）。
        self._seed_public_skills(self.template)
        self._seed_public_skills(self.target)
        self._commit_repo(self.template, "template: seed")
        # 仅模板仓有基础 commit；target 先 seed 再 commit，以便所有用例可以直接比较/添加/删除。
        self._commit_repo(self.target, "target: seed")
        # workflow_sync CLI 入口
        self.sync = SYNC
        if not self.sync.is_file():
            self.skipTest("workflow_sync.py 不在本工作树中")

    # -------------------------------------------------------------- helpers
    def _init_repo(self, repo: Path) -> None:
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "workflow-sync-matrix")
        _git(repo, "config", "user.email", "workflow-sync-matrix@example.invalid")

    def _commit_repo(self, repo: Path, msg: str) -> None:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", msg, cwd=repo)

    def _seed_public_skills(self, repo: Path) -> None:
        (repo / ".agents/skills/demo").mkdir(parents=True, exist_ok=True)
        (repo / ".agents/skills/demo/SKILL.md").write_text("# demo skill\n\nseed content\n")
        (repo / ".agents/harness/workflow").mkdir(parents=True, exist_ok=True)
        (repo / ".agents/harness/workflow/demo.py").write_text("print('demo')\n")
        (repo / ".codex").mkdir()
        (repo / ".codex/demo.md").write_text("# codex\n")
        # AGENTS.md 带公共段与项目段分隔符
        (repo / "AGENTS.md").write_text(
            "> 以下两节是**每个项目必须自行填写**的部分\n## 公共协议\n- 模板\n",
        )
        (repo / ".pi").mkdir()
        (repo / ".pi/demo.json").write_text("{}\n")
        # F-001：.claude/skills 是 .codex/skills 的 symlink；写入 .codex/skills
        # 时同源经 ../.codex/skills 通终。
        (repo / ".codex/skills").mkdir(parents=True, exist_ok=True)
        (repo / ".claude").mkdir(parents=True, exist_ok=True)
        os.symlink("../.codex/skills", repo / ".claude/skills")

    def _collect(self, direction: str):
        """运行 workflow_sync status，返回 stdout；退出码为 0。"""
        return subprocess.run(
            [
                sys.executable, str(self.sync), "status",
                "--template", str(self.template),
                "--target", str(self.target),
                "--ref", "main",
            ],
            capture_output=True, text=True, cwd=self.root,
        )

    def _do(self, direction: str):
        """运行 workflow_sync pull 或 push，返回 stdout；如果是 dry-run，方向以参数为准。"""
        argv = [sys.executable, str(self.sync), direction,
                "--template", str(self.template),
                "--target", str(self.target),
                "--ref", "main"]
        return subprocess.run(argv, capture_output=True, text=True, cwd=self.root)

    def _reseed_target(self) -> None:
        """把 target 完全重置成新的 seed（一次 commit），用在 deleted/diverged 前。"""
        subprocess.run(["rm", "-rf", str(self.target)], check=False)
        self.target.mkdir()
        self._init_repo(self.target)
        self._seed_public_skills(self.target)
        self._commit_repo(self.target, "seed: again")

    # -------------------------------------------------------------- pull
    def test_pull_diverged_noop(self):
        """diverged：两边都改过且互不包含；pull 必须人工决策，不动。"""
        self._reseed_target()
        # 双方都修改 .agents/skills/demo/SKILL.md 各加一行，制造 diverged。
        for repo, line in ((self.template, "template-change\n"),
                           (self.target, "target-change\n")):
            p = repo / ".agents/skills/demo/SKILL.md"
            prev = p.read_text()
            p.write_text(line + prev)
            self._commit_repo(repo, f"{repo.name}: diverge SKILL.md")
        status = self._collect("pull").stdout
        self.assertIn("diverged", status)
        self.assertIn("demo/SKILL.md", status)
        result = self._do("pull")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "没有需要 pull 的公共文件。")
        # 目标工作树里 SKILL.md 不该被 pull 覆盖。
        self.assertEqual(
            self.target.joinpath(".agents/skills/demo/SKILL.md").read_text(),
            "target-change\n# demo skill\n\nseed content\n",
        )

    def test_pull_deleted_manual_only(self):
        """deleted：目标没有该文件且历史中有过删除，pull 必须人工决策，不动。"""
        # 目标侧删除 .agents/skills/demo/SKILL.md（需要先让 target 有该文件）。
        self._reseed_target()
        self.target.joinpath(".agents/skills/demo/SKILL.md").unlink()
        self._commit_repo(self.target, "target: delete demo skill")
        status = self._collect("pull").stdout
        self.assertIn("deleted", status)
        self.assertIn("demo/SKILL.md", status)
        result = self._do("pull")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "没有需要 pull 的公共文件。")
        self.assertFalse(self.target.joinpath(".agents/skills/demo/SKILL.md").is_file(),
                         "deleted must be left for manual review")

    def test_pull_template_ahead(self):
        """template_ahead：目标内容等于模板历史版本；pull 覆盖到最新。"""
        # 模板侧先改 .agents/skills/demo/SKILL.md 加一行并提交。
        # 再把目标侧手动回退到模板历史版本（同一种子内容），制造 template_ahead。
        seed = self.template.joinpath(".agents/skills/demo/SKILL.md").read_text()
        self.template.joinpath(".agents/skills/demo/SKILL.md").write_text(seed + "v2\n")
        self._commit_repo(self.template, "template: evolve demo skill")
        result = self._do("pull")
        self.assertEqual(result.returncode, 0)
        self.assertIn("template_ahead", result.stdout)
        self.assertIn("demo/SKILL.md", result.stdout)
        self.assertEqual(
            self.target.joinpath(".agents/skills/demo/SKILL.md").read_text(),
            seed + "v2\n",
            "pull must overwrite with the newer template content",
        )

    def test_pull_template_only(self):
        """template_only：模板独有，目标从未有过；pull 应新增到目标。"""
        p = self.template / ".agents/skills/demo/new_file.py"
        p.write_text("# new\n")
        self._commit_repo(self.template, "template: add new common file")
        result = self._do("pull")
        self.assertEqual(result.returncode, 0)
        self.assertIn("template_only", result.stdout)
        self.assertIn("new_file.py", result.stdout)
        self.assertEqual(
            self.target.joinpath(".agents/skills/demo/new_file.py").read_text(),
            "# new\n",
            "pull must create the template-only file",
        )

    def test_pull_symlink_sync(self):
        """symlink（F-001 修复回归）：模板的 .claude/skills symlink 内容被 pull 到目标同源。"""
        # 模板侧新增一个 skill 文件；目标侧 .claude/skills 在 symlink 位置
        # sync 应当使目标侧该项改动也能通过 ./claude/skills/../.codex/skills 访问到。
        new_skill = self.template / ".codex/skills/f001-symlink-case/SKILL.md"
        new_skill.parent.mkdir(parents=True)
        new_skill.write_text("# F-001 symlink sync check\n")
        self._commit_repo(self.template, "template: add F-001 symlink case")
        result = self._do("pull")
        self.assertEqual(result.returncode, 0)
        # F-001 修复保证 .claude/skills symlink 物理同源：目标 .claude/skills ...
        mirror = self.target / ".claude/skills/f001-symlink-case/SKILL.md"
        self.assertTrue(mirror.is_file(), ".claude/skills symlink must receive the file via .codex/skills")
        self.assertEqual(
            mirror.read_text(), new_skill.read_text(),
            "symlink sync must write the same bytes through the symlink",
        )
        # 目标 .claude/skills 应仍然是 symlink，未被覆盖。
        self.assertTrue(
            (self.target / ".claude/skills").is_symlink(),
            ".claude/skills symlink must remain a symlink after pull",
        )

    def test_pull_always_new_file(self):
        """new file：两边都没有过且模板新增；template_only 处理并新增。"""
        p = self.template / ".agents/skills/demo/brand_new_from_template.md"
        p.write_text("# brand new\n")
        self._commit_repo(self.template, "template: brand new")
        result = self._do("pull")
        self.assertEqual(result.returncode, 0)
        self.assertIn("brand_new_from_template.md", result.stdout)
        self.assertTrue(
            self.target.joinpath(".agents/skills/demo/brand_new_from_template.md").is_file(),
            "new template file must be pulled into target",
        )

    # -------------------------------------------------------------- push
    def test_push_diverged_noop(self):
        """diverged push：两边都改过且互不包含，push 不应静默覆盖。"""
        self._reseed_target()
        for repo, line in ((self.template, "template-change\n"),
                           (self.target, "target-change\n")):
            p = repo / ".agents/skills/demo/SKILL.md"
            prev = p.read_text()
            p.write_text(line + prev)
            self._commit_repo(repo, f"{repo.name}: diverge SKILL.md")
        status = self._collect("push").stdout
        self.assertIn("diverged", status)
        self.assertIn("demo/SKILL.md", status)
        result = self._do("push")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "没有需要 push 的公共文件。")
        self.assertEqual(
            self.target.joinpath(".agents/skills/demo/SKILL.md").read_text(),
            "target-change\n# demo skill\n\nseed content\n",
        )
        self.assertEqual(
            self.template.joinpath(".agents/skills/demo/SKILL.md").read_text(),
            "template-change\n# demo skill\n\nseed content\n",
        )

    def test_push_deleted_manual_only(self):
        """push deleted：目标删除了公共文件，push 不能恢复。"""
        self._reseed_target()
        self.target.joinpath(".agents/skills/demo/SKILL.md").unlink()
        self._commit_repo(self.target, "target: delete demo skill (push path)")
        status = self._collect("push").stdout
        self.assertIn("deleted", status)
        self.assertIn("demo/SKILL.md", status)
        result = self._do("push")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "没有需要 push 的公共文件。")
        # 模板侧仍保留原件。
        self.assertTrue(
            self.template.joinpath(".agents/skills/demo/SKILL.md").is_file(),
            "deleted must not propagate via push",
        )

    def test_push_target_ahead(self):
        """push target_ahead：目标侧领先，push 覆盖模板。"""
        self.target.joinpath(".agents/skills/demo/SKILL.md").write_text(
            self.template.joinpath(".agents/skills/demo/SKILL.md").read_text() + "target-ahead\n",
        )
        self._commit_repo(self.target, "target: push target ahead")
        result = self._do("push")
        self.assertEqual(result.returncode, 0)
        self.assertIn("target_ahead", result.stdout)
        self.assertIn("demo/SKILL.md", result.stdout)
        self.assertEqual(
            self.template.joinpath(".agents/skills/demo/SKILL.md").read_text(),
            self.target.joinpath(".agents/skills/demo/SKILL.md").read_text(),
            "push must copy target content to template",
        )

    def test_push_target_only(self):
        """push target_only：目标独有；push 推到模板。"""
        p = self.target / ".agents/skills/demo/target_only.py"
        p.write_text("# target-only\n")
        self._commit_repo(self.target, "target: add target-only")
        result = self._do("push")
        self.assertEqual(result.returncode, 0)
        self.assertIn("target_only", result.stdout)
        self.assertIn("target_only.py", result.stdout)
        self.assertEqual(
            self.template.joinpath(".agents/skills/demo/target_only.py").read_text(),
            "# target-only\n",
            "push must propagate target-only file",
        )

    def test_push_symlink_sync(self):
        """push symlink（F-001）：目标经 .claude/skills symlink 更新，push 同步到模板 .codex/skills。"""
        # 目标侧直接写 .codex/skills（canonical 端）增加一个文件（经 .claude/skills symlink 可读）。
        new = self.target / ".codex/skills/f001-push-symlink/SKILL.md"
        new.parent.mkdir(parents=True)
        new.write_text("# F-001 push through symlink\n")
        self._commit_repo(self.target, "target: add F-001 push symlink case")
        result = self._do("push")
        self.assertEqual(result.returncode, 0)
        remote = self.template / ".codex/skills/f001-push-symlink/SKILL.md"
        self.assertTrue(remote.is_file(), "push must create mirrored file via symlink")
        self.assertEqual(remote.read_text(), new.read_text(),
                         "push must carry the same bytes to template")
        self.assertTrue(
            (self.target / ".claude/skills").is_symlink(),
            ".claude/skills symlink must remain a symlink after push",
        )

    def test_push_always_new_file(self):
        """push new file from target：目标新增，push 到模板。"""
        p = self.target / ".agents/skills/demo/target_added.py"
        p.write_text("# target added\n")
        self._commit_repo(self.target, "target: added")
        result = self._do("push")
        self.assertEqual(result.returncode, 0)
        self.assertIn("target_added.py", result.stdout)
        self.assertTrue(
            self.template.joinpath(".agents/skills/demo/target_added.py").is_file(),
            "push must propagate new target file",
        )


if __name__ == "__main__":
    unittest.main()
