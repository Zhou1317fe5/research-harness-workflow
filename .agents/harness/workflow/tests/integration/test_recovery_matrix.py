"""中断恢复矩阵集成测试：kill -9 崩溃现场 → mission-recovery 识别 → csv_state 恢复续写。

对每个执行阶段（launch / wait / pull / reviewer_wait / ingest / closing）：
1. 用 tempfile.TemporaryDirectory 构造临时 issues/<stem>/<stem>.csv 与
   mission_state registry，并直接把状态文件改到该阶段被杀后的「崩溃现场」
   （不真杀进程，状态文件即现场）；
2. 真实调用 .agents/skills/mission-recovery/scripts/scan_recovery.py 子进程，
   断言它能定位唯一候选 CSV 与恢复点（resume_target 指向当前任务）；
3. 通过 csv_state.apply_update 执行 resume 后的续写，断言恢复方向被允许、
   回退方向（重复 launch / remote 回退 / 无证据 ingest）被拒绝。
"""

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[5]
# 与其它测试一致使用 .codex/skills：.agents/skills 是它的 symlink，物理同源，
# 但 .codex 路径在 pytest/unittest discover 场景里不会与 .agents/harness 包路径前缀冲突。
_AGENTS = str(ROOT / ".agents")
_CODEX_SCRIPTS = str(ROOT / ".codex/skills/mission-csv-execute/scripts")
for _path in (_AGENTS, _CODEX_SCRIPTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from csv_state import SCHEMA, StateUpdateError, apply_update  # noqa: E402
from harness.workflow import mission_state  # noqa: E402
from mission_completion import EXPECTED_FIELDS, parse_note_tags, read_mission_csv  # noqa: E402

SCAN_SCRIPT = (
    ROOT / ".agents/skills/mission-recovery/scripts/scan_recovery.py"
)
TASK_ID = "recovery-matrix-task"
STEM = "recovery-matrix"
SOURCE_REF = "session:test#recovery-matrix"
EXP_ID = "EXP-REC-01"
RUN_ID = "run-rec-001"
ARTIFACT_REF = f"remote_artifacts/{EXP_ID}/{RUN_ID}"
WAIT_RUN_ID = "run-rec-pending"


class RecoveryMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mission-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv_path = self.root / "issues" / STEM / f"{STEM}.csv"
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        # git_isolation 的 rev-parse 回退需要 cwd 在仓库内；init + identity 确保
        # 显式 workdir、csv 目录与解析回退三者一致。
        subprocess.run(["git", "init", "-q", "."], cwd=self.root, check=True)

    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------
    @property
    def rel_csv(self):
        return self.csv_path.relative_to(self.root).as_posix()

    def _register(self):
        """登记 CSV 并把任务选为 current（active）。必须在 CSV 写盘之后调用。"""
        return mission_state.update(
            self.root,
            TASK_ID,
            action="register",
            source_ref=SOURCE_REF,
            csv=self.rel_csv,
        )

    def _write_rows(self, rows):
        with self.csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _base_row(self, row_id="IMPL-01", **overrides):
        row = dict.fromkeys(EXPECTED_FIELDS, "")
        row.update(
            id=row_id,
            phase="1",
            title=f"{row_id} title",
            dev_state="进行中",
            review_initial_state="未开始",
            review_regression_state="未开始",
            git_state="未提交",
            remote_state="",
        )
        row.update(overrides)
        return row

    def _review_row(self, **overrides):
        row = dict.fromkeys(EXPECTED_FIELDS, "")
        row.update(
            id="REVIEW-01",
            phase="9",
            title="closing review",
            dev_state="进行中",
            review_initial_state="未开始",
            review_regression_state="未开始",
            git_state="未提交",
            remote_state="not_applicable",
            notes=(
                "review_kind:vision; review_agent_mode:pending; "
                "review_independence:pending; review_result:pending; "
                "review_json:pending; claim_coverage_status:pending; "
                "scientific_outcome:pending; handoff:pending; "
                "handoff_contract:pending"
            ),
        )
        row.update(overrides)
        return row

    def _note_value(self, row, key):
        return parse_note_tags(row.get("notes", "")).get(key)

    # ------------------------------------------------------------------
    # 真实调用 mission-recovery 扫描器
    # ------------------------------------------------------------------
    def _scan(self):
        done = subprocess.run(
            [sys.executable, str(SCAN_SCRIPT), "--repo-root", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, msg=f"scan_recovery stderr: {done.stderr}")
        return json.loads(done.stdout)

    def _assert_recovery_pinpoints(self, result):
        """扫描器必须把当前任务 CSV 识别为唯一未完成候选并给出恢复点。"""
        self.assertEqual(result["current_task"], TASK_ID)
        self.assertEqual(result["candidate_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["path"], self.rel_csv)
        self.assertEqual(candidate["kind"], "directory")
        self.assertEqual(candidate["task_id"], TASK_ID)
        self.assertTrue(candidate["reasons"], msg="candidates must carry incomplete reasons")
        self.assertEqual(
            result["resume_target"],
            {"task_id": TASK_ID, "kind": "csv", "path": self.rel_csv},
        )
        self.assertEqual(result["complete"], [])
        return candidate

    def _assert_registry(self, status):
        registry = mission_state.load_registry(self.root)
        self.assertEqual(registry["current_task"], TASK_ID)
        self.assertEqual(registry["tasks"][TASK_ID]["status"], status)

    # ------------------------------------------------------------------
    # csv_state 恢复续写
    # ------------------------------------------------------------------
    def _apply(self, row_id, set_fields=None, note_tags=None):
        request = {"schema_version": SCHEMA, "row_id": row_id, "set": set_fields or {}}
        if note_tags:
            request["set_note_tags"] = note_tags
        # _assert_write_context 校验 cwd 与 CSV 仓库一致；chdir 到临时仓库满足该约束。
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            return apply_update(self.csv_path, request)
        finally:
            os.chdir(previous)

    def _apply_error(self, row_id, set_fields=None, note_tags=None):
        with self.assertRaises(StateUpdateError) as context:
            self._apply(row_id, set_fields, note_tags)
        return str(context.exception)

    def _read_row(self, row_id):
        _, rows, _ = read_mission_csv(self.csv_path)
        matches = [row for row in rows if row["id"] == row_id]
        self.assertEqual(len(matches), 1, msg=f"{row_id} must exist exactly once")
        return matches[0]

    def _assert_remote_unchanged(self, row_id, expected):
        self.assertEqual(self._read_row(row_id)["remote_state"], expected)

    # ------------------------------------------------------------------
    # 场景 1：launch 阶段 kill —— CSV 已登记、mission active、launch 前被杀
    # ------------------------------------------------------------------
    def test_launch_crash_rescans_as_unstarted_and_resume_relaunches_safely(self):
        # 崩溃现场：dev_state=进行中（执行已认领）但 remote_state 仍为空——
        # launch 未发生，schedule 指纹未登记。
        self._write_rows([self._base_row()])
        self._register()
        self._assert_registry("active")

        candidate = self._assert_recovery_pinpoints(self._scan())
        row = self._read_row("IMPL-01")
        # 未启动证据：remote_state 为空、无 run_id/artifact_path。
        self.assertEqual(row["remote_state"], "")
        self.assertFalse(row["run_id"].strip())
        self.assertFalse(row["artifact_path"].strip())
        self.assertTrue(
            any("review_row_missing" in reason for reason in candidate["reasons"]),
            msg="unstarted row must surface closing-side missing review row",
        )

        # resume：launch 决议通过 csv_state 登记为 running_remote（允许）。
        result = self._apply("IMPL-01", {"remote_state": "running_remote"})
        self.assertEqual(result["row"]["remote_state"], "running_remote")
        # 单调性拒绝回退——例如把已登记的 running 当"从未 launch"再来一次。
        error = self._apply_error("IMPL-01", {"remote_state": ""})
        self.assertIn("remote_regression", error)
        self._assert_remote_unchanged("IMPL-01", "running_remote")

    # ------------------------------------------------------------------
    # 场景 2：wait 阶段 kill —— RunID 已 launch，wait observer 被杀
    # ------------------------------------------------------------------
    def test_wait_kill_recovers_as_running_remote_and_resume_continues_without_relaunch(self):
        # 崩溃现场：launch 已登记 run_id + running_remote，wait 观察循环被杀。
        self._write_rows([
            self._base_row(remote_state="running_remote", exp_id=EXP_ID, run_id=RUN_ID),
        ])
        self._register()

        candidate = self._assert_recovery_pinpoints(self._scan())
        row = self._read_row("IMPL-01")
        self.assertEqual(row["remote_state"], "running_remote")
        self.assertEqual(row["run_id"], RUN_ID)
        self.assertTrue(
            any("running_remote" in reason for reason in candidate["reasons"]),
            msg="scanner must surface remote_state_not_terminal for running_remote",
        )

        # resume：沿用原 RunID 恢复观察，远端终态到达后 csv_state 允许推进
        # running_remote → completed，而不重新 launch（run_id 不变）。
        result = self._apply("IMPL-01", {"remote_state": "completed"})
        self.assertEqual(result["row"]["remote_state"], "completed")
        self.assertEqual(result["row"]["run_id"], RUN_ID)
        # 拒绝把 running_remote 回退为空（伪造"未 launch"）。
        error = self._apply_error("IMPL-01", {"remote_state": ""})
        self.assertIn("remote_regression", error)
        self._assert_remote_unchanged("IMPL-01", "completed")

    # ------------------------------------------------------------------
    # 场景 3：pull 阶段 kill —— remote completed 但 pull 中断，pull 幂等恢复
    # ------------------------------------------------------------------
    def test_pull_kill_recovers_as_completed_and_resume_pulls_idempotently(self):
        # 崩溃现场：远端 completed，artifacts 拉取中断——artifact_path 仍为空。
        self._write_rows([
            self._base_row(remote_state="completed", exp_id=EXP_ID, run_id=RUN_ID),
        ])
        self._register()

        candidate = self._assert_recovery_pinpoints(self._scan())
        row = self._read_row("IMPL-01")
        self.assertEqual(row["remote_state"], "completed")
        self.assertFalse(self._note_value(row, "artifact_evidence"))
        self.assertFalse(row["artifact_path"].strip())
        self.assertTrue(
            any("completed" in reason for reason in candidate["reasons"]),
            msg="scanner must keep completed (non-ingested) row as incomplete",
        )

        # resume：pull 幂等重复执行后登记 artifacts_pulled + 证据引用。
        result = self._apply(
            "IMPL-01",
            {"remote_state": "artifacts_pulled", "artifact_path": ARTIFACT_REF},
        )
        self.assertEqual(result["row"]["remote_state"], "artifacts_pulled")
        self.assertEqual(result["row"]["artifact_path"], ARTIFACT_REF)
        # 拒绝回退到 running_remote（pull/launch 事实不可撤销）。
        error = self._apply_error("IMPL-01", {"remote_state": "running_remote"})
        self.assertIn("remote_regression", error)
        self._assert_remote_unchanged("IMPL-01", "artifacts_pulled")

    # ------------------------------------------------------------------
    # 场景 4：reviewer_wait kill —— reviewer_job 等待 verdict 时被杀
    # ------------------------------------------------------------------
    def test_reviewer_wait_kill_recovers_pre_run_pending_and_resume_recovers_bounded(self):
        # 崩溃现场：PRERUN reviewer_job 已发起，verdict 未落盘（pre_run_result=expired），
        # remote 身份已登记但产物未拉取。
        self._write_rows([
            self._base_row(exp_id=EXP_ID, run_id=WAIT_RUN_ID,
                           artifact_path=f"remote_artifacts/{EXP_ID}/{WAIT_RUN_ID}",
                           notes="pre_run_result:expired"),
        ])
        self._register()

        candidate = self._assert_recovery_pinpoints(self._scan())
        row = self._read_row("IMPL-01")
        # pre_run_result=pending（expired 语义：reviewer verdict 未生效，需重发/续等）。
        self.assertEqual(self._note_value(row, "pre_run_result"), "expired")
        self.assertEqual(row["run_id"], WAIT_RUN_ID)
        self.assertTrue(candidate["reasons"])

        # resume：reviewer_job 有界恢复——沿用同一 RunID 重新发起有等待预算的
        # reviewer_job；恢复决议只改登记簿不重复 launch，故 remote/run 身份不变。
        result = self._apply(
            "IMPL-01",
            {"remote_state": "running_remote"},
            note_tags={"pre_run_result": "pending"},
        )
        self.assertEqual(result["row"]["run_id"], WAIT_RUN_ID)
        self.assertEqual(result["row"]["remote_state"], "running_remote")
        self.assertEqual(self._note_value(result["row"], "pre_run_result"), "pending")
        # 即使 reviewer 状态重发，wait 恢复也不允许重写 launch 事实为空。
        error = self._apply_error("IMPL-01", {"remote_state": ""})
        self.assertIn("remote_regression", error)
        self._assert_remote_unchanged("IMPL-01", "running_remote")

    # ------------------------------------------------------------------
    # 场景 5：ingest 阶段 kill —— artifacts_pulled 后 ingest 中断
    # ------------------------------------------------------------------
    def test_ingest_kill_recovers_as_artifacts_pulled_and_resume_reruns_ingest(self):
        # 崩溃现场：artifacts_pulled 已登记（含证据引用），ingest 登记/record 未落地。
        self._write_rows([
            self._base_row(remote_state="artifacts_pulled", exp_id=EXP_ID,
                           run_id=RUN_ID, artifact_path=ARTIFACT_REF),
        ])
        self._register()

        candidate = self._assert_recovery_pinpoints(self._scan())
        row = self._read_row("IMPL-01")
        self.assertEqual(row["remote_state"], "artifacts_pulled")
        self.assertEqual(row["artifact_path"], ARTIFACT_REF)
        self.assertTrue(
            any("artifacts_pulled" in reason for reason in candidate["reasons"]),
            msg="scanner must surface remote_state_not_terminal for artifacts_pulled",
        )

        # resume：直接登记 remote_state=ingested 必须先过 ingest 完整性核验；
        # 没有 record/RunSpec 证据时 ingest_completion_errors 拒绝（ingest_artifact_missing）。
        error = self._apply_error(
            "IMPL-01",
            {"remote_state": "ingested", "spec_id": "spec-rec",
             "commit_hash": "a" * 40},
        )
        self.assertIn("ingest_artifact_missing", error)
        self._assert_remote_unchanged("IMPL-01", "artifacts_pulled")

        # ingest 重跑期间允许继续同步非 remote 的进程事实（如 dev_state 收敛），
        # 说明 ingest_completion_errors 不会把崩溃现场锁死为不可写。
        result = self._apply("IMPL-01", {"dev_state": "已完成"})
        self.assertEqual(result["row"]["dev_state"], "已完成")
        self.assertEqual(result["row"]["remote_state"], "artifacts_pulled")

    # ------------------------------------------------------------------
    # 场景 6：closing 阶段 kill —— REVIEW-* 进行中时进程被杀
    # ------------------------------------------------------------------
    def test_closing_kill_recovers_unfinished_and_resume_reruns_final_ready(self):
        # 崩溃现场：普通行已收敛（dev/review 完成、remote artifacts_pulled、git 未提交），
        # REVIEW-01 closing 行仍在进行（verdict 全 pending）时被杀。
        self._write_rows([
            self._base_row(dev_state="已完成", review_initial_state="已完成",
                           review_regression_state="已完成", git_state="未提交",
                           remote_state="artifacts_pulled", exp_id=EXP_ID,
                           run_id=RUN_ID, artifact_path=ARTIFACT_REF),
            self._review_row(),
        ])
        self._register()

        candidate = self._assert_recovery_pinpoints(self._scan())
        reasons = candidate["reasons"]
        review = self._read_row("REVIEW-01")
        self.assertEqual(review["dev_state"], "进行中")
        self.assertEqual(self._note_value(review, "review_agent_mode"), "pending")
        self.assertTrue(
            any("REVIEW-01" in reason and "row_not_closed" in reason for reason in reasons),
            msg=f"scanner must flag unfinished REVIEW-01 row: {reasons}",
        )
        self.assertTrue(
            any("REVIEW-01" in reason and "git_state" in reason for reason in reasons),
            msg=f"scanner must flag closing git evidence gap: {reasons}",
        )

        # 拒绝在 verdict 缺口下提前关闭 REVIEW 行（review_tag_pending 必须由
        # final_ready/run_vision_review 补齐后才允许收敛）。
        self.assertEqual(self._note_value(review, "review_result"), "pending")

        # resume：final_ready 机械检查后允许继续——把 closing 行的实施部分推进到
        # 已完成（走 evidence_close 后由后续流程补 review tag 与 git 提交）。
        result = self._apply("REVIEW-01", {"dev_state": "已完成"})
        self.assertEqual(result["row"]["dev_state"], "已完成")
        self.assertEqual(result["row"]["id"], "REVIEW-01")
        # 重新扫描仍把 REVIEW-01 当作唯一恢复点（review tag/git 尚未闭环）。
        scanned = self._scan()
        self.assertEqual(scanned["candidate_count"], 1)
        self.assertEqual(scanned["candidates"][0]["path"], self.rel_csv)


if __name__ == "__main__":
    unittest.main()
