#!/usr/bin/env python3
"""端到端：reviewer_job verdict → build_rrctl_runspec gate 全矩阵。

verdict(scientifically_correct / scientifically_incorrect / not_evaluable)
× backend(codex / pi) × gate(on / off)
关键断言
- correct → gate 允许构建
- 非 correct → gate fail-closed（RunSpecBuildError）
- gate 关闭 → 构建不执行 gate 逻辑，可成功（不受 verdict 影响）
- backend 取证（codex=event-stream；pi=session-metadata）正确写入 verdict

假 codex/pi 用本地 shell 脚本，bin 目录在 PATH 中注入；测试会真实调用
reviewer_job.execute 和 build_runspec，不 mock 这两个函数。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / ".agents"))
from harness import reviewer_job  # noqa: E402
from harness.remote.build_rrctl_runspec import RunSpecBuildError, build_runspec  # noqa: E402

SCIENTIFIC_REVIEW_MODE = "scientific_review"
REQUEST_SCHEMA = "mission.rrctl-request.v1"
GATE_PROVENANCE_SCHEMA = "prerun.gate-provenance.v3"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class ReviewerGateE2ETests(unittest.TestCase):
    """verdict × backend × gate on/off 全矩阵。"""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reviewer-gate-e2e-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # Git 仓库：初始 commit 是 packet 的 pre_run_code_commit。
        self._run_git("init", "-b", "main")
        self._run_git("config", "user.name", "Reviewer Gate E2E")
        self._run_git("config", "user.email", "reviewer-gate@example.invalid")
        (self.root / "marker").write_text("initial\n")
        self._run_git("add", "marker")
        self._run_git("commit", "-m", "initial baseline")
        self.base_commit = self._run_git("rev-parse", "HEAD").stdout.strip()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "spec.md").write_text("# spec\n")
        self._run_git("add", "docs/spec.md")
        self._run_git("commit", "-m", "reviewed implementation")
        self.commit = self._run_git("rev-parse", "HEAD").stdout.strip()
        # review_diff_base_commit 是空 git tree（4b825dc...）在 Git 中静态定义；
        # 科学审查要求 pre_run_code_commit 是 review_diff_base_commit 的后代，
        # 用空树便可从仓库根开始对所有 commit 进行 diff。
        # base_commit 是与 spec 不同的一个合法 commit marker 即可。
        # Packet/task/job_dir：位于 repo_root 内（execute 会校验）。
        self.workdir = self.root / "review"
        self.workdir.mkdir()
        self.packet = self.workdir / "packet.json"
        self.task = self.workdir / "review-task.md"
        self.job_dir = self.workdir / "job"
        self.job_dir.mkdir()
        packet_body = {
            "schema_version": "prerun.scientific-review.v1",
            "review_mode": SCIENTIFIC_REVIEW_MODE,
            "pre_run_code_commit": self.commit,
            "repo_root": str(self.root),
            "review_diff_base_commit": self.base_commit,
            "approved_basis": ["unit-test"],
            "implementation_intent": "fixture",
            "exact_command": "bash train.sh",
            "output_collision_policy": "unique_output",
            "local_validation": [{"command": "bash train.sh", "observation": "fixture", "exit_code": 0}],
            "pre_review_smoke": {
                "schema_version": "prerun.pre-review-smoke.v1",
                "disposition": "passed",
                "candidate_commit": self.commit,
                "run_id": "fixture-run",
                "exact_command": "bash train.sh",
                "exit_code": 0,
                "step_budget": 1,
                "baseline_equivalence_required": False,
                "computation_kind": "training",
                "isolated_output": True,
                "production_entrypoint_reached": True,
                "official_metrics_disabled": True,
                "artifact_ingest_disabled": True,
                "finite_loss": True,
                "step_budget": 1,
                "completed_steps": 1,
                "checkpoint_cleanup_completed": True,
                "checkpoint_paths_remaining": [],
                "evidence_paths": ["smoke_console.log"],
                "retained_evidence_paths": ["console.log", "status.json", "smoke_summary.json"],
            },
            "critical_values": [{
                "name": "fixture", "source": "docs/spec.md", "sink": "stdout", "evidence": "fake",
            }],
            "experiment": {
                "benchmark": "fixture", "dataset": "fixture", "checkpoint": "fixture",
                "seed": 1, "metric_policy": "fixture", "output_path": "fixture",
            },
        }
        self.packet.write_text(json.dumps(packet_body))
        self.task.write_text("Fixture review task\n")
        # Fake backends 的 bin 目录（假 codex / pi 都从这里找）。
        self.bin_dir = self.root / "fake-bin"
        self.bin_dir.mkdir()
        # Patch：shutil.which 找 codex/pi 时重定向到 fake bin dir；其他透传。
        self._original_which = shutil.which
        shutil.which = self._which
        self.addCleanup(self._restore_which)

    def tearDown(self):
        self._restore_which()

    def _restore_which(self):
        shutil.which = self._original_which

    # 把 codex/pi 重定向到 fake bin；其他保持原 PATH 语义。
    def _which(self, cmd, mode=os.F_OK | os.X_OK, path=None):
        if cmd == "codex":
            fake = self.bin_dir / "codex"
            return str(fake) if fake.exists() else None
        if cmd == "pi":
            fake = self.bin_dir / "pi"
            return str(fake) if fake.exists() else None
        return self._original_which(cmd, mode=mode, path=path)

    def _run_git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True, check=True,
        )

    def _fresh_bin_dir(self) -> None:
        """Clear fake-bin 目录以便下一组 fixture 不受影响。"""
        if self.bin_dir.exists():
            shutil.rmtree(self.bin_dir)
        self.bin_dir.mkdir()

    def _make_fake_codex(self, result: str) -> Path:
        decision = "allow_run" if result == "scientifically_correct" else "do_not_run"
        model = reviewer_job.review_model.MODELS["codex"]
        body = f"""#!/bin/sh
