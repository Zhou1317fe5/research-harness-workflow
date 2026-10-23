from __future__ import annotations

from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch


HARNESS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HARNESS.parent))

from harness.remote import agent_event_wait  # noqa: E402


class AgentEventWaitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agent-event-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runspec = self.root / "runspec.json"
        self.runspec.write_text(json.dumps({"schema_version": "rrctl.run.v1", "run_id": "RUN-A"}))
        self.state = self.root / "wait.json"
        self.state.write_text(json.dumps({"schema_version": agent_event_wait.SCHEMA, "run_id": "RUN-A"}))

    def test_child_waits_without_deadline_then_queues_one_event(self):
        calls = []
        def run(argv, **_kwargs):
            calls.append(argv)
            if "queue" in argv:
                return subprocess.CompletedProcess(argv, 0, "queued", "")
            raise AssertionError("remote observer must stream rather than use subprocess.run")
        args = Namespace(
            state=self.state, runspec=self.runspec, thread="thread-fixture",
            profiles=None, poll_seconds=600, resume=True,
        )
        with patch.object(agent_event_wait, "observe", return_value=(0, '{"stage":"wait","ok":true}\n', "")) as observer, patch.object(agent_event_wait.subprocess, "run", side_effect=run), patch.object(agent_event_wait.shutil, "which", return_value="/fixture/codex"):
            self.assertEqual(agent_event_wait.run_child(args), 0)
        command = observer.call_args.args[0]
        self.assertIn("--observe-only", command)
        self.assertIn("--resume", command)
        self.assertEqual(command[command.index("--max-wait-seconds") + 1], "0")
        self.assertEqual(sum("queue" in call for call in calls), 1)
        self.assertIn("continue pull/ingest now", calls[0][-1])
        event = json.loads(self.state.with_suffix(".event.json").read_text())
        self.assertEqual(event["kind"], "terminal")

    def real_fixture(self, body: str):
        """Actual Python observer and CLI receiver; no production host/thread is contacted."""
        script = self.root / "remote_run.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        notifications = self.root / "notifications.jsonl"
        codex = self.root / "codex-fixture"
        codex.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys, time\n"
            f"path = pathlib.Path({str(notifications)!r})\n"
            "with path.open('a') as stream:\n"
            "    stream.write(json.dumps({'argv': sys.argv[1:], 'at': time.monotonic()}) + '\\n')\n",
            encoding="utf-8",
        )
        codex.chmod(0o700)
        args = Namespace(
            state=self.state, runspec=self.runspec, thread="thread-fixture",
            profiles=None, poll_seconds=600, resume=True,
        )
        return args, script, codex, notifications

    def test_real_child_streams_progress_before_terminal_and_skips_slow_pull(self):
        args, script, codex, notifications = self.real_fixture("""
            import json, pathlib, sys, time
            root = pathlib.Path(sys.argv[1]).parent
            frame = '[rrctl-progress] ' + json.dumps({
                'schema_version':'rrctl.progress.v1', 'run_id':'RUN-A', 'stage':'wait',
                'progress':{'state':'running', 'current_step':10}, 'message':'训练进度 10/100，预计剩余 30 分钟'
            }, ensure_ascii=False) + '\\n'
            data = frame.encode()
            split = data.index('训练'.encode()) + 1
            for part in (data[:split], data[split:]):
                sys.stderr.buffer.write(part)
                sys.stderr.buffer.flush()
                time.sleep(0.02)
            print('stdout evidence', flush=True)
            deadline = time.monotonic() + 5
            while not (root / 'release').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            (root / 'compute-terminal').touch()
            if '--observe-only' not in sys.argv:
                (root / 'pull-started').touch()
                time.sleep(3)
                (root / 'pull-done').touch()
            print('{"stage":"wait","ok":true}', flush=True)
        """)
        with patch.object(agent_event_wait, "__file__", str(script.with_name("agent_event_wait.py"))), patch.object(
            agent_event_wait.shutil, "which", return_value=str(codex)
        ), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(agent_event_wait.run_child, args)
            deadline = time.monotonic() + 3
            while not notifications.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(notifications.exists(), "heartbeat was not delivered while observer was running")
            self.assertFalse(future.done())
            messages = [json.loads(line)["argv"][-1] for line in notifications.read_text().splitlines()]
            self.assertEqual(len(messages), 1)
            self.assertIn("训练进度", messages[0])
            self.assertFalse(self.state.with_suffix(".event.json").exists())
            (self.root / "release").touch()
            self.assertEqual(future.result(timeout=1.5), 0)
        messages = [json.loads(line)["argv"][-1] for line in notifications.read_text().splitlines()]
        self.assertEqual(len(messages), 2)
        self.assertIn("terminal event", messages[1])
        self.assertIn("pull/ingest now", messages[1])
        self.assertTrue((self.root / "compute-terminal").exists())
        self.assertFalse((self.root / "pull-started").exists())
        self.assertIn("stdout evidence", self.state.with_suffix(".log").read_text())
        self.assertEqual(json.loads(self.state.read_text())["status"], "terminal")

    def test_real_child_throttles_changed_progress_and_does_not_delay_terminal(self):
        args, script, codex, notifications = self.real_fixture("""
            import json, sys
            for step in (10, 10, 11, 12):
                event = {'schema_version':'rrctl.progress.v1', 'run_id':'RUN-A', 'stage':'wait',
                         'progress':{'state':'running', 'current_step':step}, 'message':f'当前步 {step}'}
                print('[rrctl-progress] ' + json.dumps(event), file=sys.stderr, flush=True)
        """)
        with patch.object(agent_event_wait, "__file__", str(script)), patch.object(
            agent_event_wait.shutil, "which", return_value=str(codex)
        ):
            self.assertEqual(agent_event_wait.run_child(args), 0)
        messages = [json.loads(line)["argv"][-1] for line in notifications.read_text().splitlines()]
        self.assertEqual(len(messages), 2)  # First real progress + unthrottled terminal.
        self.assertIn("当前步 10", messages[0])
        self.assertIn("terminal event", messages[1])

    def test_real_child_dedupes_elapsed_only_and_recovers_after_oversized_line(self):
        args, script, codex, notifications = self.real_fixture("""
            import json, sys
            sys.stderr.write('[rrctl-progress] ' + 'x' * 70000 + '\\n')
            sys.stdout.write('x' * 20000 + '\\n')
            print('[rrctl-progress] {bad}', file=sys.stderr)
            print('[rrctl-progress] ' + json.dumps({'schema_version':'rrctl.progress.v1',
                  'run_id':'RUN-A', 'stage':'wait', 'message':'进度暂不可用', 'query_error':'TimeoutError'}), file=sys.stderr)
            for step, elapsed, run_id in ((10,1,'RUN-B'), (10,1,'RUN-A'), (10,2,'RUN-A'), (11,3,'RUN-A')):
                event = {'schema_version':'rrctl.progress.v1', 'run_id':run_id, 'stage':'wait',
                         'progress':{'state':'running', 'current_step':step, 'elapsed_seconds':elapsed},
                         'message':f'当前步 {step}, 已用 {elapsed}'}
                print('[rrctl-progress] ' + json.dumps(event), file=sys.stderr, flush=True)
        """)
        with patch.object(agent_event_wait, "__file__", str(script)), patch.object(
            agent_event_wait.shutil, "which", return_value=str(codex)
        ), patch.object(agent_event_wait, "CODEX_PROGRESS_INTERVAL_SECONDS", 0):
            self.assertEqual(agent_event_wait.run_child(args), 0)
        messages = [json.loads(line)["argv"][-1] for line in notifications.read_text().splitlines()]
        self.assertEqual(len(messages), 3)  # Step 10, step 11, terminal; elapsed-only is silent.
        event = json.loads(self.state.with_suffix(".event.json").read_text())
        self.assertLessEqual(len(event["output"]), 12000)
        self.assertLessEqual(len(event["error"]), 4000)
        self.assertGreater(self.state.with_suffix(".log").stat().st_size, 90000)

    def test_observer_spawn_error_still_queues_attention(self):
        args = Namespace(
            state=self.state, runspec=self.runspec, thread="thread-fixture",
            profiles=None, poll_seconds=600, resume=True,
        )
        with patch.object(agent_event_wait, "observe", side_effect=OSError("spawn failed")), patch.object(
            agent_event_wait.shutil, "which", return_value="/fixture/codex"
        ), patch.object(agent_event_wait, "queue_message", return_value=subprocess.CompletedProcess([], 0, "", "")) as queue:
            self.assertEqual(agent_event_wait.run_child(args), 0)
        self.assertIn("attention event", queue.call_args.args[-1])
        self.assertIn("spawn failed", json.loads(self.state.with_suffix(".event.json").read_text())["error"])

    def test_terminal_queue_failure_is_persisted(self):
        args = Namespace(
            state=self.state, runspec=self.runspec, thread="thread-fixture",
            profiles=None, poll_seconds=600, resume=True,
        )
        with patch.object(agent_event_wait, "observe", return_value=(0, "", "")), patch.object(
            agent_event_wait.shutil, "which", return_value="/fixture/codex"
        ), patch.object(agent_event_wait, "queue_message", side_effect=subprocess.TimeoutExpired("codex", 10)):
            self.assertEqual(agent_event_wait.run_child(args), 4)
        self.assertEqual(json.loads(self.state.read_text())["status"], "notification_failed")

    def test_start_reuses_one_run_bound_relay(self):
        class Process:
            pid = 12345
        args = Namespace(
            runspec=self.runspec, thread="thread-fixture", profiles=None,
            poll_seconds=600, resume=False,
        )
        capability = subprocess.CompletedProcess([], 0, "", "")
        with patch.dict("os.environ", {"XDG_STATE_HOME": str(self.root / "state")}), patch.object(
            agent_event_wait.shutil, "which", return_value="/fixture/codex"
        ), patch.object(
            agent_event_wait.subprocess, "run", return_value=capability
        ), patch.object(
            agent_event_wait.subprocess, "Popen", return_value=Process()
        ) as popen, patch.object(
            agent_event_wait, "process_start", return_value="fixture-start"
        ):
            self.assertEqual(agent_event_wait.start(args), 0)
            self.assertEqual(agent_event_wait.start(args), 0)
        popen.assert_called_once()

    def test_start_does_not_spawn_a_second_relay_while_attention_is_pending(self):
        # 同一 RunID 的 attention 已投递、执行者尚未处理时，重复 start 必须复用而不是
        # 再启动一个 relay（否则会向 thread 重复唤醒同一事件）。
        import io
        import contextlib
        args = Namespace(
            runspec=self.runspec, thread="thread-fixture", profiles=None,
            poll_seconds=600, resume=False,
        )
        with patch.dict("os.environ", {"XDG_STATE_HOME": str(self.root / "state")}):
            path = agent_event_wait.state_path(self.runspec.resolve(), "RUN-A", "thread-fixture")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "schema_version": agent_event_wait.SCHEMA, "status": "attention",
                "run_id": "RUN-A", "pid": 999999, "process_start": "dead",
            }))
            output = io.StringIO()
            with patch.object(agent_event_wait.shutil, "which", return_value="/fixture/codex"), patch.object(
                agent_event_wait.subprocess, "run",
            ) as run_mock, patch.object(
                agent_event_wait.subprocess, "Popen",
            ) as popen_mock, contextlib.redirect_stdout(output):
                self.assertEqual(agent_event_wait.start(args), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "attention_pending")
        self.assertTrue(payload["reused"])
        popen_mock.assert_not_called()
        # capability 探测都不应发生（快速路径在任何子进程之前）。
        run_mock.assert_not_called()

    def test_start_recovers_after_terminal_state(self):
        # terminal 之后允许启动新 relay（后续观察另一轮生命周期）。
        args = Namespace(
            runspec=self.runspec, thread="thread-fixture", profiles=None,
            poll_seconds=600, resume=True,
        )

        class Process:
            pid = 23456
        capability = subprocess.CompletedProcess([], 0, "", "")
        with patch.dict("os.environ", {"XDG_STATE_HOME": str(self.root / "state")}):
            path = agent_event_wait.state_path(self.runspec.resolve(), "RUN-A", "thread-fixture")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "schema_version": agent_event_wait.SCHEMA, "status": "terminal",
                "run_id": "RUN-A", "pid": 999999, "process_start": "dead",
            }))
            with patch.object(agent_event_wait.shutil, "which", return_value="/fixture/codex"), patch.object(
                agent_event_wait.subprocess, "run", return_value=capability,
            ), patch.object(
                agent_event_wait.subprocess, "Popen", return_value=Process(),
            ) as popen, patch.object(
                agent_event_wait, "process_start", return_value="fixture-start",
            ):
                self.assertEqual(agent_event_wait.start(args), 0)
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
