#!/usr/bin/env python3
"""认领已有 rrctl 运行（remote_binding_reconciliation）的回归测试。

覆盖：证据齐全时放行；缺少/伪造/不符证据时一律拒绝。这组校验是"记账中断后不重复启动"
的唯一依据，因此负向用例比正向更重要：
- 无证据不得把 not_applicable 改成 running_remote（否则等于绕过启动前置）；
- 活运行但进程观测为 false → 拒绝；
- evidence 里身份与申请不符 → 拒绝；
- evidence 路径逃逸仓库或不存在 → 拒绝；
- 身份一致但行自身 run_id/commit 不一致 → 拒绝。
"""
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))
sys.path.insert(0, str(ROOT / ".agents/harness/workflow/tests"))

from csv_state import SCHEMA, StateUpdateError, apply_update  # noqa: E402
from test_mission_contracts import EXPECTED_FIELDS  # noqa: E402

RUN_ID = "RUN-RECON-1"
DIGEST = "c" * 64


class RemoteBindingReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="binding-recon-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "test")
        (self.repo / "f.txt").write_text("x", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.commit = self.git("rev-parse", "HEAD")
        self.csv_path = self.repo / "mission.csv"
        # csv_state 会校验「cwd 与 CSV 同属一个仓库」，测试需在仓库内写入。
        self.enterContext(chdir(self.repo))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def write_csv(self, run_id=RUN_ID, commit=None, remote_state="not_applicable"):
        row = dict.fromkeys(EXPECTED_FIELDS, "")
        row.update(
            id="I-1", dev_state="未开始", review_initial_state="未开始",
            review_regression_state="未开始", git_state="未提交",
            remote_state=remote_state, run_id=run_id,
            commit_hash=commit if commit is not None else self.commit,
        )
        with self.csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPECTED_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerow(row)

    def write_inspect(self, state="running", observations=None, run_id=RUN_ID,
                      commit=None, digest=DIGEST, name="inspect.json"):
        payload = {
            "status": "succeeded",
            "result": {
                "status": {"state": state},
                "binding": {
                    "run_id": run_id,
                    "commit": commit if commit is not None else self.commit,
                    "run_spec_sha256": digest,
                    "backend": "process",
                },
                "monitor": {
                    "observations": observations if observations is not None else {
                        "executor_alive": True, "process_alive": True, "workload_alive": True,
                    }
                },
            },
        }
        path = self.repo / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def request(self, evidence="inspect.json", digest=DIGEST, run_id=RUN_ID):
        binding = {
            "run_id": run_id, "commit": self.commit, "run_spec_sha256": digest,
            "remote_state": "running", "evidence_path": evidence, "reason": "verified",
        }
        return {
            "schema_version": SCHEMA, "row_id": "I-1",
            "set": {"remote_state": "running_remote"},
            "event": dict(binding, kind="remote_binding_reconciliation"),
            "remote_binding_reconciliation": binding,
        }

    def test_evidence_backed_claim_is_accepted(self):
        self.write_csv()
        self.write_inspect()
        result = apply_update(self.csv_path, self.request())
        self.assertEqual(result["row"]["remote_state"], "running_remote")

    def test_claim_without_reconciliation_block_is_rejected(self):
        """没有证据块就改状态 = 绕过启动前置条件。"""
        self.write_csv()
        self.write_inspect()
        with self.assertRaisesRegex(StateUpdateError, "remote_regression"):
            apply_update(self.csv_path, {
                "schema_version": SCHEMA, "row_id": "I-1",
                "set": {"remote_state": "running_remote"},
            })

    def test_live_state_requires_live_process_observations(self):
        for key in ("executor_alive", "process_alive", "workload_alive"):
            with self.subTest(key=key):
                self.write_csv()
                self.write_inspect(observations={
                    "executor_alive": True, "process_alive": True, "workload_alive": True,
                    **{key: False},
                })
                with self.assertRaisesRegex(StateUpdateError, "live process observations"):
                    apply_update(self.csv_path, self.request())

    def test_inspect_identity_mismatch_is_rejected(self):
        self.write_csv()
        self.write_inspect(run_id="OTHER-RUN")
        with self.assertRaisesRegex(StateUpdateError, "identity mismatch"):
            apply_update(self.csv_path, self.request())

    def test_runspec_digest_mismatch_is_rejected(self):
        self.write_csv()
        self.write_inspect(digest="d" * 64)
        with self.assertRaisesRegex(StateUpdateError, "identity mismatch"):
            apply_update(self.csv_path, self.request())

    def test_evidence_path_escape_is_rejected(self):
        self.write_csv()
        self.write_inspect()
        with self.assertRaisesRegex(StateUpdateError, "escapes repository|missing"):
            apply_update(self.csv_path, self.request(evidence="../../etc/passwd"))

    def test_missing_evidence_is_rejected(self):
        self.write_csv()
        with self.assertRaisesRegex(StateUpdateError, "missing or escapes"):
            apply_update(self.csv_path, self.request(evidence="nope.json"))

    def test_missing_digest_or_secret_fields_rejected(self):
        self.write_csv()
        self.write_inspect()
        request = self.request()
        request["remote_binding_reconciliation"].pop("reason")
        with self.assertRaisesRegex(StateUpdateError, "remote_binding_invalid"):
            apply_update(self.csv_path, request)

    def test_row_identity_must_match_request(self):
        self.write_csv(run_id="SOMETHING-ELSE")
        self.write_inspect()
        with self.assertRaisesRegex(StateUpdateError, "RunID mismatch"):
            apply_update(self.csv_path, self.request())

    def test_non_process_backend_is_rejected(self):
        self.write_csv()
        path = self.write_inspect()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["result"]["binding"]["backend"] = "ssh"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(StateUpdateError, "backend is not process"):
            apply_update(self.csv_path, self.request())

    def test_unreadable_evidence_is_rejected(self):
        self.write_csv()
        (self.repo / "inspect.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(StateUpdateError, "unreadable"):
            apply_update(self.csv_path, self.request())


if __name__ == "__main__":
    unittest.main()
