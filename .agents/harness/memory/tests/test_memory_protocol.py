"""install_memory_hooks 往返与 memory_hooks、hindsight_mcp、hindsight_memory 低层协议；不使用宿主配置，不打真网络，全部隔离目录。

宿主配置由 install() 以原子替换写入（mode 0600）；fixture 文件保持最小权限，
避免与本仓库 pi-interpreter-guard 的宽泛 chmod 策略纠缠。
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
from harness.memory import install_memory_hooks as installer
from harness.memory.hindsight_mcp import HindsightMCP, MCPError, MAX_RESPONSE_BYTES, unpack_result
from harness.memory import hindsight_memory as hm
from harness.memory.memory_hooks import (
    capture_stop, last_reply, native_turn, recover_replies, session_key,
)
from harness.memory.research_memory import Memory, MemoryError


def chmod(path, mode):
    path.chmod(mode)


class InstallHooksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-install-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        memory = self.root / ".agents/harness/memory"
        memory.mkdir(parents=True)
        script = self.root / ".agents/harness/memory/research_memory.py"
        script.write_text("# fixture script\n")
        (self.root / ".agents/harness/memory/memory_hooks.py").write_text("# fixture\n")
        self.script = script

    def install(self, **kwargs):
        return [Path(path) for path in installer.install(self.root, **kwargs)]

    def hooks(self, host):
        path = self.root / (".codex/hooks.json" if host == "codex" else ".claude/settings.local.json")
        return json.loads(path.read_text())["hooks"]

    def commands(self, host, event):
        return [handler["command"] for group in self.hooks(host).get(event, [])
                for handler in group["hooks"]]

    def test_install_both_hosts_then_remove_returns_to_absent(self):
        changed = self.install()
        self.assertEqual({path.name for path in changed},
                         {"hooks.json", "settings.local.json"})
        for host in ("codex", "claude"):
            for event, action in installer.EVENTS.items():
                commands = self.commands(host, event)
                self.assertEqual(len(commands), 1, (host, event))
                command = commands[0]
                # command 是 shlex.join 包裹；action/host 作为 shell 位置参数出现。
                self.assertIn(str(self.root), command)
                self.assertIn("hook --action", command)
                self.assertTrue(command.rstrip().endswith(f" {host}"), command)
                self.assertIn(f"\n", command) if False else None
                self.assertIn("--binding research-memory-v1", command)
        removed = self.install(remove=True)
        self.assertEqual(sorted(removed), sorted(changed))
        for host_path in (self.root / ".codex/hooks.json",
                          self.root / ".claude/settings.local.json"):
            data = json.loads(host_path.read_text())
            self.assertEqual(data.get("hooks"), {})
        # 卸载后再次卸载是空操作；全新安装再次成为变更。
        self.assertEqual(self.install(remove=True), [])
        self.install()

    def test_install_is_idempotent_and_perm_closed(self):
        first = self.install()
        self.assertTrue(first)
        initial = {path.name: path.read_bytes() for path in first}
        later = self.install()
        self.assertEqual(later, [])
        self.assertEqual(initial, {path.name: path.read_bytes() for path in first})
        for path in first:
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_install_list_value_shape(self):
        changed = sorted(self.install())
        self.assertEqual([path.relative_to(self.root).as_posix() for path in changed],
                         [".claude/settings.local.json", ".codex/hooks.json"])

    def test_foreign_config_and_handlers_survive_install_and_remove(self):
        codex = self.root / ".codex/hooks.json"
        codex.parent.mkdir()
        foreign_hook = {"type": "command", "command": "echo foreign", "timeout": 5}
        codex.write_text(json.dumps({
            "other_key": {"preserve": True},
            "hooks": {"SessionStart": [
                {"matcher": ".*", "hooks": [foreign_hook]},
                {"hooks": [{"type": "command",
                            "command": "research_memory.py --binding other-marker"}]},
                {"hooks": [{"type": "command",
                            "command": f"research_memory.py --binding {installer.MARKER} extra"}]},
                {"hooks": []},
            ]},
        }))
        chmod(codex, 0o600)
        self.install(hosts=("codex",))
        session = self.hooks("codex")["SessionStart"]
        commands = [handler["command"] for group in session for handler in group["hooks"]]
        self.assertIn("echo foreign", commands)
        self.assertIn("research_memory.py --binding other-marker", commands)
        self.assertIn("research_memory.py --binding other-marker", commands)
        # 我们自己注入的 install hook 存在且识别为 ours。
        owned = [command for command in commands if installer.owned({"command": command})]
        self.assertEqual(len(owned), 1)
        self.assertEqual(json.loads(codex.read_text())["other_key"], {"preserve": True})
        self.assertTrue(self.install(remove=True, hosts=("codex",)))
        session = self.hooks("codex")["SessionStart"]
        remaining = [handler["command"] for group in session for handler in group["hooks"]]
        self.assertIn("echo foreign", remaining)
        self.assertIn("research_memory.py --binding other-marker", remaining)
        # `--binding research-memory-v1 extra` 被 shlex 拆为三个 token，owned 检查子串
        # `--binding research-memory-v1` 不命中，该 handler 被保留。见 BUG-CANDIDATE。
        self.assertIn(f"research_memory.py --binding {installer.MARKER} extra", remaining)
        owned_after = [command for command in remaining if installer.owned({"command": command})]
        self.assertEqual(owned_after, [])
        self.assertTrue(any(group.get("matcher") == ".*" for group in session))
        self.assertEqual(json.loads(codex.read_text())["other_key"], {"preserve": True})

    def test_remove_only_both_hosts_and_missing_file_skips_cleanly(self):
        self.assertEqual(self.install(remove=True), [])
        self.assertFalse((self.root / ".codex/hooks.json").exists())

    def test_invalid_hosts_and_missing_harness_are_rejected(self):
        with self.assertRaises(ValueError):
            installer.install(self.root / "absent")
        # host 未在白名单中命中：当前实现静默 0 变更（见 BUG-CANDIDATE）。
        self.assertEqual(self.install(hosts=("pi",)), [])

    def test_symlinked_host_config_or_parent_is_rejected(self):
        target = self.root / "elsewhere.json"
        target.write_text("{}")
        chmod(target, 0o600)
        link = self.root / ".codex/hooks.json"
        link.parent.mkdir()
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            self.install(hosts=("codex",))
        link.unlink()
        (self.root / ".codex").rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "hooks.json").write_text("{}")
        (self.root / ".codex").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.install(hosts=("codex",))

    def test_invalid_existing_config_shapes_are_rejected_without_overwrite(self):
        codex = self.root / ".codex/hooks.json"
        codex.parent.mkdir()
        cases = [
            "[]",
            json.dumps({"hooks": []}),
            json.dumps({"hooks": {"SessionStart": {"hooks": []}}}),
            json.dumps({"hooks": {"SessionStart": [{"hooks": "not-a-list"}]}}),
            json.dumps({"hooks": {"SessionStart": [{"hooks": ["not-a-dict"]}]}}),
        ]
        for text in cases:
            codex.write_text(text)
            chmod(codex, 0o600)
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.install(hosts=("codex",))
            self.assertEqual(codex.read_text(), text)

    def test_command_parser_matches_owned_binding_only(self):
        owned = installer.command(self.root, "scan", "codex")
        self.assertTrue(installer.owned({"command": owned}))
        script = str(self.root / ".agents/harness/memory/research_memory.py")
        for command in (
            # 不同 marker 拒绝；不同脚本名拒绝；同 marker 但带额外字符的也拒绝（owned 靠子串）。
            f"python {script} hook --binding other-marker",
            f"python other.py hook --binding {installer.MARKER}",
            f"python {script} hook --binding prefix-{installer.MARKER}",
            "'unterminated",
        ):
            self.assertFalse(installer.owned({"command": command}), command)
        self.assertFalse(installer.owned({"command": 42}))
        self.assertFalse(installer.owned({}))


class MemoryHooksUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-hooks-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = Memory(self.root)

    def events(self):
        if not self.memory.state_path.exists():
            return []
        return json.loads(self.memory.state_path.read_text())["events"].values()

    def test_identity_validators(self):
        self.assertEqual(session_key({"session_id": "s"}, "pi"), "pi:s")
        self.assertEqual(session_key({}, "codex"), "codex:unknown")
        for payload in ({"session_id": "x" * 201}, {"session_id": "a\nb"}, {"session_id": 1}):
            with self.assertRaises(MemoryError):
                session_key(payload, "pi")
        self.assertEqual(native_turn({"turn_id": "t-1"}), "t-1")
        self.assertEqual(native_turn({"prompt_id": "p-2"}), "p-2")
        self.assertEqual(native_turn({"message_id": "m-3"}), "m-3")
        self.assertEqual(native_turn({}), "")
        with self.assertRaises(MemoryError):
            native_turn({"turn_id": "x" * 201})

    def codex_transcript(self, path, *, turn="t-1"):
        lines = [
            json.dumps({"type": "turn_context", "payload": {"type": "task_started", "turn_id": turn}}),
            json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "channel": "final",
                "content": [{"type": "output_text", "text": "旧回复"}]}}),
            "not json at all",
            json.dumps(["not-a-dict"]),
            json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": turn, "last_agent_message": "最终回复"},
                "timestamp": "2026-09-01T00:00:00Z"}),
        ]
        path.write_bytes(("\n".join(lines) + "\n").encode())

    def test_last_reply_prefers_task_complete_and_records_source(self):
        path = self.root / "rollout.jsonl"
        self.codex_transcript(path)
        found = last_reply({"transcript_path": str(path)}, "codex", "t-1")
        self.assertEqual(found["text"], "最终回复")
        self.assertIn("#byte=", found["source"])
        self.assertEqual(found["turn_id"], "t-1")
        self.assertEqual(found["timestamp"], "2026-09-01T00:00:00Z")
        found_all = last_reply({"transcript_path": str(path)}, "codex", None)
        self.assertEqual(found_all["text"], "最终回复")
        missing_turn = last_reply({"transcript_path": str(path)}, "codex", "other-turn")
        self.assertIsNone(missing_turn)

    def test_last_reply_turn_filter_uses_last_matching_candidate(self):
        path = self.root / "rollout.jsonl"
        lines = [
            json.dumps({"type": "turn_context", "payload": {"type": "task_started", "turn_id": "t-1"}}),
            json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "text", "text": "第一轮"}]}}),
            json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "text", "text": "第一轮补充"}]}}),
        ]
        path.write_text("\n".join(lines) + "\n")
        found = last_reply({"transcript_path": str(path)}, "codex", "t-1")
        self.assertEqual(found["text"], "第一轮补充")

    def test_last_reply_rejects_unsafe_transcript_sources(self):
        self.assertIsNone(last_reply({"transcript_path": 42}, "codex", None))
        self.assertIsNone(last_reply({"transcript_path": ""}, "codex", None))
        self.assertIsNone(last_reply({}, "codex", None))
        missing = self.root / "absent.jsonl"
        self.assertIsNone(last_reply({"transcript_path": str(missing)}, "codex", None))
        other = self.root / "transcript.json"
        other.write_text("{}")
        self.assertIsNone(last_reply({"transcript_path": str(other)}, "codex", None))
        target = self.root / "real.jsonl"
        target.write_text("{}\n")
        link = self.root / "link.jsonl"
        link.symlink_to(target)
        self.assertIsNone(last_reply({"transcript_path": str(link)}, "codex", None))

    def test_last_reply_skips_oversized_and_unterminated_lines(self):
        path = self.root / "rollout.jsonl"
        path.write_bytes(b"\n".join([
            b'{"type":"event_msg","payload":{"type":"task_complete","last_agent_message":"x"}} garbage-no-newline' + b" " * 3000,
            b"garbage without closing newline",
        ]))
        self.assertIsNone(last_reply({"transcript_path": str(path)}, "codex", None))
        big_first = self.root / "big.jsonl"
        big_first.write_bytes(b"x" * 9000)
        path.write_bytes(b"\n".join([
            b" ",
            json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "late", "last_agent_message": "尾部回复"}}).encode(),
            b"",
        ]))
        found = last_reply({"transcript_path": str(path)}, "codex", "late")
        self.assertEqual(found["text"], "尾部回复")

    def test_last_reply_claude_stop_reason_and_prompt_id(self):
        path = self.root / "claude.jsonl"
        lines = [
            json.dumps({"type": "assistant", "promptId": "p-1", "message": {
                "stop_reason": "tool_use", "content": [{"type": "text", "text": "忽略"}]}}),
            json.dumps({"type": "assistant", "promptId": "p-1", "timestamp": "t", "message": {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "先行"}, {"type": "text", "text": "收尾"}]}}),
            json.dumps({"type": "assistant", "prompt_id": "legacy", "message": {
                "stop_reason": "end_turn", "content": [{"type": "text", "text": "旧格式"}]}}),
        ]
        path.write_text("\n".join(lines) + "\n")
        found = last_reply({"transcript_path": str(path)}, "claude", "p-1")
        self.assertEqual(found["text"], "先行\n收尾")
        self.assertEqual(found["turn_id"], "p-1")
        legacy = last_reply({"transcript_path": str(path)}, "claude", "legacy")
        self.assertEqual(legacy["text"], "旧格式")

    def test_capture_stop_gap_then_recover_replies_closes_it(self):
        path = self.root / "rollout.jsonl"
        payload = {"session_id": "s1", "turn_id": "t-1", "transcript_path": str(path)}
        result = capture_stop(self.memory, payload, host="codex")
        events = list(self.events())
        self.assertEqual(len(events), 1)
        gap = events[0]
        self.assertEqual(result, gap["id"])
        self.assertEqual(gap["source_role"], "capture_gap")
        self.assertEqual(gap["disposition"], "waiting")
        self.assertEqual(gap["turn_id"], "t-1")
        self.codex_transcript(path, turn="t-1")
        recover_replies(self.memory, payload, host="codex")
        events = list(self.events())
        self.assertEqual(len(events), 2)
        gap_event = next(event for event in events if event["id"] == gap["id"])
        self.assertEqual(gap_event["disposition"], "discarded")
        recovered = next(event for event in events if event["id"] != gap["id"])
        self.assertEqual(recovered["actor"], "assistant")
        self.assertIn("最终回复", recovered.get("preview", ""))
        self.assertEqual(gap_event["resolved_by_event"], recovered["id"])
        session_host_mismatch = recover_replies(self.memory, {**payload, "session_id": "other"}, host="pi")
        self.assertEqual(session_host_mismatch, "pi:other")
        self.assertEqual(len(list(self.events())), 2)

    def test_capture_stop_without_transcript_file_keeps_gap(self):
        payload = {"session_id": "s2", "turn_id": "t-9",
                   "transcript_path": str(self.root / "missing.jsonl")}
        capture_stop(self.memory, payload, host="codex")
        recover_replies(self.memory, payload, host="codex")
        events = list(self.events())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["disposition"], "waiting")

    def test_capture_stop_prefers_payload_body_over_transcript(self):
        path = self.root / "rollout.jsonl"
        self.codex_transcript(path, turn="t-1")
        result = capture_stop(self.memory, {
            "session_id": "s3", "turn_id": "t-1", "transcript_path": str(path),
            "last_assistant_message": "宿主已提供正文",
        }, host="codex")
        events = list(self.events())
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["actor"], "assistant")
        # capture_message 记录 origin=primary_session；capture_gap 是 source_role=capture_gap。
        self.assertNotEqual(event.get("source_role"), "capture_gap")
        self.assertEqual(event.get("origin"), "primary_session")
        self.assertEqual(result, event["id"])



class HindsightMCPProtocolTests(unittest.TestCase):
    """MCP 客户端走本地 opener；响应体固定为夹具，不打真网络。"""

    def make_client(self, url="https://hindsight.example.com/mcp/b1/", *, timeout=1.0):
        return HindsightMCP(url, "fixture-key", timeout=timeout)

    class StaticResponse:
        def __init__(self, body, *, status=200, headers=None):
            self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.status = status
            self.headers = headers or {}
            self._reader = iter([self._body + b"\n"])

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def readline(self, limit=-1):
            try:
                return next(self._reader)
            except StopIteration:
                return b""

        def read(self, limit=-1):
            data = self._body if limit < 0 else self._body[:limit]
            self._body = self._body[len(data):]
            return data

    def test_init_rejects_invalid_endpoints_and_missing_key(self):
        for url in ("https://user:pw@host/mcp/x/", "https://host/mcp/x/?query=1",
                    "https://host/mcp/x/#f", "ftp://host/mcp/x/",
                    "http://remote.example.com/mcp/x/", ""):
            with self.subTest(url=url), self.assertRaises(MCPError):
                HindsightMCP(url, "k")
        with self.assertRaises(MCPError):
            HindsightMCP("https://host/mcp/x/", "")
        with self.assertRaises(MCPError):
            HindsightMCP("https://host/mcp/x/", "k", timeout=0)
        local = HindsightMCP("http://127.0.0.1:8000/mcp/x/", "k")
        self.assertEqual(local.url, "http://127.0.0.1:8000/mcp/x/")

    def test_from_env_url_and_bank_fallback(self):
        for env, expects in (
            ({"HINDSIGHT_MCP_URL": "https://host/mcp/b/", "HINDSIGHT_API_KEY": "k"},
             "https://host/mcp/b/"),
            ({"HINDSIGHT_API_URL": "https://host/api", "HINDSIGHT_BANK_ID": "bank 1",
              "HINDSIGHT_API_KEY": "k"},
             "https://host/api/mcp/bank%201/"),
        ):
            with patch.dict(os.environ, env, clear=False), \
                    patch.object(HindsightMCP, "__init__") as init:
                init.return_value = None
                HindsightMCP.from_env()
                url = init.call_args[0][0]
                self.assertEqual(url, expects)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(MCPError):
            HindsightMCP.from_env()

    def test_rpc_returns_result_for_matching_json(self):
        client = self.make_client()
        captured = {}

        def open_stub(request, timeout=None):
            body = json.loads(request.data)
            captured["body"] = body
            captured["headers"] = dict(request.headers)
            return self.StaticResponse({"jsonrpc": "2.0", "id": body["id"],
                                        "result": {"ok": True}})

        client.opener = type("Op", (), {"open": staticmethod(open_stub)})
        result = client._rpc("tools/list", {"cursor": "c"})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["body"]["method"], "tools/list")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer fixture-key")

    def test_rpc_handles_sse_and_rejects_mismatched_identity(self):
        client = self.make_client()

        def sse(request, timeout=None):
            events = [
                b"data: " + json.dumps({"id": 1, "result": {"via": "sse"}}).encode() + b"\n",
                b"\n",
            ]

            class StreamResponse:
                status = 200
                headers = {"Content-Type": "text/event-stream"}

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def readline(self, limit=-1):
                    return events.pop(0) if events else b""

            return StreamResponse()

        client.opener = type("Op", (), {"open": staticmethod(sse)})
        result = client._rpc("tools/call", {"name": "recall"})
        self.assertEqual(result, {"via": "sse"})

        def bad(request, timeout=None):
            return self.StaticResponse({"jsonrpc": "2.0", "id": 999, "result": {}})

        client.opener = type("Op", (), {"open": staticmethod(bad)})
        with self.assertRaisesRegex(MCPError, "invalid JSON-RPC response identity"):
            client._rpc("tools/list")

    def test_rpc_maps_rpc_error_and_transport_errors(self):
        import urllib.error
        client = self.make_client()

        def rpc_error(request, timeout=None):
            body = json.loads(request.data)
            return self.StaticResponse({"jsonrpc": "2.0", "id": body["id"],
                                        "error": {"code": -32601, "message": "no"}})

        client.opener = type("Op", (), {"open": staticmethod(rpc_error)})
        with self.assertRaisesRegex(MCPError, "MCP RPC error"):
            client._rpc("tools/list")

        def http_error(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 500, "server", {}, None)

        client.opener = type("Op", (), {"open": staticmethod(http_error)})
        with self.assertRaisesRegex(MCPError, "MCP HTTP 500"):
            client._rpc("tools/list")

        def url_error(request, timeout=None):
            raise urllib.error.URLError("offline")

        client.opener = type("Op", (), {"open": staticmethod(url_error)})
        with self.assertRaisesRegex(MCPError, "MCP transport error"):
            client._rpc("tools/list")

    def test_rpc_refuses_oversized_json(self):
        client = self.make_client()
        big = b"{" + b"\"data\":\"" + b"x" * MAX_RESPONSE_BYTES + b"\"}"

        def large(request, timeout=None):
            return self.StaticResponse(big)

        client.opener = type("Op", (), {"open": staticmethod(large)})
        with self.assertRaisesRegex(MCPError, "MCP response exceeds byte budget"):
            client._rpc("tools/list")

    def test_initialize_list_tools_call_and_close(self):
        client = self.make_client()
        calls = []

        def responder(request, timeout=None):
            body = json.loads(request.data)
            calls.append(body)
            if body["method"] == "initialize":
                return self.StaticResponse(
                    {"jsonrpc": "2.0", "id": body["id"],
                     "result": {"protocolVersion": "2024-11-05",
                                "capabilities": {"tools": {}}}},
                    headers={"Mcp-Session-Id": "sess-1"})
            if body["method"] == "notifications/initialized":
                return self.StaticResponse({}, status=202)
            if body["method"] == "tools/list":
                if "cursor" not in (body.get("params") or {}):
                    return self.StaticResponse({"jsonrpc": "2.0", "id": body["id"],
                                                "result": {"tools": [{"name": "a"}], "nextCursor": "2"}})
                return self.StaticResponse({"jsonrpc": "2.0", "id": body["id"],
                                            "result": {"tools": [{"name": "b"}]}})
            if body["method"] == "tools/call":
                return self.StaticResponse({"jsonrpc": "2.0", "id": body["id"],
                                            "result": {"structuredContent": {"answer": 1}}})
            raise AssertionError(body)

        client.opener = type("Op", (), {"open": staticmethod(responder)})
        tools = client.list_tools()
        self.assertEqual([tool["name"] for tool in tools], ["a", "b"])
        self.assertTrue(client.initialized)
        self.assertEqual(client.headers["Mcp-Session-Id"], "sess-1")
        self.assertEqual(client.headers["MCP-Protocol-Version"], "2024-11-05")
        before = len(calls)
        result = client.call("recall", {"query": "q"})
        self.assertEqual(result, {"structuredContent": {"answer": 1}})
        methods = [c["method"] for c in calls[before:]]
        self.assertEqual(methods, ["tools/call"])

    def test_initialize_rejects_without_tools(self):
        client = self.make_client()

        def responder(request, timeout=None):
            body = json.loads(request.data)
            if body["method"] == "initialize":
                return self.StaticResponse({"jsonrpc": "2.0", "id": body["id"],
                                            "result": {"protocolVersion": "2024-11-05",
                                                       "capabilities": {}}})
            return self.StaticResponse({}, status=202)

        client.opener = type("Op", (), {"open": staticmethod(responder)})
        with self.assertRaisesRegex(MCPError, "server does not advertise MCP tools"):
            client.initialize()

    def test_unpack_result_prefers_structured_content(self):
        self.assertEqual(unpack_result({"structuredContent": {"x": 1}}), {"x": 1})
        self.assertEqual(unpack_result({"content": [{"type": "text", "text": "{\"x\":2}"}]}),
                         {"x": 2})
        self.assertEqual(unpack_result({"content": [{"type": "text", "text": "plain"}]}),
                         {"text": "plain"})
        with self.assertRaises(MCPError):
            unpack_result({"isError": True})


class HindsightMemoryProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hindsight-memory-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "research_workspace"
        self.workspace.mkdir()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.workspace), *args],
                              capture_output=True, text=True, check=True,
                              env={**os.environ, "HOME": str(self.root)}).stdout.strip()

    def init_workspace_git(self):
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")

    def commit_workspace(self, *paths):
        if not paths:
            self.git("commit", "-q", "--allow-empty", "-m", "init")
            return self.git("rev-parse", "HEAD")
        self.git("add", *[str(p) for p in paths])
        self.git("commit", "-q", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_identifier_accepts_only_strict_shape(self):
        self.assertEqual(hm.identifier("exp_2024.01-A"), "exp_2024.01-A")
        for value in ("", " leading", "a b", "../up", "-lead", "包含中文", "x" * 129):
            with self.subTest(value=value), self.assertRaises(hm.MemorySyncError):
                hm.identifier(value)

    def test_source_path_enforces_allowlist_and_symlinks(self):
        state = self.write("research_workspace/STATE.md", "S\n")
        self.write("research_workspace/CONCLUSIONS.md", "C\n")
        self.write("research_workspace/experiments/E1/analysis/analysis.md", "A\n")
        for source in ("research_workspace/STATE.md", "research_workspace/CONCLUSIONS.md",
                       "research_workspace/experiments/E1/analysis/analysis.md"):
            workspace, path, relative = hm.source_path(self.root, source)
            self.assertTrue(path.is_relative_to(workspace))
            self.assertEqual(relative, source)
        for bad in (
            "research_workspace/../../etc/passwd",
            "research_workspace/experiments/E1/analysis.mkd",
            "research_workspace/analysis.md",
            "remote_artifacts/E1/summary.json",
            "/tmp/outside",
            "research_workspace/STATE.md/extra",
        ):
            with self.subTest(bad=bad), self.assertRaises(hm.MemorySyncError):
                hm.source_path(self.root, bad)
        link = self.workspace / "CONCLUSIONS.md"
        link.unlink()
        link.symlink_to(state)
        with self.assertRaises(hm.MemorySyncError):
            hm.source_path(self.root, "research_workspace/CONCLUSIONS.md")

    def test_prepare_document_requires_committed_source_and_matching_record(self):
        self.init_workspace_git()
        self.write("research_workspace/STATE.md", "状态内容\n")
        record = {"exp_id": "E1", "source": {"spec_id": ["SPEC"], "branch": ["main"],
                                            "commit": ["a" * 40]}}
        self.write("research_workspace/experiments/E1/record.json", json.dumps(record))
        self.write("research_workspace/experiments/E1/analysis/analysis.md", "# 分析\n内容\n")
        self.commit_workspace("STATE.md", "experiments/E1/record.json",
                              "experiments/E1/analysis/analysis.md")
        document = hm.prepare_document(self.root, "research_workspace/experiments/E1/analysis/analysis.md",
                                       "demo-project")
        self.assertTrue(document["document_id"].startswith("rhw-demo-project-"))
        metadata = document["metadata"]
        self.assertEqual(metadata["project_id"], "demo-project")
        self.assertEqual(metadata["exp_id"], "E1")
        self.assertEqual(metadata["spec_ids"], '["SPEC"]')
        self.assertEqual(metadata["code_commits"], json.dumps(["a" * 40]))
        self.assertEqual(metadata["source_commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(metadata["source_path"],
                         "research_workspace/experiments/E1/analysis/analysis.md")
        self.assertIn("rhw-project:demo-project", document["tags"])

    def test_prepare_document_rejects_dirty_workspace_and_missing_fields(self):
        self.init_workspace_git()
        self.write("research_workspace/STATE.md", "第一版\n")
        self.commit_workspace("STATE.md")
        self.write("research_workspace/STATE.md", "第二版（未提交）\n")
        with self.assertRaisesRegex(hm.MemorySyncError, "未提交"):
            hm.prepare_document(self.root, "research_workspace/STATE.md", "demo")
        self.commit_workspace("STATE.md")
        record = {"exp_id": "E1", "source": {"spec_id": []}}
        self.write("research_workspace/experiments/E1/record.json", json.dumps(record))
        self.write("research_workspace/experiments/E1/analysis/analysis.md", "# a\n")
        self.commit_workspace("experiments/E1/record.json", "experiments/E1/analysis/analysis.md")
        with self.assertRaisesRegex(hm.MemorySyncError, "SpecID"):
            hm.prepare_document(self.root, "research_workspace/experiments/E1/analysis/analysis.md",
                                "demo")

    def test_verify_source_treats_unknown_and_mutated_as_unverified(self):
        self.init_workspace_git()
        self.commit_workspace()
        base = {"project_id": "demo", "source_commit": self.git("rev-parse", "HEAD"),
                "source_sha256": "0" * 64}
        self.assertEqual(hm.verify_source(self.root, {}, "demo"), "unverified")
        self.assertEqual(hm.verify_source(self.root, {**base, "source_path": "STATE.md"}, "other"),
                         "unverified")
        no_history = {**base, "source_path": "research_workspace/STATE.md"}
        # 隔离仓 无 STATE.md 文件，与 verify_source 中的 missing 分支一致；
        # 未提交变化走 committed_bytes 的 MemorySyncError 捕获，返回 unverified。
        self.assertEqual(hm.verify_source(self.root, no_history, "demo"), "missing")

    def test_verify_source_classifies_current_and_rejects_dirty_state(self):
        import hashlib
        self.init_workspace_git()
        path = self.write("research_workspace/STATE.md", "稳定内容\n")
        commit = self.commit_workspace("STATE.md")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        metadata = {"project_id": "demo", "source_path": "research_workspace/STATE.md",
                    "source_commit": commit, "source_sha256": sha}
        self.assertEqual(hm.verify_source(self.root, metadata, "demo"), "current")
        # 未提交修改：committed_bytes 报错被 MemorySyncError 捕获，返回 unverified
        # （语义：当前状态无法证明 = metadata，不猜测 changed）。
        self.write("research_workspace/STATE.md", "改过未提交\n")
        self.assertEqual(hm.verify_source(self.root, metadata, "demo"), "unverified")
        self.git("checkout", "--", "STATE.md")
        self.assertEqual(hm.verify_source(self.root, metadata, "demo"), "current")
        missing = {**metadata, "source_path": "research_workspace/CONCLUSIONS.md"}
        self.assertEqual(hm.verify_source(self.root, missing, "demo"), "missing")

    def test_sync_document_rejects_async_ack(self):
        class Resp:
            var_async = True

        class Client:
            def retain(self, **kwargs):
                return Resp()

        with self.assertRaisesRegex(hm.MemorySyncError, "异步写入"):
            hm.sync_document(Client(), "b", {"document_id": "d", "metadata": {}})

        class Slow:
            var_async = False

        class Ready:
            def retain(self, **kwargs):
                return Slow()

        result = hm.sync_document(Ready(), "b", {"document_id": "d", "metadata": {"k": "v"}})
        self.assertEqual(result, {"ok": True, "document_id": "d", "metadata": {"k": "v"}})

    def test_recall_documents_filters_tag_metadata_and_cap(self):
        self.init_workspace_git()
        self.commit_workspace()

        class Item:
            def __init__(self, i, tags, meta):
                self.text = f"reply-{i}"
                self.document_id = f"doc-{i}"
                self.tags = tags
                self.metadata = meta

        tag = "rhw-project:demo"
        items = [
            Item(0, [], {}),
            Item(1, [tag], {"project_id": "demo", "source_path": "research_workspace/STATE.md",
                            "source_commit": "a" * 40, "source_sha256": "b" * 64,
                            "extra": "ignore"}),
            Item(2, [tag], {}),
            Item(3, [tag], {"project_id": "other"}),
            Item(4, [tag], {"project_id": "demo"}),
            Item(5, [tag], {"project_id": "demo"}),
            Item(6, [tag], {"project_id": "demo"}),
        ]

        class Resp:
            results = items

        class Client:
            def recall(self, **kwargs):
                return Resp()

        out = hm.recall_documents(Client(), "bank", self.root, "demo", "查询")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["results"]), 5)
        self.assertNotIn("reply-0", [r["text"] for r in out["results"]])
        first = out["results"][0]
        self.assertNotIn("extra", first["metadata"])
        # metadata["source_commit"] 是伪造的；该 commit 在仓库 没有 blob → unverified
        # （不是 missing，因为文件本身在工作树 _registry 不存在时会先被 git show 拒绝）。
        self.assertEqual(first["source_verification"], "missing")
        self.assertFalse(first["usable_as_current_source"])

    def test_make_client_requires_all_env_and_rejects_invalid_url(self):
        for env in (
            {},
            {"HINDSIGHT_API_URL": "https://h", "HINDSIGHT_API_KEY": "k"},
            {"HINDSIGHT_API_URL": "http://remote/x", "HINDSIGHT_API_KEY": "k",
             "HINDSIGHT_BANK_ID": "b"},
            {"HINDSIGHT_API_URL": "https://user:pw@h/x", "HINDSIGHT_API_KEY": "k",
             "HINDSIGHT_BANK_ID": "b"},
        ):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(hm.MemorySyncError):
                hm.make_client()


if __name__ == "__main__":
    unittest.main()
