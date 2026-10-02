#!/usr/bin/env python3
"""Run an independent Codex vision review for mission CSV REVIEW rows."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from mission_completion import parse_note_tags, resolve_reference_path

from validate_outcome_contract import load_contract


RESULT_KEYS = {
    "review_agent_mode",
    "review_independence",
    "review_requested_model",
    "review_observed_model",
    "review_model_evidence",
    "result",
    "claim_coverage",
    "claim_coverage_status",
    "validation_limited",
    "summary",
    "gaps",
    "assumptions",
    "decision_debt",
    "deferred_findings",
    "human_required_blockers",
    "outcome_answers",
    "handoff_markdown",
}

# Closing review runs on the current session (executor) model: the model arrives via
# --model instead of being pinned to the contract, so validation only requires the
# recorded identity to be runtime-attested (see validate_review_result). `unknown` /
# `not_applicable` are only legal for evidence-close, which runs no model.
RUNTIME_MODEL_EVIDENCE = ("session-metadata", "event-stream", "parent-runtime")
# Bounded wait matches reviewer_job's attempt timeout so a stuck exec session
# is a recorded service failure, not an unbounded block.
DEFAULT_EXEC_TIMEOUT_SECONDS = 1800
_QUOTA_ERROR_MARKERS = (
    "insufficient_quota", "quota exceeded", "rate_limit", "rate limit",
    "429", "usage limit", "credits", "billing", "too many requests",
)
MODEL_EVIDENCE = {
    *RUNTIME_MODEL_EVIDENCE,
    "unknown",
    "not_applicable",
}
SCIENTIFIC_OUTCOMES = {
    "hypothesis_supported",
    "hypothesis_not_supported",
    "gate_failed",
    "inconclusive",
    "not_applicable",
}

OUTCOME_VERDICTS = {"pass", "fail", "partial", "unknown", "not_run"}
OUTCOME_CONFIDENCE = {"high", "moderate", "low", "unknown"}


def normalize_review_mode(mode: str) -> tuple[str, bool | str, bool]:
    """Return the canonical mode, independence, and supplemental flag."""
    mapping: dict[str, tuple[str, bool | str, bool]] = {
        "closing-reviewer-job": ("closing-reviewer-job", True, False),
        "self-review": ("self-review", False, False),
        "evidence-close": ("evidence-close", False, False),
        "pending": ("pending", "pending", False),
    }
    if mode not in mapping:
        raise ValueError(f"unknown review mode: {mode}")
    return mapping[mode]


def existing_file(value: str, workdir: Path) -> str:
    return resolve_existing_file(value, workdir)


def resolve_existing_file(value: str, workdir: Path, base_dir: Path | None = None) -> str:
    try:
        path = resolve_reference_path(value, base_dir or workdir, workdir)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if path.is_file():
        return str(path)
    raise argparse.ArgumentTypeError(f"file does not exist: {value}")


def artifact_output_path(value: str, workdir: Path, base_dir: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        base = base_dir if base_dir and (not path.parts or path.parts[0] != "issues") else workdir
        path = base / path
    try:
        return resolve_reference_path(str(path), workdir, workdir)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def optional_file(value: str | None, workdir: Path, base_dir: Path | None = None) -> str | None:
    if not value:
        return None
    path = artifact_output_path(value, workdir, base_dir)
    if not path.exists():
        return str(path)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"not a file: {value}")
    return str(path)


def tag_value(key: str, notes: str) -> str | None:
    return parse_note_tags(notes or "").get(key)


def discover_claim_ledger(csv_path: str, workdir: Path) -> str | None:
    path = Path(csv_path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    has_claims = False
    ledger_values: list[str] = []
    for row in rows:
        notes = row.get("notes", "")
        if tag_value("claims", notes):
            has_claims = True
        ledger = tag_value("claim_ledger", notes)
        if ledger and ledger not in ledger_values:
            ledger_values.append(ledger)
    if not ledger_values:
        if has_claims:
            raise argparse.ArgumentTypeError("CSV has claims but no claim_ledger tag")
        return None
    if len(ledger_values) > 1:
        raise argparse.ArgumentTypeError("CSV references multiple claim ledgers: " + ", ".join(ledger_values))
    return resolve_existing_file(ledger_values[0], workdir, path.parent)


def discover_outcome_contract(csv_path: str, workdir: Path) -> str | None:
    path = Path(csv_path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    values: list[str] = []
    for row in rows:
        value = tag_value("outcome_contract", row.get("notes", ""))
        if value and value not in values:
            values.append(value)
    if not values:
        return None
    if len(values) > 1:
        raise argparse.ArgumentTypeError(
            "CSV references multiple outcome contracts: " + ", ".join(values)
        )
    return resolve_existing_file(values[0], workdir, path.parent)


def discover_deferred_ledger(csv_path: str, workdir: Path) -> str | None:
    path = Path(csv_path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    values: list[str] = []
    for row in rows:
        value = tag_value("deferred_ledger", row.get("notes", ""))
        if value and value not in values:
            values.append(value)
    if not values:
        return None
    if len(values) > 1:
        raise argparse.ArgumentTypeError(
            "CSV references multiple deferred ledgers: " + ", ".join(values)
        )
    return resolve_existing_file(values[0], workdir, path.parent)


def validate_outcome_answers(result: dict, contract: dict | None) -> list[str]:
    answers = result.get("outcome_answers")
    if not isinstance(answers, list):
        return ["outcome_answers must be a list"]

    expected = {
        question.get("id")
        for question in (contract or {}).get("reader_questions", [])
        if isinstance(question, dict) and question.get("id")
    }
    seen: set[str] = set()
    errors: list[str] = []
    for index, answer in enumerate(answers):
        if not isinstance(answer, dict):
            errors.append(f"outcome_answers[{index}] must be an object")
            continue
        question_id = answer.get("question_id")
        if not isinstance(question_id, str) or not question_id:
            errors.append(f"outcome_answers[{index}] missing question_id")
            continue
        if question_id in seen:
            errors.append(f"duplicate outcome answer: {question_id}")
        seen.add(question_id)
        if question_id not in expected:
            errors.append(f"unexpected outcome answer: {question_id}")
        verdict = answer.get("verdict")
        if verdict not in OUTCOME_VERDICTS:
            errors.append(f"{question_id} has invalid verdict: {verdict}")
        confidence = answer.get("confidence")
        if confidence not in OUTCOME_CONFIDENCE:
            errors.append(f"{question_id} has invalid confidence: {confidence}")
        for key in ("answer", "boundary", "next_action"):
            if not isinstance(answer.get(key), str) or not answer[key].strip():
                errors.append(f"{question_id} missing {key}")
        refs = answer.get("evidence_refs")
        if not isinstance(refs, list) or not refs or not all(
            isinstance(value, str) and value.strip() for value in refs
        ):
            errors.append(f"{question_id} evidence_refs must be a non-empty string array")
    for question_id in sorted(expected - seen):
        errors.append(f"missing outcome answer: {question_id}")
    return errors


def output_file(value: str, workdir: Path, base_dir: Path | None = None) -> Path:
    path = artifact_output_path(value, workdir, base_dir)
    artifact_root = (base_dir or workdir).resolve()
    try:
        path.relative_to(artifact_root)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"output path escapes artifact root {artifact_root}: {path}"
        ) from exc
    return path


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_review_result(result: dict, contract: dict | None) -> list[str]:
    errors: list[str] = []
    missing = sorted(RESULT_KEYS - set(result))
    if missing:
        errors.append("review JSON missing required keys: " + ", ".join(missing))
        return errors

    mode = result.get("review_agent_mode")
    independence = result.get("review_independence")
    if mode not in {
        "closing-reviewer-job",
        "self-review",
        "evidence-close",
    }:
        errors.append(f"invalid review_agent_mode: {result.get('review_agent_mode')}")
    if not isinstance(independence, bool):
        errors.append(f"invalid review_independence: {result.get('review_independence')}")
    elif mode == "closing-reviewer-job" and not independence:
        errors.append(f"{mode} requires review_independence=true")
    elif mode in {"self-review", "evidence-close"} and independence:
        errors.append(f"{mode} requires review_independence=false")
    requested_model = result.get("review_requested_model")
    observed_model = result.get("review_observed_model")
    evidence = result.get("review_model_evidence")
    if mode == "evidence-close":
        if requested_model != "not_applicable":
            errors.append("evidence-close review_requested_model must be not_applicable")
        if observed_model != "not_applicable":
            errors.append("evidence-close review_observed_model must be not_applicable")
        if evidence != "not_applicable":
            errors.append("evidence-close review_model_evidence must be not_applicable")
    else:
        # Closing review 不固定模型（用当前会话模型），但记录值必须来自可确证的运行时通道：
        # 否则这三个字段退化成调用方自己的说法，审查记录就没有证据价值。
        if not isinstance(requested_model, str) or not requested_model.strip():
            errors.append("review_requested_model must be a non-empty string")
        elif requested_model == "not_applicable":
            errors.append("review_requested_model must name the session model")
        if not isinstance(observed_model, str) or not observed_model.strip():
            errors.append("review_observed_model must be a non-empty string")
        elif observed_model == "unknown":
            errors.append("review_observed_model must be attested by the runtime, not 'unknown'")
        if evidence not in RUNTIME_MODEL_EVIDENCE:
            errors.append(
                f"review_model_evidence must be one of {', '.join(RUNTIME_MODEL_EVIDENCE)}"
            )
    if evidence not in MODEL_EVIDENCE:
        errors.append(f"invalid review_model_evidence: {result.get('review_model_evidence')}")
    if result.get("result") not in {"vision_met", "gaps_found", "limited_review"}:
        errors.append(f"invalid review result: {result.get('result')}")
    scientific_outcome = result.get("scientific_outcome", "not_applicable")
    if scientific_outcome not in SCIENTIFIC_OUTCOMES:
        errors.append(f"invalid scientific_outcome: {result.get('scientific_outcome')}")
    if result.get("claim_coverage_status") not in {"complete", "gaps", "unknown"}:
        errors.append(f"invalid claim_coverage_status: {result.get('claim_coverage_status')}")
    for key in ("summary", "handoff_markdown"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            errors.append(f"{key} must be a non-empty string")
    for key in (
        "validation_limited",
        "assumptions",
        "decision_debt",
        "human_required_blockers",
    ):
        if not _string_list(result.get(key)):
            errors.append(f"{key} must be a string array")

    gaps = result.get("gaps")
    if not isinstance(gaps, list):
        errors.append("gaps must be an array")
        gaps = []
    else:
        for index, gap in enumerate(gaps):
            if not isinstance(gap, dict):
                errors.append(f"gaps[{index}] must be an object")
                continue
            for key in (
                "id",
                "title",
                "source_ref",
                "evidence_ref",
                "why_it_matters",
                "suggested_followup_issue",
            ):
                if not isinstance(gap.get(key), str) or not gap[key].strip():
                    errors.append(f"gaps[{index}] missing {key}")

    deferred_findings = result.get("deferred_findings")
    if not isinstance(deferred_findings, list):
        errors.append("deferred_findings must be an array")
    else:
        for index, finding in enumerate(deferred_findings):
            if not isinstance(finding, dict):
                errors.append(f"deferred_findings[{index}] must be an object")
                continue
            kind = finding.get("kind")
            if kind not in {"deferred_improvement", "future_decision"}:
                errors.append(f"deferred_findings[{index}] has invalid kind: {kind}")
            for key in (
                "id",
                "title",
                "summary",
                "why_deferred",
                "discussion_question",
            ):
                if not isinstance(finding.get(key), str) or not finding[key].strip():
                    errors.append(f"deferred_findings[{index}] missing {key}")
            for key in ("source_issue_ids", "evidence_refs"):
                value = finding.get(key)
                if not isinstance(value, list) or not value or not all(
                    isinstance(item, str) and item.strip() for item in value
                ):
                    errors.append(f"deferred_findings[{index}] {key} must be a non-empty string array")

    coverage = str(result.get("claim_coverage"))
    covered = total = None
    if coverage != "unknown":
        match = re.fullmatch(r"(\d+)/(\d+)", coverage)
        if not match:
            errors.append("claim_coverage must be '<covered>/<total>' or 'unknown'")
        else:
            covered, total = (int(match.group(1)), int(match.group(2)))
            if covered > total:
                errors.append("claim_coverage covered count cannot exceed total")
    if coverage == "unknown" and result.get("result") != "limited_review":
        errors.append("claim_coverage may be unknown only for limited_review")
    if result.get("claim_coverage_status") == "complete" and covered is not None and covered != total:
        errors.append("claim_coverage_status=complete requires covered=total")
    if result.get("claim_coverage_status") == "gaps" and covered is not None and covered >= total:
        errors.append("claim_coverage_status=gaps requires covered<total")
    if result.get("claim_coverage_status") == "unknown" and coverage != "unknown":
        errors.append("claim_coverage_status=unknown requires claim_coverage=unknown")

    errors.extend(validate_outcome_answers(result, contract))

    if result.get("result") == "vision_met":
        if result.get("claim_coverage_status") != "complete":
            errors.append("vision_met requires claim_coverage_status=complete")
        if gaps:
            errors.append("vision_met requires an empty gaps array")
        if result.get("human_required_blockers"):
            errors.append("vision_met cannot have human_required_blockers")
    if result.get("result") == "gaps_found":
        if (
            not gaps
            and result.get("claim_coverage_status") != "gaps"
            and not result.get("human_required_blockers")
        ):
            errors.append("gaps_found requires a recorded gap signal")
    return errors


def build_prompt(args: argparse.Namespace) -> str:
    review_log = args.review_log or "(review log does not exist yet)"
    claim_ledger = args.claim_ledger or "(claim ledger not provided)"
    outcome_contract = args.outcome_contract or "(outcome contract not provided; return an empty outcome_answers array)"
    deferred_ledger = args.deferred_ledger or "(deferred ledger not provided; start ids at DF-001 if needed)"
    source_doc = args.source_doc or "(no approved spec; this is an explicit compatibility CSV)"
    extra = args.extra or "None"
    return f"""You are the independent mission vision reviewer in an ephemeral, read-only Codex exec session.

