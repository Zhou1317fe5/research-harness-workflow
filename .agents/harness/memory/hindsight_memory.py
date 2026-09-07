#!/usr/bin/env python3
"""向 Hindsight 同步已提交的科研摘要，并核对召回结果的本地来源。"""

from __future__ import annotations

# 直接运行脚本和通过 Python 包导入时使用同一实现。
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.memory.hindsight_memory import main
    raise SystemExit(main())

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from harness.common.paths import REPO_ROOT
MAX_SOURCE_BYTES = 32 * 1024
METADATA_FIELDS = (
    "project_id", "source_path", "source_commit", "source_sha256",
    "exp_id", "spec_ids", "code_branches", "code_commits",
)


class MemorySyncError(ValueError):
    pass


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise MemorySyncError("project_id 和 bank_id 必须是有效的标识符")
    return value


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise MemorySyncError("无法读取来源 Git 记录，请先初始化并提交科研仓库")
    return result.stdout.strip()


def source_path(repo_root: Path, source: str) -> tuple[Path, Path, str]:
    root = repo_root.resolve()
    path = Path(source)
    path = path if path.is_absolute() else root / path
    if ".." in path.parts:
        raise MemorySyncError("来源路径不能包含上级目录跳转")
    workspace = root / "research_workspace"
    if not path.is_relative_to(workspace):
        raise MemorySyncError("来源必须位于 research_workspace")
    parts = path.relative_to(workspace).parts
    allowed = parts in (("STATE.md",), ("CONCLUSIONS.md",)) or (
        len(parts) == 4 and parts[0] == "experiments"
        and parts[2:] == ("analysis", "analysis.md")
    )
    if not allowed:
        raise MemorySyncError("只同步 STATE.md、CONCLUSIONS.md 或单实验 analysis/analysis.md")
    current = workspace
    if current.is_symlink():
        raise MemorySyncError("科研来源目录不能是符号链接")
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise MemorySyncError("科研来源路径不能包含符号链接")
    return workspace, path, path.relative_to(root).as_posix()


def committed_bytes(workspace: Path, path: Path, commit: str = "HEAD") -> bytes:
    relative = path.relative_to(workspace).as_posix()
    git(workspace, "ls-files", "--error-unmatch", "--", relative)
    if git(workspace, "status", "--porcelain", "--untracked-files=all", "--", relative):
        raise MemorySyncError("来源有未提交改动，请先提交科研仓库")
    if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise MemorySyncError("来源必须是大小不超过 32 KiB 的文件")
    content = path.read_bytes()
    snapshot = subprocess.run(
        ["git", "-C", str(workspace), "show", f"{commit}:{relative}"],
        capture_output=True, check=False,
    )
    if snapshot.returncode or snapshot.stdout != content:
        raise MemorySyncError("来源内容与记录的 Git commit 不一致")
    return content


def prepare_document(repo_root: Path, source: str, project_id: str) -> dict:
    project_id = identifier(project_id)
    workspace, path, relative = source_path(repo_root, source)
    if Path(git(workspace, "rev-parse", "--show-toplevel")).resolve() != workspace:
        raise MemorySyncError("research_workspace 必须是独立 Git 仓库")
    commit = git(workspace, "rev-parse", "HEAD")
    content = committed_bytes(workspace, path, commit)
    if not content.strip():
        raise MemorySyncError("来源摘要不能为空")
    metadata = {
        "project_id": project_id,
        "source_path": relative,
        "source_commit": commit,
        "source_sha256": hashlib.sha256(content).hexdigest(),
    }
    parts = path.relative_to(workspace).parts
    if len(parts) == 4:
        exp_id = identifier(parts[1])
        record_path = workspace / "experiments" / exp_id / "record.json"
        if record_path.is_symlink():
            raise MemorySyncError("实验 record.json 不能是符号链接")
        record = json.loads(committed_bytes(workspace, record_path, commit))
        if not isinstance(record, dict) or record.get("exp_id") != exp_id:
            raise MemorySyncError("实验记录与来源 ExpID 不一致")
        origin = record.get("source")
        if not isinstance(origin, dict):
            raise MemorySyncError("实验记录缺少 source")
        metadata["exp_id"] = exp_id
        for key, field in (
            ("spec_ids", "spec_id"), ("code_branches", "branch"), ("code_commits", "commit"),
        ):
            values = origin.get(field)
            if not isinstance(values, list) or not values or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise MemorySyncError("实验记录缺少 SpecID、代码 Branch 或 Commit")
            metadata[key] = json.dumps(values, ensure_ascii=False)
    identity = hashlib.sha256(relative.encode()).hexdigest()[:24]
    return {
        "document_id": f"rhw-{project_id}-{identity}",
        "content": content.decode("utf-8"),
        "metadata": metadata,
        "tags": [f"rhw-project:{project_id}"],
    }


