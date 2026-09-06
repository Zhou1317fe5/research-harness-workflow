from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from remote_run_control.errors import RRCError
from remote_run_control.models import OutputCleanupSpec, RunSpec, SourceSpec
from remote_run_control.profiles import ProfileStore
from remote_run_control.readiness import validate_run_spec


def test_valid_run_spec_is_ready_and_deterministic(project_factory, spec_factory, profile_file):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit, include_anchor=True)
    store = ProfileStore(profile_file)
    first = validate_run_spec(spec, profile_store=store)
    second = validate_run_spec(spec, profile_store=store)
    assert first.ready, first.to_dict()
    assert first.to_dict() == second.to_dict()
    assert len(first.run_spec_sha256) == 64
    assert first.source_content_sha256 is not None
    assert len(first.source_content_sha256) == 64


def test_runtime_relevant_drift_is_rejected(project_factory, spec_factory, profile_file):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    (repo / "dummy_workload.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
    result = validate_run_spec(spec, profile_store=ProfileStore(profile_file))
    assert not result.ready
    assert "source_drift" in {item.code for item in result.errors}


def test_explicit_bookkeeping_drift_is_allowed(project_factory, spec_factory, profile_file):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    (repo / "notes").mkdir()
    (repo / "notes" / "status.json").write_text("{}\n", encoding="utf-8")
    spec = replace(
        spec,
        source=SourceSpec(
            repo_root=spec.source.repo_root,
            branch=spec.source.branch,
            commit=spec.source.commit,
            allowed_post_commit_paths=("notes/**",),
        ),
    )
    result = validate_run_spec(spec, profile_store=ProfileStore(profile_file))
    assert result.ready, result.to_dict()


def test_secret_and_artifact_traversal_are_rejected(project_factory, spec_factory, profile_file):
    repo, commit = project_factory()
    raw = spec_factory(repo, commit).to_dict()
    raw["workload"]["argv"] = [*raw["workload"]["argv"], "--api-key=not-allowed"]
    raw["artifacts"] = [{"path": "../escape.txt", "required": True}]
    result = validate_run_spec(RunSpec.from_dict(raw), profile_store=ProfileStore(profile_file))
    codes = {item.code for item in result.errors}
    assert {"secret_in_argv", "artifact_path"} <= codes


def test_structured_secret_cwd_anchor_and_regex_are_rejected(
    project_factory, spec_factory, profile_file
):
    repo, commit = project_factory()
    raw = spec_factory(repo, commit, include_anchor=True).to_dict()
    raw["metadata"] = {"api_key": "not-allowed"}
    raw["workload"]["cwd"] = "../outside"
    raw["anchors"][0]["name"] = "../unsafe"
    raw["anchors"][0]["remote_path"] = raw["remote"]["repo_root"] + "/input.txt"
    raw["health"]["periodic"]["fatal_patterns"] = ["["]

    result = validate_run_spec(RunSpec.from_dict(raw), profile_store=ProfileStore(profile_file))
    codes = {item.code for item in result.errors}
    assert {
        "secret_in_spec",
        "workload_cwd",
        "anchor_name",
        "anchor_remote_overlap",
        "fatal_pattern",
    } <= codes


def test_schema_rejects_coerced_boolean_and_numeric_types(project_factory, spec_factory):
    repo, commit = project_factory()
    raw = spec_factory(repo, commit).to_dict()
    raw["artifacts"][0]["required"] = "false"
    with pytest.raises(RRCError, match="spec_type"):
        RunSpec.from_dict(raw)

    raw = spec_factory(repo, commit).to_dict()
    raw["health"]["periodic"]["poll_interval_seconds"] = "1"
    with pytest.raises(RRCError, match="spec_type"):
        RunSpec.from_dict(raw)

    raw = spec_factory(repo, commit).to_dict()
    raw["health"]["periodic"]["gpu_utilization_policy"] = "sometimes"
    with pytest.raises(RRCError, match="spec_type"):
        RunSpec.from_dict(raw)


def test_required_gpu_policy_needs_threshold_and_continuous_window(
    project_factory, spec_factory, profile_file
):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    periodic = replace(spec.health.periodic, gpu_utilization_policy="required")
    spec = replace(spec, health=replace(spec.health, periodic=periodic))

    result = validate_run_spec(spec, profile_store=ProfileStore(profile_file))

    assert "required_gpu_window" in {item.code for item in result.errors}


def test_output_cleanup_is_confined_to_run_bound_smoke(
    project_factory, spec_factory, profile_file
):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    cleanup = OutputCleanupSpec(
        mode="pre_review_smoke",
        retain=("smoke_summary.json",),
        delete_globs=("checkpoint-*", "**/checkpoint-*"),
        delete_files_larger_than_bytes=1024,
    )
    invalid = replace(spec, output_cleanup=cleanup)
    result = validate_run_spec(invalid, profile_store=ProfileStore(profile_file))
    assert "cleanup_purpose" in {item.code for item in result.errors}

    escaped = replace(
        spec,
        output_cleanup=replace(cleanup, delete_globs=("../checkpoint-*",)),
        metadata={"execution_purpose": "pre_review_smoke"},
    )
    result = validate_run_spec(escaped, profile_store=ProfileStore(profile_file))
    assert "cleanup_path" in {item.code for item in result.errors}

    valid = replace(
        spec,
        output_cleanup=cleanup,
        metadata={"execution_purpose": "pre_review_smoke"},
    )
    result = validate_run_spec(valid, profile_store=ProfileStore(profile_file))
    assert result.ready, result.to_dict()


def test_frozen_source_content_mismatch_is_rejected(project_factory, spec_factory, profile_file):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    spec = replace(
        spec,
        source=replace(spec.source, source_content_sha256="0" * 64),
    )

    result = validate_run_spec(spec, profile_store=ProfileStore(profile_file))

    assert "source_content_sha_mismatch" in {item.code for item in result.errors}


def test_transport_bundle_alias_normalizes_without_changing_legacy_api(
    project_factory, spec_factory
):
    repo, commit = project_factory()
    raw = spec_factory(repo, commit).to_dict()
    raw["source"]["transport_bundle_sha256"] = "1" * 64

    spec = RunSpec.from_dict(raw)

    assert spec.source.bundle_sha256 == "1" * 64
    assert spec.source.transport_bundle_sha256 == "1" * 64
    assert "gpu_utilization_policy" not in spec.to_dict()["health"]["periodic"]

    raw["source"]["bundle_sha256"] = "2" * 64
    with pytest.raises(RRCError, match="disagree"):
        RunSpec.from_dict(raw)


def test_unresolvable_full_commit_is_rejected(project_factory, spec_factory, profile_file):
    repo, commit = project_factory()
    spec = spec_factory(repo, commit)
    spec = replace(spec, source=replace(spec.source, commit="0" * 40))
    result = validate_run_spec(spec, profile_store=ProfileStore(profile_file))
    assert "commit_missing" in {item.code for item in result.errors}


def test_profile_file_with_password_must_be_private(tmp_path: Path, monkeypatch):
    path = tmp_path / "profiles.json"
    path.write_text(
        '{"profiles":{"remote":{"kind":"ssh","ssh_argv":["ssh","host"],'
        '"password_env":"TEST_PASSWORD"}}}',
        encoding="utf-8",
    )
    path.chmod(0o644)
    monkeypatch.setenv("TEST_PASSWORD", "secret")
    from remote_run_control.errors import RRCError

    try:
        ProfileStore(path).load("remote")
    except RRCError as exc:
        assert exc.code == "profile_permissions_insecure"
    else:
        raise AssertionError("insecure profile permissions were accepted")
