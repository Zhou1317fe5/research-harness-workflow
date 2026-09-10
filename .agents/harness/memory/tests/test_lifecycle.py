"""记忆处理、宿主边界和恢复的行为回归；只使用隔离目录及模拟远端。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from harness.memory.memory_hooks import handle_hook, start_sync
from harness.memory.research_memory import Memory, MemoryError, SYNC_POLICY, atomic, digest


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = Memory(self.root)

    def decision(self, text, *, at="2026-09-08T00:00:00Z", **fields):
        event = self.memory.capture("user", text, occurred_at=at)
        request = {"event_id": event, "disposition": "recorded", "records": [{
            "kind": "decision", "status": "ACTIVE", "scope": "evaluation.metric", "summary": text,
            "authorization_quote": text, "state_slot": "baseline", **fields,
        }]}
        return self.memory.process(request), request

    def index(self):
        return json.loads(self.memory.state_path.read_text())

    def test_process_then_hook_refreshes_without_new_file_scan(self):
        old, _ = self.decision("调查评估器")
        first = handle_hook(self.memory, {"session_id": "s", "memory_protocol": "2"}, "context", "pi")["hookSpecificOutput"]
        new, _ = self.decision("授权新任务", at="2026-09-09T00:00:00Z", supersedes=old)
        fresh = handle_hook(self.memory, {"session_id": "s", "memory_protocol": "2", "context_revision": first["snapshotRevision"]}, "scan", "pi")["hookSpecificOutput"]
        self.assertNotEqual(first["snapshotRevision"], fresh["snapshotRevision"])
        self.assertIn(new[0], fresh["additionalContext"])
        self.assertNotIn("调查评估器", fresh["additionalContext"])
        same = handle_hook(self.memory, {"session_id": "s", "memory_protocol": "2", "context_revision": fresh["snapshotRevision"]}, "scan", "pi")
        self.assertNotIn("additionalContext", same["hookSpecificOutput"])

    def test_disposition_only_change_invalidates_snapshot(self):
        event = self.memory.capture("user", "需要核对研究来源")
        before = self.memory.snapshot()
        self.memory.process({"event_id": event, "disposition": "waiting", "reason": "等待文件"})
        after = handle_hook(self.memory, {"session_id": "s", "memory_protocol": "2", "context_revision": before["revision"]}, "scan", "pi")
        self.assertIn("additionalContext", after["hookSpecificOutput"])

    def test_restored_snapshot_cannot_reactivate_old_decision_and_can_repair(self):
        old, _ = self.decision("旧门禁")
        path = self.memory.workspace / "CONCLUSIONS.md"
        old_text = path.read_text()
        new, request = self.decision("新授权", at="2026-09-09T00:00:00Z", supersedes=old)
        path.write_text(old_text)
        state_path = self.memory.workspace / "STATE.md"
        state_path.write_text(state_path.read_text() + "\n用户的独立进度记录。\n")
        self.assertEqual(self.memory.records(status="ACTIVE"), [])
        self.assertIn("superseded_decision_resurrection", str(self.memory.status()["projection_conflicts"]))
        with self.assertRaisesRegex(MemoryError, "projection_conflict"):
            self.memory.process(request)
        self.memory.recover(repair_projections=True)
        self.assertFalse(self.memory.status()["projection_conflicts"])
        self.assertEqual([r["id"] for r in self.memory.records(status="ACTIVE")], new)
        self.assertIn("用户的独立进度记录", state_path.read_text())
        self.assertEqual(self.memory.process(request), new)

    def test_repair_preserves_external_identity_edits(self):
        self.decision("采用新方案")
        path = self.memory.workspace / "CONCLUSIONS.md"
        edited = path.read_text().replace("Scope: evaluation.metric", "Scope: manually.changed")
        path.write_text(edited)
        with self.assertRaises(MemoryError):
            self.memory.recover(repair_projections=True)
        self.assertEqual(path.read_text(), edited)

    def test_prose_edits_do_not_revoke_source_or_block_new_decision(self):
        old, _ = self.decision("采用已经核对过的配置 A")
        path = self.memory.workspace / "CONCLUSIONS.md"
        path.write_text(path.read_text().replace("采用已经核对过的配置 A", "采用配置 A"))
        self.assertFalse(self.memory.status()["projection_conflicts"])
        new, _ = self.decision("切换到配置 B", at="2026-09-09T00:00:00Z", supersedes=old)
        self.assertEqual([r["id"] for r in self.memory.records(status="ACTIVE")], new)
        self.assertIn("采用配置 A", path.read_text())

    def test_preflight_checks_all_files_before_writing(self):
        old, _ = self.decision("旧方案")
        conclusions = self.memory.workspace / "CONCLUSIONS.md"
        before = conclusions.read_text()
        state_path = self.memory.workspace / "STATE.md"
        original_apply = self.memory._apply

        def conflict(transaction):
            state_path.write_text(state_path.read_text() + "\n并发改动。\n")
            original_apply(transaction)

        with patch.object(self.memory, "_apply", side_effect=conflict):
            with self.assertRaises(MemoryError):
                self.decision("新方案", at="2026-09-09T00:00:00Z", supersedes=old)
        self.assertEqual(conclusions.read_text(), before)
        transaction = next(t for t in self.index()["transactions"].values() if t["state"] == "prepared")
        self.memory.recover(abort_transaction=transaction["id"])
        self.assertIn("并发改动", state_path.read_text())

    def test_abort_partial_write_restores_only_own_changes(self):
        old, _ = self.decision("旧方案")
        conclusions = self.memory.workspace / "CONCLUSIONS.md"
        before = conclusions.read_text()

        def crash(transaction):
            relative, write = next(iter(transaction["writes"].items()))
            atomic(self.root / relative, write["text"])
            raise OSError("fixture interruption")

        with patch.object(self.memory, "_apply", side_effect=crash):
            with self.assertRaises(OSError):
                self.decision("新方案", at="2026-09-09T00:00:00Z", supersedes=old)
        state_path = self.memory.workspace / "STATE.md"
        state_path.write_text(state_path.read_text() + "\n保留外部备注。\n")
        transaction = next(t for t in self.index()["transactions"].values() if t["state"] == "prepared")
        self.memory.recover(abort_transaction=transaction["id"])
        self.assertEqual(conclusions.read_text(), before)
        self.assertIn("保留外部备注", state_path.read_text())
        self.assertEqual([r["id"] for r in self.memory.records(status="ACTIVE")], old)

    def test_older_effective_time_is_rejected(self):
        current, _ = self.decision("新决定", at="2026-09-09T00:00:00Z")
        with self.assertRaisesRegex(MemoryError, "decision_time_regression"):
            self.decision("较旧的决定", at="2026-09-07T00:00:00Z", supersedes=current)

    def test_new_effective_time_cannot_launder_older_source(self):
        current, _ = self.decision("新决定", at="2026-09-09T00:00:00Z")
        with self.assertRaisesRegex(MemoryError, "decision_time_regression"):
            self.decision("晚到的旧消息", at="2026-09-07T00:00:00Z", supersedes=current,
                          effective_at="2026-09-10T00:00:00Z")

    def test_parallel_protocols_and_tasks_are_distinct(self):
        self.decision("官方协议", protocol="official", task_id="A")
        self.decision("修复协议", protocol="corrected", task_id="A")
        self.decision("另一个任务", protocol="official", task_id="B")
        self.assertEqual(len(self.memory.records(status="ACTIVE")), 3)

    def test_cross_scope_retirement_requires_explicit_reason(self):
        old, _ = self.decision("旧调查门禁")
        with self.assertRaises(MemoryError):
            self.decision("开始新任务", at="2026-09-09T00:00:00Z", scope="mission.scope", retires=old)
        new, _ = self.decision("开始新任务", at="2026-09-09T00:00:00Z", scope="mission.scope",
                               retires=old, retirement_reason="用户已切换任务")
        self.assertEqual([r["id"] for r in self.memory.records(status="ACTIVE")], new)

    def test_active_decision_needs_quote_from_actual_source(self):
        event = self.memory.capture("user", "评估差异是什么？")
        request = {"event_id": event, "disposition": "recorded", "records": [{
            "kind": "decision", "status": "ACTIVE", "scope": "evaluation", "summary": "禁止训练",
        }]}
        with self.assertRaisesRegex(MemoryError, "authorization_quote"):
            self.memory.process(request)
        request["records"][0]["authorization_quote"] = "我禁止训练"
        with self.assertRaises(MemoryError):
            self.memory.process(request)

    def test_raw_sources_and_projection_scans_do_not_create_remote_jobs(self):
        self.memory.capture("user", "测试工具")
        self.memory.capture("assistant", "测试已完成")
        self.memory.workspace.mkdir()
        (self.memory.workspace / "STATE.md").write_text("# STATE\n当前进度")
        self.memory.scan()
        self.assertEqual(self.index()["jobs"], {})
        self.assertEqual(len(self.memory.search()), 2)

    def test_curated_job_contains_record_not_whole_conversation(self):
        source = "与决定无关的长背景。" * 100 + "用户决定采用配置 A。"
        event = self.memory.capture("user", source)
        self.memory.process({"event_id": event, "disposition": "recorded", "records": [{
            "kind": "decision", "status": "ACTIVE", "scope": "model", "summary": "采用配置 A",
            "authorization_quote": "用户决定采用配置 A。",
        }]})
        key, job = next(iter(self.index()["jobs"].items()))
        payload = json.loads((self.memory.store / "outbox" / f"{key}-{job['revision']}.json").read_text())
        self.assertEqual(job["policy"], SYNC_POLICY)
        self.assertIn("采用配置 A", payload["content"])
        self.assertNotIn("长背景", payload["content"])

    def test_unchanged_document_commit_does_not_create_new_source(self):
        self.memory.workspace.mkdir()
        def git(*args):
            subprocess.run(["git", "-C", str(self.memory.workspace), *args], check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (self.memory.workspace / "STATE.md").write_text("# STATE\n")
        git("add", "STATE.md")
        git("commit", "-qm", "first")
        self.memory.scan()
        count = len(self.index()["events"])
        (self.memory.workspace / "unrelated.txt").write_text("unrelated")
        git("add", "unrelated.txt")
        git("commit", "-qm", "unrelated")
        self.assertEqual(self.memory.scan(), [])
        self.assertEqual(len(self.index()["events"]), count)

    def test_explicit_analysis_publish_and_stale_invalidation(self):
        path = self.memory.workspace / "analysis/final.md"
        path.parent.mkdir(parents=True)
        path.write_text("# 完成的分析\n可追溯的发现。\n")
        self.memory.publish("research_workspace/analysis/final.md")
        self.assertEqual(len(self.index()["jobs"]), 1)
        path.write_text(path.read_text() + "新的未发布修改。\n")
        self.memory.scan()
        self.assertEqual(next(iter(self.index()["jobs"].values()))["state"], "stale")
        self.memory.publish("research_workspace/analysis/final.md")
        self.assertEqual(next(iter(self.index()["jobs"].values()))["state"], "pending")
        with self.assertRaises(MemoryError):
            self.memory.publish("research_workspace/STATE.md")

    def test_quarantine_retains_originals_but_excludes_sources(self):
        event = self.memory.capture("user", "内部工具提示词")
        self.memory.quarantine([event], "已核对为后台来源")
        self.assertEqual(self.memory.search(history=True, include_resolved=True), [])
        self.assertEqual(self.memory.get(event)["text"], "内部工具提示词")
        with self.assertRaises(MemoryError):
            self.memory.process({"event_id": event, "disposition": "discarded", "reason": "重复处理"})
        self.memory.state_path.unlink()
        self.assertEqual(self.memory.search(history=True, include_resolved=True), [])

    def test_quarantine_excludes_legacy_derived_records_from_default_recall(self):
        event = self.memory.capture("assistant", "后台生成的摘要")
        self.memory.workspace.mkdir()
        (self.memory.workspace / "CONCLUSIONS.md").write_text(
            f"# Conclusions\n\n### C001\nType: finding\nStatus: SUPPORTED\nScope: fixture\nSource: {event}\n")
        self.memory.quarantine([event], "已确认的后台来源")
        self.assertEqual(self.memory.records(), [])
        self.assertEqual(self.memory.records(history=True)[0]["processing_state"], "quarantined")

    def test_sync_never_submits_legacy_raw_jobs(self):
        self.decision("采用配置 A")
        with self.memory.locked() as state:
            state["jobs"]["legacy"] = {"state": "pending", "revision": "old", "updated_at": "2000-01-01"}
        self.memory.config["hindsight_enabled"] = True
        calls = []
        class Client:
            def call(self, name, payload):
                calls.append((name, payload))
                return {"structuredContent": {"status": "completed"}}
        result = self.memory.sync(client=Client())
        self.assertEqual(result["completed"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["metadata"]["sync_policy"], SYNC_POLICY)
        with patch("subprocess.Popen") as popen:
            start_sync(self.memory)
            popen.assert_not_called()

    def test_internal_host_hook_never_captures(self):
        for environment in ({"MAGIC_CONTEXT_PI_SUBAGENT": "1"}, {"PI_SUB_AGENT_DEPTH": "2"}):
            with patch.dict(os.environ, environment):
                result = handle_hook(self.memory, {"session_id": "child", "prompt": "后台提示词"}, "prompt", "pi")
            self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "")
        self.assertFalse(self.memory.state_path.exists())

    def test_loaded_legacy_pi_extension_is_fail_safe_until_reload(self):
        result = handle_hook(self.memory, {"session_id": "old-pi", "prompt": "最新用户任务"}, "prompt", "pi")
        self.assertEqual(result["hookSpecificOutput"]["additionalContext"], "")
        self.assertTrue(result["hookSpecificOutput"]["requiresReload"])
        self.assertEqual(len(self.memory.search()), 1)

    def test_internal_cli_guard_runs_before_creating_store(self):
        script = Path(__file__).resolve().parents[1] / "research_memory.py"
        store = self.root / "untouched-store"
        result = subprocess.run([sys.executable, str(script), "--repo-root", str(self.root), "--store", str(store),
                                 "hook", "--host", "pi", "--action", "prompt"], input="{}", text=True,
                                env={**os.environ, "MAGIC_CONTEXT_PI_SUBAGENT": "1"}, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(store.exists())

    def test_read_only_snapshot_does_not_rewrite_index(self):
        self.decision("当前任务")
        first = self.memory.snapshot()
        modified = self.memory.state_path.stat().st_mtime_ns
        second = self.memory.snapshot()
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(self.memory.state_path.stat().st_mtime_ns, modified)
        self.assertLessEqual(len(second["context"]), self.memory.config["context_chars"])

    def test_authorization_quote_can_come_from_full_local_source(self):
        text = "背景材料。" * 9000 + "明确批准配置 B。"
        event = self.memory.capture("user", text)
        ids = self.memory.process({"event_id": event, "disposition": "recorded", "records": [{
            "kind": "decision", "status": "ACTIVE", "scope": "model", "summary": "采用配置 B",
            "authorization_quote": "明确批准配置 B。",
        }]})
        self.assertEqual(ids, ["C001"])

    def test_remote_recall_filters_superseded_and_protocol(self):
        old, _ = self.decision("旧协议决定", protocol="official")
        self.decision("新协议决定", at="2026-09-09T00:00:00Z", protocol="official", supersedes=old)
        self.decision("修复协议", protocol="corrected")
        payloads = []
        for key, job in self.index()["jobs"].items():
            payload = json.loads((self.memory.store / "outbox" / f"{key}-{job['revision']}.json").read_text())
            payloads.append({"text": payload["content"], **{k: payload[k] for k in ("metadata", "tags", "document_id")}})
        class Client:
            def call(self, *_args):
                return {"structuredContent": {"results": payloads}}
        self.memory.config["hindsight_enabled"] = True
        result = self.memory.remote_search("协议", client=Client(), kind="decision", status="ACTIVE", protocol="official")
        self.assertEqual(len(result["results"]), 1)
        self.assertIn("新协议决定", result["results"][0]["text"])
        history = self.memory.remote_search("协议", client=Client(), history=True)
        self.assertEqual(len(history["results"]), 3)


if __name__ == "__main__":
    unittest.main()