Reviewer task:
- Work read-only.
- Do not trust the main agent's conclusions or summaries.
- Read the approved source document, task CSV, review log if present, and relevant code/test evidence referenced by the CSV.
- Inspect git history/diff only as needed to verify delivered work and claims.
- Reconstruct or verify the claim/evidence ledger: source claims, covered issue ids, production paths, and evidence level.
- Do not find issues for its own sake. A gap must be falsifiable and tied to a source claim or overstated delivery claim.

Inputs:
- Source doc: {source_doc}
- Source CSV: {args.csv}
- Task CSV: {args.csv}
- Claim ledger JSON: {claim_ledger}
- Outcome Contract JSON: {outcome_contract}
- Deferred findings ledger JSON: {deferred_ledger}
- Review log: {review_log}
- Requested model: {args.model or "Codex CLI default model"}
- Extra evidence: {extra}

Return exactly one JSON object and no markdown. Schema:
{{
  "result": "vision_met | gaps_found | limited_review (Mission execution only)",
  "scientific_outcome": "hypothesis_supported | hypothesis_not_supported | gate_failed | inconclusive | not_applicable",
  "claim_coverage": "covered/total, for example 12/13; use unknown only with limited_review",
  "claim_coverage_status": "complete | gaps | unknown",
  "validation_limited": ["evidence limitation, if any"],
  "summary": "one concise paragraph",
  "gaps": [
    {{
      "id": "FOLLOWUP-01",
      "title": "short executable issue title",
      "source_ref": "path:line",
      "evidence_ref": "path:line or command/log reference",
      "why_it_matters": "why this violates the approved doc or claim/evidence alignment",
      "suggested_followup_issue": "one sentence executable issue"
    }}
  ],
  "assumptions": ["..."],
  "decision_debt": ["..."],
  "deferred_findings": [
    {{
      "id": "DF-001",
      "kind": "deferred_improvement | future_decision",
      "title": "short human-readable title",
      "summary": "what was observed",
      "source_issue_ids": ["REVIEW-01"],
      "evidence_refs": ["trace/test/artifact reference"],
      "why_deferred": "why this does not block the current approved scope",
      "discussion_question": "the concrete question for the user"
    }}
  ],
  "human_required_blockers": ["..."],
  "outcome_answers": [
    {{
      "question_id": "OUTCOME-001",
      "verdict": "pass | fail | partial | unknown | not_run",
      "answer": "direct answer to the reader question",
      "evidence_refs": ["trace/test/artifact reference"],
      "confidence": "high | moderate | low | unknown",
      "boundary": "what this evidence cannot establish",
      "next_action": "how to resolve fail, partial, unknown, or not_run; write None only when no action is needed"
    }}
  ],
  "handoff_markdown": "Full human-facing handoff in Markdown. Follow the Handoff template below exactly. Audience is a non-expert human decision-maker, not a machine."
}}

