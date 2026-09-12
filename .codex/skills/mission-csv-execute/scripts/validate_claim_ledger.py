#!/usr/bin/env python3
"""Validate persisted claim references; scientific sufficiency remains a review judgment."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from mission_completion import parse_note_tags, read_mission_csv, resolve_reference_path as resolve_path

CLAIM_STATUSES = {
    "pending", "verified", "not_run_by_preregistered_gate", "failed",
    "validation_gap", "out_of_scope",
}


def split_claim_ids(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def reference_file(value: str, base_dir: Path, workdir: Path) -> Path | None:
    """显式非文件引用只校验类型；绝不把命令当作待执行操作。"""
    if not isinstance(value, str) or not value.strip() or value.strip() == "pending":
        raise ValueError("reference_empty_or_placeholder")
    value = value.strip()
    if value.startswith(("https://", "http://")):
        if not urlsplit(value).netloc:
            raise ValueError("reference_url_invalid")
        return None
    for prefix in ("command:", "manual:", "session:"):
        if value.startswith(prefix):
            if not value[len(prefix):].strip():
                raise ValueError("reference_description_missing")
            return None
    # 兼容 source.py:12 与 source.py#L12，不要求 Spec 固定章节命名。
    path_text = re.sub(r":\d+(?:-\d+)?$", "", value.split("#", 1)[0])
    path = resolve_path(path_text, base_dir, workdir)
    if not path.is_file():
        raise ValueError(f"reference_file_missing:{value}")
    if not any(path.is_relative_to(root.resolve()) for root in (base_dir, workdir)):
        raise ValueError(f"reference_outside_workdir:{value}")
    return path


def _references(value: object) -> list[str]:
    refs = [value] if isinstance(value, str) else value
    if not isinstance(refs, list) or not refs or any(not isinstance(x, str) or not x.strip() for x in refs):
        raise ValueError("non-empty evidence references required")
    return refs


def validate_ledger(
    ledger_path: Path, csv_path: Path, referenced_claims: set[str], *,
    require_terminal: bool = False, rows: list[dict[str, str]] | None = None,
    workdir: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    root = (workdir or Path.cwd()).resolve()
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        if rows is None:
            _, rows, _ = read_mission_csv(csv_path, allow_compat=True)
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        return [f"claim_ledger_unreadable:{exc}"]
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        return ["claim ledger must be an object with a claims array"]
    by_id = {row["id"]: row for row in rows}
    try:
        tags = {key: parse_note_tags(row.get("notes", "")) for key, row in by_id.items()}
    except ValueError as exc:
        return [str(exc)]
    ledger_ids: set[str] = set()
    for index, claim in enumerate(data["claims"]):
        if not isinstance(claim, dict):
            errors.append(f"claims[{index}] must be an object")
            continue
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id.strip():
            errors.append(f"claims[{index}] missing claim_id")
            continue
        if claim_id in ledger_ids:
            errors.append(f"claim_id_duplicate:{claim_id}")
        ledger_ids.add(claim_id)
        for key in ("promise", "evidence_required"):
            if not isinstance(claim.get(key), str) or not claim[key].strip():
                errors.append(f"{claim_id} missing {key}")
        if not isinstance(claim.get("production_path_required"), bool):
            errors.append(f"{claim_id} production_path_required must be boolean")
        try:
            reference_file(claim.get("source_ref"), ledger_path.parent, root)
        except ValueError as exc:
            errors.append(f"claim_source_invalid:{claim_id}:{exc}")
        covered = claim.get("covered_by")
        if not isinstance(covered, list) or any(not isinstance(x, str) or not x for x in covered):
            errors.append(f"{claim_id} covered_by must be a string array")
            covered = []
        for row_id in covered:
            if row_id not in by_id:
                errors.append(f"claim_issue_missing:{claim_id}:{row_id}")
        for row_id, row_tags in tags.items():
            if claim_id in split_claim_ids(row_tags.get("claims", "")) and row_id not in covered:
                errors.append(f"claim_coverage_mismatch:{claim_id}:{row_id}")
        status = claim.get("status")
        if not isinstance(status, str) or status not in CLAIM_STATUSES:
            errors.append(f"{claim_id} has invalid status: {status}")
        if require_terminal and status not in ("verified", "not_run_by_preregistered_gate", "out_of_scope"):
            errors.append(f"claim_not_terminal:{claim_id}:{status}")
        if status in ("verified", "not_run_by_preregistered_gate"):
            key = "gate_evidence" if status == "not_run_by_preregistered_gate" else "evidence_refs"
            files = []
            try:
                for value in _references(claim.get(key)):
                    path = reference_file(value, ledger_path.parent, root)
                    if path is not None:
                        files.append(path)
            except ValueError as exc:
                errors.append(f"claim_evidence_invalid:{claim_id}:{exc}")
            if status == "verified" and claim.get("evidence_required") == "real_e2e":
                real_rows = [tags[x] for x in covered if x in tags and tags[x].get("evidence_level") == "real_e2e"]
                if not files or not real_rows:
                    errors.append(f"claim_real_e2e_evidence_missing:{claim_id}")
                if claim.get("production_path_required") and not any(x.get("production_path") == "covered" for x in real_rows):
                    errors.append(f"claim_production_path_missing:{claim_id}")
    missing = sorted(referenced_claims - ledger_ids)
    if missing:
        errors.append("claim ledger missing referenced claim ids: " + ", ".join(missing))
    if data.get("csv") not in (None, csv_path.name, str(csv_path)):
        errors.append(f"claim ledger csv field does not match {csv_path.name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument("--workdir", default=".")
    args = parser.parse_args()
    workdir = Path(args.workdir).expanduser().resolve()
    errors: list[str] = []
    try:
        csv_path = resolve_path(args.csv_path, workdir, workdir)
        _, rows, _ = read_mission_csv(csv_path, allow_compat=True)
        ledgers: dict[Path, set[str]] = {}
        for row in rows:
            tags = parse_note_tags(row["notes"])
            claims = set(split_claim_ids(tags.get("claims", "")))
            value = tags.get("claim_ledger")
            if claims and not value:
                errors.append(f"claim_ledger_missing:{row['id']}")
            if value:
                path = resolve_path(value, csv_path.parent, workdir)
                ledgers.setdefault(path, set()).update(claims)
        for path, claims in ledgers.items():
            errors.extend(validate_ledger(path, csv_path, claims, rows=rows, workdir=workdir))
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        errors.append(str(exc))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("claim_ledger_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