# Emit codex-style event stream then write a minimal verdict payload.
out=""
session="fixture-codex-session"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-last-message) shift; out="$1" ;;
    fixture-*-session) session="$1" ;;
  esac
  shift || true
done
printf '%s\n' '{{"type":"thread.started","thread_id":"%s","model":"{model}"}}' "$session"
[ -n "$out" ] && cat > "$out" <<JSON
{{"reviewer_id":"fixture-independent","review_mode":"{SCIENTIFIC_REVIEW_MODE}","result":"{result}","decision":"{decision}","report_markdown":"fake verdict"}}
JSON
exit 0
"""
        target = self.bin_dir / "codex"
        _write_executable(target, body)
        return target

    def _make_fake_pi(self, result: str) -> Path:
        decision = "allow_run" if result == "scientifically_correct" else "do_not_run"
        model = reviewer_job.review_model.MODELS["pi"]
        body = f"""#!/bin/sh
# Pi backend: verdict is stdout; session file is written for provenance.
session=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --session) shift; session="$1" ;;
  esac
  shift || true
done
[ -n "$session" ] && cat > "$session" <<SESSIONS
{{"type":"message","role":"assistant","content":[]}}
{{"type":"model_change","provider":"openai-codex","modelId":"{model}"}}
SESSIONS
cat <<JSON
{{"reviewer_id":"fixture-independent","review_mode":"{SCIENTIFIC_REVIEW_MODE}","result":"{result}","decision":"{decision}","report_markdown":"fake verdict"}}
JSON
"""
        target = self.bin_dir / "pi"
        _write_executable(target, body)
        return target

    def _rewrite_job_dir(self) -> None:
        """delete verdict/job state 以便同 setUp 中跑多組不同 result。"""
        if self.job_dir.exists():
            shutil.rmtree(self.job_dir)
        self.job_dir.mkdir()

    def _run_review(self, backend: str, result: str) -> Path:
        """真实执行 reviewer_job 一次，返回 verdict 相对路径。"""
        self._rewrite_job_dir()
        self._fresh_bin_dir()
        if backend == "codex":
            self._make_fake_codex(result)
        else:
            self._make_fake_pi(result)
        args = Namespace(
            backend=backend,
            packet=self.packet,
            task=self.task,
            job_dir=self.job_dir,
            cwd=None,
            model=None,
            max_resumes=0,
            max_replacements=0,
            attempt_timeout_seconds=30,
        )
        code = reviewer_job.execute(args)
        self.assertEqual(code, 0, f"reviewer_job must complete: backend={backend} result={result}")
        verdict_path = self.job_dir / "verdict.json"
        self.assertTrue(verdict_path.is_file(), "verdict.json must be written")
        return Path(os.path.relpath(verdict_path, self.root))

    def _request(self, verdict_rel: Path | None, *, with_gate: bool) -> dict:
        base = {
            "schema_version": REQUEST_SCHEMA,
            "spec_id": "SPEC-A", "exp_id": "EXP-A", "run_id": "RUN-A",
            "project": "fixture",
            "source": {"repo_root": str(self.root), "branch": "main", "commit": self.commit},
            "remote": {
                "profile": "fixture",
                "stage_root": str(self.root / "remote" / "stage"),
                "repo_root": str(self.root / "remote" / "repo"),
                "control_root": str(self.root / "remote" / "control"),
                "output_root": str(self.root / "remote" / "output"),
            },
            "environment": {"kind": "conda", "name": "fixture", "conda_sh": "/fixture/conda.sh"},
            "workload": {"argv": ["bash", "train.sh"]},
            "adapter_contract": {
                "progress_path": "progress.json", "progress_count_field": "step",
                "first_step_min_count": 1, "completion_min_count": 1,
                "summary_path": "summary.json", "summary_required_fields": ["metric"],
                "summary_finite_fields": ["metric"],
            },
            "artifacts": [{"path": "summary.json", "required": True}],
        }
        if with_gate:
            review_result = (
                json.loads(self.job_dir.joinpath("verdict.json").read_text())["result"]
                if verdict_rel is not None
                else "scientifically_correct"
            )
            base["gate_provenance"] = {
                "schema_version": GATE_PROVENANCE_SCHEMA,
                "pre_run_code_commit": self.commit,
                "review_mode": SCIENTIFIC_REVIEW_MODE,
                "review_result": review_result,
                "reviewer_id": "fixture-independent",
                "verdict_artifact": str(verdict_rel) if verdict_rel is not None else "review/job/verdict.json",
                "blocker_closure_evidence": [],
            }
        return base

    # ----------------- gate 开门（scientifically_correct） -----------------
    def test_verdict_correct_gate_open_codex(self):
        rel = self._run_review("codex", "scientifically_correct")
        spec = build_runspec(self._request(rel, with_gate=True))
        self.assertIn("gate_provenance", spec["metadata"])

    def test_verdict_correct_gate_open_pi(self):
        rel = self._run_review("pi", "scientifically_correct")
        spec = build_runspec(self._request(rel, with_gate=True))
        self.assertIn("gate_provenance", spec["metadata"])

    # ----------------- gate fail-closed：scientifically_incorrect -----------------
    def test_verdict_incorrect_fails_closed_codex(self):
        rel = self._run_review("codex", "scientifically_incorrect")
        with self.assertRaisesRegex(RunSpecBuildError, "correctness_not_closed"):
            build_runspec(self._request(rel, with_gate=True))

    def test_verdict_incorrect_fails_closed_pi(self):
        rel = self._run_review("pi", "scientifically_incorrect")
        with self.assertRaisesRegex(RunSpecBuildError, "correctness_not_closed"):
            build_runspec(self._request(rel, with_gate=True))

    # ----------------- gate fail-closed：not_evaluable -----------------
    def test_verdict_not_evaluable_fails_closed_codex(self):
        rel = self._run_review("codex", "not_evaluable")
        with self.assertRaisesRegex(RunSpecBuildError, "review_result_invalid"):
            build_runspec(self._request(rel, with_gate=True))

    def test_verdict_not_evaluable_fails_closed_pi(self):
        rel = self._run_review("pi", "not_evaluable")
        with self.assertRaisesRegex(RunSpecBuildError, "review_result_invalid"):
            build_runspec(self._request(rel, with_gate=True))

    # ----------------- gate 关闭：不带 gate_provenance -----------------
    def test_gate_disabled_builds_without_provenance_codex(self):
        # Gate 不打开时，即使存在 scientifically_incorrect 的 verdict 也照常构建。
        self._run_review("codex", "scientifically_incorrect")
        spec = build_runspec(self._request(None, with_gate=False))
        self.assertNotIn("gate_provenance", spec["metadata"])

    def test_gate_disabled_builds_without_provenance_pi(self):
        self._run_review("pi", "scientifically_incorrect")
        spec = build_runspec(self._request(None, with_gate=False))
        self.assertNotIn("gate_provenance", spec["metadata"])

    # ----------------- backend 取证通道 -----------------
    def test_codex_event_stream_provenance_recorded(self):
        self._run_review("codex", "scientifically_correct")
        verdict = json.loads(self.job_dir.joinpath("verdict.json").read_text())
        self.assertEqual(verdict["model_evidence"], "event-stream")
        self.assertEqual(verdict["observed_model"], reviewer_job.review_model.MODELS["codex"])

    def test_pi_session_provenance_recorded(self):
        self._run_review("pi", "scientifically_correct")
        verdict = json.loads(self.job_dir.joinpath("verdict.json").read_text())
        self.assertEqual(verdict["model_evidence"], "session-metadata")
        self.assertEqual(verdict["observed_model"], reviewer_job.review_model.MODELS["pi"])

    def test_verdict_unknown_model_rejected_by_gate(self):
        """F-020: observed_model=unknown 的 verdict 应被 gate 拒绝。"""
        # 先跑一个正常 review 得到 verdict，然后篡改 observed_model 为 unknown
        self._run_review("pi", "scientifically_correct")
        verdict_path = self.job_dir / "verdict.json"
        verdict = json.loads(verdict_path.read_text())
        verdict["observed_model"] = "unknown"
        verdict_path.write_text(json.dumps(verdict))
        # 用篡改后的 verdict 构建 RunSpec，应被拒
        with self.assertRaises(RunSpecBuildError) as exc:
            build_runspec(self._request(Path(os.path.relpath(verdict_path, self.root)), with_gate=True))
        self.assertIn("verdict_artifact_model_unverifiable", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
