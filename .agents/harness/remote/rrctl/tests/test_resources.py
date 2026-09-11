from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remote_run_control import resources
from remote_run_control.errors import RRCError
from remote_run_control.models import ResourcesSpec


class ResourceTests(unittest.TestCase):
    def spec(self, run_id="a", ids=(), device="gpu", minimum=1024):
        return SimpleNamespace(run_id=run_id, resources=ResourcesSpec(device, tuple(ids), minimum))

    def inventory(self, fields, *, applications=False):
        return [] if applications else [["0", "GPU-one", "24000"], ["1", "GPU-two", "24000"]]

    def test_default_lease_prevents_overlapping_run(self):
        with (
            tempfile.TemporaryDirectory() as name,
            patch.object(resources, "_query", side_effect=self.inventory),
            patch.object(resources, "owned_processes", return_value=[{"pid": 123}]),
        ):
            root = Path(name)
            first = resources.acquire(self.spec(), root / "a", lease_root=root / "leases")
            self.assertEqual(first["gpu_ids"], ["GPU-one", "GPU-two"])
            with self.assertRaises(RRCError) as caught:
                resources.acquire(self.spec("b", ["1"]), root / "b", lease_root=root / "leases")
            self.assertEqual(caught.exception.code, "gpu_lease_busy")

    def test_explicit_disjoint_devices_can_run_concurrently(self):
        with (
            tempfile.TemporaryDirectory() as name,
            patch.object(resources, "_query", side_effect=self.inventory),
            patch.object(resources, "owned_processes", return_value=[{"pid": 123}]),
        ):
            root = Path(name)
            resources.acquire(self.spec("a", ["0"]), root / "a", lease_root=root / "leases")
            second = resources.acquire(
                self.spec("b", ["GPU-two"]), root / "b", lease_root=root / "leases"
            )
            self.assertEqual(second["gpu_ids"], ["GPU-two"])

    def test_index_uuid_alias_cannot_bypass_lease(self):
        with (
            tempfile.TemporaryDirectory() as name,
            patch.object(resources, "_query", side_effect=self.inventory),
            patch.object(resources, "owned_processes", return_value=[{"pid": 123}]),
        ):
            root = Path(name)
            resources.acquire(self.spec("a", ["0"]), root / "a", lease_root=root / "leases")
            with self.assertRaises(RRCError):
                resources.acquire(
                    self.spec("b", ["GPU-one"]), root / "b", lease_root=root / "leases"
                )
            with self.assertRaises(RRCError) as duplicate:
                resources.available_devices(self.spec("c", ["0", "GPU-one"]))
            self.assertEqual(duplicate.exception.code, "gpu_duplicate")

    def test_stale_lease_is_reclaimed_but_live_descendant_keeps_it(self):
        with (
            tempfile.TemporaryDirectory() as name,
            patch.object(resources, "_query", side_effect=self.inventory),
        ):
            root = Path(name)
            with patch.object(resources, "owned_processes", return_value=[]):
                first = resources.acquire(
                    self.spec("a", ["0"]), root / "a", lease_root=root / "leases"
                )
                resources.acquire(self.spec("b", ["0"]), root / "b", lease_root=root / "leases")
            self.assertFalse(Path(first["lease_path"]).exists())
            with (
                patch.object(resources, "owned_processes", return_value=[{"pid": 123}]),
                self.assertRaises(RRCError),
            ):
                resources.acquire(self.spec("c", ["0"]), root / "c", lease_root=root / "leases")

    def test_cpu_probe_never_queries_or_locks_gpus(self):
        with tempfile.TemporaryDirectory() as name, patch.object(resources, "_query") as query:
            root = Path(name)
            result = resources.acquire(
                self.spec(device="cpu"), root / "cpu", lease_root=root / "leases"
            )
            self.assertEqual(result["gpu_ids"], [])
            self.assertFalse((root / "leases").exists())
            query.assert_not_called()

    def test_foreign_gpu_process_and_insufficient_memory_block_start(self):
        def occupied(fields, *, applications=False):
            return [["GPU-one", "987"]] if applications else self.inventory(fields)

        with patch.object(resources, "_query", side_effect=occupied):
            with self.assertRaises(RRCError) as caught:
                resources.available_devices(self.spec(ids=["0"]))
            self.assertEqual(caught.exception.code, "gpu_busy")
        with (
            patch.object(resources, "_query", side_effect=self.inventory),
            self.assertRaises(RRCError),
        ):
            resources.available_devices(self.spec(minimum=30000))

    def test_invalid_registry_fails_without_deleting_it(self):
        with (
            tempfile.TemporaryDirectory() as name,
            patch.object(resources, "_query", side_effect=self.inventory),
        ):
            root = Path(name)
            leases = root / "leases"
            leases.mkdir()
            invalid = leases / "invalid.json"
            invalid.write_text(json.dumps({"schema_version": "unknown"}))
            with self.assertRaises(RRCError):
                resources.acquire(self.spec(), root / "run", lease_root=leases)
            self.assertTrue(invalid.exists())


if __name__ == "__main__":
    unittest.main()
