"""Mission 端到端生命周期集成：mission_state + csv_state 在临时 git 仓真实闭环。

四个场景（每方法一个）：
1. 正常闭环 —— 7 行 CSV（CONTRACT→IMPL→VALID→PRERUN→RUN[ingested]→RESULT-ANALYSIS
   →REVIEW/closing），register 后逐行 apply_update，每步校验 CSV/sidecar/git 三类
   证据一致；closing 时 csv_completion_errors 为空、mission_state 可转 completed。
2. 暂停恢复 —— paused 期间 CSV 字节不变，恢复 active 后 current_task 依旧。
3. 取消后重跑 —— cancelled 任务的 CSV 不能再注册到新 task；task-c 经合法
   register(replaces=task-c)→ 自动 superseded，task-d 绑定新 CSV 接管。
4. F-012 —— git=已提交 后 remote_state=running_remote 被 csv_state 拒绝。

隔离：tempfile 仓 + subprocess git（同 test_mission_contracts.init_git）；HOME 被
patch 到临时目录，session-metadata 夹具写在假 HOME 下；apply_update / update 真实
调用，不 mock。
"""

import csv
import hashlib
import io
import json
import os
from contextlib import chdir
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[5]
for _path in (ROOT / ".agents", ROOT / ".codex/skills/mission-csv-execute/scripts"):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from csv_state import SCHEMA, StateUpdateError, apply_update  # noqa: E402
from harness.workflow.mission_state import load_registry, update  # noqa: E402
from harness import review_model  # noqa: E402
from mission_completion import (  # noqa: E402
    EXPECTED_FIELDS,
    csv_completion_errors,
    read_mission_csv,
)

REF = "job:e2e#mission-lifecycle"
STEM = "mission-e2e"
TASK = "mission-e2e-task"
EXP_ID = "EXP-E2E-1"
RUN_ID = "RUN-E2E-01"
CSV_REL = f"issues/{STEM}/{STEM}.csv"
REVIEWS_REL = f"issues/{STEM}/reviews"
RUN_ROOT_REL = f"remote_artifacts/{EXP_ID}/{RUN_ID}"
ARTIFACT_DIR_REL = f"remote_artifacts/{EXP_ID}/"
# source.commit 是 40/64-hex git 提交的引用；与本地 git commit_hash 同型。由
# _write_provenance 的调用方传入 commit_c，不在模块级固死。
SIDECAR_NAME = f"{STEM}.events.json"
MODEL = review_model.model_for_host("pi")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _set_tags(**pairs):
    """简化 set_note_tags 请求构造。"""
    return dict(pairs)


