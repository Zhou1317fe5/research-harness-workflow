"""siblingWorktreeRoots 方向修复的等价逻辑回归（TS 逻辑用 node 执行，此处固化契约）。"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TS = ROOT / ".pi/extensions/rrctl-events.ts"


class SiblingWorktreeDirectionTests(unittest.TestCase):
    def test_implementation_no_longer_checks_root_self_suffix(self):
        """旧实现错误地要求 root 自身以 -worktrees 结尾；新实现探测 <root>-worktrees 兄弟目录。"""
        source = TS.read_text(encoding="utf-8")
        self.assertNotIn('base.endsWith(marker)', source)
        self.assertIn('${root}-worktrees', source)

    def test_real_layout_detects_cdfss_worktrees(self):
        """真实布局：root=/home/zhou/cdfss-dinov3 → 兄弟目录 cdfss-dinov3-worktrees 下
        的 worktree 必须被接受。用 node 执行等价逻辑验证。"""
        script = (
            'const {readdirSync, existsSync}=require("fs");'
            'const {join}=require("path");'
            f'const root={json_str(str(ROOT.parent / "cdfss-dinov3"))};'
            'const dir=root+"-worktrees";'
            "try{const roots=readdirSync(dir).filter(n=>!n.startsWith(\".\")).map(n=>join(dir,n))"
            ".filter(c=>existsSync(join(c,\".agents/harness/remote/remote_run.py\")));"
            "console.log(roots.length>0?\"ok\":\"empty\");}catch(e){console.log(\"empty\");}"
        )
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)


def json_str(value: str) -> str:
    import json
    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
