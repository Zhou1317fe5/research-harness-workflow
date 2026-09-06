from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
ROOT = HARNESS.parents[1]


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "project with spaces"
    harness = repo / ".agents/harness"
    harness.mkdir(parents=True)
    for source in HARNESS.glob("*.py"):
        shutil.copy2(source, harness / source.name)
    shutil.copytree(HARNESS / "rrctl_adapters", harness / "rrctl_adapters", ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "fit.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "p=Path(sys.argv[1]);p.mkdir(parents=True,exist_ok=True)\n"
        "(p/'model.bin').write_text(sys.argv[2])\n"
        "print('fit output',flush=True)\n"
    )
    (repo / "score.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "p=Path(sys.argv[1]);assert (p/'model.bin').is_file()\n"
        "(p/'reports').mkdir()\n"
        "(p/'reports/metrics.json').write_text(json.dumps({'scores':{'quality':0.9},'seed':7}))\n"
        "print('score output',flush=True)\n"
    )
    return repo


def configure(repo, *, failing=False, missing=False):
    literal = "literal ; $(touch should-not-exist)"
    fit = [sys.executable, "-c", "raise SystemExit(9)"] if failing else [sys.executable, "fit.py", "{output_root}", literal]
    outputs = ["missing.bin"] if missing else ["model.bin"]
    text = f'''version = 1
[[pipeline.stages]]
name = "fit"
argv = {json.dumps(fit)}
outputs = {json.dumps(outputs)}
[[pipeline.stages]]
name = "score"
argv = {json.dumps([sys.executable, "score.py", "{output_root}"])}
requires = ["model.bin"]
outputs = ["reports/metrics.json"]
[records]
summary_glob = "reports/metrics.json"
primary_metric = "scores.quality"
dimensions = ["seed"]
'''
    (repo / ".agents/harness/project.toml").write_text(text)
    return literal


def pipeline(repo, output):
    return subprocess.run([sys.executable, str(repo / ".agents/harness/run_pipeline.py"), "--output-root", str(output)], cwd=repo.parent, text=True, capture_output=True)


def test_pipeline_and_records_use_project_paths_and_field_mapping(project):
    literal = configure(project)
    output = project / "remote_artifacts/E1/R1"
    result = pipeline(project, output)
    assert result.returncode == 0, result.stderr
    assert "fit output" in result.stdout and "score output" in result.stdout
    assert (output / "model.bin").read_text() == literal
    assert not (project / "should-not-exist").exists()
    assert (output / "fit.log").read_text().strip() == "fit output"
    command = [sys.executable, str(project / ".agents/harness/experiment_records.py")]
    built = subprocess.run([*command, "build", "--exp", "E1"], cwd=project.parent, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    record = json.loads((project / "research_workspace/experiments/E1/record.json").read_text())
    assert record["runs"][0]["run_id"] == "R1"
    assert record["runs"][0]["metric"] == 0.9
    assert record["runs"][0]["seed"] == 7
    assert record["runs"][0]["summary_path"] == "remote_artifacts/E1/R1/reports/metrics.json"
    derived = subprocess.run([*command, "derive"], cwd=project.parent, capture_output=True, text=True)
    assert derived.returncode == 0, derived.stderr
    assert (project / "research_workspace/EXPERIMENTS.csv").is_file()
    assert pipeline(project, output).returncode != 0


@pytest.mark.parametrize("failing,missing", [(True, False), (False, True)])
def test_pipeline_stops_before_evaluation(project, failing, missing):
    configure(project, failing=failing, missing=missing)
    output = project / "output"
    result = pipeline(project, output)
    assert result.returncode != 0
    assert not (output / "reports/metrics.json").exists()
    assert not (output / "score.log").exists()


def test_unsafe_config_is_rejected_before_execution(project):
    configure(project)
    path = project / ".agents/harness/project.toml"
    path.write_text(path.read_text().replace('outputs = ["model.bin"]', 'outputs = ["../model.bin"]'))
    output = project / "output"
    assert pipeline(project, output).returncode != 0
    assert not output.exists()


def test_host_skill_sources_match_and_wrappers_resolve():
    def sources(host):
        base = ROOT / host / "skills"
        return {p.relative_to(base): p.read_bytes() for p in base.rglob("*") if p.is_file() and p.suffix in {".md", ".py", ".yaml", ".yml"} and "__pycache__" not in p.parts}
    assert sources(".claude") == sources(".codex")
    for host in (".claude", ".codex"):
        wrapper = ROOT / host / "skills/remote-run-snippet/scripts/generate_snippet.py"
        result = subprocess.run([sys.executable, str(wrapper), "--help"], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert not (ROOT / host / "harness").exists()
        assert not (ROOT / host / "skills/remote-pull-manifest").exists()
