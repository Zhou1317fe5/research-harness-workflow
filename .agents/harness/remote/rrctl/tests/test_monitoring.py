from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_process_backend as fixtures

from remote_run_control import cli, finalization, health, monitor, state, worker
from remote_run_control.adapter import run_adapter
from remote_run_control.errors import RRCError
from remote_run_control.health import HealthResult
from remote_run_control.jsonutil import atomic_write_json, load_json, utc_now
from remote_run_control.processes import owned_processes
from remote_run_control.transport import CommandResult


def await_condition(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.03)
    raise AssertionError("condition was not reached within its bounded wait")


class MonitoringTests(unittest.TestCase):
    setUp = fixtures.ProcessBackendTests.setUp
    tearDown = fixtures.ProcessBackendTests.tearDown
    spec = fixtures.ProcessBackendTests.spec

    def adapter_spec(self, *, delay=1.5, mode="normal", timeout=1, interval=0.15):
        (self.repo / "adapter.py").write_text(
            "import json,sys,time\nfrom pathlib import Path\n"
            "c=json.load(sys.stdin); phase=c['phase']; mode=c['metadata'].get('test_mode')\n"
            "with (Path(c['control_root'])/'adapter-calls.jsonl').open('a') as f:\n"
            " f.write(json.dumps({'phase':phase,'at':time.time()})+'\\n')\n"
            "if mode=='periodic_timeout' and phase=='periodic': time.sleep(30)\n"
            "if mode=='completion_timeout' and phase=='completion': time.sleep(30)\n"
            "if mode=='completion_pause' and phase=='completion': time.sleep(3)\n"
            "if mode=='completion_error' and phase=='completion': raise SystemExit(17)\n"
            "healthy=not((mode=='bad_first' and phase=='first_step') or "
            "(mode=='bad_completion' and phase=='completion'))\n"
            "print(json.dumps({'protocol':'rrctl.adapter.v1','healthy':healthy,"
            "'complete':healthy and phase=='completion','artifacts':[]}))\n"
        )
        subprocess.run(["git", "add", "adapter.py"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "adapter fixture"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        self.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
        ).strip()
        spec = self.spec(delay=delay)
        phases = {
            name: replace(
                getattr(spec.health, name),
                adapter_argv=("python", "adapter.py"),
                adapter_timeout_seconds=timeout,
                poll_interval_seconds=interval,
                timeout_seconds=2 if name == "completion" else 10,
            )
            for name in monitor.PHASES
        }
        return replace(spec, health=replace(spec.health, **phases), metadata={"test_mode": mode})

    def calls(self, spec):
        path = Path(spec.remote.control_root) / "adapter-calls.jsonl"
        if not path.exists():
            return Counter()
        return Counter(json.loads(line)["phase"] for line in path.read_text().splitlines())

    def launch_allow_fast_failure(self, spec):
        try:
            self.control.launch(spec)
        except RRCError as exc:
            self.assertIn(exc.code, {"first_step_terminal", "first_step_attention"})

    def fixture_monitor(self, spec=None):
        spec = spec or self.spec()
        root = Path(spec.remote.control_root)
        root.mkdir(parents=True)
        atomic_write_json(root / "run_spec.json", spec.to_dict())
        binding = {
            "run_id": spec.run_id,
            "run_spec_sha256": spec.digest,
            "worker_sha256": "a" * 64,
            "created_at": utc_now(),
            "monitoring": monitor.declaration(spec),
        }
        atomic_write_json(root / "binding.json", binding)
        for value in ("prepared", "staged", "launched", "first_step_passed", "running"):
            state.transition(root, run_id=spec.run_id, next_state=value, reason="fixture")
        watch = monitor.Monitor(spec, root)
        watch.data["monitor_status"] = "running"
        return spec, root, watch

    def test_offline_monitoring_finalization_and_multiple_readers_reuse_checks(self):
        spec = self.adapter_spec()
        self.control.launch(spec)
        root = Path(spec.remote.control_root)
        # 仅查看本地测试目录；没有客户端 health/wait 驱动远端收尾。
        await_condition(lambda: state.read_status(root)["state"] == "completed")
        counts = self.calls(spec)
        self.assertGreaterEqual(counts["periodic"], 2)
        self.assertEqual(counts["completion"], 1)
        self.assertTrue((root / "artifact_manifest.json").is_file())
        self.assertTrue((root / "completion.json").is_file())
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(
                pool.map(lambda _: self.control.wait(spec.run_id, max_wait_seconds=5), range(3))
            )
        self.assertTrue(all(result["status"]["state"] == "completed" for result in results))
        for phase in monitor.PHASES:
            for _ in range(3):
                self.assertTrue(self.control.health(spec.run_id, phase=phase)["cached"])
        self.assertEqual(self.calls(spec), counts)
        saved = load_json(root / "monitor.json")
        self.assertEqual(saved["adapter_calls"], dict(counts))
        self.assertEqual(saved["event_sequence"], 0)
        self.assertEqual(
            len((root / "health.jsonl").read_text().splitlines()), sum(counts.values())
        )
        self.assertTrue(self.control.pull(spec.run_id)["entries"])

    def test_killed_observer_does_not_stop_periodic_checks_or_completion(self):
        spec = self.adapter_spec(delay=2.5)
        self.control.launch(spec)
        root = Path(spec.remote.control_root)
        before = self.calls(spec)["periodic"]
        observer = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "remote_run_control",
                "--profiles",
                str(self.root / "profiles.json"),
                "--state-root",
                str(self.root / "index"),
                "--json",
                "wait",
                spec.run_id,
                "--max-wait-seconds",
                "0",
            ],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            time.sleep(0.15)
            os.killpg(observer.pid, signal.SIGTERM)
            observer.communicate(timeout=5)
            await_condition(lambda: self.calls(spec)["periodic"] > before)
            self.assertTrue(owned_processes(spec.run_id, root))
            await_condition(lambda: state.read_status(root)["state"] == "completed")
            self.assertEqual(self.calls(spec)["completion"], 1)
        finally:
            if observer.poll() is None:
                os.killpg(observer.pid, signal.SIGKILL)
                observer.communicate(timeout=5)

    def test_fast_exit_still_requires_first_step_and_artifacts(self):
        spec = self.adapter_spec(delay=0, mode="bad_first")
        self.launch_allow_fast_failure(spec)
        root = Path(spec.remote.control_root)
        await_condition(lambda: state.read_status(root)["state"] == "failed")
        self.assertEqual(state.read_status(root)["reason"], "first_step_contract_failed")
        self.assertEqual(load_json(root / "workload-exit.json")["exit_code"], 0)
        self.assertFalse((root / "artifact_manifest.json").exists())

    def test_missing_artifact_does_not_publish_completed(self):
        from remote_run_control.models import ArtifactSpec

        spec = self.spec(delay=0)
        spec = replace(spec, artifacts=(*spec.artifacts, ArtifactSpec("missing.json")))
        self.launch_allow_fast_failure(spec)
        root = Path(spec.remote.control_root)
        await_condition(lambda: state.read_status(root)["state"] == "failed")
        self.assertEqual(state.read_status(root)["reason"], "artifact_contract_failed")
        self.assertEqual(state.read_status(root)["detail"]["exit_code"], 0)
        self.assertFalse((root / "completion.json").exists())

    def test_explicit_completion_rejection_is_a_result_contract_failure(self):
        spec = self.adapter_spec(delay=0.15, mode="bad_completion")
        self.launch_allow_fast_failure(spec)
        root = Path(spec.remote.control_root)
        await_condition(lambda: state.read_status(root)["state"] == "failed")
        status = state.read_status(root)
        self.assertEqual(status["reason"], "completion_contract_failed")
        self.assertEqual(status["detail"]["failure_kind"], "result_contract")
        self.assertEqual(status["detail"]["exit_code"], 0)
        self.assertEqual(self.calls(spec)["completion"], 1)

    def test_offline_smoke_cleanup_contract_is_preserved(self):
        from remote_run_control.models import ArtifactSpec, OutputCleanupSpec

        original = self.spec()
        code = (
            "import os,time\nfrom pathlib import Path\n"
            "out=Path(os.environ['RRCTL_OUTPUT_ROOT']); out.mkdir()\n"
            "(out/'discard.pt').write_text('temporary checkpoint fixture')\n"
            "time.sleep(0.1)\n"
        )
        spec = replace(
            original,
            workload=replace(original.workload, argv=("python", "-c", code)),
            artifacts=(ArtifactSpec("smoke_summary.json"),),
            output_cleanup=OutputCleanupSpec(mode="pre_review_smoke", delete_globs=("*.pt",)),
            metadata={"execution_purpose": "pre_review_smoke"},
        )
        self.control.launch(spec)
        root = Path(spec.remote.control_root)
        await_condition(lambda: state.read_status(root)["state"] == "completed")
        output = Path(spec.remote.output_root)
        self.assertFalse((output / "discard.pt").exists())
        summary = load_json(output / "smoke_summary.json")
        self.assertEqual(summary["run_id"], spec.run_id)
        self.assertTrue(summary["checkpoint_cleanup_completed"])
        self.assertEqual(summary["checkpoint_paths_remaining"], [])
        self.assertGreater(summary["released_bytes"], 0)

    def test_checker_timeouts_raise_one_alert_without_stopping_workload(self):
        spec = self.adapter_spec(delay=30, mode="periodic_timeout", interval=0.05)
        self.control.launch(spec)
        result = self.control.wait(spec.run_id, poll_seconds=0.2, max_wait_seconds=7)
        root = Path(spec.remote.control_root)
        self.assertEqual(result["observation"], "attention")
        self.assertEqual(state.read_status(root)["state"], "running")
        self.assertTrue(owned_processes(spec.run_id, root))
        self.assertFalse((root / "workload-exit.json").exists())
        events = (root / "monitor-events.jsonl").read_text().splitlines()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0])["code"], "adapter_timeout")
        self.assertEqual(
            self.control.wait(spec.run_id, max_wait_seconds=1)["observation"], "attention"
        )
        self.control.abort(spec.run_id, confirmed=True)

    def test_completion_checker_failure_preserves_exit_fact_and_pending_state(self):
        spec = self.adapter_spec(delay=0.2, mode="completion_error")
        self.control.launch(spec)
        root = Path(spec.remote.control_root)
        await_condition(lambda: load_json(root / "monitor.json")["monitor_status"] == "attention")
        self.assertEqual(state.read_status(root)["state"], "workload_complete")
        self.assertEqual(load_json(root / "workload-exit.json")["exit_code"], 0)
        self.assertEqual(load_json(root / "finalization-error.json")["kind"], "checker_error")
        count = self.calls(spec)
        with self.assertRaises(RRCError) as caught:
            self.control.wait(spec.run_id, max_wait_seconds=2)
        self.assertEqual(caught.exception.code, "monitor_unavailable")
        self.assertEqual(self.calls(spec), count)
        self.assertFalse((root / "completion.json").exists())

    def test_abort_during_completion_adapter_does_not_wait_for_state_lock(self):
        spec = self.adapter_spec(delay=0.2, mode="completion_pause", timeout=10)
        self.control.launch(spec)
        root = Path(spec.remote.control_root)
        await_condition(lambda: self.calls(spec)["completion"] == 1)
        started = time.monotonic()
        result = self.control.abort(spec.run_id, confirmed=True)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(result["state"], "aborted")
        self.assertFalse(owned_processes(spec.run_id, root))
        self.assertFalse((root / "completion.json").exists())
        health_after_abort = self.control.health(spec.run_id, phase="first_step")
        self.assertFalse(health_after_abort["healthy"])
        self.assertFalse(health_after_abort["complete"])
        self.assertFalse(health_after_abort["observations"]["process_alive"])
        self.assertEqual(health_after_abort["run_state"], "aborted")

    def test_duplicate_execute_cannot_restart_workload(self):
        spec = self.spec(delay=1)
        self.control.launch(spec)
        root = Path(spec.remote.control_root)
        pid = load_json(root / "binding.json")["workload_pid"]
        with self.assertRaises(RRCError) as caught:
            worker.execute(root)
        self.assertEqual(caught.exception.code, "monitor_busy")
        self.assertEqual(load_json(root / "binding.json")["workload_pid"], pid)
        await_condition(lambda: state.read_status(root)["state"] == "completed")
        await_condition(lambda: not owned_processes(spec.run_id, root))
        with self.assertRaises(RRCError) as caught:
            worker.execute(root)
        self.assertEqual(caught.exception.code, "execute_already_started")

    def test_sealed_completion_detects_corruption_without_rerunning_adapter(self):
        spec = self.adapter_spec(delay=0.2)
        self.control.launch(spec)
        root = Path(spec.remote.control_root)
        await_condition(lambda: state.read_status(root)["state"] == "completed")
        counts = self.calls(spec)
        receipt = load_json(root / "completion.json")
        receipt["health"]["complete"] = False
        atomic_write_json(root / "completion.json", receipt)
        with self.assertRaises(RRCError) as caught:
            self.control.wait(spec.run_id, max_wait_seconds=2)
        self.assertEqual(caught.exception.code, "monitor_unavailable")
        self.assertEqual(self.calls(spec), counts)
        self.assertEqual(state.read_status(root)["state"], "completed")

    def test_pure_sampling_does_not_write_gpu_history_or_lifecycle(self):
        original = self.spec()
        spec = replace(
            original,
            health=replace(
                original.health,
                periodic=replace(
                    original.health.periodic,
                    gpu_min_percent=50,
                    low_gpu_limit_seconds=10,
                ),
            ),
        )
        spec, root, _ = self.fixture_monitor(spec)
        before = (root / "status.json").read_bytes()
        with (
            patch.object(health, "_sample_gpu", return_value=0),
            patch.object(health.time, "time", return_value=100),
        ):
            first = health.sample_health(spec, root, phase="periodic", process_required=False)
        with (
            patch.object(health, "_sample_gpu", return_value=1),
            patch.object(health.time, "time", return_value=120),
        ):
            second = health.sample_health(
                spec, root, phase="periodic", process_required=False, tracking=first.tracking
            )
        self.assertEqual(second.observations["low_gpu_duration_seconds"], 20)
        self.assertEqual(second.status, "degraded")
        self.assertEqual((root / "status.json").read_bytes(), before)
        self.assertFalse((root / "health_state.json").exists())
        self.assertFalse((root / "health.jsonl").exists())

    def test_alert_debounce_replay_recovery_and_stable_advisory(self):
        _, root, watch = self.fixture_monitor()
        good = HealthResult(True, False, "periodic", {}, ())
        retry = replace(
            good,
            healthy=False,
            status="unavailable",
            issues=(
                {
                    "code": "adapter_timeout",
                    "subject": "",
                    "required": True,
                    "retryable": True,
                },
            ),
        )
        with patch.object(monitor, "sample_health", return_value=retry):
            watch.check("periodic")
            watch.check("periodic")
            self.assertEqual(watch.data["event_sequence"], 0)
            watch.check("periodic")
            watch.check("periodic")
        self.assertEqual(watch.data["event_sequence"], 1)
        with patch.object(monitor, "probe_bound_process", return_value={"state": "alive"}):
            alert = monitor.observe(root, timeout_seconds=0)
            self.assertEqual(alert["observation"], "attention")
            self.assertEqual(monitor.observe(root, timeout_seconds=0)["observation"], "attention")
            cursor = alert["monitor"]["event_cursor"]
            self.assertEqual(
                monitor.observe(root, timeout_seconds=0, after_event=cursor)["observation"],
                "no_event",
            )
            with self.assertRaises(RRCError):
                monitor.observe(root, timeout_seconds=0, after_event="0" * 64 + ":1")
        with patch.object(monitor, "sample_health", return_value=good):
            watch.check("periodic")
        self.assertEqual(watch.data["event_sequence"], 2)
        with patch.object(monitor, "sample_health", return_value=retry):
            for _ in range(3):
                watch.check("periodic")
        self.assertEqual(watch.data["event_sequence"], 3)
        with patch.object(monitor, "sample_health", return_value=good):
            watch.check("periodic")
        before = watch.data["event_sequence"]
        for percent in range(10):
            advisory = replace(
                good,
                status="degraded",
                observations={"gpu": percent},
                issues=(
                    {
                        "code": "gpu_below_advisory",
                        "required": False,
                        "retryable": False,
                    },
                ),
            )
            with patch.object(monitor, "sample_health", return_value=advisory):
                watch.check("periodic")
        self.assertEqual(watch.data["event_sequence"], before)
        lifecycle = (root / "events.jsonl").read_text().splitlines()
        self.assertEqual(len(lifecycle), 5)

    def test_corrupt_cache_and_stale_heartbeat_are_read_only_unknowns(self):
        _, root, watch = self.fixture_monitor()
        with patch.object(
            monitor, "sample_health", return_value=HealthResult(True, False, "periodic", {}, ())
        ):
            watch.check("periodic")
        history = (root / "health.jsonl").read_bytes()
        with (
            patch.object(monitor, "probe_bound_process", return_value={"state": "alive"}),
            patch.object(monitor, "sample_health", side_effect=AssertionError("reader sampled")),
        ):
            saved = load_json(root / "monitor.json")
            stale = {**saved, "heartbeat_epoch": time.time() - 1000}
            atomic_write_json(root / "monitor.json", stale)
            result = monitor.read_health(root, "periodic")
            self.assertEqual(result["monitor_status"], "stale")
            self.assertFalse(result["healthy"])
            atomic_write_json(root / "monitor.json", saved)
            atomic_write_json(root / "health-latest/periodic.json", {})
            self.assertEqual(monitor.read_observation(root)["observation"], "unavailable")
        self.assertEqual((root / "health.jsonl").read_bytes(), history)
        self.assertEqual(state.read_status(root)["state"], "running")

    def test_first_pass_stays_valid_until_long_periodic_interval(self):
        original = self.spec()
        spec = replace(
            original,
            health=replace(
                original.health,
                periodic=replace(original.health.periodic, poll_interval_seconds=600.0),
            ),
        )
        _, root, watch = self.fixture_monitor(spec)
        passed = HealthResult(True, False, "first_step", {}, (), gate_passed=True)
        with patch.object(monitor, "sample_health", return_value=passed):
            watch.check("first_step")
        watch.data["phase"] = "periodic"
        watch.heartbeat()
        cache_path = root / "health-latest/first_step.json"
        cached = load_json(cache_path)
        cached["observed_epoch"] -= 200
        atomic_write_json(cache_path, cached)
        with patch.object(monitor, "probe_bound_process", return_value={"state": "alive"}):
            self.assertEqual(monitor.read_observation(root)["observation"], "no_event")
            self.assertTrue(monitor.read_health(root, "first_step")["gate_passed"])

    def test_manifest_publication_io_error_and_abort_cannot_fake_completion(self):
        spec, root, watch = self.fixture_monitor()
        output = Path(spec.remote.output_root)
        output.mkdir()
        for name in ("summary.json", "started.json"):
            (output / name).write_text("{}")
        report = HealthResult(True, True, "completion", {}, ())
        original_write = state.atomic_write_json

        def fail_receipt(path, value, *args, **kwargs):
            if path.name == "completion.json":
                raise OSError("injected publication failure")
            return original_write(path, value, *args, **kwargs)

        with patch.object(state, "atomic_write_json", side_effect=fail_receipt):
            result = finalization.finalize_exit(
                spec, root, 0, check=lambda _: report, heartbeat=watch.heartbeat
            )
        self.assertEqual(result["state"], "workload_complete")
        self.assertFalse((root / "completion.json").exists())
        build = finalization.build_artifact_manifest

        def abort_after_hash(**kwargs):
            manifest = build(**kwargs)
            state.request_stop(root, run_id=spec.run_id, reason="race", state="aborted")
            state.transition_if_open(root, run_id=spec.run_id, next_state="aborted", reason="race")
            return manifest

        with patch.object(finalization, "build_artifact_manifest", side_effect=abort_after_hash):
            result = finalization.finalize_exit(
                spec, root, 0, check=lambda _: report, heartbeat=watch.heartbeat
            )
        self.assertEqual(result["state"], "aborted")
        self.assertFalse((root / "completion.json").exists())

    def test_virtual_day_keeps_heartbeat_separate_from_checks_and_detects_exit(self):
        original = self.spec()
        spec = replace(
            original,
            health=replace(
                original.health,
                periodic=replace(original.health.periodic, poll_interval_seconds=600.0),
            ),
        )
        _, _, watch = self.fixture_monitor(spec)
        clock = SimpleNamespace(now=0.0)
        watch.clock = lambda: clock.now
        checked = []
        beats = []

        class Process:
            def poll(self):
                return 0 if clock.now >= 86405 else None

            def wait(self, timeout=None):
                if timeout is None or clock.now + timeout >= 86405:
                    clock.now = 86405
                    return 0
                clock.now += timeout
                raise subprocess.TimeoutExpired("fixture", timeout)

        def check(phase):
            checked.append((clock.now, phase))
            return HealthResult(True, False, phase, {}, (), gate_passed=True)

        with (
            patch.object(watch, "check", side_effect=check),
            patch.object(watch, "heartbeat", side_effect=lambda: beats.append(clock.now)),
        ):
            self.assertEqual(watch.supervise(Process()), 0)
        self.assertEqual(clock.now, 86405)
        self.assertEqual(len(checked), 145)
        self.assertGreater(len(beats), len(checked) * 10)
        self.assertTrue(all(at % 600 == 0 for at, _ in checked))

    def test_virtual_client_budget_quiet_wait_retries_and_legacy_dispatch(self):
        spec = self.spec()
        clock = SimpleNamespace(now=0.0)
        calls = []
        binding = {"monitoring": monitor.declaration(spec), "run_spec_sha256": spec.digest}
        current = {
            "run_id": spec.run_id,
            "binding": binding,
            "status": {"state": "running"},
            "observation": "no_event",
        }
        finish = SimpleNamespace(after=None, errors=False)

        def run(argv, *, timeout_seconds):
            budget = float(argv[argv.index("--timeout-seconds") + 1])
            calls.append((clock.now, budget, timeout_seconds))
            self.assertLess(budget, timeout_seconds)
            clock.now += min(budget + 0.1, timeout_seconds)
            if finish.errors:
                raise RRCError("transport_connection", "fixture disconnect", "observer")
            value = dict(current)
            if finish.after is not None and len(calls) >= finish.after:
                value.update(status={"state": "completed"}, observation="terminal")
            return CommandResult(0, json.dumps(value).encode(), b"")

        ref = SimpleNamespace(
            remote_python="python", worker_path="worker.pyz", control_root="control"
        )
        with (
            patch.object(
                self.control,
                "_runtime",
                return_value=(ref, spec, SimpleNamespace(run=run, secret_values=())),
            ),
            patch.object(self.control, "inspect", return_value=current),
            patch.object(self.control.index, "save"),
            patch("remote_run_control.controller.time.monotonic", side_effect=lambda: clock.now),
            patch(
                "remote_run_control.controller.time.sleep",
                side_effect=lambda amount: setattr(clock, "now", clock.now + amount),
            ),
        ):
            expired = self.control.wait(spec.run_id)
            self.assertEqual(expired["observation"], "timeout")
            self.assertEqual(clock.now, 900)
            self.assertTrue(all(start + budget <= 900 for start, _, budget in calls))
            clock.now = 0
            calls.clear()
            finish.after = 2
            output = io.StringIO()
            with (
                patch.object(cli, "Controller", return_value=self.control),
                contextlib.redirect_stdout(output),
            ):
                code = cli.main(["--json", "wait", spec.run_id, "--max-wait-seconds", "0"])
            self.assertEqual(code, 0)
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertEqual(len(calls), 2)
            calls.clear()
            finish.errors = True
            with self.assertRaises(RRCError) as caught:
                self.control.wait(spec.run_id, max_wait_seconds=0)
            self.assertEqual(caught.exception.code, "observation_unavailable")
            self.assertEqual(len(calls), 3)
        legacy = {"status": {"state": "completed"}, "binding": {}}
        with (
            patch.object(self.control, "inspect", return_value=legacy),
            patch.object(self.control, "_wait_legacy", return_value=legacy) as fallback,
        ):
            self.assertEqual(
                self.control.wait(spec.run_id)["monitoring_mode"], "client_compatibility"
            )
            fallback.assert_called_once()
        broken = {**current, "binding": {"monitoring": {"protocol": "unknown"}}}
        with (
            patch.object(self.control, "inspect", return_value=broken),
            patch.object(
                self.control, "_wait_legacy", side_effect=AssertionError("silent fallback")
            ),
            self.assertRaises(RRCError),
        ):
            self.control.wait(spec.run_id)

    def test_adapter_timeout_reaps_its_child_without_killing_caller(self):
        child_file = self.root / "checker-child.json"
        code = (
            "import subprocess,sys,time,json\nfrom pathlib import Path\n"
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
            f"Path({str(child_file)!r}).write_text(json.dumps({{'pid':p.pid}}))\n"
            "time.sleep(30)\n"
        )
        with self.assertRaises(RRCError) as caught:
            run_adapter((sys.executable, "-c", code), {}, timeout_seconds=1, cwd=self.root)
        self.assertEqual(caught.exception.code, "adapter_timeout")
        pid = load_json(child_file)["pid"]
        from remote_run_control.processes import process_identity

        try:
            self.assertIn(process_identity(pid)["state"], {"Z", "X"})
        except RRCError as exc:
            self.assertEqual(exc.code, "process_missing")

    def test_cli_does_not_report_success_for_an_unfinished_wait(self):
        output = io.StringIO()
        with (
            patch.object(
                cli,
                "Controller",
                return_value=SimpleNamespace(
                    wait=lambda *args, **kwargs: {"status": {"state": "running"}},
                ),
            ),
            contextlib.redirect_stdout(output),
        ):
            code = cli.main(["--json", "wait", "fixture"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
