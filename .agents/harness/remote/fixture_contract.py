#!/usr/bin/env python3
"""本地 adapter 契约 fixture：不上 GPU，先把监控契约跑通。

用途
----
监控 adapter 的路径、计数、字段、序列化、退出码契约与 GPU 无关。历史上把这些
拿真机去试，代价是每修一处就消耗一个 RunID（`summary_path` → 首步计数 →
`progress_path=null` → 序列化，四个 RunID）。

本工具按 RunSpec 的 `metadata.adapter_contract` **合成**一个输出目录，再调用真实
adapter（`validate_adapter.py` 的同一实现）跑 `first_step/periodic/completion`。
能捕获：JSONL 解析、literal dotted key、缺字段、identity 字段不符、协议字段错误、
退出码非零。

边界（必须严格遵守）
--------------------
- fixture **不证明**任何 GPU 行为、显存占用、loss/log、checkpoint 或科学结论。
- fixture 输出目录必须放在仓库外的临时目录（默认 `temp/`），不得进入 Mission 的
  科研证据路径，也不得作为任何 gate 的通过依据。
- 真实 smoke / official 运行仍必须走真实运行。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.remote.validate_adapter import PHASES, validate_outputs  # noqa: E402
else:  # pragma: no cover - 包内导入
    from .validate_adapter import PHASES, validate_outputs


class FixtureError(ValueError):
    """runspec 不足以合成契约 fixture。"""


def _contract(runspec: dict[str, Any]) -> dict[str, Any]:
    metadata = runspec.get("metadata")
    if not isinstance(metadata, dict):
        raise FixtureError("runspec.metadata must be an object")
    contract = metadata.get("adapter_contract")
    if not isinstance(contract, dict):
        raise FixtureError("runspec.metadata.adapter_contract missing")
    return contract


def _count_field(contract: dict[str, Any]) -> str:
    value = contract.get("progress_count_field")
    if not isinstance(value, str) or not value:
        raise FixtureError("adapter_contract.progress_count_field missing")
    return value


def _count_records(contract: dict[str, Any]) -> int:
    """满足 first_step_min_count 与 completion_exact_count 的记录条数。"""
    minimum = contract.get("first_step_min_count", 1)
    if not isinstance(minimum, int) or minimum < 0:
        raise FixtureError("adapter_contract.first_step_min_count invalid")
    exact = contract.get("completion_exact_count")
    if isinstance(exact, int) and exact >= 0:
        return max(exact, minimum)
    completion = contract.get("completion_min_count", 1)
    if isinstance(completion, int) and completion > minimum:
        return max(completion, minimum)
    return max(minimum, 1)


def _identity(contract: dict[str, Any], key: str, context: dict[str, Any]) -> dict[str, Any]:
    """identity 字段是契约期望的精确值；`$run_id` 等占位符由调用方解析。"""
    fields = contract.get(key, {})
    if not isinstance(fields, dict):
        raise FixtureError(f"adapter_contract.{key} must be an object")
    resolved: dict[str, Any] = {}
    for name, value in fields.items():
        if isinstance(value, str) and value.startswith("$"):
            resolved[name] = context.get(value[1:], "")
        else:
            resolved[name] = value
    return resolved


def _finite_fields(contract: dict[str, Any], key: str) -> list[str]:
    fields = contract.get(key, [])
    if not isinstance(fields, list) or any(not isinstance(item, str) for item in fields):
        raise FixtureError(f"adapter_contract.{key} must be a string array")
    return fields


def synthesize(runspec: dict[str, Any], output_root: Path) -> dict[str, Any]:
    """按 adapter_contract 合成 progress/summary 文件，返回写出的清单。"""
    contract = _contract(runspec)
    output_root.mkdir(parents=True, exist_ok=True)
    context = {
        "run_id": runspec.get("run_id"),
        "project": runspec.get("project"),
        "repo_root": (runspec.get("source") or {}).get("repo_root"),
    }
    written: dict[str, Any] = {}

    progress_path = contract.get("progress_path")
    if not isinstance(progress_path, str) or not progress_path:
        raise FixtureError("adapter_contract.progress_path missing")
    count_field = _count_field(contract)
    count = _count_records(contract)
    finite = _finite_fields(contract, "progress_finite_fields")
    progress_identity = _identity(contract, "progress_identity_fields", context)
    with (output_root / progress_path).open("w", encoding="utf-8") as handle:
        for index in range(count):
            if count == 1:
                value = 0
            else:
                value = index
            record: dict[str, Any] = {count_field: value}
            for name in finite:
                record.setdefault(name, float(value))
            record.update(progress_identity)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    written["progress"] = {"path": progress_path, "records": count}

    summary_path = contract.get("summary_path")
    if isinstance(summary_path, str) and summary_path:
        summary: dict[str, Any] = {}
        required = contract.get("summary_required_fields", [])
        if not isinstance(required, list):
            raise FixtureError("adapter_contract.summary_required_fields must be an array")
        for name in required:
            if not isinstance(name, str):
                raise FixtureError("adapter_contract.summary_required_fields items must be strings")
            summary[name] = []
        for name in _finite_fields(contract, "summary_finite_fields"):
            summary[name] = 0.0
        summary.update(_identity(contract, "summary_identity_fields", context))
        (output_root / summary_path).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written["summary"] = {"path": summary_path}

    artifacts = contract.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise FixtureError("adapter_contract.artifacts must be an array")
    for name in artifacts:
        if not isinstance(name, str) or not name:
            raise FixtureError("adapter_contract.artifacts items must be non-empty strings")
        target = output_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("", encoding="utf-8")
    written["artifacts"] = list(artifacts)
    return written


def run(runspec_path: Path, output_root: Path, phases: list[str]) -> dict[str, Any]:
    runspec = json.loads(runspec_path.read_text(encoding="utf-8"))
    if not isinstance(runspec, dict):
        raise FixtureError("runspec must be an object")
    written = synthesize(runspec, output_root)
    result = validate_outputs(runspec, output_root, phases)
    return {"fixture": written, "adapter": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runspec", type=Path, help="RunSpec JSON 路径")
    parser.add_argument("--output-root", type=Path, default=None,
                        help="fixture 输出目录；默认在系统临时目录下新建")
    parser.add_argument("--phase", action="append", choices=PHASES, dest="phases")
    args = parser.parse_args(argv)
    temporary = None
    output_root = args.output_root
    if output_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="adapter-contract-fixture-")
        output_root = Path(temporary.name) / "output"
    try:
        result = run(args.runspec, output_root, args.phases or list(PHASES))
    except (OSError, ValueError, FixtureError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    finally:
        if temporary is not None:
            temporary.cleanup()
    result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