def verify_source(repo_root: Path, metadata: dict, project_id: str) -> str:
    if metadata.get("project_id") != project_id:
        return "unverified"
    if not all(isinstance(metadata.get(key), str) for key in (
        "source_path", "source_commit", "source_sha256",
    )):
        return "unverified"
    commit = metadata["source_commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return "unverified"
    try:
        workspace, path, _ = source_path(repo_root, metadata["source_path"])
        if Path(git(workspace, "rev-parse", "--show-toplevel")).resolve() != workspace:
            return "unverified"
        if not path.is_file():
            return "missing"
        relative = path.relative_to(workspace).as_posix()
        historic = subprocess.run(
            ["git", "-C", str(workspace), "show", f"{commit}:{relative}"],
            capture_output=True, check=False,
        )
        if historic.returncode or hashlib.sha256(historic.stdout).hexdigest() != metadata["source_sha256"]:
            return "unverified"
        current = committed_bytes(workspace, path)
        return "current" if hashlib.sha256(current).hexdigest() == metadata["source_sha256"] else "changed"
    except (OSError, MemorySyncError):
        return "unverified"


def make_client():
    values = {key: os.environ.get(key, "").strip() for key in (
        "HINDSIGHT_API_URL", "HINDSIGHT_API_KEY", "HINDSIGHT_BANK_ID",
    )}
    if not all(values.values()):
        raise MemorySyncError("先设置 HINDSIGHT_API_URL、HINDSIGHT_API_KEY 和 HINDSIGHT_BANK_ID")
    url = urlsplit(values["HINDSIGHT_API_URL"])
    local_http = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    if not url.hostname or (url.scheme != "https" and not local_http) or (
        url.username or url.password or url.query or url.fragment
    ):
        raise MemorySyncError("API URL 必须使用 HTTPS 或本机 HTTP，且不能包含凭据、查询串或片段")
    bank_id = identifier(values["HINDSIGHT_BANK_ID"])
    try:
        from hindsight_client import Hindsight
    except ImportError as error:
        raise MemorySyncError("请在独立 Python 环境安装 requirements-hindsight.txt") from error
    return Hindsight(
        base_url=values["HINDSIGHT_API_URL"], api_key=values["HINDSIGHT_API_KEY"], timeout=45,
    ), bank_id


def sync_document(client, bank_id: str, document: dict) -> dict:
    response = client.retain(bank_id=bank_id, **document)
    if getattr(response, "var_async", False):
        raise MemorySyncError("服务返回异步写入；尚未验证完成，请核查服务 operation 状态")
    return {"ok": True, "document_id": document["document_id"], "metadata": document["metadata"]}


def recall_documents(client, bank_id: str, repo_root: Path, project_id: str, query: str) -> dict:
    tag = f"rhw-project:{identifier(project_id)}"
    response = client.recall(
        bank_id=bank_id, query=query, types=["world", "experience"],
        max_tokens=1200, budget="low", tags=[tag], tags_match="all_strict",
    )
    results = []
    for item in response.results:
        if tag not in (item.tags or []):
            continue
        metadata = {key: value for key, value in (item.metadata or {}).items()
                    if key in METADATA_FIELDS and isinstance(value, str)}
        verification = verify_source(repo_root, metadata, project_id)
        results.append({
            "text": item.text[:1400], "document_id": item.document_id,
            "metadata": metadata, "source_verification": verification,
            "usable_as_current_source": verification == "current",
        })
        if len(results) == 5:
            break
    return {"ok": True, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--project-id", default=os.environ.get("HINDSIGHT_PROJECT_ID", ""))
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="准备一份已提交的科研摘要")
    sync.add_argument("source")
    sync.add_argument("--execute", action="store_true", help="实际写入 Hindsight")
    recall = commands.add_parser("recall", help="召回最多五条来源可核查的候选事实")
    recall.add_argument("query")
    args = parser.parse_args()
    client = None
    try:
        project_id = identifier(args.project_id)
        document = None
        if args.command == "sync":
            document = prepare_document(args.repo_root, args.source, project_id)
            if not args.execute:
                print(json.dumps({
                    "ok": True, "dry_run": True, "document_id": document["document_id"],
                    "metadata": document["metadata"], "source_bytes": len(document["content"].encode()),
                }, ensure_ascii=False))
                return 0
        elif not args.query.strip() or len(args.query) > 2000:
            raise MemorySyncError("查询不能为空且不能超过 2000 字符")
        client, bank_id = make_client()
        result = sync_document(client, bank_id, document) if document is not None else recall_documents(
            client, bank_id, args.repo_root, project_id, args.query,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (MemorySyncError, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    except Exception as error:
        # 外部异常可能携带响应正文；只输出类型和状态码。
        print(json.dumps({"ok": False, "error_type": type(error).__name__,
                          "status": getattr(error, "status", None)}))
        return 2
    finally:
        if client is not None:
            client.close()
