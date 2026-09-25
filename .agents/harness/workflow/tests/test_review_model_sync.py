#!/usr/bin/env python3
"""审查模型"单源"约束。

`review_model.py` 是每个宿主审查模型的唯一事实源。宿主工具仍需在少数结构化位置
实际使用该值：Pi 的 `scientific-reviewer` agent frontmatter 决定子代理用哪个模型，
PRERUN launcher 与 closing review 的运行参数则决定 verdict 记录的 `requested_model`。
本测试把两类漏改提前到提交前：

1. 结构化位置必须与 canonical 值一致（frontmatter 精确相等，launcher 不再复述）；
2. 说明性文档不得复述 canonical 值，而应指向 `review_model.py`。

否则换模型时只能等门禁在运行时 fail-closed 拒收（`analysis_requested_model_invalid`
/ `gate_provenance.verdict_artifact_model_mismatch`）才发现。
"""
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
REVIEW_MODEL_PATH = ROOT / ".agents/harness/review_model.py"
PI_REVIEWER_AGENT = ROOT / ".pi/agents/scientific-reviewer.md"
LAUNCHER_START = "<!-- reviewer-launcher:start -->"
LAUNCHER_END = "<!-- reviewer-launcher:end -->"
PRERUN_LAUNCHERS = (
    ROOT / ".codex/skills/pre-run-implementation-review/SKILL.md",
    ROOT / ".claude/skills/pre-run-implementation-review/SKILL.md",
    ROOT / ".pi/skills/pre-run-implementation-review/SKILL.md",
)
# 说明性文档与注释：必须指向 canonical 源，不得复述具体模型值。
NON_RESTATING_FILES = (
    ROOT / ".codex/skills/post-run-result-analysis/SKILL.md",
    ROOT / ".claude/skills/post-run-result-analysis/SKILL.md",
    ROOT / ".codex/skills/post-run-result-analysis/scripts/validate_result_analysis.py",
    ROOT / ".claude/skills/post-run-result-analysis/scripts/validate_result_analysis.py",
    ROOT / ".codex/skills/mission-csv-execute/csv-schema.md",
    ROOT / ".claude/skills/mission-csv-execute/csv-schema.md",
    ROOT / ".codex/skills/mission-csv-execute/references/closing-review.md",
    ROOT / ".claude/skills/mission-csv-execute/references/closing-review.md",
    ROOT / ".codex/skills/mission-approved-doc/SKILL.md",
    ROOT / ".claude/skills/mission-approved-doc/SKILL.md",
    ROOT / ".agents/harness/config/review_contract.example.toml",
    *PRERUN_LAUNCHERS,
)


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


def _without_launcher_blocks(text: str) -> str:
    while LAUNCHER_START in text and LAUNCHER_END in text:
        head, rest = text.split(LAUNCHER_START, 1)
        _, tail = rest.split(LAUNCHER_END, 1)
        text = head + tail
    return text


class ReviewModelSyncTests(unittest.TestCase):
    def setUp(self):
        self.review_model = _load_review_model()

    def test_pi_reviewer_agent_frontmatter_matches_canonical_value(self):
        fields = _frontmatter(PI_REVIEWER_AGENT)
        self.assertEqual(fields["model"], self.review_model.model_for_host("pi"))
        self.assertEqual(fields["thinking"], self.review_model.REVIEW_THINKING)

    def test_each_prerun_launcher_leaves_the_model_to_review_model(self):
        for path in PRERUN_LAUNCHERS:
            with self.subTest(path=path.name):
                block = _launcher_block(path)
                backend = _launcher_option(block, "--backend")
                self.assertIn(backend, self.review_model.MODELS, f"未知 backend: {backend}")
                self.assertNotIn(
                    "--model", block,
                    f"{path.name} 不应复述模型值；省略 --model 让 runner 取 canonical 值",
                )
                # runner 的默认值必须确实是该宿主的获批身份（两个函数保持一致的护栏）。
                self.assertTrue(
                    self.review_model.is_accepted_model_identity(
                        self.review_model.review_job_model(backend), backend
                    ),
                    f"{backend} 的默认调用名不是获批身份",
                )

    def test_docs_and_comments_do_not_restate_the_canonical_model(self):
        literals = set(self.review_model.RECORDED_MODELS.values())
        literals |= {
            self.review_model.review_job_model(host) for host in self.review_model.MODELS
        }
        for path in NON_RESTATING_FILES:
            with self.subTest(path=path.name, parent=path.parent.name):
                text = _without_launcher_blocks(path.read_text(encoding="utf-8"))
                present = sorted(literal for literal in literals if literal in text)
                self.assertEqual(
                    present, [],
                    f"{path} 复述了 canonical 模型值 {present}；应改为指向 review_model.py",
                )


