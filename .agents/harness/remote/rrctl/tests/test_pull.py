from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remote_run_control.controller import Controller, _PullProgress, _pull_lock
from remote_run_control.errors import RRCError
from remote_run_control.jsonutil import sha256_json
from remote_run_control.profiles import Profile
from remote_run_control.transport import SSHTransport, Transport, download_timeout_seconds


class FakeTransport:
    """Deliberately only provides the old bytes download interface."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = Counter()
        self.hook = None

    def download(self, path):
        fixture = self.fixture
        if path == "/control/binding.json":
            return json.dumps({"run_id": "run-1", "run_spec_sha256": fixture.spec.digest,
                               "control_root": "/control"}).encode()
        if path == "/control/artifact_manifest.json":
            return json.dumps(fixture.manifest).encode()
        if path == "/control/completion.json":
            raise RRCError("download_failed", "legacy fixture has no receipt", "transport")
        name = path.removeprefix("/output/")
        self.calls[name] += 1
        if self.hook:
            answer = self.hook(name)
            if answer is not None:
                return answer
        return fixture.data[name]


class PullTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rrctl-pull-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.spec = SimpleNamespace(digest="a" * 64, local_pull_root=str(self.root / "pulled"),
                                    remote=SimpleNamespace(output_root="/output"))
        self.ref = SimpleNamespace(control_root="/control")
        self.data = {"a.txt": b"first", "sub/b.bin": b"second", "c.json": b"third"}
        self.make_manifest()
        self.transport = FakeTransport(self)
        self.control = Controller(state_root=self.root / "index")
        runtime = patch.object(self.control, "_runtime", return_value=(self.ref, self.spec, self.transport))
        runtime.start()
        self.addCleanup(runtime.stop)
        self.destination = Path(self.spec.local_pull_root) / "run-1"

    def make_manifest(self):
        self.manifest = {"schema_version": "rrctl.artifacts.v1", "run_id": "run-1",
                         "generated_at": "fixed", "entries": [
                             {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                             for name, data in self.data.items()]}

    def staging(self):
        identity = sha256_json({"manifest": self.manifest, "run_spec_sha256": self.spec.digest,
                                "source_root": "/output"})
        return self.destination.parent / f".run-1.pull-{identity}"

    def interrupt_after_first(self):
        def fail(name):
            if name == "sub/b.bin":
                raise RRCError("transport_connection", "fixture interrupted", "transport")
        self.transport.hook = fail
        with self.assertRaises(RRCError):
            self.control.pull("run-1")
        self.assertFalse(self.destination.exists())
        self.assertEqual((self.staging() / "a.txt").read_bytes(), self.data["a.txt"])
        self.assertFalse((self.staging() / "sub/b.bin").exists())

    def test_minimal_policy_pulls_only_light_entries(self):
        self.spec.metadata = {"artifact_pull_policy": {"mode": "minimal", "on_demand": []}}
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            result = self.control.pull("run-1")
        self.assertEqual(result["pull_scope"], "summary")
        self.assertEqual(result["entries"], 2)          # a.txt + c.json (sub/b.bin 是重负载)
        self.assertEqual(result["total_entries"], 3)
        self.assertTrue((self.destination / "a.txt").is_file())
        self.assertTrue((self.destination / "c.json").is_file())
        self.assertFalse((self.destination / "sub" / "b.bin").exists())
        scope = json.loads((self.destination / "pull_scope.json").read_text())
        self.assertEqual(scope["retained_remotely"], ["sub/b.bin"])
        # 本地仍保留完整 manifest 供 provenance，仅下载集是轻量子集
        self.assertTrue((self.destination / "artifact_manifest.json").is_file())

    def test_force_all_overrides_minimal_policy(self):
        self.spec.metadata = {"artifact_pull_policy": {"mode": "minimal", "on_demand": []}}
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            result = self.control.pull("run-1", force_all=True)
        self.assertEqual(result["pull_scope"], "all")
        self.assertEqual(result["entries"], 3)
        self.assertTrue((self.destination / "sub" / "b.bin").is_file())

    def test_minimal_policy_keeps_required_heavy_entries(self):
        self.spec.metadata = {"artifact_pull_policy": {"mode": "minimal", "on_demand": []}}
        self.spec.artifacts = (SimpleNamespace(path="sub", required=True),)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            result = self.control.pull("run-1")
        # 合同 required 的重负载子树必须留在本地拉取集，否则 ingest 会缺必需文件
        self.assertEqual(result["pull_scope"], "summary")
        self.assertEqual(result["entries"], 3)
        self.assertTrue((self.destination / "sub" / "b.bin").is_file())

    def test_interruption_keeps_verified_files_and_retry_downloads_only_missing(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            self.interrupt_after_first()
            self.transport.hook = None
            result = self.control.pull("run-1")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(result["resumed_files"], 1)
        self.assertEqual(result["resumed_bytes"], len(self.data["a.txt"]))
        self.assertEqual(self.transport.calls, Counter({"a.txt": 1, "sub/b.bin": 2, "c.json": 1}))
        self.assertFalse(self.staging().exists())
        for name, data in self.data.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
        events = [json.loads(line.removeprefix("[rrctl-progress] "))
                  for line in stderr.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["start", "error", "start", "end"])
        end = events[-1]
        self.assertEqual(end["schema_version"], "rrctl.progress.v1")
        self.assertEqual(end["stage"], "pull")
        self.assertEqual(end["files"], end["total_files"])
        self.assertEqual(end["bytes"], end["total_bytes"])
        self.assertEqual(end["download_timeout_seconds"], 300)
        self.assertEqual(end["budget_scope"], "per_file")
        self.assertFalse((self.destination / ".rrctl").exists())
        self.assertTrue(self.control.pull("run-1")["reused"])
        self.assertEqual(self.transport.calls["a.txt"], 1)

    def test_same_process_threads_are_single_flight_and_busy_keeps_staging(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(name):
            if name == "sub/b.bin":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test release timed out")
        self.transport.hook = blocked
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.control.pull, "run-1")
            try:
                self.assertTrue(entered.wait(5))
                staged = self.staging() / "a.txt"
                with self.assertRaises(RRCError) as caught:
                    self.control.pull("run-1")
                self.assertEqual(caught.exception.code, "pull_busy")
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(staged.read_bytes(), self.data["a.txt"])
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5)["entries"], 3)

    def test_flock_excludes_another_process(self):
        source = str(Path(__file__).resolve().parents[1] / "src")
        script = ("import sys; from pathlib import Path; "
                  "from remote_run_control.controller import _pull_lock\n"
                  "try:\n with _pull_lock(Path(sys.argv[1])): print('unexpected')\n"
                  "except Exception as e: print(e.code, e.retryable)\n")
        with _pull_lock(self.destination):
            result = subprocess.run([sys.executable, "-c", script, str(self.destination)],
                                    env={**os.environ, "PYTHONPATH": source}, capture_output=True,
                                    text=True, check=True)
        self.assertEqual(result.stdout.strip(), "pull_busy True")

    def test_manifest_change_never_mixes_staging_even_with_same_file_hashes(self):
        self.interrupt_after_first()
        previous = self.staging()
        self.manifest["generated_at"] = "new-generation"
        self.transport.hook = None
        result = self.control.pull("run-1")
        self.assertEqual(result["resumed_files"], 0)
        self.assertEqual(self.transport.calls["a.txt"], 2)
        self.assertEqual((previous / "a.txt").read_bytes(), self.data["a.txt"])

    def test_run_spec_change_never_mixes_staging(self):
        self.interrupt_after_first()
        previous = self.staging()
        self.spec.digest = "b" * 64
        self.transport.hook = None
        self.assertEqual(self.control.pull("run-1")["resumed_files"], 0)
        self.assertTrue(previous.is_dir())
        self.assertEqual(self.transport.calls["a.txt"], 2)

    def test_wrong_sha_is_not_published_or_cached_and_good_files_survive(self):
        self.transport.hook = lambda name: b"WRONG!" if name == "sub/b.bin" else None
        with self.assertRaises(RRCError) as caught:
            self.control.pull("run-1")
        self.assertEqual(caught.exception.code, "pull_integrity")
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.staging() / "sub/b.bin").exists())
        self.assertTrue((self.staging() / "a.txt").is_file())
        self.transport.hook = None
        self.assertEqual(self.control.pull("run-1")["resumed_files"], 1)

    def test_cached_files_are_rehashed_and_corrupt_cache_is_redownloaded(self):
        self.interrupt_after_first()
        (self.staging() / "a.txt").write_bytes(b"wrong")
        self.transport.hook = None
        self.assertEqual(self.control.pull("run-1")["resumed_files"], 0)
        self.assertEqual(self.transport.calls["a.txt"], 2)

    def test_final_verification_includes_previously_verified_cache(self):
        self.interrupt_after_first()
        def tamper(name):
            if name == "c.json":
                (self.staging() / "a.txt").write_bytes(b"wrong")
        self.transport.hook = tamper
        with self.assertRaises(RRCError) as caught:
            self.control.pull("run-1")
        self.assertEqual(caught.exception.code, "pull_integrity")
        self.assertFalse(self.destination.exists())

    def test_staged_parent_symlink_is_rejected_without_touching_outside(self):
        self.interrupt_after_first()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "b.bin").write_text("do not touch")
        (self.staging() / "sub").rmdir()
        (self.staging() / "sub").symlink_to(outside, target_is_directory=True)
        self.transport.hook = None
        with self.assertRaises(RRCError) as caught:
            self.control.pull("run-1")
        self.assertEqual(caught.exception.code, "pull_symlink")
        self.assertEqual((outside / "b.bin").read_text(), "do not touch")

    def test_incomplete_destination_is_still_collision(self):
        self.destination.mkdir(parents=True)
        (self.destination / "artifact_manifest.json").write_text(json.dumps(self.manifest))
        with self.assertRaises(RRCError) as caught:
            self.control.pull("run-1")
        self.assertEqual(caught.exception.code, "pull_collision")
        self.assertFalse(self.staging().exists())

    def test_progress_has_low_frequency_batches_and_thirty_second_heartbeat(self):
        self.data = {f"{index}.bin": b"x" for index in range(70)}
        self.make_manifest()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.control.pull("run-1")
        events = [json.loads(line.split(" ", 1)[1]) for line in stderr.getvalue().splitlines()]
        self.assertEqual([item["files"] for item in events], [0, 32, 64, 70])
        stderr = io.StringIO()
        with patch("remote_run_control.controller.time.monotonic", side_effect=[0, 0, 29, 30, 31]):
            progress = _PullProgress("run-1", self.manifest, 1800)
            with contextlib.redirect_stderr(stderr):
                progress.emit("start")
                progress.emit(current_bytes=2)
                progress.emit(current_bytes=3)
                progress.emit(current_bytes=4)
        heartbeat = [json.loads(line.split(" ", 1)[1]) for line in stderr.getvalue().splitlines()]
        self.assertEqual(len(heartbeat), 2)
        self.assertEqual(heartbeat[-1]["bytes"], 3)
        self.assertEqual(heartbeat[-1]["elapsed_seconds"], 30)
        self.assertIn("文件 0/70", heartbeat[-1]["message"])
        self.assertIn("已传 3.0 B/70.0 B", heartbeat[-1]["message"])
        self.assertIn("速度", heartbeat[-1]["message"])
        self.assertNotIn("ETA", heartbeat[-1]["message"])

    def test_lock_and_staging_symlinks_are_rejected(self):
        self.destination.parent.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        lock = self.destination.parent / ".run-1.pull.lock"
        lock.symlink_to(outside / "lock")
        with self.assertRaises(RRCError) as caught:
            self.control.pull("run-1")
        self.assertEqual(caught.exception.code, "pull_symlink")
        self.assertFalse((outside / "lock").exists())
        lock.unlink()
        self.staging().symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RRCError) as caught:
            self.control.pull("run-1")
        self.assertEqual(caught.exception.code, "pull_symlink")
        self.assertEqual(list(outside.iterdir()), [])

    def completion_controls(self):
        from remote_run_control.finalization import run_identity
        self.spec.run_id = "run-1"
        self.spec.remote.control_root = "/control"
        binding_bytes = self.transport.download("/control/binding.json")
        receipt = {"schema_version": "rrctl.completion.v1", "run_id": "run-1",
                   "run_spec_sha256": self.spec.digest,
                   "run_identity": run_identity(self.spec, json.loads(binding_bytes)),
                   "manifest_sha256": sha256_json(self.manifest), "exit_code": 0,
                   "health": {"schema_version": "rrctl.health.v2", "phase": "completion",
                              "healthy": True, "complete": True}}
        status = {"schema_version": "rrctl.status.v1", "run_id": "run-1", "state": "completed",
                  "sequence": 5, "detail": {"completion_sha256": sha256_json(receipt)}}
        return {"/control/completion.json": json.dumps(receipt).encode(),
                "/control/status.json": json.dumps(status).encode()}

    def test_receipt_spec_manifest_status_and_binding_hashes_are_checked(self):
        controls = self.completion_controls()
        controls["/control/status.json"] = controls["/control/status.json"].replace(
            sha256_json(json.loads(controls["/control/completion.json"])).encode(), b"0" * 64)
        original_download = self.transport.download
        self.transport.download = lambda path: controls[path] if path in controls else original_download(path)
        with self.assertRaises(RRCError) as caught:
            self.control.pull("run-1")
        self.assertEqual(caught.exception.code, "completion_receipt_invalid")
        self.assertFalse(self.destination.exists())
        self.assertTrue((self.staging() / "a.txt").is_file())
        controls.update(self.completion_controls())
        result = self.control.pull("run-1")
        self.assertEqual(result["resumed_files"], 3)
        for filename in ("completion.json", "status.json"):
            self.assertEqual((self.destination / ".rrctl" / filename).read_bytes(), controls["/control/" + filename])
        self.assertTrue(self.control.pull("run-1")["reused"])

    def test_receipt_mismatched_spec_manifest_or_binding_identity_is_rejected(self):
        original_download = self.transport.download
        for field in ("run_spec_sha256", "manifest_sha256", "run_identity"):
            with self.subTest(field=field):
                controls = self.completion_controls()
                receipt = json.loads(controls["/control/completion.json"])
                receipt[field] = "0" * 64
                controls["/control/completion.json"] = json.dumps(receipt).encode()
                status = json.loads(controls["/control/status.json"])
                status["detail"]["completion_sha256"] = sha256_json(receipt)
                controls["/control/status.json"] = json.dumps(status).encode()
                self.transport.download = lambda path: controls[path] if path in controls else original_download(path)
                with self.assertRaises(RRCError) as caught:
                    self.control.pull("run-1")
                self.assertEqual(caught.exception.code, "completion_receipt_invalid")
                self.assertFalse(self.destination.exists())
                self.transport.download = original_download

    def test_reserved_metadata_never_overwrites_workload(self):
        self.data[".rrctl/completion.json"] = b"workload data"
        self.make_manifest()
        with self.assertRaises(RRCError) as caught:
            self.control.pull("run-1")
        self.assertEqual(caught.exception.code, "pull_manifest_path")
        self.assertFalse(self.destination.exists())


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rrctl-download-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def transport(self, source):
        program = self.root / "ssh"
        program.write_text(f"#!{sys.executable}\n" + source)
        program.chmod(0o700)
        return SSHTransport(Profile("fixture", "ssh", (str(program), "fixture-host")))

    def test_stream_download_preserves_binary_arguments_and_does_not_use_bytes_capture(self):
        transport = self.transport("import os,sys\nos.execv('/bin/sh',['sh','-c',sys.argv[-1]])\n")
        remote = self.root / "binary 'quoted' $(must-not-execute).bin"
        data = bytes(range(256)) * 8192
        remote.write_bytes(data)
        destination = self.root / "local.bin"
        counts = []
        with patch("remote_run_control.transport._capture_command", side_effect=AssertionError("bytes capture")):
            transport.download_to_file(str(remote), destination, timeout_seconds=5, progress=counts.append)
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(counts[-1], len(data))

    def test_infinite_stream_stops_before_exceeding_declared_size(self):
        transport = self.transport("import os\nwhile True: os.write(1,b'x'*65536)\n")
        destination = self.root / "oversize.bin"
        with self.assertRaises(RRCError) as caught:
            transport.download_to_file("ignored", destination, timeout_seconds=5,
                                       expected_size=100000, progress=lambda _: None)
        self.assertEqual(caught.exception.code, "pull_integrity")
        self.assertLessEqual(destination.stat().st_size, 100000)

    def test_local_and_legacy_download_enforce_size_before_writing(self):
        from remote_run_control.transport import LocalTransport
        source = self.root / "source"
        source.write_bytes(b"too large")
        class Legacy(Transport):
            def download(self, path):
                return source.read_bytes()
        for index, transport in enumerate((LocalTransport(), Legacy())):
            destination = self.root / f"bounded-{index}"
            with self.assertRaises(RRCError) as caught:
                transport.download_to_file(str(source), destination, timeout_seconds=5,
                                           expected_size=2, progress=lambda _: None)
            self.assertEqual(caught.exception.code, "pull_integrity")
            self.assertTrue(not destination.exists() or destination.stat().st_size <= 2)

    def test_stream_error_redacts_secrets_without_decoding_binary_stdout(self):
        transport = self.transport("import os,sys\nos.write(1,b'\\xff\\x00');"
                                   "os.write(2,b'private-token');sys.exit(1)\n")
        transport.redaction_values = ("private-token",)
        destination = self.root / "binary-error"
        with self.assertRaises(RRCError) as caught:
            transport.download_to_file("ignored", destination, timeout_seconds=5,
                                       expected_size=2, progress=lambda _: None)
        self.assertEqual(destination.read_bytes(), b"\xff\x00")
        self.assertNotIn("private-token", json.dumps(caught.exception.to_dict()))

    def test_stream_timeout_is_real_wall_clock_and_does_not_publish(self):
        transport = self.transport("import os,time\nos.write(1,b'partial');time.sleep(5)\n")
        destination = self.root / "partial.bin"
        with self.assertRaises(RRCError) as caught:
            transport.download_to_file("ignored", destination, timeout_seconds=0.1, progress=lambda _: None)
        self.assertEqual(caught.exception.code, "transport_timeout")
        self.assertEqual(caught.exception.details["timeout_seconds"], 0.1)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(destination.read_bytes(), b"partial")

    def test_download_budget_default_override_and_invalid_values(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(download_timeout_seconds(), 300)
        with patch.dict(os.environ, {"RRCTL_DOWNLOAD_TIMEOUT_SECONDS": "7200"}):
            self.assertEqual(download_timeout_seconds(), 7200)
        for value in ("nan", "inf", "-1", "0", "86401", "bad"):
            with self.subTest(value=value), patch.dict(os.environ, {"RRCTL_DOWNLOAD_TIMEOUT_SECONDS": value}):
                with self.assertRaises(RRCError) as caught:
                    download_timeout_seconds()
                self.assertEqual(caught.exception.code, "transport_budget")

    def test_legacy_transport_subclass_falls_back_to_download(self):
        class Legacy(Transport):
            def download(self, remote_path):
                return b"legacy"
        destination = self.root / "legacy"
        counts = []
        Legacy().download_to_file("ignored", destination, timeout_seconds=1800, progress=counts.append)
        self.assertEqual(destination.read_bytes(), b"legacy")
        self.assertEqual(counts, [6])
