"""主分析发布的行为回归；文件、队列与远端均使用隔离夹具。"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from harness.memory.analysis_publication import confirm, preview, sync_batch
from harness.memory.research_memory import MAX_SNAPSHOT, Memory, MemoryError, atomic, digest, main


ANALYSIS = """# 实验分析

## Change
调整采样模块，其余参数沿用对照。

## Result
主指标为 0.72，单种子结果尚不能说明稳定收益。

## Finding
本次结果仅支持当前评测协议下的观察。

## Next
下一轮增加种子，验证方差。
"""


class Client:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback
        self.closed = False

    def call(self, name, payload):
        self.calls.append((name, payload))
        response = self.callback(name, payload) if self.callback else {"status": "completed"}
        return {"structuredContent": response}

    def close(self):
        self.closed = True


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-publication-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = Memory(self.root)
        self.memory.config["hindsight_enabled"] = True

    def analysis(self, exp_id="EXP_A", text=ANALYSIS):
        path = self.memory.workspace / "experiments" / exp_id / "analysis/analysis.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def index(self):
        return json.loads(self.memory.state_path.read_text()) if self.memory.state_path.exists() else {}

    def batch(self, *exp_ids):
        return preview(self.memory, exp_ids=exp_ids or None)["batch_id"]

    def cli(self, *args):
        with patch.object(sys, "argv", ["research_memory.py", "--repo-root", str(self.root), *args]), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            code = main()
        return code, json.loads(output.getvalue())

    def test_preview_only_selects_main_analysis_and_does_not_scan_queue_or_connect(self):
        path = self.analysis(text=ANALYSIS + "\n附件：[诊断](diagnostic.md)\n")
        (path.parent / "diagnostic.md").write_text("诊断正文不应被预读。")
        (path.parent / "raw.jsonl").write_text('{"role":"assistant","text":"原始对话"}\n')
        outside = self.memory.workspace / "analysis/final.md"
        outside.parent.mkdir()
        outside.write_text(ANALYSIS)
        with patch.object(self.memory, "scan", side_effect=AssertionError("不应扫描来源")), \
                patch.object(self.memory, "_capture", side_effect=AssertionError("不应采集来源")), \
                patch("harness.memory.hindsight_mcp.HindsightMCP.from_env") as connect:
            result = preview(self.memory)
        connect.assert_not_called()
        self.assertEqual(result["selected_count"], 1)
        self.assertEqual(result["selected"][0]["path"], path.relative_to(self.root).as_posix())
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.index(), {})
        self.assertFalse((self.memory.store / "events").exists())
        self.assertFalse((self.memory.store / "outbox").exists())
        snapshot = Path(result["preview_path"]).parent / "files/0.md"
        self.assertEqual(snapshot.read_text(), path.read_text())
        self.assertEqual(snapshot.stat().st_mode & 0o077, 0)
        self.assertEqual(snapshot.parent.parent.stat().st_mode & 0o077, 0)

    def test_preview_does_not_recover_transactions_or_migrate_existing_index(self):
        self.analysis()
        state = {"schema_version": 1, "events": {}, "documents": {}, "jobs": {
            "legacy": {"state": "pending", "revision": "raw"}}, "transactions": {
                "T": {"state": "prepared", "writes": {"research_workspace/STATE.md": {"text": "旧状态"}}}}}
        atomic(self.memory.state_path, state)
        before = self.memory.state_path.read_bytes()
        preview(self.memory)
        self.assertEqual(self.memory.state_path.read_bytes(), before)
        self.assertFalse((self.memory.workspace / "STATE.md").exists())

    def test_sensitive_draft_conversation_log_and_incomplete_files_are_excluded(self):
        cases = {
            "SECRET": (ANALYSIS + "\npassword=fixture-private-value\n", "sensitive_content"),
            "TOKEN": (ANALYSIS + "\n--api-key fixture-private-value\n", "sensitive_content"),
            "DRAFT": (ANALYSIS.replace("# 实验分析", "# 实验分析 Draft"), "draft_or_unfinished"),
            "STATUS": (ANALYSIS + "\nstatus: draft\n", "draft_or_unfinished"),
            "TEMPLATE": (ANALYSIS.replace("调整采样模块，其余参数沿用对照。", "<填写变更内容>"), "draft_or_unfinished"),
            "CHAT": (ANALYSIS + "\nuser: fixture-conversation-text\nassistant: 回复\n", "raw_conversation"),
            "LOG": (ANALYSIS + "\n" + "2026-09-11 00:00:00 INFO fixture-log-line\n" * 3, "raw_log"),
            "JSONL": (ANALYSIS + "\n```jsonl\n{}\n```\n", "raw_log"),
            "EMPTY_SECTION": (ANALYSIS.replace("下一轮增加种子，验证方差。", ""), "analysis_sections_empty"),
            "SECTIONS": ("# 分析\n只有开头。\n", "analysis_sections_missing"),
            "LARGE": (ANALYSIS + "x" * MAX_SNAPSHOT, "analysis_too_large"),
        }
        for exp_id, (text, _) in cases.items():
            self.analysis(exp_id, text)
        result = preview(self.memory)
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["blocked_count"], len(cases))
        blocked = {entry["exp_id"]: entry for entry in result["blocked"]}
        for exp_id, (_, reason) in cases.items():
            with self.subTest(exp_id=exp_id):
                self.assertIn(reason, blocked[exp_id]["issues"])
                self.assertNotIn("excerpt", blocked[exp_id])
        for file in (Path(result["manifest_path"]), Path(result["preview_path"])):
            content = file.read_text()
            self.assertNotIn("fixture-private-value", content)
            self.assertNotIn("fixture-conversation-text", content)
            self.assertNotIn("fixture-log-line", content)
        self.assertFalse((Path(result["preview_path"]).parent / "files").exists())

    def test_environment_values_are_blocked_but_variable_references_are_allowed(self):
        self.analysis("VALUE", ANALYSIS + "\nfixture-environment-secret-012345\n")
        self.analysis("REFERENCE", ANALYSIS + '\n--password "$PUBLICATION_TEST_PASSWORD"\n')
        with patch.dict(os.environ, {"PUBLICATION_TEST_PASSWORD": "fixture-environment-secret-012345"}):
            result = preview(self.memory)
        self.assertEqual([entry["exp_id"] for entry in result["selected"]], ["REFERENCE"])
        self.assertEqual(result["blocked"][0]["issues"], ["sensitive_content"])

    def test_symlink_non_regular_and_invalid_utf8_sources_are_blocked(self):
        path = self.analysis("LINK")
        target = self.root / "outside.md"
        target.write_text(ANALYSIS)
        path.unlink()
        path.symlink_to(target)
        fifo = self.analysis("FIFO")
        fifo.unlink()
        os.mkfifo(fifo)
        self.analysis("ENCODING").write_bytes(b"\xff\xfe")
        result = preview(self.memory)
        self.assertEqual(result["selected_count"], 0)
        self.assertEqual(result["blocked_count"], 3)
        self.assertTrue(all(entry["issues"] == ["source_unavailable_or_unsafe_path"] for entry in result["blocked"]))

    def test_selection_is_explicit_bounded_and_deduplicated(self):
        self.analysis("EXP_A")
        self.analysis("EXP_B")
        result = preview(self.memory, exp_ids=["EXP_B", "EXP_B"])
        self.assertEqual([item["exp_id"] for item in result["selected"]], ["EXP_B"])
        for invalid in ("../EXP_A", "EXP_A/analysis", "EXP_A\n", ".", ".."):
            with self.subTest(value=invalid), self.assertRaises(MemoryError):
                preview(self.memory, exp_ids=[invalid])
        with patch("harness.memory.analysis_publication.MAX_FILES", 1), self.assertRaises(MemoryError):
            preview(self.memory)

    def test_sync_and_invalid_cli_options_cannot_bypass_confirmation(self):
        self.analysis()
        batch_id = self.batch()
        client = Client()
        with self.assertRaisesRegex(MemoryError, "尚未确认"):
            sync_batch(self.memory, batch_id, client=client)
        cases = [("publish-batch", "--sync"),
                 ("publish-batch", "--confirm", batch_id, "--exp-id", "EXP_A"),
                 ("publish-batch", "--confirm", batch_id, "--sync", "--seconds", "0")]
        for args in cases:
            with self.subTest(args=args):
                code, _ = self.cli(*args)
                self.assertEqual(code, 2)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.index(), {})

    def test_cli_preview_confirm_sync_and_resume_preserve_batch_scope(self):
        self.analysis()
        atomic(self.memory.config_path, {"hindsight_enabled": True})
        code, result = self.cli("publish-batch")
        self.assertEqual(code, 0)
        self.assertEqual(self.index(), {})
        batch_id = result["batch_id"]
        client = Client()
        with patch("harness.memory.hindsight_mcp.HindsightMCP.from_env", return_value=client) as connect:
            code, result = self.cli("publish-batch", "--confirm", batch_id, "--sync")
            self.assertEqual(code, 0)
            self.assertTrue(result["sync"]["complete"])
            code, result = self.cli("sync", "--batch", batch_id)
            self.assertEqual(code, 0)
            self.assertTrue(result["complete"])
        connect.assert_called_once()
        self.assertTrue(client.closed)
        self.assertEqual(len(client.calls), 1)

    def test_cli_returns_nonzero_for_a_failed_remote_batch(self):
        self.analysis()
        atomic(self.memory.config_path, {"hindsight_enabled": True})
        batch_id = self.batch()

        def fail(*_):
            raise TimeoutError("夹具超时")

        with patch("harness.memory.hindsight_mcp.HindsightMCP.from_env", return_value=Client(fail)):
            code, result = self.cli("publish-batch", "--confirm", batch_id, "--sync")
        self.assertEqual(code, 2)
        self.assertEqual(result["sync"]["queue"]["pending"], 1)
        self.assertEqual(result["sync"]["error_type"], "publication_sync_error")

    def test_sensitive_identity_metadata_is_excluded_without_echoing_values(self):
        path = self.analysis()
        atomic(path.parent.parent / "record.json", {"source": {"spec_id": "api_key=fixture-private-value"}})
        result = preview(self.memory)
        self.assertEqual(result["selected_count"], 0)
        self.assertIn("sensitive_metadata", result["blocked"][0]["issues"])
        self.assertNotIn("fixture-private-value", json.dumps(result))

    def test_changed_file_aborts_whole_confirmation(self):
        self.analysis("EXP_A")
        path = self.analysis("EXP_B")
        batch_id = self.batch()
        path.write_text(ANALYSIS + "\n新分析尚未审阅。\n")
        with self.assertRaisesRegex(MemoryError, "重新预览"):
            confirm(self.memory, batch_id)
        self.assertEqual(self.index(), {})
        self.assertFalse((self.memory.store / "outbox").exists())

    def test_identity_change_requires_new_preview(self):
        path = self.analysis()
        record = path.parent.parent / "record.json"
        atomic(record, {"source": {"spec_id": "SPEC_A", "commit": "abc"}})
        batch_id = self.batch()
        atomic(record, {"source": {"spec_id": "SPEC_B", "commit": "abc"}})
        with self.assertRaisesRegex(MemoryError, "身份信息"):
            confirm(self.memory, batch_id)
        self.assertEqual(self.index(), {})

    def test_snapshot_manifest_and_foreign_project_tampering_are_rejected(self):
        self.analysis()
        first = preview(self.memory)
        snapshot = Path(first["preview_path"]).parent / "files/0.md"
        snapshot.write_text(ANALYSIS + "\n预览被修改。\n")
        with self.assertRaises(MemoryError):
            confirm(self.memory, first["batch_id"])
        second = preview(self.memory)
        manifest = Path(second["manifest_path"])
        value = json.loads(manifest.read_text())
        value["created_at"] = "changed"
        atomic(manifest, value)
        with self.assertRaises(MemoryError):
            confirm(self.memory, second["batch_id"])
        third = self.batch()
        foreign = Memory(self.root / "other-project", store=self.memory.store)
        with self.assertRaises(MemoryError):
            confirm(foreign, third)
        self.assertEqual(self.index(), {})

    def test_partial_io_failure_never_commits_partial_remote_jobs(self):
        self.analysis("EXP_A")
        self.analysis("EXP_B")
        batch_id = self.batch()
        publish = self.memory._publish_document
        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("夹具写盘失败")
            return publish(*args)

        with patch.object(self.memory, "_publish_document", side_effect=fail_second), self.assertRaises(OSError):
            confirm(self.memory, batch_id)
        self.assertEqual(self.index(), {})
        self.assertEqual(self.memory.status()["sync_pending"], 0)
        self.assertEqual(confirm(self.memory, batch_id)["published_count"], 2)

    def test_confirmation_is_atomic_idempotent_and_never_connects(self):
        self.analysis("EXP_A")
        self.analysis("EXP_B")
        batch_id = self.batch()
        with patch("harness.memory.hindsight_mcp.HindsightMCP.from_env") as connect:
            first = confirm(self.memory, batch_id)
            before = self.index()
            second = confirm(self.memory, batch_id)
        connect.assert_not_called()
        self.assertEqual(first["queue"]["pending"], 2)
        self.assertTrue(second["reused"])
        self.assertEqual(self.index(), before)
        self.assertEqual(len(before["jobs"]), 2)
        self.assertTrue(all(event["disposition"] == "recorded" for event in before["events"].values()))

    def test_changed_published_file_is_previewed_without_requiring_scan(self):
        path = self.analysis()
        first = self.batch()
        confirm(self.memory, first)
        first_version = next(iter(self.index()["jobs"].values()))["revision"]
        path.write_text(ANALYSIS + "\n补充了误差范围。\n")
        second = preview(self.memory)
        self.assertEqual(second["selected_count"], 1)
        self.assertEqual(second["blocked_count"], 0)
        self.assertEqual(second["selected"][0]["publication_state"], "unpublished")
        confirm(self.memory, second["batch_id"])
        self.assertNotEqual(next(iter(self.index()["jobs"].values()))["revision"], first_version)

    def test_large_batch_syncs_in_rounds_without_unrelated_pending_jobs(self):
        for index in range(25):
            self.analysis(f"EXP_{index:02d}")
        unrelated = self.memory.workspace / "analysis/unrelated.md"
        unrelated.parent.mkdir()
        unrelated.write_text("# 已完成的额外分析\n本次批量选择范围之外。\n")
        self.memory.publish(unrelated.relative_to(self.root).as_posix())
        other_key = next(iter(self.index()["jobs"]))
        result = preview(self.memory, limit=2)
        self.assertEqual(result["selected_count"], 25)
        self.assertEqual(len(result["selected"]), 2)
        self.assertEqual(len(json.loads(Path(result["manifest_path"]).read_text())["selected"]), 25)
        batch_id = result["batch_id"]
        confirm(self.memory, batch_id)
        client = Client()
        with patch.object(self.memory, "sync", wraps=self.memory.sync) as sync:
            result = sync_batch(self.memory, batch_id, client=client)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual([call.kwargs["limit"] for call in sync.call_args_list], [20, 5])
        self.assertTrue(result["complete"])
        self.assertEqual(result["queue"]["synced"], 25)
        self.assertEqual(len(client.calls), 25)
        self.assertTrue(all(name == "retain" and "/experiments/" in payload["metadata"]["source_ref"]
                            for name, payload in client.calls))
        self.assertEqual(self.index()["jobs"][other_key]["state"], "pending")
        with patch("harness.memory.hindsight_mcp.HindsightMCP.from_env") as connect:
            self.assertTrue(sync_batch(self.memory, batch_id)["complete"])
        connect.assert_not_called()
        again = preview(self.memory)
        self.assertEqual(again["selected_count"], 0)
        self.assertEqual(again["already_synced_count"], 25)

    def test_submitted_batch_resumes_by_polling_existing_operations(self):
        self.analysis()
        batch_id = self.batch()
        confirm(self.memory, batch_id)
        first = Client(lambda *_: {"status": "pending", "operation_id": "fixture-operation"})
        result = sync_batch(self.memory, batch_id, client=first)
        self.assertFalse(result["complete"])
        self.assertEqual(result["queue"]["submitted"], 1)
        second = Client()
        self.assertTrue(sync_batch(self.memory, batch_id, client=second)["complete"])
        self.assertEqual(second.calls, [("get_operation", {"operation_id": "fixture-operation"})])

    def test_remote_failure_is_reported_and_same_batch_can_resume(self):
        self.analysis()
        batch_id = self.batch()
        confirm(self.memory, batch_id)

        def fail(*_):
            raise TimeoutError("夹具远端超时")

        result = sync_batch(self.memory, batch_id, client=Client(fail))
        self.assertFalse(result["complete"])
        self.assertEqual(result["error_type"], "publication_sync_error")
        self.assertEqual(result["errors"][0]["error"], "TimeoutError")
        result = sync_batch(self.memory, batch_id, client=Client())
        self.assertTrue(result["complete"])
        self.assertEqual(result["errors"], [])

    def test_batch_budget_keeps_remaining_jobs_pending_for_resume(self):
        self.analysis("EXP_A")
        self.analysis("EXP_B")
        batch_id = self.batch()
        confirm(self.memory, batch_id)
        clock = [0.0]

        def advance(*_):
            clock[0] += 3
            return {"status": "completed"}

        client = Client(advance)
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            result = sync_batch(self.memory, batch_id, client=client, seconds=1)
        self.assertEqual(result["queue"]["synced"], 1)
        self.assertEqual(result["queue"]["pending"], 1)
        self.assertFalse(result["complete"])
        self.assertIsNone(result["error_type"])
        self.assertTrue(sync_batch(self.memory, batch_id, client=Client())["complete"])

    def test_scan_during_retain_cannot_resurrect_invalidated_job(self):
        path = self.analysis()
        batch_id = self.batch()
        confirm(self.memory, batch_id)

        def change(*_):
            path.write_text(ANALYSIS + "\n未发布的新版本。\n")
            self.memory.scan()
            return {"status": "completed"}

        result = sync_batch(self.memory, batch_id, client=Client(change))
        self.assertFalse(result["complete"])
        self.assertEqual(result["queue"]["blocked"], 1)
        self.assertEqual(next(iter(self.index()["jobs"].values()))["state"], "stale")
        with self.assertRaises(MemoryError):
            sync_batch(self.memory, batch_id, client=Client())

    def test_changed_queued_file_is_never_sent_by_regular_sync(self):
        path = self.analysis()
        confirm(self.memory, self.batch())
        path.write_text(ANALYSIS + "\n尚未批准的变化。\n")
        client = Client()
        result = self.memory.sync(client=client)
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(next(iter(self.index()["jobs"].values()))["state"], "stale")

    def test_legacy_raw_inflight_is_not_resent_through_a_curated_job(self):
        self.analysis()
        batch_id = self.batch()
        confirm(self.memory, batch_id)
        state = self.index()
        key, job = next(iter(state["jobs"].items()))
        old_revision = digest("legacy-raw-revision")
        job["inflight"] = {"revision": old_revision, "operation_id": None}
        atomic(self.memory.state_path, state)
        atomic(self.memory.store / "outbox" / f"{key}-{old_revision}.json", {"content": "原始对话"})
        client = Client()
        result = sync_batch(self.memory, batch_id, client=client)
        self.assertFalse(result["complete"])
        self.assertEqual(result["error_type"], "publication_sync_error")
        self.assertEqual(result["errors"][0]["error"], "prior_inflight_unresolved")
        self.memory.sync(client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.index()["jobs"][key]["error"], "prior_inflight_not_curated")

    def test_corrupt_payload_and_quarantined_source_cannot_be_published(self):
        self.analysis()
        confirm(self.memory, self.batch())
        key, job = next(iter(self.index()["jobs"].items()))
        payload_path = self.memory.store / "outbox" / f"{key}-{job['revision']}.json"
        payload = json.loads(payload_path.read_text())
        payload["content"] += "\n未经核对的队列改动。\n"
        atomic(payload_path, payload)
        self.assertEqual(preview(self.memory)["blocked"][0]["issues"], ["queue_payload_invalid"])
        client = Client()
        self.memory.sync(client=client)
        self.assertEqual(client.calls, [])
        event_id = next(iter(self.index()["events"]))
        self.memory.quarantine([event_id], "夹具隔离")
        self.assertIn("quarantined_source", preview(self.memory)["blocked"][0]["issues"])

    def test_git_commit_without_content_change_does_not_invalidate_preview(self):
        self.analysis()
        workspace = self.memory.workspace

        def git(*args):
            subprocess.run(["git", "-C", str(workspace), *args], check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        result = preview(self.memory)
        self.assertEqual(result["selected"][0]["source_git_state"], "working_tree")
        git("add", "experiments")
        git("commit", "-qm", "fixture analysis")
        confirmed = confirm(self.memory, result["batch_id"])
        self.assertEqual(confirmed["published_count"], 1)
        self.assertTrue(sync_batch(self.memory, result["batch_id"], client=Client())["complete"])

    def test_single_publish_remains_compatible_and_rejects_explicit_drafts(self):
        path = self.memory.workspace / "analysis/final.md"
        path.parent.mkdir(parents=True)
        path.write_text("# 完成的独立分析\n已有结论与证据。\n")
        self.memory.publish(path.relative_to(self.root).as_posix())
        self.assertEqual(self.memory.sync(client=Client())["completed"], 1)
        path.write_text("# Draft\n待完成。\n")
        with self.assertRaisesRegex(MemoryError, "草稿"):
            self.memory.publish(path.relative_to(self.root).as_posix())


if __name__ == "__main__":
    unittest.main()
