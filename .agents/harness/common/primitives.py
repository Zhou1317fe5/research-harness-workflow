"""工作流通用基础原语：确定性序列化、原子写、时间戳与公共模式。

仅被 .agents/harness 包内模块引用；rrctl 打包产物（remote_run_control）必须保持自包含，
不得引用本模块（远端 zipapp 环境不存在 .agents 目录）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def utc_now() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串表示。"""
    return datetime.now(timezone.utc).isoformat()


# 兼容既有 now() 调用名
now = utc_now


def canonical_json(value: Any) -> bytes:
    """确定性紧凑 JSON 序列化，用于跨进程摘要一致性。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# 兼容既有 _canonical_bytes 调用名
canonical_bytes = canonical_json


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    """带 fsync 与权限收紧的原子 JSON 文件落盘。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def append_error(errors: list[str], code: str, path: str, detail: str) -> None:
    """标准化追加校验错误项（四段式 code: path: detail）。"""
    errors.append(f"{code}: {path}: {detail}")


def dependency_closure(graph: dict[str, Any], stage_id: str) -> set[str]:
    """计算有向无环阶段图在指定阶段的上游全部传递依赖集合。"""
    result: set[str] = set()
    pending = list(graph.get(stage_id, {}).get("depends_on", []))
    while pending:
        dependency = pending.pop()
        if dependency in result:
            continue
        result.add(dependency)
        pending.extend(graph.get(dependency, {}).get("depends_on", []))
    return result
