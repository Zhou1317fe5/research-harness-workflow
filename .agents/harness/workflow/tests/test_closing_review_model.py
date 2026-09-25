#!/usr/bin/env python3
"""closing review 的模型记录规则。

closing review 用当前会话（执行）模型，不再固定为契约里的审定模型；因此验证器不再比对一个
固定值，改而要求记录值来自可确证的运行时通道：requested 必须点名会话模型、observed 必须由
运行时上报（不接受 `unknown`）、evidence 必须是真实通道。否则这三个字段会退化成调用方自己的
说法，审查记录就没有证据价值。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))
from run_vision_review import RUNTIME_MODEL_EVIDENCE, validate_review_result  # noqa: E402


def base_result(**overrides) -> dict:
    result = {
        "review_agent_mode": "reviewer-subagent",
        "review_independence": True,
        "review_requested_model": "xiaojimao/gpt-6-astra:high",
        "review_observed_model": "xiaojimao/gpt-6-astra",
        "review_model_evidence": "session-metadata",
        "result": "vision_met",
        "claim_coverage": "3/3",
        "claim_coverage_status": "complete",
        "validation_limited": [],
        "summary": "scope reviewed against the approved document",
        "gaps": [],
        "assumptions": [],
        "decision_debt": [],
        "deferred_findings": [],
        "human_required_blockers": [],
        "outcome_answers": [],
        "handoff_markdown": "handoff body",
    }
    result.update(overrides)
    return result


class ClosingReviewModelTests(unittest.TestCase):
    def errors(self, **overrides) -> list[str]:
        return validate_review_result(base_result(**overrides), None)

    def test_session_model_is_accepted(self):
        """核心行为：审查模型 = 会话模型，不再要求等于契约里的审定模型。"""
        self.assertEqual(self.errors(), [])

    def test_observed_model_must_be_attested(self):
        errors = self.errors(review_observed_model="unknown")
        self.assertTrue(any("unknown" in error for error in errors), errors)

    def test_evidence_must_come_from_a_runtime_channel(self):
        self.assertEqual(self.errors(review_model_evidence="parent-runtime"), [])
        for value in ("unknown", "not_applicable"):
            with self.subTest(evidence=value):
                errors = self.errors(review_model_evidence=value)
                self.assertTrue(
                    any("review_model_evidence" in error for error in errors), errors
                )
        self.assertEqual(
            sorted(RUNTIME_MODEL_EVIDENCE),
            ["event-stream", "parent-runtime", "session-metadata"],
        )

    def test_requested_model_must_name_the_session_model(self):
        for value in ("", "   ", "not_applicable", None):
            with self.subTest(requested=value):
                errors = self.errors(review_requested_model=value)
                self.assertTrue(
                    any("review_requested_model" in error for error in errors), errors
                )

    def test_self_review_records_parent_runtime_and_no_independence(self):
        self.assertEqual(
            self.errors(
                review_agent_mode="self-review",
                review_independence=False,
                review_model_evidence="parent-runtime",
                review_requested_model="openai-codex/gpt-5.6-sol",
                review_observed_model="openai-codex/gpt-5.6-sol",
            ),
            [],
        )

    def test_independence_flag_follows_the_mode(self):
        errors = self.errors(review_independence=False)
        self.assertTrue(any("requires review_independence=true" in e for e in errors), errors)

        side_errors = self.errors(
            review_agent_mode="self-review",
            review_independence=True,
        )
        self.assertTrue(any("requires review_independence=false" in e for e in side_errors), side_errors)

    def test_evidence_close_runs_no_model(self):
        self.assertEqual(
            self.errors(
                review_agent_mode="evidence-close",
                review_independence=False,
                review_requested_model="not_applicable",
                review_observed_model="not_applicable",
                review_model_evidence="not_applicable",
            ),
            [],
        )
        errors = self.errors(
            review_agent_mode="evidence-close",
            review_independence=False,
            review_requested_model="xiaojimao/gpt-6-astra",
            review_observed_model="not_applicable",
            review_model_evidence="not_applicable",
        )
        self.assertTrue(any("must be not_applicable" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
