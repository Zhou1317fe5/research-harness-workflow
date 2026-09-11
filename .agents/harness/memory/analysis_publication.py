"""主分析的固定清单预览、显式确认及定向同步。"""

from __future__ import annotations

import contextlib
import fcntl
import json
import re
import time
from pathlib import Path

from harness.memory.research_memory import (
    CURATED_TAG, MAX_INPUT, MAX_SNAPSHOT, SYNC_POLICY, MemoryError, atomic, digest, now, sensitive,
)

SCHEMA = "rhw.analysis-publication.v1"
MAIN_ANALYSIS = re.compile(r"^research_workspace/experiments/([^/]+)/analysis/analysis\.md$")
BATCH_ID = re.compile(r"B[0-9a-f]{32}")
MAX_FILES = 1000
_DRAFT = re.compile(
    r"^(?:#\s+[^\n]*(?:\bdraft\b|\bwip\b|草稿|初稿)|"
    r"(?:status|状态)\s*[:：]\s*(?:draft|wip|in_progress|草稿|待定稿)\b)|"
    r"^\s*(?:[-*]\s+)?(?:TODO|TBD|FIXME|待填写|待补充|待定稿)\s*[:：.。]?\s*$|"
    r"<填写[^>]*>", re.I | re.M,
)
_CONVERSATION = re.compile(
    r"^\s*(?:user|assistant|system|tool(?:_result)?|用户|助手|系统|工具)\s*[:：]|"
    r"<\|(?:im_start|im_end|user|assistant|system|tool)|"
    r'^\s*\{\s*"(?:role|type)"\s*:\s*"(?:user|assistant|toolResult|session)"', re.I | re.M,
)
_LOG = re.compile(
    r"(?im)^\s*(?:Traceback \(most recent call last\)|"
    r"\d{4}-\d\d-\d\d[T ][^\n]*\b(?:INFO|DEBUG|ERROR|WARNING)\b|"
    r"(?:epoch|step|iter(?:ation)?)\s*[:= ]\s*\d+[^\n]*(?:loss|lr)\s*[:=])"
)
_SECRET_FLAG = re.compile(
    r"--(?:password|passwd|api-key|access-token|token|secret)(?:=|\s+)['\"]?[^\s'\"<>$]+|"
    r"(?:auth[_-]?token|api[_-]?token|ssh[_-]?password)['\"]?\s*[:=]\s*['\"]?[^\s'\"<>$]+", re.I,
)


def publication_issues(relative, text, *, primary=False):
    """只返回类别，不回显可能包含凭据的匹配内容。"""
    issues = []
    if sensitive(text) or _SECRET_FLAG.search(text):
        issues.append("sensitive_content")
    if not text.strip():
        issues.append("empty_analysis")
    stem = Path(relative).stem
    if re.search(r"(?i)(?:^|[_.-])(?:draft|wip|raw|logs?|transcript|conversation|session)(?:$|[_.-])", stem) or _DRAFT.search(text):
        issues.append("draft_or_unfinished")
    if _CONVERSATION.search(text):
        issues.append("raw_conversation")
    if len(_LOG.findall(text)) >= 3 or re.search(r"(?im)^```(?:log|jsonl)\s*$", text):
        issues.append("raw_log")
    if primary:
        sections = list(re.finditer(r"(?im)^#{1,6}\s+(Change|Result|Finding|Next)\s*[:：]?\s*$", text))
        if [match[1].lower() for match in sections] != ["change", "result", "finding", "next"]:
            issues.append("analysis_sections_missing")
        elif any(not text[match.end():sections[index + 1].start() if index + 1 < len(sections) else len(text)].strip()
                 for index, match in enumerate(sections)):
            issues.append("analysis_sections_empty")
    return sorted(set(issues))


