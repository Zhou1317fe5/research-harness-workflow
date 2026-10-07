from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from remote_run_control.artifacts import load_artifact_manifest
from remote_run_control.controller import _RECOVERY_SCRIPT
from remote_run_control.errors import RRCError
from remote_run_control.security import confined_relative_path


class RecoverySnapshotTests(unittest.TestCase):
    def run_helper(self, control: Path, output: Path, paths: list[str]):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _RECOVERY_SCRIPT,
                str(control),
                str(output),
                json.dumps(paths),
                "run-1",
                "a" * 64,
            ],
            capture_output=True,
            text=True,
        )
        return result

    def test_manifest_binds_identity_and_hashes_explicit_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control, output = root / "control", root / "output"
            (output / "predictions" / "masks").mkdir(parents=True)
            (output / "predictions" / "predictions.jsonl").write_text('{"ok":true}\n')
            (output / "predictions" / "masks" / "m.png").write_bytes(b"mask")

            result = self.run_helper(control, output, ["predictions/predictions.jsonl", "predictions/masks"])
            self.assertEqual(result.returncode, 0, result.stderr)
            created = json.loads(result.stdout)
            manifest_path = Path(created["root"]) / "artifact_manifest.json"
            manifest = load_artifact_manifest(manifest_path, expected_run_id="run-1")
            self.assertEqual(manifest["run_spec_sha256"], "a" * 64)
            self.assertEqual(
                {entry["path"] for entry in manifest["entries"]},
                {"output/predictions/predictions.jsonl", "output/predictions/masks/m.png"},
            )

    def test_rejects_parent_and_symlink_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control, output = root / "control", root / "output"
            output.mkdir()
            (output / "real.txt").write_text("x")
            (output / "link.txt").symlink_to(output / "real.txt")
            parent = self.run_helper(control, output, ["../real.txt"])
            self.assertNotEqual(parent.returncode, 0)
            link = self.run_helper(control, output, ["link.txt"])
            self.assertNotEqual(link.returncode, 0)

    def test_manifest_validation_rejects_tampered_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(json.dumps({
                "schema_version": "rrctl.artifacts.v1",
                "run_id": "run-1",
                "entries": [{"path": "output/a", "size": 1, "sha256": "0" * 64}],
            }))
            manifest = load_artifact_manifest(path, expected_run_id="run-1")
            self.assertEqual(manifest["entries"][0]["sha256"], "0" * 64)
            with self.assertRaises(RRCError):
                confined_relative_path("../output/a", field="recovery.path")