Rules:
- Use result `vision_met` when the approved protocol was implemented and executed as specified, claim coverage is complete or conditionally terminal, no actionable execution gaps remain, and evidence levels are honestly labeled. A negative hypothesis result or preregistered gate stop does not by itself make the Mission `gaps_found`.
- Use result `gaps_found` for actionable discrepancies.
- Use result `limited_review` if you cannot inspect enough evidence or cannot perform independent review.
- Record the scientific conclusion separately in `scientific_outcome`; never convert `gate_failed` or `hypothesis_not_supported` into an execution gap.
- If there are no gaps, return an empty `gaps` array.
- Classify every discovery before reporting it. A current-scope gap violates the approved source or an acceptance criterion; it belongs in `gaps` and must not be deferred.
- Use `deferred_improvement` only for a non-blocking quality or architecture improvement outside the current approved scope. Use `future_decision` only when the current scope is complete but a later product or architecture choice needs the user.
- `deferred_findings` contains only those two non-blocking kinds. Preserve existing open findings from the ledger, deduplicate by meaning and evidence, and allocate the next DF number for new findings.
- Never append or execute a CSV issue for a deferred finding. The mission ends after the current CSV and presents these items for user discussion.
- If an Outcome Contract is provided, return exactly one outcome answer for every reader question.
- Do not infer pass from implementation status. Use only cited evidence, and preserve `unknown` or `not_run` when evidence is absent.
- If no Outcome Contract is provided, return an empty `outcome_answers` array for legacy compatibility.

