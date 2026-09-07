#!/usr/bin/env python3
"""科研记忆的宿主事件适配；同步工作与宿主前台回调分开。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness.memory.research_memory import MAX_INPUT, MemoryError, digest, sensitive, timestamp

TRANSCRIPT_BUDGET = 8 * 1024 * 1024


def session_key(payload, host):
    sid = payload.get("session_id") or "unknown"
    if not isinstance(sid, str) or len(sid) > 200 or any(c in sid for c in "\r\n\0"):
        raise MemoryError("无效的宿主 session_id")
    return host + ":" + sid


def native_turn(payload):
    value = payload.get("turn_id") or payload.get("prompt_id") or payload.get("message_id") or ""
    if not isinstance(value, str) or len(value) > 200 or any(c in value for c in "\r\n\0"):
        raise MemoryError("无效的宿主消息标识")
    return value


def capture_message(memory, payload, host, actor, text):
    if not isinstance(text, str) or len(text.encode()) > MAX_INPUT:
        raise MemoryError("宿主消息必须是 4 MiB 以内的文本")
    key, native = session_key(payload, host), native_turn(payload)
    source = payload.get("transcript_path") or "session:" + key
    if not isinstance(source, str) or len(source) > 2000 or any(c in source for c in "\r\n\0"):
        raise MemoryError("无效的会话来源引用")
    occurred_at = timestamp(payload.get("timestamp"))
    with memory.locked() as state:
        session = state["sessions"].setdefault(key, {})
        body_hash = digest(text.encode())
        if native:
            turn = native
        elif actor == "user":
            if session.get("phase") == "open" and session.get("prompt_hash") == body_hash:
                turn = session["turn"]
            else:
                session["sequence"] = session.get("sequence", 0) + 1
                turn = f"fallback-{session['sequence']}"
        else:
            turn = session.get("turn") or "fallback-stop"
        if actor == "user":
            session.update(turn=turn, prompt_hash=body_hash, phase="open")
        else:
            session.update(turn=turn, phase="closed")
        eid = memory._capture(
            state, actor, text, source, [key, turn, actor, body_hash],
            extra={"session_id": payload.get("session_id", "unknown"), "turn_id": turn,
                   "host": host, "occurred_at": occurred_at,
                   "identity_basis": "native" if native else "session_sequence"},
        )
    return eid


def last_reply(payload, host, turn):
    """旧宿主缺少 Stop 正文时的有限补收；不把 transcript 当稳定接口。"""
    value = payload.get("transcript_path")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    if path.suffix != ".jsonl" or path.is_symlink() or not path.is_file():
        return None
    size = path.stat().st_size
    offset = max(0, size - TRANSCRIPT_BUDGET)
    candidate, current_turn = None, ""
    with path.open("rb") as stream:
        stream.seek(offset)
        if offset:
            stream.readline()
        while stream.tell() < size:
            position = stream.tell()
            raw = stream.readline(MAX_INPUT + 1)
            if not raw.endswith(b"\n") or len(raw) > MAX_INPUT:
                if len(raw) > MAX_INPUT:
                    # 跳到下一行，不解析被截断的工具输出或推理数据。
                    while raw and not raw.endswith(b"\n"):
                        raw = stream.readline(MAX_INPUT + 1)
                continue
            try:
                item = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(item, dict):
                continue
            body, text = item.get("payload") or {}, None
            if not isinstance(body, dict):
                continue
            if host == "codex":
                if item.get("type") == "turn_context" or body.get("type") == "task_started":
                    current_turn = body.get("turn_id", "")
                if item.get("type") == "event_msg" and body.get("type") == "task_complete":
                    current_turn = body.get("turn_id", current_turn)
                    text = body.get("last_agent_message")
                elif (item.get("type") == "response_item" and body.get("type") == "message"
                      and body.get("role") == "assistant"
                      and (body.get("channel") == "final" or body.get("phase") == "final_answer")):
                    text = "\n".join(x.get("text", "") for x in body.get("content", [])
                                     if isinstance(x, dict) and x.get("type") in {"text", "output_text"})
            else:
                message = item.get("message") or {}
                if (item.get("type") == "assistant" and isinstance(message, dict)
                        and message.get("stop_reason") in {"end_turn", "stop_sequence"}):
                    text = "\n".join(x.get("text", "") for x in message.get("content", [])
                                     if isinstance(x, dict) and x.get("type") == "text")
                    current_turn = item.get("promptId") or item.get("prompt_id") or ""
            if isinstance(text, str) and text and (not turn or current_turn == turn):
                candidate = {"text": text, "source": str(path) + f"#byte={position}",
                             "timestamp": item.get("timestamp"), "turn_id": current_turn}
    return candidate


def capture_stop(memory, payload, host):
    body = payload.get("last_assistant_message")
    turn = native_turn(payload)
    if not isinstance(body, str) or not body:
        recovered = last_reply(payload, host, turn)
        if recovered:
            payload = {**payload, "transcript_path": recovered["source"],
                       "timestamp": recovered["timestamp"],
                       "turn_id": recovered["turn_id"] or turn}
            body = recovered["text"]
    if isinstance(body, str) and body:
        return capture_message(memory, payload, host, "assistant", body)
    key = session_key(payload, host)
    with memory.locked() as state:
        session = state["sessions"].setdefault(key, {})
        turn = turn or session.get("turn", "unknown")
        session["phase"] = "closed"
        return memory._capture(
            state, "notice", "宿主未提供可确认的最终回复；保留来源缺口，恢复时尝试补收。",
            payload.get("transcript_path") or "session:" + key,
            [key, turn, "missing_final"],
            extra={"host": host, "session_id": payload.get("session_id", "unknown"),
                   "turn_id": turn, "source_role": "capture_gap"}, disposition="waiting",
        )


def recover_replies(memory, payload, host):
    key = session_key(payload, host)
    with memory.locked() as state:
        gaps = [dict(e) for e in state["events"].values()
                if e.get("source_role") == "capture_gap" and e["disposition"] == "waiting"
                and e.get("host") == host and e.get("session_id") == payload.get("session_id")]
    for gap in gaps:
        recovered = last_reply({**payload, "transcript_path": gap["source"]}, host, gap["turn_id"])
        if not recovered:
            continue
        eid = capture_message(memory, {**payload, "turn_id": recovered["turn_id"],
                              "timestamp": recovered["timestamp"], "transcript_path": recovered["source"]},
                              host, "assistant", recovered["text"])
        with memory.locked() as state:
            state["events"][gap["id"]].update(
                disposition="discarded", reason="已补收最终回复，见 " + eid, resolved_by_event=eid)
    return key


def start_sync(memory):
    if not memory.config["hindsight_enabled"]:
        return
    # worker 自己取得非阻塞同步锁；关闭所有宿主管道，宿主不等待网络返回。
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("research_memory.py")),
             "--repo-root", str(memory.root), "--store", str(memory.store), "sync", "--limit", "4"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
    except OSError as error:
        with memory.locked() as state:
            state["sync_worker_error"] = type(error).__name__


def handle_hook(memory, payload, action, host="codex"):
    if not isinstance(payload, dict):
        raise MemoryError("宿主事件必须是 JSON 对象")
    event_name = payload.get("hook_event_name") or "SessionStart"
    key = session_key(payload, host)
    if action == "prompt":
        capture_message(memory, payload, host, "user", payload.get("prompt"))
    elif action == "stop":
        capture_stop(memory, payload, host)
    elif action == "context":
        recover_replies(memory, payload, host)
    elif action == "sync":
        start_sync(memory)
        return {}
    changes = memory.scan()
    if action in {"context", "stop"} or changes:
        start_sync(memory)
    if action in {"context", "prompt"} or action == "scan" and changes:
        query = payload.get("prompt") or ""
        if not isinstance(query, str) or sensitive(query):
            query = ""
        # 生命周期回调只读取本地；可选远端检索由 recall 命令按需执行。
        return {"hookSpecificOutput": {"hookEventName": event_name,
                                       "additionalContext": memory.context(query)}}
    return {}
