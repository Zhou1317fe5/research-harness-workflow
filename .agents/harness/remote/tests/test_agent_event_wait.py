from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
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
            return subprocess.CompletedProcess(argv, 0, '{"stage":"pull","ok":true}\n', "")
        args = Namespace(
            state=self.state, runspec=self.runspec, thread="thread-fixture",
            profiles=None, poll_seconds=600, resume=True,
        )
        with patch.object(agent_event_wait.subprocess, "run", side_effect=run), patch.object(agent_event_wait.shutil, "which", return_value="/fixture/codex"):
            self.assertEqual(agent_event_wait.run_child(args), 0)
        self.assertIn("--max-wait-seconds", calls[0])
        self.assertEqual(calls[0][calls[0].index("--max-wait-seconds") + 1], "0")
        self.assertEqual(sum("queue" in call for call in calls), 1)
        event = json.loads(self.state.with_suffix(".event.json").read_text())
        self.assertEqual(event["kind"], "terminal")

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


if __name__ == "__main__":
    unittest.main()
