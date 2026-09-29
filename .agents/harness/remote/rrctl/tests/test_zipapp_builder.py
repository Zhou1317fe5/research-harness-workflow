"""worker zipapp 构建的确定性与拒绝路径。"""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remote_run_control import zipapp_builder


class ZipappBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rrctl-zipapp-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "src"
        package = self.source / "remote_run_control"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# test package\n")
        (package / "worker.py").write_text("def main():\n    return 0\n")

    def tearDown(self):
        self.temporary.cleanup()

    def test_build_produces_deterministic_executable_zipapp(self):
        first = self.root / "first.pyz"
        second = self.root / "second.pyz"
        sha_first = zipapp_builder.build_worker_zipapp(self.source, first)
        sha_second = zipapp_builder.build_worker_zipapp(self.source, second)
        self.assertEqual(sha_first, sha_second)
        self.assertEqual(len(sha_first), 64)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o700)
        with zipfile.ZipFile(first) as archive:
            names = set(archive.namelist())
        self.assertIn("__main__.py", names)
        self.assertIn("remote_run_control/__init__.py", names)
        self.assertIn("remote_run_control/worker.py", names)

    def test_main_entry_imports_worker_main(self):
        destination = self.root / "worker.pyz"
        zipapp_builder.build_worker_zipapp(self.source, destination)
        with zipfile.ZipFile(destination) as archive:
            main = archive.read("__main__.py").decode()
        self.assertIn("from remote_run_control.worker import main", main)

    def test_real_package_build_includes_all_modules(self):
        source_root = Path(__file__).resolve().parents[1] / "src"
        destination = self.root / "real.pyz"
        zipapp_builder.build_worker_zipapp(source_root, destination)
        with zipfile.ZipFile(destination) as archive:
            names = set(archive.namelist())
        for module in ("health.py", "readiness.py", "finalization.py",
                       "cleanup.py", "security.py", "worker.py"):
            self.assertIn(f"remote_run_control/{module}", names)

    def test_missing_package_directory_is_rejected(self):
        destination = self.root / "nested" / "out.pyz"
        with self.assertRaises(ValueError):
            zipapp_builder.build_worker_zipapp(self.root / "no-such-src", destination)

    def test_destination_parent_is_created(self):
        destination = self.root / "deep" / "nested" / "worker.pyz"
        zipapp_builder.build_worker_zipapp(self.source, destination)
        self.assertTrue(destination.is_file())


if __name__ == "__main__":
    unittest.main()