class MissionLifecycleE2ETests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mission-e2e-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv_path = self.root / CSV_REL
        # HOME 指向临时目录 → ~/.pi 隔离（不再被 reviewer 使用，仅为遗留夹具）。
        self._home = patch.dict(os.environ, {"HOME": str(self.root / "home")})
        self._home.start()
        self.addCleanup(self._home.stop)
        # csv_state 的 _assert_write_context 以 cwd 校验仓库归属。
        self._chdir = chdir(self.root)
        self._chdir.__enter__()
        self.addCleanup(self._chdir.__exit__, None, None, None)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "E2E Fixture")
        self.git("config", "user.email", "e2e@example.invalid")
        self.git("commit", "--allow-empty", "-m", "c0 baseline")

    # ------------------------------------------------------------------ infra
    def git(self, *argv):
        return subprocess.run(
            ["git", *argv], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def head(self):
        return self.git("rev-parse", "HEAD")

    def commit(self, paths, message):
        self.git("add", *[str(p) for p in paths])
        self.git("commit", "-m", message)
        return self.head()

    def commit_contains(self, commit, rel):
        return subprocess.run(
            ["git", "cat-file", "-e", f"{commit}:{rel}"],
            cwd=self.root, capture_output=True,
        ).returncode == 0

    def row(self, row_id, **kw):
        base = dict.fromkeys(EXPECTED_FIELDS, "")
        base.update(
            id=row_id, dev_state="未开始", review_initial_state="未开始",
            review_regression_state="未开始", git_state="未提交", remote_state="",
            spec_id="spec-e2e",
        )
        base.update(kw)
        return base

    def write_csv(self, rows):
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", newline="", encoding="utf-8") as s:
            w = csv.DictWriter(s, fieldnames=EXPECTED_FIELDS)
            w.writeheader()
            w.writerows(rows)

    def read_row(self, row_id):
        _, rows, _ = read_mission_csv(self.csv_path)
        return next(r for r in rows if r["id"] == row_id)

    def mstate(self, task, action, **kw):
        return update(self.root, task, action=action, source_ref=REF, **kw)

    # ------------------------------------------------------------------ step-evidence
    def apply(self, row_id, request):
        """apply_update + 三类证据一致性（返回 dict / CSV 字节 / sidecar）。"""
        result = apply_update(self.csv_path, {
            "schema_version": SCHEMA, "row_id": row_id, **request,
        })
        csv_bytes = self.csv_path.read_bytes()
        # 1. 可信重写 hash 与盘上内容一致
        self.assertEqual(hashlib.sha256(csv_bytes).hexdigest(), result["csv_sha256"])
        # 2. 再读盘，目标行与返回 row 一致
        _, rows, _ = read_mission_csv(self.csv_path)
        target = next(r for r in rows if r["id"] == row_id)
        self.assertEqual(target, result["row"])
        # 3. sidecar ↔ notes 事件链一致（仅当请求带 event）
        if "event" in request:
            digest = result["event_sha256"]
            self.assertIn(f"event:{SIDECAR_NAME}#{digest}", target["notes"])
            sidecar = json.loads(
                (self.csv_path.parent / SIDECAR_NAME).read_text(encoding="utf-8"))
            hits = [e for e in sidecar if e["event_sha256"] == digest]
            self.assertTrue(hits, f"sidecar must carry {digest}")
            self.assertEqual(hits[-1]["row_id"], row_id)
            self.assertEqual(hits[-1]["event"], request["event"])
        return result

    def close_git(self, row_id, refs, commit, **extra_set):
        """set git_state=已提交 + refs + commit_hash；断言 commit 真实包含 refs。"""
        self.apply(row_id, {
            "set": {"git_state": "已提交", "commit_hash": commit,
                    "refs": "; ".join(refs), **extra_set},
            "event": {"phase": "git-close", "row": row_id},
        })
        for rel in refs:
            if rel.startswith(("command:", "manual:", "session:")):
                continue
            self.assertTrue(self.commit_contains(commit, rel),
                            f"{commit} must contain {rel}")
        self.assertEqual(self.read_row(row_id)["git_state"], "已提交")

    # ------------------------------------------------------------------ fixtures
    def _write_claim_ledger(self):
        _write(self.root / "docs/spec-e2e.md", "An approved research question.\n")
        _write(self.root / "run.log", "fixture execution output\n")
        _write(self.root / "claims.json", json.dumps({
            "csv": f"{STEM}.csv",
            "claims": [{
                "claim_id": "C1",
                "source_ref": "docs/spec-e2e.md:1",
                "promise": "exercise the production path",
                "covered_by": ["IMPL-01"],
                "evidence_required": "real_e2e",
                "production_path_required": True,
                "status": "verified",
                "evidence_refs": ["run.log"],
            }],
        }, ensure_ascii=False, indent=2) + "\n")

    def _write_prerun_verdict(self):
        _write(self.csv_path.parent / "reviews/verdict.json", json.dumps({
            "schema_version": "pre_run_implementation.verdict.v1",
            "review_mode": "targeted_review",
            "review_result": "targeted_correct",
            "gated_run": RUN_ID,
        }, ensure_ascii=False, indent=2) + "\n")

    def _write_closing_review(self):
        review = {
            "review_agent_mode": "evidence-close",
            "review_independence": False,
            "review_requested_model": "not_applicable",
            "review_observed_model": "not_applicable",
            "review_model_evidence": "not_applicable",
            "result": "vision_met",
            "scientific_outcome": "inconclusive",
            "claim_coverage": "0/0",
            "claim_coverage_status": "complete",
            "validation_limited": [],
            "summary": "E2E fixture scope closed.",
            "gaps": [],
            "assumptions": [],
            "decision_debt": [],
            "deferred_findings": [],
            "human_required_blockers": [],
            "outcome_answers": [],
            "handoff_markdown": _HANDOFF,
        }
        _write(self.csv_path.parent / "reviews/closing-review.json",
               json.dumps(review, ensure_ascii=False, indent=2) + "\n")
        _write(self.csv_path.parent / "closing.handoff.md", _HANDOFF)

    def _write_result_analysis(self, run_ids, digest):
        """reviews/result-analysis.json + reviewer_job verdict 夹具。"""
        analysis_md = self.root / (
            f"research_workspace/experiments/{EXP_ID}/analysis/analysis.md")
        analysis_text = ("## Change\ncode\n\n## Result\nmetric=0.5\n\n"
                         "## Finding\nuncertain\n\n## Next\nrepeat\n")
        _write(analysis_md, analysis_text)
        analysis_sha = hashlib.sha256(analysis_md.read_bytes()).hexdigest()
        review_output = json.dumps({
            "exp_id": EXP_ID,
            "run_ids": run_ids,
            "analysis_markdown": analysis_text,
            "scientific_outcome": "inconclusive",
            "limitations": [],
            "validation_gaps": [],
        }, ensure_ascii=False, sort_keys=True)
        job_rel = f"reviews/result-analysis-{EXP_ID}"
        job_dir = self.csv_path.parent / job_rel
        job_dir.mkdir(parents=True, exist_ok=True)
        packet_path = job_dir / "packet.json"
        packet_text = json.dumps({
            "exp_id": EXP_ID, "run_ids": list(run_ids), "repo_root": str(self.root),
        }, ensure_ascii=False, indent=2)
        packet_path.write_text(packet_text, encoding="utf-8")
        task_path = job_dir / "task.md"
        task_text = f"Analyze {EXP_ID} {sorted(run_ids)}\n"
        task_path.write_text(task_text, encoding="utf-8")
        raw_path = job_dir / "raw-response.json"
        raw_text = json.dumps({"raw": review_output}, ensure_ascii=False, indent=2)
        raw_path.write_text(raw_text, encoding="utf-8")
        packet_sha = hashlib.sha256(packet_path.read_bytes()).hexdigest()
        task_sha = hashlib.sha256(task_path.read_bytes()).hexdigest()
        raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        review_sha = hashlib.sha256(review_output.strip().encode()).hexdigest()
        evidence_ref = f"job:{job_rel}/verdict.json#verdict"
        verdict = {
            "schema_version": "post-run.result-analysis-verdict.v1",
            "status": "completed",
            "review_mode": "implementation_review",
            "review_kind": "result-analysis",
            "job_id": job_rel,
            "backend": "pi",
            "reviewer_session_id": f"e2e00000-0000-4000-8000-000000000001",
            "requested_model": f"{MODEL}:max",
            "observed_model": f"{MODEL}:max",
            "model_evidence": "job-verdict",
            "model_source": "contract",
            "packet_path": str(packet_path),
            "packet_sha256": packet_sha,
            "task_path": str(task_path),
            "task_sha256": task_sha,
            "raw_response_path": str(raw_path),
            "raw_response_sha256": raw_sha,
            "response_path": str(raw_path),
            "response_sha256": raw_sha,
            "replacement_count": 0,
            "resume_count": 0,
            "transport_exit_code": 0,
            "completed_at": "2026-01-01T00:00:00Z",
            "exp_id": EXP_ID,
            "run_ids": list(run_ids),
            "review_output": review_output,
            "review_output_sha256": review_sha,
        }
        _write(job_dir / "verdict.json",
               json.dumps(verdict, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        output_sha = review_sha
        index = {
            "schema_version": "post-run.result-analysis.v1",
            "status": "complete",
            "analysis_agent_mode": "result-analysis-reviewer-job",
            "analysis_independence": True,
            "requested_model": f"{MODEL}:max",
            "observed_model": f"{MODEL}:max",
            "model_evidence": "job-verdict",
            "model_evidence_ref": evidence_ref,
            "entries": [{
                "exp_id": EXP_ID, "run_id": r,
                "analysis_path": (f"research_workspace/experiments/{EXP_ID}"
                                  "/analysis/analysis.md"),
                "analysis_sha256": analysis_sha,
                "scientific_outcome": "inconclusive",
                "review_evidence_ref": evidence_ref,
                "review_output_sha256": output_sha,
                "evidence_refs": [f"{RUN_ROOT_REL}/summary.json"],
                "limitations": [], "validation_gaps": [],
            } for r in run_ids],
        }
        _write(self.csv_path.parent / "reviews/result-analysis.json",
               json.dumps(index, ensure_ascii=False, indent=2) + "\n")

    def _write_provenance(self, spec_id, commit_c):
        """runspec/summary/manifest + digest；全部引用真实存在的 commit_c。

        RunSpec.source.commit 与 remote_artifacts 不入 Git（避免 catalog 冲突），
        内部数据 citation 只要求 commit_c 在仓中可解析。
        """
        from harness.remote.build_rrctl_runspec import run_spec_digest
        sys.path.insert(0, str(ROOT / ".agents/harness/remote/rrctl/src"))
        from remote_run_control.artifacts import build_artifact_manifest
        from remote_run_control.models import ArtifactSpec

        run_root = self.root / RUN_ROOT_REL
        summary_path = run_root / "summary.json"
        _write(summary_path, json.dumps({
            "metric": 0.5, "run_id": RUN_ID, "spec_id": spec_id, "commit": commit_c,
            "protocol": "fixture-protocol",
        }, ensure_ascii=False, sort_keys=True))
        spec = {
            "schema_version": "rrctl.runspec.v1",
            "run_id": RUN_ID,
            "project": "e2e-fixture",
            "source": {
                "repo_root": "/nonexistent/e2e", "branch": "main",
                "commit": commit_c, "bundle_sha256": "b" * 64,
            },
            "remote": {
                "profile": "e2e-fixture", "stage_root": "/nonexistent/stage",
                "repo_root": "/nonexistent/repo", "control_root": "/nonexistent/control",
                "output_root": "/nonexistent/output", "python": "/nonexistent/python3",
            },
            "environment": {"kind": "conda", "name": "e2e-fixture"},
            "workload": {"argv": ["python3", "train.py"], "cwd": "."},
            "health": {
                phase: {"timeout_seconds": 60, "poll_interval_seconds": 1}
                for phase in ("first_step", "periodic", "completion")
            },
            "artifacts": [{"path": "summary.json", "required": True}],
            "local_pull_root": str(run_root),
            "metadata": {
                "exp_id": EXP_ID, "spec_id": spec_id, "mission_csv": CSV_REL,
            },
        }
        _write(self.csv_path.parent / "runs" / RUN_ID / "runspec.json",
               json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
        manifest = build_artifact_manifest(
            run_id=RUN_ID, output_root=run_root,
            declared=(ArtifactSpec("summary.json"),),
            destination=run_root / "artifact_manifest.json",
        )
        manifest["provenance"] = {
            "spec_id": spec_id, "exp_id": EXP_ID, "commit": commit_c,
            "run_spec_sha256": run_spec_digest(spec),
        }
        _write(run_root / "artifact_manifest.json",
               json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return run_spec_digest(spec)

    def _write_research_workspace(self, digest, pending, commit):
        """record.json + EXPERIMENTS.csv；所有 identity 均与当前仓中真实证明对齐。

        数值字段与 `_read_run_projection` 的真实 `build_record` 字典保持一致：
        optional_summary_value 遇字段缺失返回 None（非 NaN）。
        """
        from harness.records.experiment_records import record_index_row
        record = {
            "exp_id": EXP_ID, "parent": None, "relation": None,
            "source": {
                "spec_id": ["spec-e2e"], "branch": ["main"],
                "commit": [commit], "mission_csv": [CSV_REL], "csv": [CSV_REL],
            },
            "metrics": {
                "protocol": "fixture-protocol", "baseline_id": None, "baseline_run_id": None,
                "baseline_metric": None, "ours_metric": 0.5, "delta_metric": None,
            },
            "runs": [{
                "run_id": RUN_ID,
                "summary_path": f"{RUN_ROOT_REL}/summary.json",
                "eval_dir": RUN_ROOT_REL,
                "dimensions": {}, "protocol": "fixture-protocol", "metric": 0.5,
                "steps": None, "metric_aux": None, "weights_path": None,
                "commit": commit, "run_spec_sha256": digest,
            }],
            "outcome": "pending",
            "artifact_path": ARTIFACT_DIR_REL,
            "next_action": None,
            "_pending": pending,
            "_generated_by": ".agents/harness/records/experiment_records.py",
            "_projection_version": 2,
        }
        _write(self.root / f"research_workspace/experiments/{EXP_ID}/record.json",
               json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        buf = io.StringIO(newline="")
        row_dict = record_index_row(record)
        writer = csv.DictWriter(buf, fieldnames=list(row_dict), lineterminator="\n")
        writer.writeheader()
        writer.writerow({k: ("" if v is None else str(v)) for k, v in row_dict.items()})
        _write(self.root / "research_workspace/EXPERIMENTS.csv", buf.getvalue())

    def _close_development_states(self, row_ids):
        for row_id in row_ids:
            self.apply(row_id, {
                "set": {"dev_state": "已完成", "review_initial_state": "已完成",
                        "review_regression_state": "已完成"},
                "event": {"phase": "dev-close", "row": row_id},
            })

    # ==================================================================
    # 场景 1：正常闭环
    # ==================================================================
    def test_normal_lifecycle_closes_with_consistent_csv_sidecar_and_git_evidence(self):
        rows = [
            self.row("CONTRACT-01", phase="contract", remote_state="not_applicable",
                     notes="artifact_policy:none"),
            self.row("IMPL-01", phase="implementation", remote_state="not_applicable",
                     notes="artifact_policy:none"),
            self.row("VALID-01", phase="validation", remote_state="not_applicable",
                     notes="artifact_policy:none"),
            self.row("PRERUN-REVIEW-01", phase="review", remote_state="not_applicable",
                     notes=(
                         "review_kind:pre_run_implementation; "
                         "review_mode:targeted_review; "
                         f"gated_run:{RUN_ID}"
                     )),
            self.row("RUN-01", phase="remote", remote_state="",
                     exp_id=EXP_ID, run_id=RUN_ID, artifact_path=RUN_ROOT_REL),
            self.row("RESULT-ANALYSIS-01", phase="analysis",
                     required_skills="post-run-result-analysis",
                     remote_state="not_applicable", exp_id=EXP_ID),
            self.row("REVIEW-01", phase="review", remote_state="not_applicable"),
        ]
        self.write_csv(rows)
        self.mstate(TASK, "register", csv=CSV_REL)

        # ---- commit_b：开发期工件 + PRERUN verdict + CSV 初始形态，一次 commit
        self._write_claim_ledger()
        self._write_prerun_verdict()
        self._write_closing_review()
        commit_b = self.commit([
            "docs/spec-e2e.md", "run.log", "claims.json", CSV_REL,
            f"{REVIEWS_REL}/verdict.json", f"{REVIEWS_REL}/closing-review.json",
            f"issues/{STEM}/closing.handoff.md",
        ], "c1 development fixtures")

        # (a) CONTRACT/IMPL/VALID —— 开发期行 set 完成 + git 收口（全部挂在 commit_b）
        # IMPL 不 close，待 claim 标签收到 commit_c 时一并提交 CSV。
        self._close_development_states(["CONTRACT-01", "IMPL-01", "VALID-01"])
        self.close_git("CONTRACT-01", refs=[CSV_REL], commit=commit_b)
        self.close_git("VALID-01", refs=[CSV_REL], commit=commit_b)
        self.apply("IMPL-01", {
            "set_note_tags": _set_tags(
                claims="C1", claim_ledger="claims.json",
                evidence_level="real_e2e", production_path="covered",
            ),
        })

        # (b) PRERUN —— targeted_review 需要时间轴最快完成单 run；初始 CSV 已含
        # review_kind/review_mode/gated_run，这里仅补结果 + verdict 证据，舿1 set 到完成。
        self._close_development_states(["PRERUN-REVIEW-01"])
        self.apply("PRERUN-REVIEW-01", {
            "set_note_tags": _set_tags(
                review_result="targeted_correct",
                verdict_artifact=f"{REVIEWS_REL}/verdict.json",
            ),
        })
        self.close_git("PRERUN-REVIEW-01",
                       refs=[f"{REVIEWS_REL}/verdict.json"], commit=commit_b)

        # (c) RUN —— 远程完成（推进 dev 完成后 remote→completed）
        self._close_development_states(["RUN-01"])
        self.apply("RUN-01", {
            "set": {"remote_state": "running_remote"},
            "event": {"phase": "remote-launch"},
        })
        self.apply("RUN-01", {
            "set": {"remote_state": "completed"},
            "event": {"phase": "remote-finish"},
        })

        # ---- commit_c：MSK 合规要求 RunSpec.source.commit / record('commit') 指
        # 真实 commit。先提交「IMPL 带 claim 标签的 CSV」以在现实中创建 commit_c。
        commit_c = self.commit([CSV_REL], "c2 claim-tagged CSV (anchor for identity)")

        # ---- 生成 provenance/run/record（引用 commit_c），再一次性提交
        # summary 需含 protocol 使 run(protocol)='fixture-protocol'，
        # 进而在 build_record._pending 中剔除 metrics.protocol（与服务端的 PENDING 语义对应）。
        digest = self._write_provenance("spec-e2e", commit_c)
        pending = ["parent", "metrics.baseline_id", "outcome"]
        self._write_research_workspace(digest, pending, commit_c)
        self._write_result_analysis([RUN_ID], digest)
        commit_d = self.commit([
            f"research_workspace/experiments/{EXP_ID}/record.json",
            "research_workspace/EXPERIMENTS.csv",
            f"issues/{STEM}/runs",
            f"{REVIEWS_REL}/result-analysis.json",
        ], "c3 provenance + research workspace")

        # IMPL git 收口（commit_c 确实包含了 claim 标签后的 CSV）
        self.close_git("IMPL-01", refs=[CSV_REL], commit=commit_c)

        # RUN git 收口：RUN row 的 refs 需指向 [commit_c 中存在的文件]
        self.close_git("RUN-01", refs=[CSV_REL], commit=commit_c)

        # (d) RUN → ingested：runspec/manifest/record/EXPERIMENTS.csv 全部就位
        self.apply("RUN-01", {
            "set": {"remote_state": "ingested"},
            "event": {"phase": "ingest"},
        })
        self.assertEqual(self.read_row("RUN-01")["remote_state"], "ingested")

        # (e) RESULT-ANALYSIS —— 7 metadata tags + git 收口（commit_d 含 result-analysis.json）
        self._close_development_states(["RESULT-ANALYSIS-01"])
        self.apply("RESULT-ANALYSIS-01", {
            "set_note_tags": _set_tags(
                analysis_kind="post_run",
                result_analysis=f"{REVIEWS_REL}/result-analysis.json",
                analysis_agent_mode="result-analysis-reviewer-job",
                analysis_independence="true",
                analysis_requested_model=f"{MODEL}:max",
                analysis_observed_model=f"{MODEL}:max",
                analysis_model_evidence="job-verdict",
                analysis_model_evidence_ref=f"job:reviews/result-analysis-{EXP_ID}/verdict.json#verdict"),
            "event": {"phase": "analysis", "row": "RESULT-ANALYSIS-01"},
        })

        self.close_git("RESULT-ANALYSIS-01",
                       refs=[f"{REVIEWS_REL}/result-analysis.json"],
                       commit=commit_d)

        # (f) REVIEW —— 8 review tags + git 收口（commit_d 含 review/handoff + analysis）
        self._close_development_states(["REVIEW-01"])
        self.apply("REVIEW-01", {
            "set_note_tags": _set_tags(
                review_kind="vision",
                result_analysis=f"{REVIEWS_REL}/result-analysis.json",
                review_agent_mode="evidence-close",
                review_independence="false",
                review_result="vision_met",
                scientific_outcome="inconclusive",
                claim_coverage_status="complete",
                review_json=f"{REVIEWS_REL}/closing-review.json",
                handoff=f"issues/{STEM}/closing.handoff.md",
                handoff_contract="passed",
            ),
        })
        self.close_git("REVIEW-01",
                       refs=[f"{REVIEWS_REL}/closing-review.json",
                              f"issues/{STEM}/closing.handoff.md"],
                       commit=commit_d)

        # ---- closing：csv_completion_errors 独立断言为空，再走 mission completed
        errors = csv_completion_errors(self.csv_path, workdir=self.root)
        self.assertEqual(errors, [], f"must be fully delivered: {errors}")
        self.mstate(TASK, "transition", status="completed", reason="闭环")
        registry = load_registry(self.root)
        self.assertEqual(registry["tasks"][TASK]["status"], "completed")
        self.assertIsNone(registry["current_task"])

        # 收尾：每行均达到四闭态；sidecar 含每个带 event 的 apply。
        for row_id in ("CONTRACT-01", "IMPL-01", "VALID-01", "PRERUN-REVIEW-01",
                       "RUN-01", "RESULT-ANALYSIS-01", "REVIEW-01"):
            row = self.read_row(row_id)
            for field in ("dev_state", "review_initial_state",
                          "review_regression_state"):
                self.assertEqual(row[field], "已完成", f"{row_id}.{field}")
            self.assertEqual(row["git_state"], "已提交", row_id)
        sidecar = json.loads(
            (self.csv_path.parent / SIDECAR_NAME).read_text(encoding="utf-8"))
        # 至少 10 个带 event 的 apply：3 dev-close×3、1 IMPL close、1 claim tags、
        # RUN dev+remote+ingest、PRERUN close、ANALYSIS dev+close、REVIEW dev+close。
        self.assertGreaterEqual(len(sidecar), 10)


    # ==================================================================
    # 场景 2：暂停恢复
    # ==================================================================
    def test_pause_resume_preserves_csv_progress_and_current_task(self):
        self.write_csv([self.row("CONTRACT-01", remote_state="not_applicable",
                                 notes="artifact_policy:none")])
        self.commit([CSV_REL], "c1 csv initial")
        self.mstate(TASK, "register", csv=CSV_REL)
        self.apply("CONTRACT-01", {"set": {"dev_state": "进行中"}})

        before = self.csv_path.read_bytes()
        self.mstate(TASK, "transition", status="paused", reason="暂停")
        self.assertEqual(self.csv_path.read_bytes(), before, "暂停禁止改 CSV")

        self.mstate(TASK, "transition", status="active")
        registry = load_registry(self.root)
        self.assertEqual(registry["current_task"], TASK)
        self.assertEqual(registry["tasks"][TASK]["status"], "active")
        row = self.read_row("CONTRACT-01")
        self.assertEqual(row["dev_state"], "进行中", "恢复后已推进状态保留")
        self.assertEqual(row["git_state"], "未提交", "git_state 不受暂停影响")

    # ==================================================================
    # 场景 3：取消后重跑 + superseded 新 CSV
    # ==================================================================
    def test_cancelled_csv_not_reusable_superseded_accepts_new_csv(self):
        # A: cancelled 任务的 CSV 不能再注册到新 task
        self.write_csv([self.row("CONTRACT-A1", remote_state="not_applicable",
                                 notes="artifact_policy:none")])
        self.commit([CSV_REL], "c1 csv A")
        self.mstate("task-a", "register", csv=CSV_REL)
        self.mstate("task-a", "transition", status="cancelled", reason="取消")
        with self.assertRaisesRegex(ValueError, "同一 CSV 只能绑定一个任务"):
            self.mstate("task-b", "register", csv=CSV_REL)

        # B: task-c 被标 superseded→replacement 是 spec 登记的 task-d；task-d bind 新 CSV。
        stem_b = "mission-e2e-b"
        csv_b_rel = f"issues/{stem_b}/{stem_b}.csv"
        csv_b = self.root / csv_b_rel
        csv_b.parent.mkdir(parents=True, exist_ok=True)
        with csv_b.open("w", newline="", encoding="utf-8") as s:
            w = csv.DictWriter(s, fieldnames=EXPECTED_FIELDS)
            w.writeheader()
            w.writerow(self.row("CONTRACT-B1", remote_state="not_applicable",
                                notes="artifact_policy:none"))
        self.commit([csv_b_rel], "c2 csv B")
        self.mstate("task-c", "register", csv=csv_b_rel)
        spec_d = self.root / "docs/specs/mission-e2e-d.md"
        _write(spec_d, "# task-d spec\n")
        self.mstate("task-d", "register", spec="docs/specs/mission-e2e-d.md")
        self.mstate("task-c", "transition", status="superseded",
                    reason="被替换", replacement="task-d")
        stem_d = "mission-e2e-d"
        csv_d_rel = f"issues/{stem_d}/{stem_d}.csv"
        csv_d = self.root / csv_d_rel
        csv_d.parent.mkdir(parents=True, exist_ok=True)
        with csv_d.open("w", newline="", encoding="utf-8") as s:
            w = csv.DictWriter(s, fieldnames=EXPECTED_FIELDS)
            w.writeheader()
            w.writerow(self.row("CONTRACT-D1", remote_state="not_applicable",
                                notes="artifact_policy:none"))
        self.commit([csv_d_rel], "c3 csv D")
        self.mstate("task-d", "bind", csv=csv_d_rel)
        registry = load_registry(self.root)
        self.assertEqual(registry["tasks"]["task-c"]["status"], "superseded")
        self.assertEqual(registry["tasks"]["task-c"]["replacement"], "task-d")
        self.assertEqual(registry["tasks"]["task-d"]["status"], "active")
        self.assertEqual(registry["tasks"]["task-d"]["csv"], csv_d_rel)
        self.assertEqual(registry["current_task"], "task-d")

    # ==================================================================
    # 场景 4：F-012
    # ==================================================================
    def test_committed_row_rejects_running_remote_transition(self):
        _write(self.root / "impl.py", "value = 1\n")
        self.write_csv([self.row("IMPL-01", remote_state="", refs="impl.py")])
        commit_b = self.commit(["impl.py", CSV_REL], "c1 impl")
        # close dev 先（row_transition 拒绝 dev=未开始下 close git）
        self._close_development_states(["IMPL-01"])
        self.close_git("IMPL-01", refs=["impl.py"], commit=commit_b)

        with self.assertRaisesRegex(StateUpdateError,
                                    "git_committed_with_remote_running"):
            apply_update(self.csv_path, {
                "schema_version": SCHEMA, "row_id": "IMPL-01",
                "set": {"remote_state": "running_remote"},
            })
        self.assertEqual(self.read_row("IMPL-01")["remote_state"], "",
                         "拒绝应原子（CSV 未被改写）")


_HANDOFF = "\n".join([
    "# E2E closing 施工交工单",
    "独立性: 无 outcome contract，本交工单按通用模板生成",
    "",
    "## 先看结论",
    "Mission E2E 闭环 fixture 已完成。",
    "",
    "## 对账",
    "| spec 目标 | 交付状态 | 证据 | 边界 |",
    "| --------- | -------- | ---- | ---- |",
    "| 7 行 CSV 四闭态 | pass | csv_completion_errors 为空 | fixture |",
    "| RUN 远端 ingest | pass | record/EXPERIMENTS.csv 引用一致 | fixture |",
    "",
    "## 施工细节",
    "参考 mission_state + csv_state 集成路径。",
    "",
    "## 验证情况",
    "本地 csv_completion_errors 为空。",
    "",
    "## 后续",
    "无需后续动作。",
    "",
])


if __name__ == "__main__":
    unittest.main()
