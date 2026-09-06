"""Build a deterministic, standard-library-only remote worker zipapp."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

from .jsonutil import sha256_file

MAIN = (
    "from remote_run_control.worker import main\n"
    "if __name__ == '__main__':\n"
    "    raise SystemExit(main())\n"
)


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, data)


def build_worker_zipapp(source_root: Path, destination: Path) -> str:
    package_root = source_root / "remote_run_control"
    if not package_root.is_dir():
        raise ValueError(f"package directory not found: {package_root}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        _write_entry(archive, "__main__.py", MAIN.encode("utf-8"), mode=0o755)
        for path in sorted(package_root.rglob("*.py")):
            relative = path.relative_to(source_root).as_posix()
            _write_entry(archive, relative, path.read_bytes())
    destination.chmod(0o700)
    return sha256_file(destination)
