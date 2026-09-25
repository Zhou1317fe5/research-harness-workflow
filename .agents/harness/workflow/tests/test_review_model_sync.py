#!/usr/bin/env python3
"""审查模型"复述 = canonical"约束。

`review_model.py` 是每个宿主审查模型的唯一事实源。但宿主工具仍需复述具体值：
Pi 的 `scientific-reviewer` agent frontmatter 决定子代理实际用哪个模型，PRERUN
launcher 的 `--model` 则成为 verdict 的 `requested_model`。这两处若在换模型时漏改，
门禁会在运行时以 fail-closed 拒收（`analysis_requested_model_invalid` /
`gate_provenance.verdict_artifact_model_mismatch`），但那要等到跑完审查才发现。
本测试把这类漏改提前到提交前。
"""
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
REVIEW_MODEL_PATH = ROOT / ".agents/harness/review_model.py"
PRERUN_LAUNCHERS = (
    ROOT / ".codex/skills/pre-run-implementation-review/SKILL.md",
    ROOT / ".claude/skills/pre-run-implementation-review/SKILL.md",
    ROOT / ".pi/skills/pre-run-implementation-review/SKILL.md",
)
PI_REVIEWER_AGENT = ROOT / ".pi/agents/scientific-reviewer.md"
LAUNCHER_START = "<!-- reviewer-launcher:start -->"
LAUNCHER_END = "<!-- reviewer-launcher:end -->"


def _load_review_model():
    spec = importlib.util.spec_from_file_location("review_model", REVIEW_MODEL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load review model registry: {REVIEW_MODEL_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError(f"{path} 缺少 frontmatter")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    raise AssertionError(f"{path} 的 frontmatter 未闭合")


def _launcher_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.count(LAUNCHER_START) != 1 or text.count(LAUNCHER_END) != 1:
        raise AssertionError(f"{path} 的 reviewer-launcher 标记数量异常")
    return text.split(LAUNCHER_START)[1].split(LAUNCHER_END)[0]


def _launcher_option(block: str, option: str) -> str:
    match = re.search(rf"{re.escape(option)}\s+(\S+)", block)
    if match is None:
        raise AssertionError(f"launcher 缺少 {option}")
    return match.group(1)


class ReviewModelSyncTests(unittest.TestCase):
    def setUp(self):
        self.review_model = _load_review_model()

    def test_pi_reviewer_agent_frontmatter_matches_canonical_value(self):
        fields = _frontmatter(PI_REVIEWER_AGENT)
        self.assertEqual(fields["model"], self.review_model.model_for_host("pi"))
        self.assertEqual(fields["thinking"], self.review_model.REVIEW_THINKING)

    def test_each_prerun_launcher_requests_an_approved_model_for_its_backend(self):
        for path in PRERUN_LAUNCHERS:
            with self.subTest(path=path.name, host=path.parent.parent.parent.name):
                block = _launcher_block(path)
                backend = _launcher_option(block, "--backend")
                model = _launcher_option(block, "--model")
                self.assertIn(backend, self.review_model.MODELS, f"未知 backend: {backend}")
                # codex CLI 只接受裸模型名，Pi 接受带 thinking 后缀的调用名，因此断言
                # "属于该宿主的获批身份"而不是与 review_job_model() 精确相等。
                self.assertTrue(
                    self.review_model.is_accepted_model_identity(model, backend),
                    f"{path.name} 的 --model={model} 不是 {backend} 宿主的获批身份",
                )


if __name__ == "__main__":
    unittest.main()