Handoff template (handoff_markdown):
Write a complete Markdown document that reads like a "construction completion report". The reader is the person who wrote or approved the spec -- they come back days later and need to understand what got done, without opening CSV, claims.json, or git log.

Write in the SAME style as the source design doc: use comparison tables, arrow-flow diagrams, conclusion sentences, inline term definitions. NOT audit-log style.

HARD RULES for handoff style:
- Structure follows the spec's goals/capabilities, NOT CSV row numbers, NOT CLAIM-XXX IDs
- Self-contained: inline all information. Never write "see claims.json" or reference CLAIM-XXX
- Plain language first: say "users can now search for contact emails" BEFORE saying "research_contacts returns non-empty list"
- Explain terms on first use with parentheses
- No review audit jargon: never write "scope checked", "evidence checked", "claim coverage", "vision_met"
- Honest: if something is degraded or unverified, say so plainly. Thin evidence = thin section, never pad
- handoff_markdown is REQUIRED even when result is limited_review

Required structure when an Outcome Contract is provided:

# <task topic> -- 施工交工单

> 独立性: true | mode=closing-reviewer-job | requested_model={args.model}
> 日期: <date>

## 先看结论

One paragraph with the overall verdict, decisive result, and most important blocked claim. A non-technical manager should understand it.

