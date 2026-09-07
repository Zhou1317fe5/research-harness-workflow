#!/usr/bin/env python3
"""Deterministically locate incomplete Mission CSVs under issues/."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "mission.recovery-scan.v1"


def _load_completion():
    path = (
        Path(__file__).resolve().parents[2]
        / "mission-csv-execute/scripts/mission_completion.py"
    )
    spec = importlib.util.spec_from_file_location("mission_completion", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load completion contract: {path}")
    module = importlib.util.module_from_spec(spec)
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def scan(repo_root: Path) -> dict[str, Any]:
    root = repo_root.expanduser().resolve()
    issues = root / "issues"
    completion = _load_completion()
    directory_csvs = sorted(issues.glob("*/*.csv"))
    flat_csvs = sorted(
        path for path in issues.glob("*.csv") if path.name != "TEMPLATE.csv"
    )
    directory_stems = {path.stem for path in directory_csvs}
    candidates: list[dict[str, Any]] = []
    complete: list[str] = []
    for kind, paths in (("directory", directory_csvs), ("legacy_flat", flat_csvs)):
        for path in paths:
            if kind == "legacy_flat" and path.stem in directory_stems:
                continue
            reasons = completion.csv_completion_errors(path, workdir=root)
            relative = str(path.relative_to(root))
            if reasons:
                candidates.append(
                    {
                        "path": relative,
                        "kind": kind,
                        "mtime_ns": path.stat().st_mtime_ns,
                        "reasons": reasons,
                    }
                )
            else:
                complete.append(relative)
    candidates.sort(
        key=lambda item: (
            item["kind"] != "directory", -item["mtime_ns"], item["path"]
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "complete": sorted(complete),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()
    try:
        result = scan(Path(args.repo_root))
    except (OSError, RuntimeError) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "candidates": [],
            "candidate_count": 0,
            "complete": [],
            "errors": [f"scan_failed:{exc}"],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
