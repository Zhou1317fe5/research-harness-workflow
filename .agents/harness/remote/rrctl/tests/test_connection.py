from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import resource
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_process_backend as fixtures

from remote_run_control import cli
from remote_run_control.controller import _parse_worker_result
from remote_run_control.doctor import connection_doctor
from remote_run_control.errors import RRCError
from remote_run_control.jsonutil import load_json
from remote_run_control.output_limits import CONTROL_OUTPUT_LIMIT, OutputCapture
from remote_run_control.profiles import Profile, ProfileStore
from remote_run_control.security import StreamRedactor, redact_data
from remote_run_control.transport import (
    CommandResult,
    LocalTransport,
    SSHTransport,
    _capture_command,
)


class ConnectionTests(unittest.TestCase):
    setUp = fixtures.ProcessBackendTests.setUp
    tearDown = fixtures.ProcessBackendTests.tearDown
    spec = fixtures.ProcessBackendTests.spec

    def profile(self, **changes):
        value = {
            "kind": "local",
            "diagnostics": {
                "python": sys.executable,
                "conda_env_var": "CONNECTION_TEST_ENV",
                "conda_sh_var": "CONNECTION_TEST_SH",
                "directories": [str(self.root)],
            },
            **changes,
        }
        path = self.root / "doctor-profiles.json"
        path.write_text(json.dumps({"profiles": {"probe": value}}))
        path.chmod(0o600)
        return ProfileStore(path)

    def environment(self):
        return patch.dict(
            os.environ,
            {
                "CONNECTION_TEST_ENV": "fixture-environment-name",
                "CONNECTION_TEST_SH": str(self.conda),
            },
        )

    def test_control_output_is_streamed_and_keeps_head_tail_under_budget(self):
        # 总输出远大于预算；RSS 门禁排除先全量 capture 再截断的实现。
        code = (
            "import os\n"
            "os.write(1,b'BEGIN-STDOUT\\n');os.write(2,b'BEGIN-STDERR\\n')\n"
            "block=b'ordinary log line\\n'*8192\n"
            "for _ in range(256): os.write(1,block);os.write(2,block)\n"
            "os.write(1,b'END-STDOUT\\n');os.write(2,b'TAIL-ERROR\\n')\n"
        )
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        reply = LocalTransport().run([sys.executable, "-c", code], timeout_seconds=30)
        growth = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
        self.assertEqual(reply.returncode, 0)
        self.assertTrue(reply.truncated)
        self.assertLessEqual(len(reply.stdout), CONTROL_OUTPUT_LIMIT)
        self.assertLessEqual(len(reply.stderr), CONTROL_OUTPUT_LIMIT)
        self.assertIn(b"BEGIN-STDOUT", reply.stdout)
        self.assertIn(b"END-STDOUT", reply.stdout)
        self.assertIn(b"BEGIN-STDERR", reply.stderr)
        self.assertIn(b"TAIL-ERROR", reply.stderr)
        expected = len(b"ordinary log line\n") * 8192 * 256
        self.assertEqual(
            reply.output["streams"]["stdout"]["original_bytes"],
            expected + len(b"BEGIN-STDOUT\nEND-STDOUT\n"),
        )
        self.assertLess(growth, 24 * 1024)

    def test_redaction_precedes_capture_edges_and_chunk_boundaries(self):
        value = "fixture-sensitive-value-多字节"
        prefix = b"x" * (CONTROL_OUTPUT_LIMIT // 2 - 8)
        data = (
            prefix
            + value.encode()
            + b"\n"
            + b"y" * CONTROL_OUTPUT_LIMIT
            + value.encode()
            + b"\nTAIL-ERROR"
        )
        capture = OutputCapture(CONTROL_OUTPUT_LIMIT, StreamRedactor((value,)))
        for index in range(0, len(data), 113):
            capture.feed(data[index : index + 113])
        capture.finish()
        preview = capture.preview().decode("utf-8", errors="replace")
        self.assertNotIn("fixture-sensitive", preview)
        self.assertNotIn("多字节", preview)
        self.assertIn("[REDACTED]", preview)
        self.assertIn("TAIL-ERROR", preview)
        self.assertTrue(capture.raw.truncated)
        escaped = json.dumps(value)[1:-1]
        encoded_capture = OutputCapture(CONTROL_OUTPUT_LIMIT, StreamRedactor((value,)))
        encoded_capture.feed(
            ("start\n" + escaped + "\n" + "x" * CONTROL_OUTPUT_LIMIT + escaped).encode()
        )
        encoded_capture.finish()
        self.assertNotIn(
            "fixture-sensitive", encoded_capture.preview().decode("utf-8", errors="replace")
        )

    def test_long_credential_and_private_key_are_redacted_without_retaining_them(self):
        payload = (
            b'prefix\nAPI_TOKEN="'
            + b"credential-fragment" * 30000
            + b'"\n'
            + b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
            + b"private-body" * 30000
            + b"\n-----END OPENSSH PRIVATE KEY-----\nTAIL-ERROR\n"
        )
        redactor = StreamRedactor()
        output = []
        for index in range(0, len(payload), 4093):
            output.append(redactor.feed(payload[index : index + 4093]))
            self.assertLessEqual(len(redactor.pending), redactor.keep + 128)
        output.append(redactor.feed(b"", final=True))
        text = b"".join(output).decode()
        self.assertNotIn("credential-fragment", text)
        self.assertNotIn("private-body", text)
        self.assertIn("TAIL-ERROR", text)
        self.assertLess(len(text), 1000)
        self.assertEqual(
            redact_data({"API_TOKEN": "fixture", "SSH_PASSWORD": "fixture"}),
            {"API_TOKEN": "[REDACTED]", "SSH_PASSWORD": "[REDACTED]"},
        )

    def test_normal_json_is_unchanged_and_truncated_json_is_not_parsed_as_success(self):
        payload = {"ok": True, "field": "含空格与引号 ' \"", "number": 7}
        code = "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        reply = LocalTransport().run([sys.executable, "-c", code], input_data=encoded)
        self.assertEqual(reply.stdout, encoded)
        self.assertEqual(_parse_worker_result(reply, phase="inspect"), payload)
        huge = json.dumps({"ok": True, "data": "x" * (CONTROL_OUTPUT_LIMIT * 2)}).encode()
        clipped = LocalTransport().run([sys.executable, "-c", code], input_data=huge)
        with self.assertRaises(RRCError) as caught:
            _parse_worker_result(clipped, phase="inspect")
        self.assertEqual(caught.exception.code, "control_output_truncated")
        self.assertTrue(caught.exception.details["control_output"]["truncated"])

    def test_streaming_stdin_and_both_outputs_do_not_deadlock(self):
        data = b"input" * 200000
        code = (
            "import os,sys,json,hashlib\n"
            "os.write(2,b'warning\\n'*100000)\n"
            "data=sys.stdin.buffer.read()\n"
            "print(json.dumps({'ok':True,'sha256':hashlib.sha256(data).hexdigest()}))\n"
        )
        reply = LocalTransport().run(
            [sys.executable, "-c", code], input_data=data, timeout_seconds=10
        )
        result = self.control._parse_result(reply, phase="inspect")
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertTrue(self.control.control_output["truncated"])
        self.assertFalse(reply.stdout_truncated)

    def test_timeout_has_bounded_partial_output_and_unknown_total_size(self):
        started = time.monotonic()
        with self.assertRaises(RRCError) as caught:
            LocalTransport().run(
                [sys.executable, "-c", "import os,time;os.write(1,b'x'*2000000);time.sleep(10)"],
                timeout_seconds=0.3,
            )
        self.assertLess(time.monotonic() - started, 3)
        error = caught.exception
        self.assertEqual(error.code, "transport_timeout")
        self.assertTrue(error.details["remote_state_unknown"])
        self.assertIsNone(error.details["control_output"]["streams"]["stdout"]["original_bytes"])

    def test_binary_artifact_download_is_uncapped_and_arguments_stay_literal(self):
        program = self.root / "ssh"
        program.write_text(
            f"#!{sys.executable}\nimport os,sys\nos.execv('/bin/sh',['sh','-c',sys.argv[-1]])\n"
        )
        program.chmod(0o700)
        path = self.root / "binary 'quoted' $(must-not-execute).bin"
        data = bytes(range(256)) * 4096
        path.write_bytes(data)
        transport = SSHTransport(Profile("fixture", "ssh", (str(program), "fixture-host")))
        downloaded = transport.download(str(path))
        self.assertEqual(len(downloaded), len(data))
        self.assertEqual(hashlib.sha256(downloaded).digest(), hashlib.sha256(data).digest())
        reply = transport.run(
            [
                sys.executable,
                "-c",
                "import json,sys;print(json.dumps(sys.argv[1:]))",
                "a ' b",
                "$(literal)",
            ]
        )
        self.assertEqual(json.loads(reply.stdout), ["a ' b", "$(literal)"])

    def test_lost_launch_reply_is_unknown_and_same_run_can_be_observed(self):
        spec = self.spec(delay=0.8)
        readiness, _ = self.control._require_ready(spec)

        class DropReply(LocalTransport):
            launch_calls = 0

            def run(inner, argv, **kwargs):
                result = super().run(argv, **kwargs)
                if len(argv) > 2 and argv[2] == "launch":
                    inner.launch_calls += 1
                    raise RRCError("transport_connection", "fixture dropped reply", "observer")
                return result

        transport = DropReply()
        with (
            patch.object(self.control, "_require_ready", return_value=(readiness, transport)),
            self.assertRaises(RRCError) as caught,
        ):
            self.control.launch(spec)
        error = caught.exception
        self.assertEqual(error.outcome, "unknown")
        self.assertFalse(error.retryable)
        self.assertEqual(error.details["run_id"], spec.run_id)
        self.assertEqual(transport.launch_calls, 1)
        self.assertTrue(any(spec.run_id in action["argv"] for action in error.next_actions))
        record = load_json(self.control.index.root / spec.run_id / "operation-latest.json")
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(
            self.control.wait(spec.run_id, poll_seconds=0.05, max_wait_seconds=5)["status"][
                "state"
            ],
            "completed",
        )
        with self.assertRaises(RRCError) as duplicate:
            self.control.launch(spec)
        self.assertEqual(duplicate.exception.code, "run_already_indexed")
        self.assertEqual(transport.launch_calls, 1)

    def test_lost_abort_reply_does_not_claim_workload_was_preserved(self):
        spec = self.spec(delay=30)
        self.control.launch(spec)
        ref, _, _ = self.control._runtime(spec.run_id)

        class DropReply(LocalTransport):
            calls = 0

            def run(inner, argv, **kwargs):
                result = super().run(argv, **kwargs)
                if len(argv) > 2 and argv[2] == "abort":
                    inner.calls += 1
                    raise RRCError("transport_connection", "fixture dropped reply", "observer")
                return result

        transport = DropReply()
        with (
            patch.object(self.control, "_runtime", return_value=(ref, spec, transport)),
            self.assertRaises(RRCError) as caught,
        ):
            self.control.abort(spec.run_id, confirmed=True)
        self.assertEqual(caught.exception.outcome, "unknown")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("remote_workload_preserved", caught.exception.details)
        self.assertEqual(self.control.inspect(spec.run_id)["status"]["state"], "aborted")
        self.assertEqual(transport.calls, 1)

    def test_failed_preflight_reports_not_dispatched_and_points_to_doctor(self):
        spec = self.spec()
        with (
            patch.object(
                self.control,
                "_require_ready",
                side_effect=RRCError("transport_connection", "fixture", "observer"),
            ),
            self.assertRaises(RRCError) as caught,
        ):
            self.control.launch(spec)
        self.assertEqual(caught.exception.outcome, "failed")
        self.assertFalse(caught.exception.details["launch_dispatched"])
        self.assertEqual(caught.exception.next_actions[0]["operation"], "doctor")
        self.assertFalse((self.control.index.root / spec.run_id).exists())

    def test_server_rejection_and_local_spawn_failure_are_not_unknown(self):
        spec = self.spec()
        ref = SimpleNamespace(run_id=spec.run_id)
        failure = CommandResult(2, b'{"ok":false,"error":{"code":"rejected"}}', b"")
        transport = SimpleNamespace(run=lambda *_: failure, secret_values=())
        with self.assertRaises(RRCError) as caught:
            self.control._mutating_request(ref, transport, ["fixture"], operation="launch")
        self.assertEqual(caught.exception.code, "worker_command")
        self.assertNotEqual(caught.exception.outcome, "unknown")
        with self.assertRaises(RRCError) as spawn:
            LocalTransport().run([str(self.root / "missing-executable")])
        self.assertEqual(spawn.exception.code, "transport_spawn")

    def test_doctor_checks_local_and_remote_without_sourcing_env_or_creating_run(self):
        directory = self.root / "directory 'quoted' $(literal)"
        directory.mkdir()
        store = self.profile(
            diagnostics={
                "python": sys.executable,
                "conda_env_var": "CONNECTION_TEST_ENV",
                "conda_sh_var": "CONNECTION_TEST_SH",
                "directories": [str(directory)],
            }
        )
        marker = self.root / "must-not-be-created"
        env_file = store.path.parent / ".env"
        env_file.write_text(f"touch {marker}\n")
        env_file.chmod(0o600)
        before = set(self.root.iterdir())
        with self.environment():
            result = connection_doctor(store, "probe")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["connection_checked"])
        self.assertTrue(result["environment_checked"])
        self.assertFalse(result["server_health_checked"])
        self.assertFalse(marker.exists())
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertNotIn("fixture-environment-name", json.dumps(result))
        self.assertNotIn(str(self.conda), json.dumps(result))
        self.assertFalse((self.root / "index").exists())

    def test_doctor_reports_missing_variables_permissions_and_tools_before_connecting(self):
        store = self.profile()
        with (
            patch.dict(os.environ, {"CONNECTION_TEST_ENV": "", "CONNECTION_TEST_SH": ""}),
            patch(
                "remote_run_control.doctor.transport_for",
                side_effect=AssertionError("unexpected connection"),
            ),
        ):
            missing = connection_doctor(store, "probe")
        self.assertFalse(missing["ok"])
        self.assertEqual(
            [row["state"] for row in missing["checks"] if row["check"] == "environment_variable"],
            ["unset", "unset"],
        )
        env_file = store.path.parent / ".env"
        env_file.write_text("fixture")
        env_file.chmod(0o644)
        with (
            self.environment(),
            patch(
                "remote_run_control.doctor.transport_for",
                side_effect=AssertionError("unexpected connection"),
            ),
        ):
            insecure = connection_doctor(store, "probe")
        self.assertIn("env_file_permissions_insecure", [row["code"] for row in insecure["checks"]])
        env_file.chmod(0o600)
        store = self.profile(kind="ssh", ssh_argv=["ssh", "fixture-host"])
        with (
            self.environment(),
            patch("remote_run_control.doctor.shutil.which", return_value=None),
            patch(
                "remote_run_control.doctor.transport_for",
                side_effect=AssertionError("unexpected connection"),
            ),
        ):
            missing_tool = connection_doctor(store, "probe")
        self.assertIn("tool_missing", [row["code"] for row in missing_tool["checks"]])

    def test_doctor_reports_bad_profile_python_conda_and_directory(self):
        store = self.profile()
        self.assertFalse(connection_doctor(store, "absent")["ok"])
        store.path.write_text("not-json")
        self.assertEqual(
            connection_doctor(store, "probe")["checks"][0]["code"], "profile_file_invalid"
        )
        store = self.profile(
            diagnostics={
                "python": str(self.root / "missing-python"),
                "conda_env_var": "CONNECTION_TEST_ENV",
                "conda_sh_var": "CONNECTION_TEST_SH",
            }
        )
        with self.environment():
            missing_python = connection_doctor(store, "probe")
        self.assertIn("python_not_executable", [row["code"] for row in missing_python["checks"]])
        store = self.profile(
            diagnostics={
                "python": sys.executable,
                "conda_env_var": "CONNECTION_TEST_ENV",
                "conda_sh_var": "CONNECTION_TEST_SH",
                "directories": [str(self.root / "absent-directory")],
            }
        )
        with self.environment():
            missing_directory = connection_doctor(store, "probe")
        self.assertIn(
            "directory_inaccessible", [row["code"] for row in missing_directory["checks"]]
        )
        store = self.profile()
        with (
            self.environment(),
            patch.dict(os.environ, {"CONNECTION_TEST_SH": str(self.root / "missing-conda")}),
        ):
            missing_conda = connection_doctor(store, "probe")
        self.assertFalse(missing_conda["ok"])
        self.assertTrue(missing_conda["environment_checked"])
        self.assertIn("remote_preflight", [row["code"] for row in missing_conda["checks"]])
        self.assertNotIn(str(self.root / "missing-conda"), json.dumps(missing_conda))

    def test_profile_references_and_existing_host_options_are_preserved(self):
        options = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "fixture-host",
        ]
        store = self.profile(kind="ssh", ssh_argv=options)
        self.assertEqual(list(store.load("probe").ssh_argv), options)
        store = self.profile(kind="ssh", ssh_argv=["ssh", "fixture-host", "touch", "unexpected"])
        with self.assertRaises(RRCError):
            store.load("probe")
        store = self.profile(password_env="literal credential value")
        result = connection_doctor(store, "probe")
        self.assertFalse(result["ok"])
        self.assertNotIn("literal credential value", json.dumps(result))
        store = self.profile(password="fixture-inline-secret")
        result = connection_doctor(store, "probe")
        self.assertEqual(result["checks"][0]["code"], "profile_inline_credential")
        self.assertNotIn("fixture-inline-secret", json.dumps(result))

    def test_cli_envelope_and_old_wrapper_accept_success_failure_timeout_and_unknown(self):
        spec = self.spec()
        path = self.root / "spec.json"
        path.write_text(json.dumps(spec.to_dict()))
        cases = [
            (["doctor"], SimpleNamespace(), 0, "succeeded"),
            (
                ["ready", str(path), "--offline"],
                SimpleNamespace(ready=lambda *a, **kw: {"ready": True}),
                0,
                "succeeded",
            ),
            (
                ["ready", str(path), "--offline"],
                SimpleNamespace(ready=lambda *a, **kw: {"ready": False, "errors": []}),
                1,
                "failed",
            ),
            (
                ["inspect", spec.run_id],
                SimpleNamespace(inspect=lambda *a, **kw: {"status": {"state": "failed"}}),
                0,
                "succeeded",
            ),
            (
                ["wait", spec.run_id],
                SimpleNamespace(
                    wait=lambda *a, **kw: {"status": {"state": "running"}, "observation": "timeout"}
                ),
                124,
                "attention",
            ),
            (
                ["pull", spec.run_id],
                SimpleNamespace(pull=lambda *a, **kw: {"destination": "fixture"}),
                0,
                "succeeded",
            ),
            (
                ["health", spec.run_id, "--phase", "periodic"],
                SimpleNamespace(health=lambda *a, **kw: {"status": "unhealthy", "healthy": False}),
                0,
                "attention",
            ),
        ]

        def unknown(*args, **kwargs):
            raise RRCError(
                "transport_timeout",
                "fixture unknown launch",
                "observer",
                {"run_id": spec.run_id},
                outcome="unknown",
                retryable=False,
            )

        cases.append((["launch", str(path)], SimpleNamespace(launch=unknown), 2, "unknown"))
        sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
        from harness.remote.remote_run import _emit_stage

        for arguments, controller, expected_code, expected_status in cases:
            with self.subTest(operation=arguments[0], status=expected_status):
                output = io.StringIO()
                with (
                    patch.object(cli, "Controller", return_value=controller),
                    contextlib.redirect_stdout(output),
                ):
                    code = cli.main(["--json", *arguments])
                packet = json.loads(output.getvalue())
                self.assertEqual(code, expected_code)
                self.assertEqual(packet["schema_version"], "rrctl.cli.v1")
                self.assertEqual(packet["operation"], arguments[0])
                self.assertEqual(packet["status"], expected_status)
                self.assertIsInstance(packet["result"], dict)
                self.assertIsInstance(packet["error"], dict)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                if arguments[0] == "doctor":
                    self.assertIn("process", packet["backends"])
                    self.assertEqual(packet["backends"], packet["result"]["backends"])
                with (
                    patch.object(Path, "home", return_value=self.root),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    _emit_stage(arguments[0], packet, spec.run_id, code, full=False)

    def test_json_argument_error_is_one_structured_object(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["--json", "wait"])
        packet = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(packet["operation"], "wait")
        self.assertEqual(packet["status"], "failed")
        self.assertTrue(packet["error"]["next_actions"])

    def test_known_secret_is_removed_from_control_failure_previews(self):
        value = "fixture-only-long-credential"
        code = (
            "import os;os.write(2,os.environ['CONNECTION_TEST_SECRET'].encode()+b'\\nTAIL-ERROR');"
            "raise SystemExit(7)"
        )
        result = _capture_command(
            [sys.executable, "-c", code],
            input_data=None,
            timeout_seconds=5,
            env={**os.environ, "CONNECTION_TEST_SECRET": value},
            secret_values=(value,),
        )
        self.assertEqual(result.returncode, 7)
        self.assertNotIn(value, json.dumps(result.diagnostics()))
        self.assertIn("TAIL-ERROR", result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