@contextlib.contextmanager
def index_transaction(memory, *, write=False):
    """批量预览不回放事务；确认中的任何异常都不提交部分同步队列。"""
    with (memory.store / "index.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        state = json.loads(memory.state_path.read_text()) if memory.state_path.is_file() else {
            "schema_version": 1, "events": {}, "documents": {}, "jobs": {}, "transactions": {},
        }
        if state.get("schema_version") != 1 or any(not isinstance(state.get(key), dict)
                for key in ("events", "documents", "jobs", "transactions")):
            raise MemoryError("本地记忆索引无效，不能生成或确认发布清单")
        before = digest(state)
        yield state
        if write and digest(state) != before:
            atomic(memory.state_path, state)


def _quarantined(memory):
    path = memory.store / "quarantine.json"
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise MemoryError("隔离来源清单格式无效")
    return set(value)


def _inspect(memory, relative, state):
    match = MAIN_ANALYSIS.fullmatch(relative)
    if match is None:
        raise MemoryError("批量发布仅支持各实验的 analysis/analysis.md")
    if sensitive(relative) or _SECRET_FLAG.search(relative):
        return {"reference": digest(relative), "issues": ["sensitive_reference"]}
    entry = {"path": relative, "exp_id": match[1], "issues": []}
    try:
        path = memory.safe_path(relative)
        if not path.is_file():
            raise MemoryError("主分析必须是普通文件")
        with path.open("rb") as stream:
            raw = stream.read(MAX_SNAPSHOT + 1)
        entry["size_bytes"] = len(raw)
        if len(raw) > MAX_SNAPSHOT:
            entry["issues"] = ["analysis_too_large"]
            return entry
        text, metadata = memory._source(path, raw=raw)
    except (MemoryError, OSError, UnicodeError, ValueError):
        entry["issues"] = ["source_unavailable_or_unsafe_path"]
        return entry
    issues = publication_issues(relative, text, primary=True)
    if sensitive(json.dumps(metadata, ensure_ascii=False)):
        issues.append("sensitive_metadata")
    semantic = memory.document_semantics(metadata)
    sha = digest(raw)
    event_id = "E" + digest(["document", relative, semantic, sha])[:24]
    old_id = state["documents"].get(relative, {}).get("event_id")
    old = state["events"].get(old_id, {})
    if old.get("content_sha256") == sha and all(old.get(k) == v for k, v in semantic.items()):
        event_id = old_id
    if (event_id in _quarantined(memory) or state["events"].get(event_id, {}).get("quarantined")
            or state["events"].get(event_id, {}).get("disposition") == "quarantined"):
        issues.append("quarantined_source")
    if issues:
        entry["issues"] = sorted(set(issues))
        return entry
    key = digest([memory.config["project_id"], "document:" + relative])[:32]
    entry.update(sha256=sha, metadata_sha256=digest(semantic), event_id=event_id,
                 source_git_state=metadata.get("source_git_state"), job_key=key,
                 title=next((line.lstrip("# ") for line in text.splitlines() if line.startswith("#")), "analysis")[:120],
                 excerpt=text[:700], publication_state="unpublished", _text=text, _metadata=metadata)
    job = state["jobs"].get(key, {})
    if job.get("policy") == SYNC_POLICY and job.get("state") in {"pending", "submitted", "synced"}:
        try:
            payload = _payload(memory, key, job["revision"])
            meta = payload.get("metadata", {}) if isinstance(payload, dict) else {}
            same_version = isinstance(meta, dict) and meta.get("event_id") == event_id and meta.get("content_sha256") == sha
            if not payload_allowed(memory, state, key, job["revision"], payload, current=same_version):
                raise MemoryError("既有同步对象未通过身份检查")
            if same_version:
                entry["publication_state"] = job["state"]
        except (MemoryError, OSError, ValueError, KeyError):
            entry["issues"] = ["queue_payload_invalid"]
            entry.pop("_text", None)
            entry.pop("_metadata", None)
    return entry


