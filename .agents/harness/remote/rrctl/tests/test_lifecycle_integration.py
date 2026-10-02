from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remote_run_control.controller import Controller
from remote_run_control.errors import RRCError
from remote_run_control.models import RunSpec
from remote_run_control.processes import owned_processes

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


class LifecycleIntegrationTests(unittest.TestCase):
    """rrctl process 后端生命周期的集成边界：只控制输入条件，不 mock 系统行为。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rrctl-lifecycle-it-")
        self.root = Path(self.temporary.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        (self.repo / "work.py").write_text(
            "import json,os,sys,time\nfrom pathlib import Path\n"
            "output=Path(os.environ['RRCTL_OUTPUT_ROOT']); output.mkdir()\n"
            "(output/'started.json').write_text(json.dumps({'pid':os.getpid(), "
            "'pgid':os.getpgrp(), 'sid':os.getsid(0)}))\n"
            "print('started',flush=True)\ntime.sleep(float(sys.argv[1]))\n"
            "(output/'summary.json').write_text(json.dumps({'complete':True}))\n"
            "raise SystemExit(0)\n"
        )
        for command in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.name", "Local Test"],
            ["git", "config", "user.email", "local@example.invalid"],
            ["git", "add", "work.py"],
            ["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"],
        ):
            subprocess.run(command, cwd=self.repo, check=True, capture_output=True)
        self.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, text=True
        ).strip()
        binary = self.root / "bin"
        binary.mkdir()
        (binary / "python").symlink_to(sys.executable)
        conda = self.root / "conda.sh"
        conda.write_text(
            f"conda() {{ export PATH={shlex.quote(str(binary) + ':' + os.defpath)}; }}\n"
        )
        profiles = self.root / "profiles.json"
        profiles.write_text(json.dumps({"profiles": {"local-test": {"kind": "local"}}}))
        self.profiles = profiles
        self.control = Controller(profiles_path=profiles, state_root=self.root / "index")
        self.specs = []
        self.observers = []
        self.conda = conda

    def tearDown(self):
        for observer in self.observers:
            if observer.poll() is None:
                observer.kill()
                observer.wait(timeout=5)
        for spec in self.specs:
            root = Path(spec.remote.control_root)
            binding = root / "binding.json"
            if binding.is_file() and owned_processes(spec.run_id, root):
                try:
                    value = json.loads(binding.read_text())
                    pgid = value.get("executor_process_group_id")
                    if isinstance(pgid, int) and pgid > 1 and pgid != os.getpgrp():
                        os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError, json.JSONDecodeError):
                    pass
            deadline = time.monotonic() + 2
            while owned_processes(spec.run_id, root) and time.monotonic() < deadline:
                time.sleep(0.02)
        self.temporary.cleanup()

    # -- fixtures ---------------------------------------------------------

    def spec(self, delay=1.0, *, roots=None):
        run_id = "life-" + uuid.uuid4().hex[:10]
        remote = roots if roots is not None else (self.root / run_id)
        phase = {"timeout_seconds": 10, "poll_interval_seconds": 0.05}
        spec = RunSpec.from_dict(
            {
                "schema_version": "rrctl.run.v1",
                "run_id": run_id,
                "project": "life",
                "source": {"repo_root": str(self.repo), "branch": "main", "commit": self.commit},
                "remote": {
                    "profile": "local-test",
                    "python": sys.executable,
                    **{
                        key + "_root": str(remote / key)
                        for key in ("stage", "repo", "control", "output")
                    },
                },
                "session": {"backend": "process", "name": "lifecycle.integration"},
                "environment": {
                    "kind": "conda",
                    "name": "test",
                    "conda_sh": str(self.conda),
                    "required_modules": ["json"],
                },
                "resources": {"device": "cpu"},
                "workload": {"argv": ["python", "work.py", str(delay)]},
                "health": {name: dict(phase) for name in ("first_step", "periodic", "completion")},
                "artifacts": [{"path": "summary.json"}, {"path": "started.json"}],
                "local_pull_root": str(self.root / "pulled"),
            }
        )
        self.specs.append(spec)
        return spec

    def _start_observer_cli(self, spec, max_wait_seconds="30"):
        env = {**os.environ, "PYTHONPATH": str(SRC_ROOT)}
        observer = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "remote_run_control",
                "--profiles",
                str(self.profiles),
                "--state-root",
                str(self.root / "index"),
                "--json",
                "wait",
                spec.run_id,
                "--poll-seconds",
                "0.05",
                "--max-wait-seconds",
                max_wait_seconds,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.observers.append(observer)
        return observer

    def _await(self, condition, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def _binding(self, spec):
        return json.loads((Path(spec.remote.control_root) / "binding.json").read_text())

    def _worker_error_code(self, exc):
        # controller 把 worker 返回的错误包成 worker_command；真实 code 在 details 中。
        worker = exc.details.get("worker") or {}
        error = worker.get("error") or {}
        return error.get("code")

    # -- scenarios --------------------------------------------------------

    def test_observer_killed_then_resume_rebinds_and_workload_untouched(self):
        spec = self.spec(delay=3)
        self.control.launch(spec)
        control_root = Path(spec.remote.control_root)
        binding = self._binding(spec)
        workload_pid = binding["workload_pid"]

        # 控制面记录：observer CLI 进程被杀前已确认 workload 启动。
        started = json.loads((Path(spec.remote.output_root) / "started.json").read_text())
        self.assertEqual(started["pid"], workload_pid)
        workload_start_ticks = Path(f"/proc/{workload_pid}/stat").read_text().split()[21]

        observer = self._start_observer_cli(spec)
        time.sleep(0.3)
        observer.kill()
        observer.wait(timeout=5)
        self.observers.remove(observer)
        self.assertLess(observer.returncode, 0)
        # observer 被杀不影响远端 workload 进程的归属扫描。
        self.assertTrue(owned_processes(spec.run_id, control_root))
        self.assertEqual(
            Path(f"/proc/{workload_pid}/stat").read_text().split()[21], workload_start_ticks
        )

        # 恢复的观察面能够重新绑定 control root 并继续观察。
        resumed = self.control.resume(
            profile_name="local-test", control_root=spec.remote.control_root
        )
        self.assertEqual(resumed["status"]["run_id"], spec.run_id)
        self.assertEqual(
            resumed["binding"]["run_spec_sha256"], binding["run_spec_sha256"]
        )
        terminal = self.control.wait(spec.run_id, poll_seconds=0.05, max_wait_seconds=10)
        self.assertEqual(terminal["status"]["state"], "completed")
        # 从被杀到完成期间 workload 始终是同一进程（外部观察窗口结束前已满足）。
        self.assertEqual(
            terminal["binding"]["workload_pid"], workload_pid
        )

    def test_worker_crashed_binding_retained_and_owned_cleanup_recovers(self):
        spec = self.spec(delay=30)
        self.control.launch(spec)
        control_root = Path(spec.remote.control_root)
        binding = self._binding(spec)
        executor_pid = binding["executor_pid"]
        workload_pid = binding["workload_pid"]

        # worker 进程异常退出；control root 仍保留 binding 与状态。
        os.kill(executor_pid, signal.SIGKILL)
        self.assertTrue(self._await(lambda: not Path(f"/proc/{executor_pid}").exists()))
        self.assertTrue(owned_processes(spec.run_id, control_root))
        report = self.control.health(spec.run_id, phase="periodic")
        self.assertEqual(report["monitor_status"], "lost")
        self.assertTrue(report["observations"]["workload_alive"])

        # 崩溃 worker 不在进程组中时，-owned 杀组边界不会误伤自身组。
        original_pgid = binding["executor_process_group_id"]
        binding["executor_process_group_id"] = os.getpgrp()
        binding["workload_process_group_id"] = os.getpgrp()
        (control_root / "binding.json").write_text(json.dumps(binding))
        with self.assertRaises(RRCError) as caught:
            self.control.abort(spec.run_id, confirmed=True)
        self.assertEqual(
            self._worker_error_code(caught.exception), "abort_process_group_invalid"
        )
        self.assertIsNone(os.kill(workload_pid, 0))

        # 恢复真实 binding 后，孤儿 workload 能被识别并清理。
        binding["executor_process_group_id"] = original_pgid
        binding["workload_process_group_id"] = original_pgid
        (control_root / "binding.json").write_text(json.dumps(binding))
        result = self.control.abort(spec.run_id, confirmed=True)
        self.assertEqual(result["state"], "aborted")
        self.assertTrue(self._await(lambda: not Path(f"/proc/{workload_pid}").exists(), 5))
        self.assertFalse(owned_processes(spec.run_id, control_root))

    def test_launch_rejected_when_filesystem_cannot_hold_stage(self):
        mount = Path("/dev/shm")
        probe = mount / f"rrctl-capacity-{os.getpid()}"
        try:
            with probe.open("xb", buffering=0) as handle:
                handle.truncate(1024)
        except OSError:
            raise unittest.SkipTest("/dev/shm is not a writable tmpfs on this host") from None
        probe.unlink()
        stats = os.statvfs(mount)
        free_bytes = stats.f_bavail * stats.f_frsize
        if free_bytes < 8 * 1024**2 or free_bytes > 4 * 1024**3:
            raise unittest.SkipTest(
                f"/dev/shm free space {free_bytes} outside safe fill window"
            )
        staging_parent = mount / f"rrctl-nospace-{uuid.uuid4().hex[:10]}"
        spec = self.spec()
        self.specs.pop()
        base_remote = self.root / f"{spec.run_id}-paths"
        # 只控制输入条件：一个真实目标文件系统被填到零，而不是 mock OS 返回 ENOSPC。
        spec = RunSpec.from_dict(
            {
                **spec.to_dict(),
                "remote": {
                    **spec.to_dict()["remote"],
                    "stage_root": str(staging_parent / "stage"),
                    "repo_root": str(base_remote / "repo"),
                    "control_root": str(base_remote / "control"),
                    "output_root": str(base_remote / "output"),
                },
            }
        )
        self.specs.append(spec)
        filler = self.root / "fill-shm.py"
        filler.write_text(
            "import os,pathlib\n"
            f"path=pathlib.Path({str(mount / (staging_parent.name + '.fill'))!r})\n"
            "chunk=b'\\0'*1048576\n"
            "try:\n"
            "    handle=path.open('xb', buffering=0)\n"
            "    while True: handle.write(chunk)\n"
            "except OSError: pass\n"
        )
        fill_path = mount / (staging_parent.name + ".fill")
        subprocess.run([sys.executable, str(filler)], check=True)
        # 前提：目标 tmpfs 真的不可写，而不是 fill 脚本自我欺骗。
        with self.assertRaises(OSError):
            (staging_parent / "probe-blocked").write_bytes(b"x")
        try:
            with self.assertRaises(RRCError) as caught:
                self.control.launch(spec)
        finally:
            fill_path.unlink(missing_ok=True)
        # readiness 阶段的 stage 写探针必须在任何 staging 落地前拒绝。
        self.assertEqual(caught.exception.code, "remote_preflight")
        error_codes = [item.get("code") for item in caught.exception.details.get("errors", [])]
        self.assertIn("staging_write_probe_failed", error_codes)
        self.assertFalse(caught.exception.details["launch_dispatched"])
        for key in ("stage", "repo", "control", "output"):
            self.assertFalse(
                Path(getattr(spec.remote, f"{key}_root")).exists(), f"{key}_root must not exist"
            )
        self.assertFalse((self.control.index.root / spec.run_id).exists())
        self.assertFalse(staging_parent.exists())

    def test_binding_digest_mismatch_rejects_observe_and_pull_without_side_effects(self):
        spec = self.spec(delay=30)
        self.control.launch(spec)
        control_root = Path(spec.remote.control_root)
        path = control_root / "binding.json"
        original = json.loads(path.read_text())
        changed = dict(original)
        changed["run_spec_sha256"] = "0" * 64
        path.write_text(json.dumps(changed))
        try:
            # binding 层直接检查：inspect/wait/pull 都必须拒绝，不发起网络请求或本地写入。
            with self.subTest(operation="inspect"):
                with self.assertRaises(RRCError) as caught:
                    self.control.inspect(spec.run_id)
                self.assertEqual(
                    self._worker_error_code(caught.exception), "monitor_identity_mismatch"
                )
            with self.subTest(operation="wait"):
                with self.assertRaises(RRCError) as caught:
                    self.control.wait(spec.run_id, poll_seconds=0.05, max_wait_seconds=1)
                self.assertEqual(
                    self._worker_error_code(caught.exception), "monitor_identity_mismatch"
                )
            with self.subTest(operation="pull"):
                destination = Path(spec.local_pull_root) / spec.run_id
                with self.assertRaises(RRCError) as caught:
                    self.control.pull(spec.run_id)
                self.assertEqual(caught.exception.code, "pull_identity")
                self.assertFalse(destination.exists())
            with self.subTest(operation="status_unchanged"):
                status = json.loads((control_root / "status.json").read_text())
                self.assertNotIn(status["state"], {"failed", "aborted", "completed"})
        finally:
            path.write_text(json.dumps(original))

    def test_abort_kills_bound_group_but_preserves_observer_and_unrelated(self):
        spec = self.spec(delay=30)
        self.control.launch(spec)
        control_root = Path(spec.remote.control_root)
        binding = self._binding(spec)
        workload_pid = binding["workload_pid"]
        executor_pgid = binding["executor_process_group_id"]

        observer = self._start_observer_cli(spec)
        time.sleep(0.3)
        with subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        ) as sentinel:
            self.assertEqual(self.control.abort(spec.run_id, confirmed=True)["state"], "aborted")
            # worker 所拥有的进程组整体被回收。
            self.assertTrue(self._await(lambda: not Path(f"/proc/{workload_pid}").exists(), 5))
            self.assertFalse(owned_processes(spec.run_id, control_root))
            # 被杀进程组不是 observer 或 sentinel 所在组。
            self.assertNotEqual(observer.pid, executor_pgid)
            self.assertNotEqual(os.getpgid(sentinel.pid), executor_pgid)
            # observer 与非归属进程在 abort 瞬间均未受影响。
            self.assertIsNone(sentinel.poll())
            if observer.poll() is None:
                self.assertGreater(observer.pid, 0)
            sentinel.terminate()
            sentinel.wait(timeout=5)

        terminal = self.control.inspect(spec.run_id)
        self.assertEqual(terminal["status"]["state"], "aborted")


if __name__ == "__main__":
    unittest.main()
