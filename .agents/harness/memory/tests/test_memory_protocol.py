"""install_memory_hooks 往返与 memory_hooks 本地协议；不使用宿主配置，不打真网络，全部隔离目录。

宿主配置由 install() 以原子替换写入（mode 0600）；fixture 文件保持最小权限，
避免与本仓库 pi-interpreter-guard 的宽泛 chmod 策略纠缠。
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
from harness.memory import install_memory_hooks as installer  # noqa: E402
from harness.memory.memory_hooks import (  # noqa: E402
    capture_stop, last_reply, native_turn, recover_replies, session_key,
)
from harness.memory.research_memory import Memory, MemoryError  # noqa: E402


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
                # command 直接启动本地 Python；不加载 .env 或远端凭据。
                self.assertIn(str(self.root), command)
                self.assertIn(" hook --action ", command)
                self.assertIn(f"--host {host}", command)
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
        # 修复后 owned() 按形状判定：`--binding research-memory-v1 extra` 是手工遗留
        # marker，被识别为 ours 并随 remove 清除。见 F-016。
        self.assertNotIn(f"research_memory.py --binding {installer.MARKER} extra", remaining)
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
        # FIXED BC-13/F-016: 未知 host 现在显式拒绝（以前是静默 0 变更）。
        with self.assertRaisesRegex(ValueError, "unknown host"):
            self.install(hosts=("pi",))

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




if __name__ == "__main__":
    unittest.main()
