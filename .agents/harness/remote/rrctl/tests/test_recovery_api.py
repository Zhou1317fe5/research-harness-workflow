from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remote_run_control import cli
from remote_run_control.controller import Controller
from remote_run_control.errors import RRCError
from remote_run_control.transport import LocalTransport


class RecoveryAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rrctl-recovery-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.control_root = self.root / "control"
        self.control_root.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.artifact = self.output / "checkpoint.bin"
        self.artifact.write_bytes(bytes(range(256)) * 32)
        self.spec = SimpleNamespace(digest="a" * 64, remote=SimpleNamespace(
            python=sys.executable, control_root=str(self.control_root), output_root=str(self.output)))
        self.binding = {"run_id": "failed-run", "run_spec_sha256": self.spec.digest,
                        "control_root": str(self.control_root), "source_content_sha256": "b" * 64}
        self.status = {"run_id": "failed-run", "state": "failed", "detail": {"exit_code": 7}}
        self.exit = {"run_id": "failed-run", "run_spec_sha256": self.spec.digest, "exit_code": 7}
        for filename, value in (("binding.json", self.binding), ("status.json", self.status),
                                ("workload-exit.json", self.exit)):
            (self.control_root / filename).write_text(json.dumps(value))
        self.controller = Controller(state_root=self.root / "index")
        self.ref = SimpleNamespace(control_root=str(self.control_root))
        for name, value in (("_runtime", (self.ref, self.spec, LocalTransport())),
                            ("inspect", {"status": self.status})):
            mock = patch.object(self.controller, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def test_nonzero_requires_opt_in_then_preserves_real_assets_and_exit_identity(self):
        original = {p: p.read_bytes() for p in self.control_root.iterdir()}
        artifact_bytes = self.artifact.read_bytes()
        destination = self.root / "retained"
        with self.assertRaises(RRCError) as caught:
            self.controller.recover_evidence("failed-run", paths=["checkpoint.bin"],
                                             destination=destination)
        self.assertEqual(caught.exception.code, "recovery_exit")
        self.assertFalse(destination.exists())
        self.assertFalse((self.control_root / "recovery").exists())
        result = self.controller.recover_evidence("failed-run", paths=["checkpoint.bin"],
                                                  destination=destination, allow_nonzero_exit=True)
        manifest_bytes = (destination / "artifact_manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        remote_manifest = self.control_root / "recovery" / result["recovery_id"] / "artifact_manifest.json"
        self.assertEqual(manifest_bytes, remote_manifest.read_bytes())
        self.assertEqual(manifest["run_spec_sha256"], self.spec.digest)
        self.assertEqual(manifest["exit_code"], 7)
        self.assertEqual(manifest["terminal_state"], "failed")
        self.assertIs(manifest["completion_contract_passed"], False)
        self.assertEqual(result["exit_code"], 7)
        self.assertTrue(result["remote_state_preserved"])
        self.assertEqual((destination / "output/checkpoint.bin").read_bytes(), artifact_bytes)
        self.assertEqual(manifest["entries"][0]["sha256"], hashlib.sha256(artifact_bytes).hexdigest())
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.artifact.read_bytes(), artifact_bytes)
        self.assertFalse((self.control_root / "completion.json").exists())
        self.assertFalse((self.control_root / "artifact_manifest.json").exists())

    def test_nonzero_opt_in_does_not_bypass_original_exit_identity(self):
        self.exit["run_spec_sha256"] = "c" * 64
        (self.control_root / "workload-exit.json").write_text(json.dumps(self.exit))
        with self.assertRaises(RRCError) as caught:
            self.controller.recover_evidence("failed-run", paths=["checkpoint.bin"],
                destination=self.root / "retained", allow_nonzero_exit=True)
        self.assertEqual(caught.exception.code, "recovery_exit")
        self.assertFalse((self.control_root / "recovery").exists())

    def test_cli_explicit_nonzero_flag_is_forwarded_and_default_remains_false(self):
        for flags, expected in (([], False), (["--allow-nonzero-exit"], True)):
            with self.subTest(flags=flags), patch.object(cli, "Controller") as constructor:
                constructor.return_value.control_output = None
                constructor.return_value.recover_evidence.return_value = {"run_id": "failed-run"}
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main(["--json", "recover-evidence", "failed-run", "--path", "checkpoint.bin",
                                     "--destination", str(self.root / "retained"), *flags])
                self.assertEqual(code, 0)
                constructor.return_value.recover_evidence.assert_called_once_with(
                    "failed-run", paths=["checkpoint.bin"], destination=self.root / "retained",
                    allow_nonzero_exit=expected)
