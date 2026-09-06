from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from remote_run_control.artifacts import build_artifact_manifest, load_artifact_manifest
from remote_run_control.errors import RRCError
from remote_run_control.jsonutil import sha256_file, utc_now
from remote_run_control.models import ArtifactSpec
from remote_run_control.profiles import Profile
from remote_run_control.transport import LocalTransport, SSHTransport
from remote_run_control.zipapp_builder import build_worker_zipapp


def test_utc_now_uses_python310_compatible_timezone_api():
    value = utc_now()
    assert value.endswith("+00:00")
    assert "UTC" not in Path(
        __import__("remote_run_control.jsonutil").jsonutil.__file__
    ).read_text(encoding="utf-8").split("from datetime import", 1)[1].splitlines()[0]


def test_worker_zipapp_build_is_deterministic(tmp_path):
    source = __import__("remote_run_control").__path__[0]
    source_root = __import__("pathlib").Path(source).parent
    first = tmp_path / "first.pyz"
    second = tmp_path / "second.pyz"
    assert build_worker_zipapp(source_root, first) == build_worker_zipapp(source_root, second)
    assert first.read_bytes() == second.read_bytes()
    result = subprocess.run(
        [sys.executable, str(first), "--help"], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "rrctl-worker" in result.stdout


def test_artifact_manifest_hashes_files_and_rejects_symlinks(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "artifact.txt").write_text("data\n", encoding="utf-8")
    destination = tmp_path / "manifest.json"
    value = build_artifact_manifest(
        run_id="run-1",
        output_root=output,
        declared=(ArtifactSpec("artifact.txt"),),
        destination=destination,
    )
    assert value["entries"][0]["sha256"] == sha256_file(output / "artifact.txt")
    assert load_artifact_manifest(destination, expected_run_id="run-1") == value

    (output / "link.txt").symlink_to(output / "artifact.txt")
    with pytest.raises(RRCError, match="artifact_symlink"):
        build_artifact_manifest(
            run_id="run-1",
            output_root=output,
            declared=(ArtifactSpec("link.txt"),),
            destination=destination,
        )


def test_diagnostic_snapshot_without_completion_is_bounded_and_immutable(tmp_path):
    from remote_run_control.artifacts import build_diagnostic_snapshot

    control = tmp_path / "control"
    output = tmp_path / "output"
    control.mkdir()
    output.mkdir()
    (control / "status.json").write_text('{"state":"failed"}')
    (control / "console.log").write_bytes(b"x" * 100 + b"OOM\n")
    (output / "progress.json").write_text('{"step":3}')
    (output / "weights.pt").write_bytes(b"weights")
    result = build_diagnostic_snapshot(
        run_id="run-1",
        control_root=control,
        output_root=output,
        output_paths=["progress.json", "missing.json", "weights.pt"],
        max_file_bytes=32,
        max_total_bytes=80,
    )
    snapshot = control / "diagnostics" / result["snapshot_id"]
    assert (snapshot / "control/console.log").read_bytes().endswith(b"OOM\n")
    assert len((snapshot / "control/console.log").read_bytes()) == 32
    assert not (snapshot / "output/weights.pt").exists()
    assert not (control / "artifact_manifest.json").exists()
    assert json.loads((control / "status.json").read_text())["state"] == "failed"
    (control / "console.log").write_text("changed")
    manifest = load_artifact_manifest(snapshot / "artifact_manifest.json", expected_run_id="run-1")
    for entry in manifest["entries"]:
        assert sha256_file(snapshot / entry["path"]) == entry["sha256"]
    metadata = json.loads((snapshot / "snapshot.json").read_text())
    assert any(item["truncated"] for item in metadata["files"])
    assert sum(item["copied_bytes"] for item in metadata["files"]) <= 80


def test_overlapping_artifact_declarations_are_deduplicated(tmp_path):
    output = tmp_path / "output"
    (output / "logs").mkdir(parents=True)
    (output / "logs/train.log").write_text("log")
    path = tmp_path / "manifest.json"
    build_artifact_manifest(
        run_id="run-1",
        output_root=output,
        declared=(ArtifactSpec("logs"), ArtifactSpec("logs/train.log")),
        destination=path,
    )
    assert len(load_artifact_manifest(path)["entries"]) == 1


def test_local_transport_upload_is_atomic_and_collision_safe(tmp_path):
    transport = LocalTransport()
    stage = tmp_path / "missing-parent" / "stage"
    transport.mkdir_exclusive(str(stage))
    source = tmp_path / "source.bin"
    source.write_bytes(b"content")
    destination = stage / "target.bin"
    transport.upload(source, str(destination), sha256_file(source))
    assert transport.download(str(destination)) == b"content"
    with pytest.raises(RRCError, match="upload_collision"):
        transport.upload(source, str(destination), sha256_file(source))

    mismatched = stage / "mismatched.bin"
    with pytest.raises(RRCError, match="upload_sha_mismatch"):
        transport.upload(source, str(mismatched), "0" * 64)
    assert not mismatched.exists()
    assert not (stage / ".mismatched.bin.rrctl-upload").exists()


def test_artifact_manifest_rejects_duplicate_paths_and_non_hex_sha(tmp_path):
    path = tmp_path / "manifest.json"
    entry = {"path": "artifact.txt", "size": 1, "sha256": "g" * 64}
    path.write_text(
        json.dumps(
            {
                "schema_version": "rrctl.artifacts.v1",
                "run_id": "run-1",
                "entries": [entry],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RRCError, match="artifact_entry_sha"):
        load_artifact_manifest(path)

    entry["sha256"] = "0" * 64
    path.write_text(
        json.dumps(
            {
                "schema_version": "rrctl.artifacts.v1",
                "run_id": "run-1",
                "entries": [entry, entry],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RRCError, match="artifact_entry_duplicate"):
        load_artifact_manifest(path)


def test_ssh_transport_quotes_argv_and_redacts_password(monkeypatch):
    secret = "credential-value"
    monkeypatch.setenv("DUMMY_SSH_PASSWORD", secret)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=secret.encode())

    monkeypatch.setattr(subprocess, "run", fake_run)
    transport = SSHTransport(
        Profile(
            name="remote-test",
            kind="ssh",
            ssh_argv=("ssh", "host-alias"),
            password_env="DUMMY_SSH_PASSWORD",
        )
    )

    with pytest.raises(RRCError) as caught:
        transport.mkdir_exclusive("/remote/path with spaces")

    assert captured["argv"][:4] == ["sshpass", "-e", "ssh", "host-alias"]
    assert captured["argv"][-1].startswith("bash -c ")
    assert "mkdir -p -m 700" in captured["argv"][-1]
    assert "mkdir -m 700" in captured["argv"][-1]
    assert captured["argv"][-1].endswith(
        "rrctl-mkdir-exclusive '/remote/path with spaces'"
    )
    assert captured["env"]["SSHPASS"] == secret
    assert all(secret not in argument for argument in captured["argv"])
    assert secret not in str(caught.value)