## 这份交工单告诉你什么

State the artifact role, evidence inputs, consumers, and what the handoff does not prove or override.

## 你现在可以确定什么

Render every reader question from the Outcome Contract. Use this table:

| 你关心的问题 | 判定 | 直接答案 | 关键证据 | 可信度 | 结论边界 | 下一步 |
|---|---|---|---|---|---|---|

Do not expose OUTCOME-* ids. Keep the question text unchanged so contract validation can match it.
Copy verdict, answer, confidence, boundary, and next_action exactly from outcome_answers. Join multiple evidence_refs with the literal separator `; `. These table cells are machine-checked and must not be paraphrased later.

## 决定整体状态的结果

Explain the decisive end-to-end result, the last confirmed healthy stage, and the first failure or evidence gap. Separate implementation status, validation status, and capability verdict.

## 目前仍不能声称什么

Render every blocked claim with its reason and release condition. Never aggregate partial, unknown, or not_run into pass.
Copy claim, reason, and release_condition exactly from the Outcome Contract; these cells are machine-checked.

## spec 目标逐条对账

Use a table or numbered list. One row per spec goal (NOT per CSV issue). Columns:

| spec 目标 | 状态 | 实际效果 | 备注 |
|-----------|------|----------|------|

- "spec 目标": extract from source doc using the spec's own wording (condense if too long)
- "状态": 完成 / 部分完成 / 降级 / 未开始
- "实际效果": describe from user/product perspective what changed
- "备注": if degraded/incomplete, explain why and what's missing

