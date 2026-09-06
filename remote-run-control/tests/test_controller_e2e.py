from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import remote_run_control.controller as controller_module
from remote_run_control.controller import Controller
from remote_run_control.errors import RRCError
from remote_run_control.jsonutil import sha256_file
from remote_run_control.source_identity import source_content_sha256


def test_dummy_local_e2e_launch_wait_pull_and_resume(
    tmp_path: Path, project_factory, spec_factory, profile_file
):
    repo, commit = project_factory(delay_seconds=1.0)
    spec = spec_factory(repo, commit)
    state_root = tmp_path / "state"
    controller = Controller(profiles_path=profile_file, state_root=state_root)

    ready = controller.ready(spec)
    assert ready["ready"], ready
    launched = controller.launch(spec)
    assert launched["first_step"]["healthy"]
    assert launched["first_step"]["status"] == "healthy"
    binding = controller.inspect(spec.run_id)["binding"]
    assert binding["source_content_sha256"] == source_content_sha256(repo, spec.source)
    assert binding["transport_bundle_sha256"] == binding["bundle_sha256"]

    diagnostic = controller.pull(spec.run_id, diagnostic=True)
    diagnostic_path = Path(diagnostic["destination"])
    assert (diagnostic_path / "control/console.log").is_file()

    terminal = controller.wait(spec.run_id, poll_seconds=0.1)
    assert terminal["status"]["state"] == "completed"
    events = [
        json.loads(line)
        for line in (Path(spec.remote.control_root) / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["state"] for event in events][-2:] == [
        "workload_complete",
        "completed",
    ]
    assert (
        subprocess.run(
            ["tmux", "has-session", "-t", spec.session.name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0
    )
    pulled = controller.pull(spec.run_id)
    destination = Path(pulled["destination"])
    assert (destination / "artifact.txt").read_text(encoding="utf-8") == "artifact-data\n"
    assert (destination / "summary.json").is_file()
    assert (diagnostic_path / "control/console.log").is_file()

    worker_path = Path(spec.remote.stage_root) / "rrctl-worker.pyz"
    worker_bytes = worker_path.read_bytes()
    worker_path.write_bytes(worker_bytes + b"tampered")
    with pytest.raises(RRCError) as caught:
        controller.resume(profile_name="local-test", control_root=spec.remote.control_root)
    assert caught.value.code == "resume_worker_sha_mismatch"
    worker_path.write_bytes(worker_bytes)

    (Path(spec.remote.control_root) / "status.json").write_text("{broken", encoding="utf-8")
    shutil.rmtree(state_root / spec.run_id)
    resumed = controller.resume(profile_name="local-test", control_root=spec.remote.control_root)
    assert resumed["status"]["state"] == "completed"
    assert resumed["status_recovered"] is True
    assert controller.inspect(spec.run_id)["status"]["state"] == "completed"


def test_abort_requires_matching_owned_process_and_session(
    tmp_path: Path, project_factory, spec_factory, profile_file
):
    repo, commit = project_factory(delay_seconds=30.0)
    spec = spec_factory(repo, commit)
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")
    sentinel = subprocess.Popen(["sleep", "30"])
    binding_path = Path(spec.remote.control_root) / "binding.json"
    original_binding = None
    try:
        controller.launch(spec)
        original_binding = json.loads(binding_path.read_text(encoding="utf-8"))
        mismatched = {**original_binding, "workload_pid": sentinel.pid}
        binding_path.write_text(json.dumps(mismatched), encoding="utf-8")

        with pytest.raises(RRCError) as caught:
            controller.abort(spec.run_id, confirmed=True)
        assert caught.value.details["worker"]["error"]["code"] == "abort_owner_mismatch"
        assert sentinel.poll() is None

        binding_path.write_text(json.dumps(original_binding), encoding="utf-8")
        aborted = controller.abort(spec.run_id, confirmed=True)
        assert aborted["state"] == "aborted"
        assert sentinel.poll() is None
    finally:
        if original_binding is not None and binding_path.is_file():
            binding_path.write_text(json.dumps(original_binding), encoding="utf-8")
        subprocess.run(
            ["tmux", "kill-session", "-t", spec.session.name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sentinel.terminate()
        sentinel.wait(timeout=5)


def test_authoritative_first_step_failure_reaps_owned_group(
    tmp_path: Path, project_factory, spec_factory, profile_file
):
    repo, _ = project_factory(delay_seconds=30.0)
    (repo / "dummy_adapter.py").write_text(
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'protocol':'rrctl.adapter.v1','healthy':False,"
        "'complete':False,'progress':{},'observations':{},'artifacts':[]}))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "dummy_adapter.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "unhealthy adapter"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    spec = spec_factory(repo, commit)
    spec = replace(
        spec,
        health=replace(
            spec.health,
            first_step=replace(
                spec.health.first_step,
                timeout_seconds=1,
                poll_interval_seconds=0.1,
            ),
        ),
    )
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")

    with pytest.raises(RRCError) as caught:
        controller.launch(spec)
    assert caught.value.code == "first_step_health"
    inspected = controller.inspect(spec.run_id)
    assert inspected["status"]["state"] == "failed"
    assert inspected["status"]["reason"] == "authoritative_first_step_unhealthy"
    assert inspected["status"]["detail"]["cleanup"] == "reaped"
    diagnostic = controller.pull(spec.run_id, diagnostic=True)
    diagnostic_status = json.loads(
        (Path(diagnostic["destination"]) / "control/status.json").read_text()
    )
    assert diagnostic_status["state"] == "failed"
    assert (
        subprocess.run(
            ["tmux", "has-session", "-t", spec.session.name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0
    )


def test_periodic_failure_reports_attention_and_preserves_owned_group(
    tmp_path: Path, project_factory, spec_factory, profile_file
):
    repo, commit = project_factory(delay_seconds=30.0)
    spec = spec_factory(repo, commit)
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")
    controller.launch(spec)
    (Path(spec.remote.output_root) / "progress.json").unlink()

    try:
        with pytest.raises(RRCError) as caught:
            controller.wait(spec.run_id, poll_seconds=0.1)
        assert caught.value.code == "periodic_health"
        assert caught.value.phase == "observer"
        assert caught.value.details["remote_workload_preserved"] is True
        assert caught.value.details["explicit_abort_required"] is True
        inspected = controller.inspect(spec.run_id)
        assert inspected["status"]["state"] == "running"
        assert inspected["binding"]["workload_pid"]
        assert (
            subprocess.run(
                ["tmux", "has-session", "-t", spec.session.name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
    finally:
        controller.abort(spec.run_id, confirmed=True)


def test_frozen_bundle_sha_is_reproducible_and_mismatch_fails_before_remote_mutation(
    tmp_path: Path, project_factory, spec_factory, profile_file, monkeypatch
):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")
    commands: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(argv, *args, **kwargs):
        commands.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(controller_module.subprocess, "run", recording_run)
    first = tmp_path / "first.bundle"
    second = tmp_path / "second.bundle"
    first_sha = controller._create_bundle(spec, first)
    second_sha = controller._create_bundle(spec, second)
    assert first_sha == second_sha
    assert any(
        command[:4] == ["git", "-c", "pack.threads=1", "-C"]
        and command[5:7] == ["bundle", "create"]
        for command in commands
    )

    frozen = replace(
        spec,
        source=replace(spec.source, bundle_sha256="0" * 64),
    )
    with pytest.raises(RRCError) as caught:
        controller._create_bundle(frozen, tmp_path / "mismatch.bundle")
    assert caught.value.code == "bundle_sha_mismatch"
    assert not Path(spec.remote.stage_root).exists()


def test_keyboard_interrupt_detaches_observer_without_reaping_remote_workload(
    tmp_path: Path, project_factory, spec_factory, profile_file, monkeypatch
):
    repo, commit = project_factory(delay_seconds=30.0)
    spec = spec_factory(repo, commit)
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")
    controller.launch(spec)

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(controller, "health", interrupt)
    try:
        with pytest.raises(RRCError) as caught:
            controller.wait(spec.run_id, poll_seconds=0.1)
        assert caught.value.code == "observer_detached"
        assert caught.value.details["remote_workload_preserved"] is True
        inspected = controller.inspect(spec.run_id)
        assert inspected["status"]["state"] == "running"
        assert inspected["binding"]["workload_pid"]
    finally:
        monkeypatch.undo()
        controller.abort(spec.run_id, confirmed=True)


def test_connection_error_does_not_reap_remote_workload(
    tmp_path: Path, project_factory, spec_factory, profile_file, monkeypatch
):
    repo, commit = project_factory(delay_seconds=30.0)
    spec = spec_factory(repo, commit)
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")
    controller.launch(spec)

    def disconnect(*_args, **_kwargs):
        raise RRCError("connection_lost", "observer transport failed", "transport")

    monkeypatch.setattr(controller, "health", disconnect)
    try:
        with pytest.raises(RRCError) as caught:
            controller.wait(spec.run_id, poll_seconds=0.1)
        assert caught.value.code == "connection_lost"
        inspected = controller.inspect(spec.run_id)
        assert inspected["status"]["state"] == "running"
    finally:
        monkeypatch.undo()
        controller.abort(spec.run_id, confirmed=True)


def test_source_content_identity_excludes_run_id_and_transport_bytes(
    tmp_path: Path, project_factory, spec_factory, profile_file
):
    repo, commit = project_factory()
    first = spec_factory(repo, commit)
    retry = replace(first, run_id=f"{first.run_id}-retry")
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")

    first_source = source_content_sha256(repo, first.source)
    retry_source = source_content_sha256(repo, retry.source)
    bundle = tmp_path / "source.bundle"
    controller._create_bundle(first, bundle)
    transport_before = sha256_file(bundle)
    bundle.write_bytes(bundle.read_bytes() + b"transport-envelope-change")
    transport_after = sha256_file(bundle)

    assert first_source == retry_source
    assert transport_before != transport_after
    assert source_content_sha256(repo, first.source) == first_source