def _public(entry):
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _folder(memory, batch_id):
    if not isinstance(batch_id, str) or BATCH_ID.fullmatch(batch_id) is None:
        raise MemoryError("发布清单 ID 无效")
    path = memory.store / "publications" / batch_id
    if any(parent.is_symlink() for parent in (path, path.parent, path.parent.parent)):
        raise MemoryError("发布清单目录不能是符号链接")
    return path


def _load(memory, batch_id):
    path = _folder(memory, batch_id) / "manifest.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INPUT:
        raise MemoryError("发布清单缺失或无效，需重新预览")
    plan = json.loads(path.read_text())
    if (not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA
            or plan.get("project_id") != memory.config["project_id"] or plan.get("repo_root") != str(memory.root)
            or "B" + digest(plan)[:32] != batch_id or not isinstance(plan.get("selected"), list)):
        raise MemoryError("发布清单已变化或不属于当前项目，需重新预览")
    return plan


def preview(memory, *, exp_ids=None, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise MemoryError("预览 limit 必须在 1 到 100 之间")
    if exp_ids is not None and any(not isinstance(value, str) or not value or value in {".", ".."}
                                 or any(c in value for c in "/\\\r\n\0") for value in exp_ids):
        raise MemoryError("ExpID 必须是单个实验目录名")
    base = memory.workspace / "experiments"
    if memory.workspace.is_symlink() or base.is_symlink():
        raise MemoryError("批量分析目录不能是符号链接")
    paths = ([f"research_workspace/experiments/{value}/analysis/analysis.md" for value in sorted(set(exp_ids))]
             if exp_ids else sorted(path.relative_to(memory.root).as_posix() for path in base.glob("*/analysis/analysis.md")))
    if len(paths) > MAX_FILES:
        raise MemoryError("一次最多预览 1000 份主分析，请用 --exp-id 缩小范围")
    with index_transaction(memory) as state:
        entries = [_inspect(memory, relative, state) for relative in paths]
    selected = [entry for entry in entries if not entry["issues"] and entry["publication_state"] != "synced"]
    plan = {"schema_version": SCHEMA, "repo_root": str(memory.root), "project_id": memory.config["project_id"],
            "created_at": now(), "selected": [_public(entry) for entry in selected],
            "blocked": [_public(entry) for entry in entries if entry["issues"]],
            "skipped": [_public(entry) for entry in entries if not entry["issues"] and entry["publication_state"] == "synced"]}
    batch_id = "B" + digest(plan)[:32]
    folder = _folder(memory, batch_id)
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    atomic(folder / "manifest.json", plan)
    review = [f"批量主分析预览 {batch_id}", "", "只对下列固定内容确认发布；此预览没有加入同步队列。", ""]
    for index, entry in enumerate(selected):
        atomic(folder / "files" / f"{index}.md", entry["_text"])
        review += [f"- {entry['exp_id']}：[{entry['path']}](files/{index}.md)，{entry['size_bytes']} bytes，"
                   f"SHA256 `{entry['sha256']}`，{entry['source_git_state']} / {entry['publication_state']}"]
    if plan["blocked"]:
        review += ["", "未纳入的文件：", ""]
        review += [f"- {entry.get('path', '敏感路径已隐藏')}：{', '.join(entry['issues'])}" for entry in plan["blocked"]]
    atomic(folder / "review.md", "\n".join(review) + "\n")
    return {"dry_run": True, "batch_id": batch_id, "selected_count": len(selected),
            "blocked_count": len(plan["blocked"]), "already_synced_count": len(plan["skipped"]),
            "total_bytes": sum(entry["size_bytes"] for entry in selected),
            "selected": plan["selected"][:limit], "blocked": plan["blocked"][:limit],
            "preview_path": str(folder / "review.md"), "manifest_path": str(folder / "manifest.json"),
            "requires_confirmation": bool(selected), "hindsight_enabled": memory.config["hindsight_enabled"]}


def _validate_files(memory, batch_id, plan, state):
    entries = []
    for index, expected in enumerate(plan["selected"]):
        current = _inspect(memory, expected["path"], state)
        snapshot = _folder(memory, batch_id) / "files" / f"{index}.md"
        if (current["issues"] or snapshot.is_symlink() or not snapshot.is_file()
                or snapshot.stat().st_size > MAX_SNAPSHOT
                or digest(snapshot.read_bytes()) != expected["sha256"]
                or any(current.get(key) != expected.get(key)
                       for key in ("sha256", "metadata_sha256", "event_id", "job_key", "size_bytes"))):
            raise MemoryError("主分析、身份信息或预览已变化，未发布任何新版本；请重新预览并确认")
        entries.append(current)
    return entries


def confirm(memory, batch_id):
    plan = _load(memory, batch_id)
    if not plan["selected"]:
        raise MemoryError("发布清单没有可确认的主分析")
    with index_transaction(memory, write=True) as state:
        entries = _validate_files(memory, batch_id, plan, state)
        batches = state.setdefault("publications", {})
        previous = batches.get(batch_id)
        if previous is not None:
            return {"batch_id": batch_id, "published_count": len(previous["jobs"]), "reused": True,
                    "queue": _queue_status(state, previous["jobs"])}
        jobs = {}
        for entry in entries:
            memory._register_document(state, entry["path"], entry["_text"], entry["_metadata"])
            memory._publish_document(state, entry["path"])
            jobs[entry["job_key"]] = state["jobs"][entry["job_key"]]["revision"]
        batches[batch_id] = {"confirmed_at": now(), "confirmation": "explicit_confirm", "jobs": jobs,
                             "project_id": memory.config["project_id"], "manifest_sha256": digest(plan)}
        return {"batch_id": batch_id, "published_count": len(jobs), "reused": False,
                "queue": _queue_status(state, jobs)}


def _queue_status(state, jobs):
    result = {"synced": 0, "pending": 0, "submitted": 0, "blocked": 0}
    for key, revision in jobs.items():
        job = state["jobs"].get(key, {})
        name = job.get("state") if job.get("revision") == revision else "blocked"
        result[name if name in result else "blocked"] += 1
    return result


def _payload(memory, key, revision):
    if not re.fullmatch(r"[0-9a-f]{32}", key) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise MemoryError("同步对象标识无效")
    path = memory.store / "outbox" / f"{key}-{revision}.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INPUT:
        raise MemoryError("同步对象缺失或超过上限")
    return json.loads(path.read_text())


def payload_allowed(memory, state, key, version, payload, *, current=True):
    """发送前核对精选对象和批准快照；原文任务不能借旧 inflight 混入。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        return False
    meta = payload["metadata"]
    tags = payload.get("tags", [])
    content = payload.get("content")
    if (not isinstance(content, str) or len(content.encode()) > MAX_SNAPSHOT
            or sensitive(json.dumps(payload, ensure_ascii=False))
            or meta.get("sync_policy") != SYNC_POLICY or meta.get("project_id") != memory.config["project_id"]
            or meta.get("revision") != version or payload.get("document_id") != "rhw-" + key
            or not isinstance(tags, list) or CURATED_TAG not in tags
            or "rhw-project:" + memory.config["project_id"] not in tags):
        return False
    base_meta = {k: v for k, v in meta.items() if k not in {"project_id", "revision"}}
    if digest([content, base_meta, tags]) != version:
        return False
    event = state["events"].get(meta.get("event_id"), {})
    if event.get("quarantined") or meta.get("event_id") in _quarantined(memory):
        return False
    if meta.get("source_role") == "analysis":
        relative = meta.get("source_ref", "")
        if (not event or event.get("actor") != "document" or event.get("disposition") != "recorded"
                or relative not in event.get("references", [])
                or meta.get("content_sha256") != digest(content.encode())
                or event.get("content_sha256") != meta.get("content_sha256")
                or digest([memory.config["project_id"], "document:" + relative])[:32] != key
                or publication_issues(relative, content)):
            return False
        if current:
            try:
                with memory.safe_path(relative).open("rb") as stream:
                    return digest(stream.read(MAX_SNAPSHOT + 1)) == meta["content_sha256"]
            except (MemoryError, OSError):
                return False
        return True
    cid = meta.get("record_ids", "")
    if (meta.get("source_role") not in {"decision", "finding", "hypothesis", "execution"}
            or not isinstance(cid, str) or not re.fullmatch(r"C[0-9]+", cid)
            or cid not in event.get("record_ids", []) or event.get("disposition") != "recorded"
            or meta.get("source_ref") != "research_workspace/CONCLUSIONS.md#" + cid.lower()
            or digest([memory.config["project_id"], "record:" + cid])[:32] != key):
        return False
    try:
        statuses = json.loads(meta.get("record_statuses", "{}"))
    except (ValueError, TypeError):
        return False
    return isinstance(statuses, dict) and statuses.get(cid) not in {None, "OPEN", "PROPOSED"}


def sync_batch(memory, batch_id, *, client=None, seconds=120):
    if type(seconds) is not int or not 1 <= seconds <= 900:
        raise MemoryError("批量同步 seconds 必须在 1 到 900 之间")
    plan = _load(memory, batch_id)
    with index_transaction(memory) as state:
        approval = state.get("publications", {}).get(batch_id)
        if not approval or approval.get("manifest_sha256") != digest(plan):
            raise MemoryError("此清单尚未确认，不能同步")
        _validate_files(memory, batch_id, plan, state)
        jobs = dict(approval["jobs"])
        todo = {key: version for key, version in jobs.items()
                if state["jobs"].get(key, {}).get("revision") == version
                and state["jobs"][key].get("state") in {"pending", "submitted"}}
    attempted = completed = 0
    error = None
    deadline = time.monotonic() + seconds
    owned = client is None
    try:
        if todo and memory.config["hindsight_enabled"]:
            if owned:
                from harness.memory.hindsight_mcp import HindsightMCP
                client = HindsightMCP.from_env(timeout=8)
            while todo and time.monotonic() < deadline:
                result = memory.sync(client=client, limit=min(20, len(todo)), job_revisions=todo,
                                     budget_seconds=min(50, max(0.01, deadline - time.monotonic())))
                attempted += result.get("attempted", 0)
                completed += result.get("completed", 0)
                visited = set(result.get("attempted_keys", [])) | set(result.get("skipped_keys", []))
                if result.get("error_type") or result.get("busy") or not visited:
                    error = result.get("error_type") or "sync_busy_or_no_progress"
                    break
                todo = {key: version for key, version in todo.items() if key not in visited}
    except Exception as exc:
        error = type(exc).__name__
    finally:
        if owned and client is not None:
            client.close()
    with index_transaction(memory) as state:
        queue = _queue_status(state, jobs)
        errors = []
        for key, version in jobs.items():
            job = state["jobs"].get(key, {})
            code = job.get("error")
            if job.get("revision") == version and code and job.get("state") != "synced":
                errors.append({"job_key": key, "error": code if isinstance(code, str)
                               and re.fullmatch(r"[A-Za-z0-9_]{1,100}", code) else "sync_job_error"})
    if errors and error is None:
        error = "publication_sync_error"
    return {"batch_id": batch_id, "enabled": memory.config["hindsight_enabled"], "attempted": attempted,
            "completed": completed, "queue": queue, "complete": queue["synced"] == len(jobs),
            "error_type": error, "errors": errors, "resume_argv": ["python", ".agents/harness/memory/research_memory.py",
                "--repo-root", str(memory.root), "--store", str(memory.store), "sync", "--batch", batch_id]}
