from __future__ import annotations

import hashlib
import json
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from remote_run_control.models import RunSpec


def _run(*argv: str, cwd: Path) -> str:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def project_factory(tmp_path: Path):
    def create(*, delay_seconds: float = 1.0) -> tuple[Path, str]:
        repo = tmp_path / f"project-{uuid.uuid4().hex}"
        repo.mkdir()
        (repo / "dummy_workload.py").write_text(
            textwrap.dedent(
                f"""
                import json
                import os
                from pathlib import Path
                import time

                output = Path(os.environ["RRCTL_OUTPUT_ROOT"])
                output.mkdir(parents=True, exist_ok=False)
                (output / "progress.json").write_text(
                    json.dumps({{"completed": 1, "total": 2}}), encoding="utf-8"
                )
                print("dummy workload started", flush=True)
                time.sleep({delay_seconds})
                (output / "artifact.txt").write_text("artifact-data\\n", encoding="utf-8")
                (output / "summary.json").write_text(
                    json.dumps({{"complete": True, "items": 1}}), encoding="utf-8"
                )
                print("dummy workload completed", flush=True)
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (repo / "dummy_adapter.py").write_text(
            textwrap.dedent(
                """
                import json
                from pathlib import Path
                import sys

                context = json.load(sys.stdin)
                output = Path(context["output_root"])
                progress = output / "progress.json"
                summary = output / "summary.json"
                phase = context["phase"]
                healthy = progress.is_file()
                complete = phase == "completion" and summary.is_file()
                print(json.dumps({
                    "protocol": "rrctl.adapter.v1",
                    "healthy": healthy,
                    "complete": complete,
                    "progress": json.loads(progress.read_text()) if progress.is_file() else {},
                    "observations": {"phase": phase},
                    "artifacts": ["artifact.txt", "summary.json"] if complete else [],
                }))
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (repo / "anchor.txt").write_text("anchor-data\n", encoding="utf-8")
        _run("git", "init", "-b", "main", cwd=repo)
        _run("git", "config", "user.name", "Test", cwd=repo)
        _run("git", "config", "user.email", "test@example.invalid", cwd=repo)
        _run("git", "add", ".", cwd=repo)
        _run("git", "commit", "-m", "fixture", cwd=repo)
        return repo, _run("git", "rev-parse", "HEAD", cwd=repo)

    return create


@pytest.fixture
def profile_file(tmp_path: Path) -> Path:
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"profiles": {"local-test": {"kind": "local"}}}), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def spec_factory(tmp_path: Path, profile_file: Path):
    def create(repo: Path, commit: str, *, include_anchor: bool = False) -> RunSpec:
        run_id = f"dummy-{uuid.uuid4().hex[:10]}"
        remote = tmp_path / f"remote-{run_id}"
        remote.mkdir()
        pull = tmp_path / "pulled"
        conda_sh = Path("/home/zhou/miniconda3/etc/profile.d/conda.sh")
        if not conda_sh.is_file():
            conda_sh = Path("/root/miniconda3/etc/profile.d/conda.sh")
        raw = {
            "schema_version": "rrctl.run.v1",
            "run_id": run_id,
            "project": "dummy-project",
            "source": {
                "repo_root": str(repo),
                "branch": "main",
                "commit": commit,
                "allowed_post_commit_paths": [],
            },
            "remote": {
                "profile": "local-test",
                "stage_root": str(remote / "stage"),
                "repo_root": str(remote / "repo"),
                "control_root": str(remote / "control"),
                "output_root": str(remote / "output"),
                "python": "python3",
            },
            "session": {"backend": "tmux", "name": f"rrctl-{run_id}"},
            "environment": {"kind": "conda", "name": "base", "conda_sh": str(conda_sh)},
            "workload": {"argv": ["python", "dummy_workload.py"], "cwd": "."},
            "health": {
                "first_step": {
                    "timeout_seconds": 15,
                    "poll_interval_seconds": 0.1,
                    "console_stale_seconds": 10,
                    "progress_path": "progress.json",
                    "progress_stale_seconds": 10,
                    "adapter_argv": ["python", "dummy_adapter.py"],
                },
                "periodic": {
                    "timeout_seconds": 30,
                    "poll_interval_seconds": 0.1,
                    "console_stale_seconds": 10,
                    "progress_path": "progress.json",
                    "progress_stale_seconds": 10,
                    "adapter_argv": ["python", "dummy_adapter.py"],
                },
                "completion": {
                    "timeout_seconds": 30,
                    "poll_interval_seconds": 0.1,
                    "adapter_argv": ["python", "dummy_adapter.py"],
                },
            },
            "anchors": [],
            "artifacts": [{"path": "artifact.txt", "required": True}],
            "local_pull_root": str(pull),
            "metadata": {"purpose": "generic-e2e"},
        }
        if include_anchor:
            anchor = repo / "anchor.txt"
            raw["anchors"] = [
                {
                    "name": "input-anchor",
                    "local_path": str(anchor),
                    "remote_path": str(remote / "input" / "anchor.txt"),
                    "sha256": hashlib.sha256(anchor.read_bytes()).hexdigest(),
                }
            ]
        return RunSpec.from_dict(raw)

    return create