## 施工细节

Organize by module or functional area (NOT by CSV row). For each area:
- What changed: which files/functions, before vs after (use arrow-flow or comparison table)
- Problems hit: issues discovered during execution, root cause, how fixed
- Decisions made: choices the spec didn't prescribe, what was chosen, why

Example format:
```
改之前：chat runtime 没有注入 store → remember_user_memory 报错
改之后：main.py:112 暴露 store → chat.py:172 注入 → 工具正常写入
```

When this round changes multi-layer data flow, cross-file call relationships, or architecture boundaries (responsibility moved/demoted/promoted), you MUST add a mermaid diagram here, not just text:
- At most two diagrams: one "改之前", one "改之后", so the reader sees the change as a diff. One diagram is fine if only one side changed.
- Draw ONLY nodes/edges related to this change, never the whole system (a full-system map drowns the change point).
- Mark what changed: in the "改后" diagram, label the new/changed node or edge (e.g. a dotted edge `-.本轮新增.->`).
- Do NOT force a diagram for text-only edits, single-function internal logic, or changes with no cross-file/cross-layer relationship -- thin source = thin section, never pad with visuals.
- Fallback: if mermaid cannot be written or fails to render, fall back to the ASCII arrow block above. A diagram must never block the handoff.

mermaid example (改后 data flow; dotted edge = added this round):
```mermaid
flowchart LR
    A[main.py:112 暴露 store] --> B[chat.py:172 注入]
    B -.本轮新增.-> C[remember_user_memory 正常写入]
```

## 验证情况

Brief (this is NOT the main section):
- What tests ran, results (one or two lines)
- What was NOT verified and why (honestly)

## 待讨论

Include this section only when the deferred ledger has open findings or `deferred_findings` is non-empty. Render every open deferred finding from the ledger plus every new item in natural language. Put `<!-- deferred:DF-001 -->` immediately before its text so coverage can be checked. Keep ids out of visible headings and prose.

## 后续可操作

Only include subsections that apply -- do not write empty subsections:

**还剩什么**: unfinished items, items needing product decisions, known limitations

**阻塞/配置**: things the user must do (configure credentials, start services, approve something). State the exact unblock condition.

**怎么复现** (if applicable): complete E2E steps -- what to start, what to input, what result to expect

**去哪看** (if applicable): addresses, filter conditions, DB queries, log paths for any observable data relevant to this work