class ReviewContractLoadingTests(unittest.TestCase):
    """值在项目自有配置里，逻辑仍在本仓；缺文件回退默认值，坏文件 fail-closed。"""

    def setUp(self):
        self.review_model = _load_review_model()
        self.temp = tempfile.TemporaryDirectory(prefix="review-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, body: str) -> Path:
        path = self.root / "review_contract.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_missing_contract_file_keeps_builtin_defaults(self):
        contract = self.review_model.load_contract(self.root / "absent.toml")
        self.assertEqual(contract.models, self.review_model.DEFAULT_MODELS)
        self.assertEqual(contract.thinking, self.review_model.DEFAULT_THINKING)
        self.assertEqual(contract.suffix_hosts, self.review_model.DEFAULT_SUFFIX_HOSTS)

    def test_partial_contract_overrides_only_what_it_names(self):
        contract = self.review_model.load_contract(self.write(
            'schema_version = 1\n\n[models]\npi = "xiaojimao/gpt-6-astra"\n\n'
            '[thinking]\nlevel = "max"\n'
        ))
        self.assertEqual(contract.models["pi"], "xiaojimao/gpt-6-astra")
        self.assertEqual(
            contract.models["codex"], self.review_model.DEFAULT_MODELS["codex"]
        )
        self.assertEqual(contract.thinking, "max")
        self.assertEqual(contract.suffix_hosts, self.review_model.DEFAULT_SUFFIX_HOSTS)

    def test_malformed_contract_fails_closed(self):
        cases = {
            "未知宿主": '[models]\nnewapi = "a/b"\n',
            "未知顶层键": 'schema_version = 1\n[extra]\nx = 1\n',
            "空值": '[models]\npi = "  "\n',
            "schema 版本": 'schema_version = 99\n',
            "suffix_hosts 非数组": '[thinking]\nsuffix_hosts = "pi"\n',
            "suffix_hosts 未知宿主": '[thinking]\nsuffix_hosts = ["newapi"]\n',
            "TOML 语法": '[models\npi = "a/b"\n',
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "review_contract_invalid"):
                    self.review_model.load_contract(self.write(body))

    def test_module_constants_come_from_the_contract(self):
        contract = self.review_model.CONTRACT
        self.assertEqual(self.review_model.MODELS, contract.models)
        self.assertEqual(self.review_model.REVIEW_THINKING, contract.thinking)
        self.assertEqual(
            self.review_model.MODEL_TAKES_THINKING_SUFFIX,
            {host: host in contract.suffix_hosts for host in contract.models},
        )
        self.assertEqual(
            self.review_model.REVIEW_JOB_MODEL,
            self.review_model.review_job_model("pi"),
        )

    def test_project_owned_contract_file_is_excluded_from_template_sync(self):
        # workflow_sync.py 是模板侧的本地工具（在本仓被 .gitignore 排除），
        # 新克隆的仓库可能没有它；只在本机存在时校验排除项。
        source_path = ROOT / ".agents/harness/workflow/workflow_sync.py"
        if not source_path.is_file():
            self.skipTest("workflow_sync.py 不在本工作树中")
        source = source_path.read_text(encoding="utf-8")
        self.assertIn(".agents/harness/config/review_contract.toml", source)
        self.assertNotIn(".agents/harness/config/review_contract.example.toml", source)


if __name__ == "__main__":
    unittest.main()
