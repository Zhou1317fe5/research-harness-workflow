from __future__ import annotations

import json
import subprocess
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from remote_run_control.controller import Controller
from remote_run_control.errors import RRCError
from remote_run_control.models import ArtifactSpec, OutputCleanupSpec


def _commit_workload(repo: Path, source: str) -> str:
    (repo / "dummy_workload.py").write_text(textwrap.dedent(source).strip() + "\n")
    subprocess.run(["git", "add", "dummy_workload.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "smoke cleanup fixture"], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _smoke_spec(spec):
    return replace(
        spec,
        health=replace(
            spec.health,
            completion=replace(spec.health.completion, adapter_argv=()),
        ),
        artifacts=(ArtifactSpec("smoke_summary.json", True),),
        output_cleanup=OutputCleanupSpec(
            mode="pre_review_smoke",
            retain=("smoke_summary.json",),
            delete_globs=(
                "checkpoint-*",
                "**/checkpoint-*",
                "optimizer*",
                "**/optimizer*",
                "scheduler*",
                "**/scheduler*",
            ),
            delete_files_larger_than_bytes=1024,
        ),
        metadata={"execution_purpose": "pre_review_smoke", "thin_smoke": True},
    )


def _summary(spec) -> dict:
    return json.loads(
        (Path(spec.remote.output_root) / "smoke_summary.json").read_text(encoding="utf-8")
    )


def _assert_clean(spec, terminal_state: str) -> None:
    output = Path(spec.remote.output_root)
    summary = _summary(spec)
    assert summary["terminal_state"] == terminal_state
    assert summary["checkpoint_cleanup_completed"] is True
    assert summary["checkpoint_paths_remaining"] == []
    assert not list(output.rglob("checkpoint-*"))
    assert not list(output.rglob("optimizer*"))
    assert not list(output.rglob("scheduler*"))
    assert not (output / "large.bin").exists()


def test_successful_smoke_cleans_disposable_outputs(
    tmp_path: Path, project_factory, spec_factory, profile_file
):
    repo, _ = project_factory()
    commit = _commit_workload(
        repo,
        """
        import json, os
        from pathlib import Path

        output = Path(os.environ["RRCTL_OUTPUT_ROOT"])
        output.mkdir(parents=True, exist_ok=False)
        (output / "checkpoint-1").mkdir()
        (output / "checkpoint-1" / "model.bin").write_bytes(b"x" * 32)
        (output / "optimizer.pt").write_bytes(b"x" * 32)
        (output / "scheduler.pt").write_bytes(b"x" * 32)
        (output / "large.bin").write_bytes(b"x" * 2048)
        (output / "progress.json").write_text(json.dumps({"completed": 1, "total": 2}))
        (output / "artifact.txt").write_text("artifact-data\\n")
        (output / "summary.json").write_text(json.dumps({"complete": True, "items": 1}))
        """,
    )
    spec = _smoke_spec(spec_factory(repo, commit))
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")

    controller.launch(spec)
    terminal = controller.wait(spec.run_id, poll_seconds=0.1)
    assert terminal["status"]["state"] == "completed"
    _assert_clean(spec, "workload_exit_zero")


def test_failed_smoke_cleans_disposable_outputs(
    tmp_path: Path, project_factory, spec_factory, profile_file
):
    repo, _ = project_factory()
    commit = _commit_workload(
        repo,
        """
        import os
        from pathlib import Path

        output = Path(os.environ["RRCTL_OUTPUT_ROOT"])
        output.mkdir(parents=True, exist_ok=False)
        (output / "checkpoint-2").mkdir()
        (output / "optimizer.pt").write_bytes(b"x" * 32)
        (output / "large.bin").write_bytes(b"x" * 2048)
        raise SystemExit(7)
        """,
    )
    spec = _smoke_spec(spec_factory(repo, commit))
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")

    with pytest.raises(RRCError, match="first_step_terminal"):
        controller.launch(spec)
    assert controller.inspect(spec.run_id)["status"]["state"] == "failed"
    _assert_clean(spec, "failed")


def test_aborted_smoke_cleans_disposable_outputs(
    tmp_path: Path, project_factory, spec_factory, profile_file
):
    repo, _ = project_factory()
    commit = _commit_workload(
        repo,
        """
        import json, os, time
        from pathlib import Path

        output = Path(os.environ["RRCTL_OUTPUT_ROOT"])
        output.mkdir(parents=True, exist_ok=False)
        (output / "checkpoint-3").mkdir()
        (output / "scheduler.pt").write_bytes(b"x" * 32)
        (output / "large.bin").write_bytes(b"x" * 2048)
        (output / "progress.json").write_text(json.dumps({"completed": 1, "total": 2}))
        time.sleep(30)
        """,
    )
    spec = _smoke_spec(spec_factory(repo, commit))
    controller = Controller(profiles_path=profile_file, state_root=tmp_path / "state")

    controller.launch(spec)
    aborted = controller.abort(spec.run_id, confirmed=True)
    assert aborted["state"] == "aborted"
    _assert_clean(spec, "aborted")