Legacy fallback: when no Outcome Contract is provided, keep the existing summary, spec-goal reconciliation, construction details, validation, and next-action structure. Do not invent reader questions.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--source-doc")
    parser.add_argument("--claim-ledger", help="Claim/evidence ledger JSON. Defaults to claim_ledger tag discovered from the CSV.")
    parser.add_argument("--outcome-contract", help="Outcome Contract JSON. Defaults to outcome_contract tag discovered from the CSV.")
    parser.add_argument("--deferred-ledger", help="Deferred findings ledger JSON. Defaults to deferred_ledger tag discovered from the CSV.")
    parser.add_argument("--review-log")
    parser.add_argument("--extra", help="Short extra evidence summary or path list.")
    parser.add_argument("--workdir", default=os.getcwd())
    parser.add_argument(
        "--model",
        required=True,
        help="审查模型：用当前会话（执行）模型。脚本不再固定为契约值，验证器只要求记录值来自可确证的运行时通道。",
    )
    parser.add_argument(
        "--backend",
        choices=("pi", "codex"),
        default="pi",
        help="审查执行的后端；Pi 会话用 `pi`（正确），Codex/Claude 会话用 `codex`。",
    )
    parser.add_argument("--output", help="Write final JSON to this file.")
    parser.add_argument("--handoff", help="Write handoff_markdown to this .md file.")
    args = parser.parse_args()

    workdir_path = Path(args.workdir).expanduser().resolve()
    args.csv = existing_file(args.csv, workdir_path)
    args.source_doc = existing_file(args.source_doc, workdir_path) if args.source_doc else None
    args.claim_ledger = (
        resolve_existing_file(args.claim_ledger, workdir_path, Path(args.csv).parent)
        if args.claim_ledger
        else discover_claim_ledger(args.csv, workdir_path)
    )
    args.outcome_contract = (
        resolve_existing_file(args.outcome_contract, workdir_path, Path(args.csv).parent)
        if args.outcome_contract
        else discover_outcome_contract(args.csv, workdir_path)
    )
    args.deferred_ledger = (
        resolve_existing_file(args.deferred_ledger, workdir_path, Path(args.csv).parent)
        if args.deferred_ledger
        else discover_deferred_ledger(args.csv, workdir_path)
    )
    outcome_contract_data = None
    if args.outcome_contract:
        outcome_contract_data, contract_errors = load_contract(Path(args.outcome_contract))
        if contract_errors:
            for error in contract_errors:
                sys.stderr.write(error + "\n")
            return 5
    csv_dir = Path(args.csv).parent
    args.review_log = optional_file(args.review_log, workdir_path, csv_dir)
    output_path = output_file(args.output, workdir_path, csv_dir) if args.output else None
    handoff_path = output_file(args.handoff, workdir_path, csv_dir) if args.handoff else None
    prompt = build_prompt(args)

    # closing review 由 reviewer_job 承载。reviewer_job 与主会话架构对齐：
    # fresh read-only session、message 与 verdict 都落盘、packet/task/raw sha 验证。
    # --model 直接来自当前会话主模型（invoker），不是 review_contract。
    repo_root = workdir_path
    sys.path.insert(0, str(repo_root / ".agents"))
    sys.path.insert(0, str(repo_root / ".agents" / "harness"))
    from harness import reviewer_job  # noqa: E402

    job_dir = csv_dir / "reviews" / f"closing-reviewer-job-{Path(args.csv).stem}"
    job_dir.mkdir(parents=True, exist_ok=True)

    packet_payload = {
        "schema_version": "closing.vision-review.v1",
        "repo_root": str(repo_root),
        "csv": str(Path(args.csv).relative_to(repo_root)),
    }
    if args.source_doc:
        packet_payload["source_doc"] = str(Path(args.source_doc).relative_to(repo_root))
    if args.claim_ledger:
        packet_payload["claim_ledger"] = str(Path(args.claim_ledger).relative_to(repo_root))
    if args.outcome_contract:
        packet_payload["outcome_contract"] = str(Path(args.outcome_contract).relative_to(repo_root))
    if args.deferred_ledger:
        packet_payload["deferred_ledger"] = str(Path(args.deferred_ledger).relative_to(repo_root))
    if args.review_log:
        packet_payload["review_log"] = str(Path(args.review_log).relative_to(repo_root))
    if args.extra:
        packet_payload["extra"] = args.extra

    packet_path = job_dir / "packet.json"
    packet_path.write_text(json.dumps(packet_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    task_path = job_dir / "task.md"
    task_path.write_text(prompt, encoding="utf-8")

    rj_args = Namespace(
        packet=str(packet_path),
        task=str(task_path),
        backend=args.backend,
        job_dir=str(job_dir),
        cwd=None,
        model=args.model,
        max_resumes=0,
        max_replacements=0,
        attempt_timeout_seconds=DEFAULT_EXEC_TIMEOUT_SECONDS,
        review_kind="closing",
        model_source="invoker",
        # legacy compat knobs (unused by closing kind)
        netns=None,
        netns_keepalive=False,
        unshare_pid=False,
        acquire_run_lock=True,
        expected_tools=None,
        expected_model=None,
        session_id=None,
        skip_patch_validation=False,
    )
    code = reviewer_job.execute(rj_args)
    if code != 0:
        return code

    verdict_path = job_dir / "verdict.json"
    if not verdict_path.is_file():
        sys.stderr.write(f"verdict not written: {verdict_path}\n")
        return 2
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    review_output_text = verdict.get("review_output") or ""
    if not review_output_text.strip():
        sys.stderr.write("verdict has empty review_output\n")
        return 2
    try:
        closing_result = json.loads(review_output_text.strip())
    except json.JSONDecodeError:
        sys.stderr.write("closing review output was not valid JSON\n")
        sys.stderr.write(review_output_text + "\n")
        return 3

    # Schema check: closing.vision-review.v1 has reviewer_id, result, report_markdown, gaps[].
    # Validate before releasing to callers (downstream verify_report relies on these keys).
    if not isinstance(closing_result, dict):
        sys.stderr.write(f"closing review output is not an object\n")
        return 4
    if closing_result.get("result") not in {"pass", "issues_found", "not_evaluable"}:
        sys.stderr.write(f"closing review result is not one of pass|issues_found|not_evaluable: {closing_result.get('result')}\n")
        return 5
    if not isinstance(closing_result.get("report_markdown"), str):
        sys.stderr.write("closing review missing report_markdown\n")
        return 5
    gaps = closing_result.get("gaps")
    if gaps is not None and not isinstance(gaps, list):
        sys.stderr.write("closing review gaps must be a list\n")
        return 5

    observed_model = verdict.get("observed_model") or "unknown"
    model_evidence = verdict.get("model_evidence") or "unknown"
    result = {
        "review_agent_mode": "closing-reviewer-job",
        "review_independence": True,
        "review_requested_model": verdict.get("requested_model") or args.model,
        "review_observed_model": observed_model,
        "review_model_evidence": model_evidence,
        # Schema-compatible top-level keys for downstream verify_report / csv checks.
        "result": closing_result.get("result"),
        "report_markdown": closing_result.get("report_markdown"),
        "gaps": closing_result.get("gaps") or [],
        "closing_reason": closing_result.get("closing_reason"),
        "reviewer_id": closing_result.get("reviewer_id") or "reviewer_job",
        # Keep legacy aliases for the older reviewers and human reviewers.
        "vision_met": closing_result.get("vision_met"),
        "blocking_issues": closing_result.get("blocking_issues") or [],
        "review_notes": closing_result.get("review_notes"),
        "summary": closing_result.get("summary"),
        "validation_limited": closing_result.get("validation_limited"),
        "assumptions": closing_result.get("assumptions") or [],
        "decision_debt": closing_result.get("decision_debt") or [],
        "deferred_findings": closing_result.get("deferred_findings") or [],
        "human_required_blockers": closing_result.get("human_required_blockers") or [],
        "outcome_answers": closing_result.get("outcome_answers") or [],
        "handoff_markdown": closing_result.get("handoff_markdown"),
    }
    review_errors = validate_review_result(result, outcome_contract_data)
    if review_errors:
        for error in review_errors:
            sys.stderr.write(error + "\n")
        return 5
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output + "\n", encoding="utf-8")
    if handoff_path:
        handoff_md = result.get("handoff_markdown")
        if handoff_md:
            handoff_path.parent.mkdir(parents=True, exist_ok=True)
            handoff_path.write_text(handoff_md.rstrip() + "\n", encoding="utf-8")
        else:
            sys.stderr.write("warning: --handoff requested but review JSON had no handoff_markdown\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
