"""mission-csv-execute 脚本盲区回归：阶段推进、远程路由、产物压缩与行的幂等。

只覆盖字段校验、确定性决策与文件级幂等；不调用远端、模型或子进程。
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".codex/skills/mission-csv-execute/scripts"))
sys.path.insert(0, str(ROOT / ".agents"))

from mission_completion import EXPECTED_FIELDS, read_mission_csv  # noqa: E402
import stage_flow  # noqa: E402
import remote_route  # noqa: E402
import compact_artifacts  # noqa: E402
from ensure_review_row import ensure_review_row  # noqa: E402
from ensure_result_analysis_row import ensure_result_analysis_row  # noqa: E402
from run_vision_review import validate_review_result  # noqa: E402
from validate_deferred_ledger import load_csv_deferred  # noqa: E402

REVIEWED_COMMIT = "a" * 40
CANDIDATE_COMMIT = "b" * 40


# --- stage_flow 夹具 --------------------------------------------------------


def _runspec_template(run_id):
    return {
        "run_id": run_id,
        "metadata": {"exp_id": "EXP-1", "run_id": run_id},
        "local_pull_root": f"remote_artifacts/{run_id}",
    }


def _ready_plan(stage_graph=None, runspec_templates=None, **overrides):
    if stage_graph is None:
        stage_graph = {"alpha": {"depends_on": []}}
    if runspec_templates is None:
        runspec_templates = {
            stage: _runspec_template(f"run-{stage}") for stage in stage_graph
        }
    plan = {
        "schema_version": "prerun.execution-plan.v2",
        "plan_id": "plan-1",
        "source_intent": {"objective": "blindspot fixture"},
        "reviewed_commit": REVIEWED_COMMIT,
        "stage_graph": stage_graph,
        "runspec_templates": runspec_templates,
        "adapter_contracts": {},
        "scientific_gates": {},
        "late_bound_fields": [],
        "rrctl_binding": {"release": "rrctl-1"},
        "critical_surface_rules": [],
    }
    plan.update(overrides)
    return plan


def _manifest(plan_id, stage_id):
    return {
        "stage_id": stage_id,
        "status": "succeeded",
        "plan_id": plan_id,
        "run_id": f"run-{stage_id}",
    }


def _stage_request(**overrides):
    request = {
        "schema_version": stage_flow.REQUEST_SCHEMA,
        "execution_plan": _ready_plan(),
        "mission_state": {
            "schema_version": stage_flow.STATE_SCHEMA,
            "reviewed_execution_plan_sha256": "unused",
            "reviewed_review_snapshot_sha256": "unused",
            "reviewed_semantic_fingerprint": "unused",
            "validated_rrctl_binding_sha256": "unused",
            "implementation_reviewed_commit": REVIEWED_COMMIT,
            "prerun_passed": True,
            "stages": {"alpha": "pending"},
        },
        "manifests": {},
        "scientific_gate_results": {},
        "manual_overrides": {},
    }
    request.update(overrides)
    return request


class StageFlowTests(unittest.TestCase):
    def advance(self, request):
        return stage_flow.advance_stage(request)

    def test_pending_stage_with_satisfied_dependencies_is_ready(self):
        result = self.advance(_stage_request())
        self.assertEqual(result["decision"], "ready", result["errors"])
        self.assertEqual(result["stage_id"], "alpha")
        self.assertIn("run-alpha", json.dumps(result["run_spec"]))

    def test_cycle_with_running_state_reports_plan_blocked(self):
        # 环上的依赖永远无法变 succeeded：计划校验层必须先拒绝，而不是静默推进。
        graph = {
            "a": {"depends_on": ["b"]},
            "b": {"depends_on": ["a"]},
        }
        request = _stage_request(execution_plan=_ready_plan(stage_graph=graph))
        request["mission_state"]["stages"] = {"a": "running", "b": "pending"}
        result = self.advance(request)
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reason_codes"], ["execution_plan_invalid"])
        self.assertTrue(any("stage_cycle" in e for e in result["errors"]))

    def test_cycle_with_all_pending_reports_plan_blocked(self):
        graph = {
            "a": {"depends_on": ["b"]},
            "b": {"depends_on": ["a"]},
        }
        request = _stage_request(execution_plan=_ready_plan(stage_graph=graph))
        request["mission_state"]["stages"] = {"a": "pending", "b": "pending"}
        result = self.advance(request)
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reason_codes"], ["execution_plan_invalid"])
        self.assertTrue(any("stage_cycle" in e for e in result["errors"]))

    def test_orphan_stage_state_mismatch_is_rejected(self):
        # 状态里多出 plan 未声明的阶段，属于孤儿节点，必须拒绝。
        request = _stage_request()
        request["mission_state"]["stages"] = {"alpha": "pending", "ghost": "pending"}
        result = self.advance(request)
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(any("stage_state_mismatch" in e for e in result["errors"]))

    def test_orphan_plan_stage_without_state_is_rejected(self):
        graph = {
            "alpha": {"depends_on": []},
            "ghost": {"depends_on": []},
        }
        request = _stage_request(execution_plan=_ready_plan(stage_graph=graph))
        request["mission_state"]["stages"] = {"alpha": "pending"}
        result = self.advance(request)
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(any("stage_state_mismatch" in e for e in result["errors"]))

    def test_invalid_stage_state_value_is_rejected(self):
        request = _stage_request()
        request["mission_state"]["stages"] = {"alpha": "done"}
        result = self.advance(request)
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(any("stage_state_invalid" in e for e in result["errors"]))

    def test_first_pending_stage_without_dependencies_wins_before_downstream(self):
        # base（无依赖、pending）排在 next（依赖 base、pending）之前；因此 ready 的是 base，
        # 而不是越过依赖去推进 next。这验证依赖顺序不被跳过。
        graph = {
            "base": {"depends_on": []},
            "next": {"depends_on": ["base"]},
        }
        request = _stage_request(execution_plan=_ready_plan(stage_graph=graph))
        request["mission_state"]["stages"] = {"base": "pending", "next": "pending"}
        result = self.advance(request)
        self.assertEqual(result["decision"], "ready", result["errors"])
        self.assertEqual(result["stage_id"], "base")

    def test_blocked_downstream_stage_waits_for_upstream(self):
        # 只把下游阶段留为 pending 时，它必须等待上游 succeeded。
        graph = {
            "base": {"depends_on": []},
            "next": {"depends_on": ["base"]},
        }
        request = _stage_request(execution_plan=_ready_plan(stage_graph=graph))
        request["mission_state"]["stages"] = {"base": "failed", "next": "pending"}
        result = self.advance(request)
        self.assertEqual(result["decision"], "wait")
        self.assertEqual(result["reason_codes"], ["upstream_stage_not_succeeded"])

    def test_dependency_closure_requires_manifest_for_indirect_ancestors(self):
        graph = {
            "base": {"depends_on": []},
            "middle": {"depends_on": ["base"]},
            "leaf": {"depends_on": ["middle"]},
        }
        request = _stage_request(execution_plan=_ready_plan(stage_graph=graph))
        request["mission_state"]["stages"] = {
            "base": "succeeded",
            "middle": "succeeded",
            "leaf": "pending",
        }
        # 只提供直接依赖 middle 的清单，缺失间接祖先 base 的清单。
        request["manifests"] = {"middle": _manifest("plan-1", "middle")}
        result = self.advance(request)
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reason_codes"], ["upstream_manifest_invalid"])
        self.assertTrue(any("manifest_missing" in e for e in result["errors"]))

    def test_dependency_closure_with_all_manifests_is_ready(self):
        graph = {
            "base": {"depends_on": []},
            "middle": {"depends_on": ["base"]},
            "leaf": {"depends_on": ["middle"]},
        }
        request = _stage_request(execution_plan=_ready_plan(stage_graph=graph))
        request["mission_state"]["stages"] = {
            "base": "succeeded",
            "middle": "succeeded",
            "leaf": "pending",
        }
        request["manifests"] = {
            "base": _manifest("plan-1", "base"),
            "middle": _manifest("plan-1", "middle"),
        }
        result = self.advance(request)
        self.assertEqual(result["decision"], "ready", result["errors"])
        self.assertEqual(result["stage_id"], "leaf")

    def test_stage_order_follows_sorted_pending_not_graph_insertion(self):
        graph = {
            "zeta": {"depends_on": []},
            "alpha": {"depends_on": []},
        }
        plan = _ready_plan(stage_graph=graph)
        request = _stage_request(execution_plan=plan)
        request["mission_state"]["stages"] = {"zeta": "pending", "alpha": "pending"}
        result = self.advance(request)
        self.assertEqual(result["decision"], "ready", result["errors"])
        self.assertEqual(result["stage_id"], "alpha")

    def test_manual_override_is_blocked(self):
        request = _stage_request(manual_overrides={"alpha": {"field": "value"}})
        result = self.advance(request)
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reason_codes"], ["manual_override_unsupported"])

    def test_upstream_skipped_propagates_skip_to_descendants(self):
        graph = {
            "base": {"depends_on": []},
            "mid": {"depends_on": ["base"]},
            "end": {"depends_on": ["mid"]},
        }
        plan = _ready_plan(stage_graph=graph)
        request = _stage_request(execution_plan=plan)
        request["mission_state"]["stages"] = {
            "base": "skipped",
            "mid": "pending",
            "end": "pending",
        }
        result = self.advance(request)
        self.assertEqual(result["decision"], "skipped")
        self.assertEqual(result["reason_codes"], ["upstream_stage_skipped"])
        self.assertEqual(result["state_patch"]["stages"], {"mid": "skipped", "end": "skipped"})


# --- remote_route 夹具 -------------------------------------------------------


def _change_manifest(route="full_review", probe_exit_code=0):
    if route == "no_prerun":
        changes = [
            {
                "path": "docs/readme.md",
                "change_class": "documentation",
                "production_reachable": False,
                "dependency_closure_changed": False,
                "blocker_ids": [],
                "probes": [],
            }
        ]
    elif route == "micro_validation":
        changes = [
            {
                "path": "scripts/train.sh",
                "change_class": "shell_syntax",
                "production_reachable": True,
                "dependency_closure_changed": False,
                "blocker_ids": [],
                "probes": [
                    {
                        "command": "bash -n scripts/train.sh",
                        "exit_code": probe_exit_code,
                        "observation": "syntax check",
                        "reaches_production": True,
                    }
                ],
            }
        ]
    elif route == "smoke_validation":
        changes = [
            {
                "path": "scripts/launcher.py",
                "change_class": "process_supervision",
                "production_reachable": True,
                "dependency_closure_changed": False,
                "blocker_ids": [],
                "probes": [
                    {
                        "command": "python3 scripts/launcher.py --smoke",
                        "exit_code": probe_exit_code,
                        "observation": "1 step ok",
                        "reaches_production": True,
                    }
                ],
            }
        ]
    elif route == "targeted_review":
        changes = [
            {
                "path": "src/auth.py",
                "change_class": "credential",
                "production_reachable": True,
                "dependency_closure_changed": False,
                "blocker_ids": [],
                "probes": [],
            }
        ]
    else:
        changes = [
            {
                "path": "src/model.py",
                "change_class": "model",
                "production_reachable": True,
                "dependency_closure_changed": False,
                "blocker_ids": [],
                "probes": [],
            }
        ]
    return {
        "schema_version": "prerun.change-route.v1",
        "reviewed_commit": REVIEWED_COMMIT,
        "candidate_commit": CANDIDATE_COMMIT,
        "changes": changes,
    }


def _smoke_spec(commit=CANDIDATE_COMMIT):
    return {
        "candidate_commit": commit,
        "max_steps": 5,
        "gpu_count": 1,
        "isolated_output": True,
        "official_metrics_disabled": True,
        "artifact_ingest_disabled": True,
        "user_authorized": True,
        "production_command_bound": True,
        "fail_on_output_collision": True,
        "checkpoint_cleanup_required": True,
    }


def _probe_spec(commit=CANDIDATE_COMMIT):
    return {
        "candidate_commit": commit,
        "base_validation_only": True,
        "parameter_updates_disabled": True,
        "official_access_disabled": True,
        "artifact_ingest_disabled": True,
        "user_authorized": True,
        "production_command_bound": True,
        "fail_on_output_collision": True,
        "preregistered_before_review": True,
    }


def _route_request(**overrides):
    request = {
        "schema_version": "mission.remote-route.v1",
        "execution_kind": "remote",
        "lifecycle": "not_started",
        "has_running_evidence": False,
        "command_owner": "rrctl",
        "rrctl": {"available": True, "readiness": "passed", "launch": "passed"},
        "code_changed": False,
        "custom_control_scripts": [],
        "execution_purpose": "official",
    }
    request.update(overrides)
    return request


class RemoteRouteTests(unittest.TestCase):
    def decide(self, request):
        return remote_route.decide_remote_route(request)

    def test_no_prerun_route_proceeds_without_prerun(self):
        result = self.decide(
            _route_request(code_changed=True, change_manifest=_change_manifest("no_prerun"))
        )
        self.assertEqual(result["decision"], "proceed", result["errors"])
        self.assertEqual(result["route"], "rrctl")
        self.assertIn("explicit_owner_rrctl", result["reason_codes"])

    def test_no_prerun_route_falls_back_to_full_review_when_manifest_invalid(self):
        manifest = _change_manifest("no_prerun")
        manifest["changes"][0]["change_class"] = "unknown"
        result = self.decide(_route_request(code_changed=True, change_manifest=manifest))
        self.assertEqual(result["decision"], "blocked")
        # 未知 change_class 的分类结果仍然是 full_review，官方用途要求 formal review。
        self.assertEqual(result["reason_codes"], ["formal_review_required"])

    def test_micro_validation_proceeds_with_passing_probe(self):
        result = self.decide(
            _route_request(
                code_changed=True,
                change_manifest=_change_manifest("micro_validation", probe_exit_code=0),
            )
        )
        self.assertEqual(result["decision"], "proceed", result["errors"])
        self.assertEqual(result["change_route"]["route"], "micro_validation")

    def test_micro_validation_falls_back_to_full_review_when_probe_fails(self):
        result = self.decide(
            _route_request(
                code_changed=True,
                change_manifest=_change_manifest("micro_validation", probe_exit_code=1),
            )
        )
        # 探针失败时绝不走 micro_validation；fail-closed 升级为 full_review，
        # 官方用途下必须 formal review，而不是静默放行。
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reason_codes"], ["formal_review_required"])
        self.assertEqual(result["change_route"]["route"], "full_review")

    def test_smoke_validation_proceeds_with_passing_probe(self):
        result = self.decide(
            _route_request(
                code_changed=True,
                change_manifest=_change_manifest("smoke_validation", probe_exit_code=0),
            )
        )
        self.assertEqual(result["decision"], "proceed", result["errors"])
        self.assertEqual(result["change_route"]["route"], "smoke_validation")

    def test_smoke_validation_falls_back_to_full_review_when_probe_fails(self):
        result = self.decide(
            _route_request(
                code_changed=True,
                change_manifest=_change_manifest("smoke_validation", probe_exit_code=1),
            )
        )
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reason_codes"], ["formal_review_required"])
        self.assertEqual(result["change_route"]["route"], "full_review")
        self.assertEqual(
            result["change_route"]["reason_codes"],
            ["scaffolding_smoke_evidence_incomplete"],
        )

    def test_targeted_review_requires_formal_review_when_official(self):
        result = self.decide(
            _route_request(code_changed=True, change_manifest=_change_manifest("targeted_review"))
        )
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reason_codes"], ["formal_review_required"])

    def test_targeted_review_proceeds_with_matching_formal_review(self):
        review = {
            "passed": True,
            "candidate_commit": CANDIDATE_COMMIT,
            "review_mode": "targeted_review",
            "review_result": "targeted_correct",
            "reviewer_id": "reviewer-1",
            "closure_evidence_paths": ["reviews/closure.md"],
        }
        result = self.decide(
            _route_request(
                code_changed=True,
                change_manifest=_change_manifest("targeted_review"),
                formal_review=review,
            )
        )
        self.assertEqual(result["decision"], "proceed", result["errors"])
        self.assertEqual(result["change_route"]["route"], "targeted_review")

    def test_full_review_requires_formal_review_for_official_purpose(self):
        result = self.decide(
            _route_request(code_changed=True, change_manifest=_change_manifest("full_review"))
        )
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reason_codes"], ["formal_review_required"])

    def test_pre_review_smoke_proceeds_when_bound_correctly(self):
        result = self.decide(
            _route_request(
                execution_purpose="pre_review_smoke",
                code_changed=True,
                change_manifest=_change_manifest("full_review"),
                pre_review_smoke=_smoke_spec(),
            )
        )
        self.assertEqual(result["decision"], "proceed", result["errors"])
        self.assertEqual(result["execution_purpose"], "pre_review_smoke")
        self.assertIn("pre_review_smoke_authorized", result["reason_codes"])

    def test_pre_review_smoke_rejects_candidate_commit_mismatch(self):
        result = self.decide(
            _route_request(
                execution_purpose="pre_review_smoke",
                code_changed=True,
                change_manifest=_change_manifest("full_review"),
                pre_review_smoke=_smoke_spec(commit="c" * 40),
            )
        )
        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["reason_codes"], ["pre_review_smoke_commit_mismatch"])

    def test_fallback_allowed_is_hardcoded_false_for_all_decisions(self):
        cases = [
            self.decide(_route_request()),
            self.decide(_route_request(execution_kind="local")),
            self.decide(_route_request(lifecycle="closed")),
            self.decide(_route_request(code_changed=True, change_manifest=_change_manifest("no_prerun"))),
            self.decide("not an object"),
        ]
        for index, result in enumerate(cases):
            with self.subTest(case=index, decision=result["decision"]):
                self.assertIn("fallback_allowed", result)
                self.assertIs(result["fallback_allowed"], False)

    def test_preregistered_probe_rejects_boundary_flag_false(self):
        probe = _probe_spec()
        probe["base_validation_only"] = False
        result = self.decide(
            _route_request(
                execution_purpose="preregistered_read_only_probe",
                code_changed=True,
                change_manifest=_change_manifest("full_review"),
                preregistered_read_only_probe=probe,
            )
        )
        self.assertEqual(result["decision"], "blocked")
        self.assertTrue(any("probe_boundary_invalid" in e for e in result["errors"]))

    def test_preregistered_probe_proceeds_when_all_flags_true(self):
        result = self.decide(
            _route_request(
                execution_purpose="preregistered_read_only_probe",
                code_changed=True,
                change_manifest=_change_manifest("full_review"),
                preregistered_read_only_probe=_probe_spec(),
            )
        )
        self.assertEqual(result["decision"], "proceed", result["errors"])
        self.assertIn("preregistered_read_only_probe_authorized", result["reason_codes"])

    def test_running_remote_rrctl_resume_generates_resume_not_blocked(self):
        result = self.decide(
            _route_request(
                lifecycle="running_remote",
                has_running_evidence=True,
                command_owner="rrctl",
            )
        )
        self.assertEqual(result["decision"], "resume")
        self.assertEqual(result["route"], "rrctl")
        self.assertEqual(result["reason_codes"], ["running_rrctl_resume"])


# --- compact_artifacts 夹具 ---------------------------------------------------


class CompactArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="compact-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "research_workspace").mkdir()

    def write_csv(self, rows):
        path = self.root / "tasks.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def row(self, row_id, remote_state="artifacts_pulled"):
        return {
            "id": row_id,
            "dev_state": "已完成",
            "review_initial_state": "已完成",
            "review_regression_state": "已完成",
            "git_state": "已提交",
            "remote_state": remote_state,
        }

    def test_non_csv_input_is_rejected(self):
        target = self.root / "not-a-csv.txt"
        target.write_text("{}\n")
        with self.assertRaisesRegex(compact_artifacts.CompactError, "input_not_csv"):
            compact_artifacts._select_csv(target)

    def test_missing_input_is_rejected(self):
        with self.assertRaisesRegex(compact_artifacts.CompactError, "input_missing"):
            compact_artifacts._select_csv(self.root / "missing")

    def test_empty_csv_directory_is_ambiguous(self):
        directory = self.root / "empty"
        directory.mkdir()
        with self.assertRaisesRegex(compact_artifacts.CompactError, "csv_selection_ambiguous"):
            compact_artifacts._select_csv(directory)

    def test_multiple_csv_files_are_ambiguous(self):
        directory = self.root / "multi"
        directory.mkdir()
        (directory / "a.csv").write_text("id\n")
        (directory / "b.csv").write_text("id\n")
        with self.assertRaisesRegex(compact_artifacts.CompactError, "csv_selection_ambiguous"):
            compact_artifacts._select_csv(directory)

    def test_unique_csv_file_is_selected(self):
        directory = self.root / "single"
        directory.mkdir()
        candidate = directory / "single.csv"
        candidate.write_text("id\n")
        self.assertEqual(compact_artifacts._select_csv(directory), candidate.resolve())

    def test_schema_invalid_json_is_treated_as_unverified(self):
        (self.root / "state-bad.json").write_text("{not json}\n", encoding="utf-8")
        verified, unverified = compact_artifacts._verified_state_inputs(self.root, {})
        self.assertEqual(verified, [])
        self.assertEqual([p.name for p in unverified], ["state-bad.json"])

    def test_wrong_schema_version_is_treated_as_unverified(self):
        (self.root / "state-x.json").write_text(
            json.dumps({"schema_version": "wrong.schema", "row_id": "I-1", "event": {}}),
            encoding="utf-8",
        )
        verified, unverified = compact_artifacts._verified_state_inputs(self.root, {})
        self.assertEqual(verified, [])
        self.assertEqual([p.name for p in unverified], ["state-x.json"])

    def test_unrecorded_event_input_is_treated_as_unverified(self):
        (self.root / "state-y.json").write_text(
            json.dumps(
                {
                    "schema_version": "mission.csv-state-update.v1",
                    "row_id": "I-1",
                    "event": {"kind": "fixture"},
                }
            ),
            encoding="utf-8",
        )
        verified, unverified = compact_artifacts._verified_state_inputs(self.root, {})
        self.assertEqual(verified, [])
        self.assertEqual([p.name for p in unverified], ["state-y.json"])

    def test_run_id_rejects_path_traversal_and_empty_components(self):
        for bad in ("", "a//b", "../../etc/passwd", "a/b"):
            with self.subTest(run_id=bad):
                with self.assertRaises(compact_artifacts.CompactError):
                    compact_artifacts._run_id({"run_id": bad})

    def test_mission_not_closing_ready_is_rejected_in_require_full(self):
        path = self.write_csv([self.row("I-1", remote_state="pending")])
        with self.assertRaisesRegex(compact_artifacts.CompactError, "mission_not_closing_ready"):
            compact_artifacts.compact(
                path, self.root / "research_workspace", apply=True, require_full=True
            )

    def test_schema_invalid_csv_missing_state_fields_is_rejected(self):
        path = self.root / "broken.csv"
        path.write_text("id,name\nI-1,task one\n", encoding="utf-8")
        with self.assertRaisesRegex(compact_artifacts.CompactError, "csv_schema_missing_state_fields"):
            compact_artifacts.compact(
                path, self.root / "research_workspace", apply=True, require_full=False
            )


# --- ensure_review_row / ensure_result_analysis_row 幂等 -----------------------


class EnsureRowIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ensure-row-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "tasks.csv"

    def write_csv(self, rows):
        with self.path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def base_row(self, row_id, **overrides):
        row = dict.fromkeys(EXPECTED_FIELDS, "")
        row.update(
            id=row_id,
            phase="1",
            dev_state="未开始",
            review_initial_state="未开始",
            review_regression_state="未开始",
            git_state="未提交",
            remote_state="not_applicable",
        )
        row.update(overrides)
        return row

    def test_ensure_review_row_is_idempotent(self):
        self.write_csv([self.base_row("ISSUE-01")])
        self.assertTrue(ensure_review_row(self.path))
        first = self.path.read_text(encoding="utf-8-sig")
        self.assertFalse(ensure_review_row(self.path))
        second = self.path.read_text(encoding="utf-8-sig")
        self.assertEqual(first, second)
        fieldnames, rows, _ = read_mission_csv(self.path)
        review_rows = [row for row in rows if row["id"].startswith("REVIEW-")]
        self.assertEqual(len(review_rows), 1)

    def test_ensure_review_row_respects_any_review_prefix(self):
        self.write_csv([self.base_row("ISSUE-01"), self.base_row("REVIEW-07")])
        self.assertFalse(ensure_review_row(self.path))
        _, rows, _ = read_mission_csv(self.path)
        self.assertEqual([row["id"] for row in rows], ["ISSUE-01", "REVIEW-07"])

    def test_ensure_result_analysis_row_is_idempotent(self):
        run = self.base_row("RUN-1", exp_id="EXP-1", remote_state="completed")
        review = self.base_row("REVIEW-01")
        self.write_csv([run, review])
        self.assertTrue(ensure_result_analysis_row(self.path))
        first = self.path.read_text(encoding="utf-8-sig")
        self.assertFalse(ensure_result_analysis_row(self.path))
        second = self.path.read_text(encoding="utf-8-sig")
        self.assertEqual(first, second)
        _, rows, _ = read_mission_csv(self.path)
        analysis_rows = [row for row in rows if row["id"].startswith("RESULT-ANALYSIS-")]
        self.assertEqual(len(analysis_rows), 1)

    def test_ensure_result_analysis_row_respects_any_analysis_prefix(self):
        run = self.base_row("RUN-1", exp_id="EXP-1")
        analysis = self.base_row("RESULT-ANALYSIS-99")
        self.write_csv([run, analysis])
        self.assertFalse(ensure_result_analysis_row(self.path))
        _, rows, _ = read_mission_csv(self.path)
        self.assertEqual([row["id"] for row in rows], ["RUN-1", "RESULT-ANALYSIS-99"])


# --- run_vision_review 证据绑定与 schema 拒绝 ---------------------------------


def _vision_result(**overrides):
    result = {
        "review_agent_mode": "reviewer-subagent",
        "review_independence": True,
        "review_requested_model": "xiaojimao/gpt-6-astra:high",
        "review_observed_model": "xiaojimao/gpt-6-astra",
        "review_model_evidence": "session-metadata",
        "result": "vision_met",
        "claim_coverage": "1/1",
        "claim_coverage_status": "complete",
        "validation_limited": [],
        "summary": "scope reviewed",
        "gaps": [],
        "assumptions": [],
        "decision_debt": [],
        "deferred_findings": [],
        "human_required_blockers": [],
        "outcome_answers": [],
        "handoff_markdown": "handoff",
    }
    result.update(overrides)
    return result


class VisionReviewSchemaTests(unittest.TestCase):
    def errors(self, **overrides):
        return validate_review_result(_vision_result(**overrides), None)

    def test_missing_required_key_is_rejected(self):
        result = _vision_result()
        result.pop("summary")
        errors = validate_review_result(result, None)
        self.assertTrue(any("missing required keys" in e for e in errors), errors)

    def test_wrong_numeric_coverage_is_rejected(self):
        errors = self.errors(claim_coverage="2/1")
        self.assertTrue(any("cannot exceed total" in e for e in errors), errors)

    def test_complete_status_requires_exact_coverage(self):
        errors = self.errors(claim_coverage="0/2", claim_coverage_status="complete")
        self.assertTrue(any("requires covered=total" in e for e in errors), errors)

    def test_gaps_status_requires_coverage_gap(self):
        errors = self.errors(claim_coverage="1/1", claim_coverage_status="gaps")
        self.assertTrue(any("requires covered<total" in e for e in errors), errors)

    def test_unknown_status_requires_unknown_literal(self):
        errors = self.errors(claim_coverage="0/1", claim_coverage_status="unknown")
        self.assertTrue(any("requires claim_coverage=unknown" in e for e in errors), errors)

    def test_unknown_coverage_requires_limited_review(self):
        errors = self.errors(
            claim_coverage="unknown",
            claim_coverage_status="unknown",
            result="vision_met",
        )
        self.assertTrue(any("may be unknown only for limited_review" in e for e in errors), errors)

    def test_schema_invalid_mode_rejects_session_evidence(self):
        # 这个组合已经把 review_agent_mode 覆盖了，但 schema_invalid 仍应拒绝。
        errors = self.errors(
            review_agent_mode="bogus-mode",
            review_model_evidence="unknown",
        )
        self.assertTrue(any("invalid review_agent_mode" in e for e in errors), errors)
        self.assertTrue(
            any("review_model_evidence must be one of" in e for e in errors),
            errors,
        )

    def test_vision_met_requires_complete_coverage_and_no_gaps(self):
        gap = {
            "id": "G-1",
            "title": "gap",
            "source_ref": "x",
            "evidence_ref": "y",
            "why_it_matters": "z",
            "suggested_followup_issue": "F-1",
        }
        errors = self.errors(
            result="vision_met",
            claim_coverage_status="complete",
            gaps=[gap],
        )
        self.assertTrue(any("empty gaps array" in e for e in errors), errors)

    def test_gaps_found_requires_gap_signal(self):
        errors = self.errors(
            result="gaps_found",
            claim_coverage_status="unknown",
            claim_coverage="unknown",
            gaps=[],
            human_required_blockers=[],
        )
        self.assertTrue(any("requires a recorded gap signal" in e for e in errors), errors)


# --- validate_deferred_ledger 入口/出口校验 ------------------------------------


class DeferredLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="deferred-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "tasks.csv"
        self.ledger_path = self.root / "deferred.json"

    def write_csv(self, rows):
        with self.path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def base_row(self, row_id, notes=""):
        row = dict.fromkeys(EXPECTED_FIELDS, "")
        row.update(
            id=row_id,
            dev_state="已完成",
            review_initial_state="已完成",
            review_regression_state="已完成",
            git_state="已提交",
            remote_state="not_applicable",
            notes=notes,
        )
        return row

    def write_ledger(self, payload):
        self.ledger_path.write_text(json.dumps(payload), encoding="utf-8")

    def valid_finding(self, finding_id="DF-001", status="open"):
        return {
            "id": finding_id,
            "kind": "deferred_improvement",
            "status": status,
            "title": "t",
            "summary": "s",
            "why_deferred": "w",
            "discussion_question": "q",
            "source_issue_ids": ["ISSUE-01"],
            "evidence_refs": ["e"],
        }

    def test_rejects_wrong_schema_version(self):
        self.write_ledger({"schema_version": 2, "csv": "tasks.csv", "findings": []})
        self.write_csv([self.base_row("ISSUE-01", notes="deferred_ledger:deferred.json")])
        _, _, errors, _ = load_csv_deferred(self.path, self.root)
        self.assertTrue(any("schema_version must be 1" in e for e in errors), errors)

    def test_rejects_csv_field_mismatch(self):
        self.write_ledger({"schema_version": 1, "csv": "other.csv", "findings": []})
        self.write_csv([self.base_row("ISSUE-01", notes="deferred_ledger:deferred.json")])
        _, _, errors, _ = load_csv_deferred(self.path, self.root)
        self.assertTrue(any("csv field does not match" in e for e in errors), errors)

    def test_rejects_unreferenced_finding(self):
        self.write_ledger({"schema_version": 1, "csv": "tasks.csv", "findings": [self.valid_finding()]})
        self.write_csv([self.base_row("ISSUE-01", notes="deferred_ledger:deferred.json")])
        _, _, errors, _ = load_csv_deferred(self.path, self.root)
        self.assertTrue(any("unreferenced finding ids" in e for e in errors), errors)

    def test_rejects_reference_to_unknown_finding(self):
        self.write_ledger({"schema_version": 1, "csv": "tasks.csv", "findings": []})
        self.write_csv(
            [
                self.base_row(
                    "ISSUE-01",
                    notes="deferred_ledger:deferred.json; deferred_findings:DF-001",
                )
            ]
        )
        _, _, errors, _ = load_csv_deferred(self.path, self.root)
        self.assertTrue(any("unknown deferred finding ids" in e for e in errors), errors)

    def test_rejects_unknown_source_issue_id(self):
        finding = self.valid_finding()
        finding["source_issue_ids"] = ["GHOST-99"]
        self.write_ledger({"schema_version": 1, "csv": "tasks.csv", "findings": [finding]})
        self.write_csv(
            [
                self.base_row(
                    "ISSUE-01",
                    notes="deferred_ledger:deferred.json; deferred_findings:DF-001",
                )
            ]
        )
        _, _, errors, _ = load_csv_deferred(self.path, self.root)
        self.assertTrue(any("unknown source issue ids" in e for e in errors), errors)

    def test_rejects_invalid_finding_id_format(self):
        finding = self.valid_finding("BAD-1")
        self.write_ledger({"schema_version": 1, "csv": "tasks.csv", "findings": [finding]})
        self.write_csv(
            [
                self.base_row(
                    "ISSUE-01",
                    notes="deferred_ledger:deferred.json; deferred_findings:BAD-1",
                )
            ]
        )
        _, _, errors, _ = load_csv_deferred(self.path, self.root)
        self.assertTrue(any("invalid id" in e for e in errors), errors)

    def test_rejects_duplicate_finding_id(self):
        self.write_ledger(
            {
                "schema_version": 1,
                "csv": "tasks.csv",
                "findings": [self.valid_finding("DF-001"), self.valid_finding("DF-001")],
            }
        )
        self.write_csv(
            [
                self.base_row(
                    "ISSUE-01",
                    notes="deferred_ledger:deferred.json; deferred_findings:DF-001",
                )
            ]
        )
        _, _, errors, _ = load_csv_deferred(self.path, self.root)
        self.assertTrue(any("duplicate deferred finding id" in e for e in errors), errors)

    def test_rejects_invalid_kind_or_status(self):
        for key, value in (("kind", "bogus"), ("status", "closed")):
            with self.subTest(field=key):
                finding = self.valid_finding()
                finding[key] = value
                self.write_ledger({"schema_version": 1, "csv": "tasks.csv", "findings": [finding]})
                self.write_csv(
                    [
                        self.base_row(
                            "ISSUE-01",
                            notes="deferred_ledger:deferred.json; deferred_findings:DF-001",
                        )
                    ]
                )
                _, _, errors, _ = load_csv_deferred(self.path, self.root)
                self.assertTrue(
                    any(f"invalid {key}" in e for e in errors),
                    errors,
                )

    def test_review_coverage_must_match_open_count(self):
        self.write_ledger({"schema_version": 1, "csv": "tasks.csv", "findings": [self.valid_finding(status="open")]})
        self.write_csv(
            [
                self.base_row(
                    "ISSUE-01",
                    notes="deferred_ledger:deferred.json; deferred_findings:DF-001",
                ),
                self.base_row(
                    "REVIEW-01",
                    notes="deferred_ledger:deferred.json; deferred_coverage:5/2",
                ),
            ]
        )
        _, _, errors, _ = load_csv_deferred(self.path, self.root)
        self.assertTrue(any("deferred_coverage total" in e for e in errors), errors)
        self.assertTrue(any("covered count cannot exceed total" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
