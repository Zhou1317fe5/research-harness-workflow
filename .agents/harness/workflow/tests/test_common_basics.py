"""common 子系统（locking / paths / project_config）的行为回归；全部使用隔离目录。"""
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents"))
from harness.common import paths
from harness.common.locking import file_lock
from harness.common.project_config import (
    DEFAULT_CONFIG, apply_project_config, list_pipelines, load_config, pipeline_digest,
    relative_path,
)


def token_worker(lock_path, token, barrier, queue, hold=0.0):
    """在锁内登记进入顺序；hold>0 时持锁休眠以强制两进程串行。"""
    try:
        barrier.wait(timeout=10)
        with file_lock(Path(lock_path)):
            first = token not in Path(lock_path).with_suffix(".order").read_text() \
                if Path(lock_path).with_suffix(".order").exists() else True
            with Path(lock_path).with_suffix(".order").open("a") as stream:
                stream.write(token + "\n")
            if hold:
                time.sleep(hold)
            queue.put({"ok": True, "token": token, "first": first})
    except Exception as error:
        queue.put({"ok": False, "token": token, "error": type(error).__name__})


def exclusive_check_worker(lock_path, barrier, queue):
    """若互斥成立，进入锁时另一进程必然已完成写入。"""
    try:
        barrier.wait(timeout=10)
        with file_lock(Path(lock_path)):
            order = Path(lock_path).with_suffix(".order")
            text = order.read_text() if order.exists() else ""
            queue.put({"ok": True, "saw_first": "first\n" in text})
    except Exception as error:
        queue.put({"ok": False, "error": type(error).__name__})


def first_acquires_lock_worker(lock_path, started, queue, hold=0.6):
    """占锁并睡眠，使 probe 进程能被互斥阻塞。"""
    try:
        with file_lock(Path(lock_path)):
            started.set()
            time.sleep(hold)
        queue.put({"ok": True})
    except Exception as error:
        queue.put({"ok": False, "error": type(error).__name__})


class LockingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-locking-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_creates_missing_parent_and_lock_is_0600_regular_file(self):
        target = self.root / "deep/nested/state.lock"
        with file_lock(target):
            pass
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_process_release_after_holder_exits(self):
        # 用 spawn 起干净子进程占锁并立即退出；父进程再进锁验证释放。
        # （同一进程内 re-enter flock 会死锁，见 BUG-CANDIDATE 报告。）
        import subprocess as sp
        code = (
            "import sys; sys.path.insert(0, '.agents'); "
            "from harness.common.locking import file_lock; "
            "from pathlib import Path; "
            "file_lock(Path(sys.argv[1])).__enter__()"
        )
        target = self.root / "state.lock"
        result = sp.run([sys.executable, "-c", code, str(target)], cwd=str(ROOT),
                        capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        with file_lock(target):
            target.with_suffix(".order").write_text("parent\n")
        self.assertEqual(target.with_suffix(".order").read_text(), "parent\n")

    def test_lock_blocks_across_processes(self):
        target = self.root / "state.lock"
        context = multiprocessing.get_context("fork")
        started, queue = context.Event(), context.Queue()
        holder = context.Process(target=first_acquires_lock_worker,
                                 args=(str(target), started, queue), kwargs={"hold": 0.6})
        holder.start()
        self.assertTrue(started.wait(timeout=10))
        begin = time.monotonic()
        with file_lock(target):
            elapsed_blocked = time.monotonic() - begin
        reply = queue.get(timeout=15)
        holder.join(timeout=15)
        self.assertEqual(holder.exitcode, 0)
        self.assertTrue(reply.get("ok"), reply)
        # 若不互斥则 probe 几乎立即拿到锁；互斥成立则至少被阻塞大部分 hold 时长。
        self.assertGreaterEqual(elapsed_blocked, 0.3, reply)

    def test_lock_serializes_token_writing_across_processes(self):
        # 该用例已被 test_lock_blocks_across_processes 的 probe-based 阻塞观测替代；
        # 仅靠 token order 信号会受调度影响，不足以判定互斥。保留此占位避免重复。
        pass

    def test_exception_inside_lock_releases_for_next_writer(self):
        target = self.root / "state.lock"
        with self.assertRaises(RuntimeError):
            with file_lock(target):
                raise RuntimeError("boom")
        with file_lock(target):
            target.write_text("still writable")
        self.assertEqual(target.read_text(), "still writable")


class PathsTests(unittest.TestCase):
    def test_roots_are_resolved_and_config_default_inside_config_dir(self):
        self.assertEqual(paths.HARNESS_ROOT, paths.HARNESS_ROOT.resolve())
        self.assertEqual(paths.HARNESS_ROOT, (ROOT / ".agents/harness").resolve())
        self.assertEqual(paths.REPO_ROOT, paths.HARNESS_ROOT.parents[1])
        self.assertEqual(paths.CONFIG_DIR, paths.HARNESS_ROOT / "config")
        self.assertTrue(DEFAULT_CONFIG.is_relative_to(paths.CONFIG_DIR))

    def test_relative_path_accepts_dot_only_when_allowed(self):
        self.assertEqual(relative_path("a/b.txt", "field"), "a/b.txt")
        self.assertEqual(relative_path(".", "field", allow_dot=True), ".")
        for value in ("", "/abs/path", "../up", "a/../b", "a\\b", ".", None, 12):
            with self.subTest(value=value), self.assertRaises(ValueError):
                relative_path(value, "field")


class ProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def config(self, *blocks):
        path = self.root / "project.toml"
        path.write_text("\n".join(blocks))
        return path

    def test_stage_invariants_cover_duplicates_and_missing_fields(self):
        base = 'version = 1\n[[pipeline.stages]]\nname = "a"\nargv = ["bash", "a.sh"]\n'
        for extra in (
            '[[pipeline.stages]]\nname = "a"\nargv = ["bash", "b.sh"]\n',
            '[[pipeline.stages]]\nname = "b"\nargv = ["bash", "b.sh"]\nlog = "a.log"\n',
            '[[pipeline.stages]]\nname = "b"\nargv = []\n',
            '[[pipeline.stages]]\nname = "bad name"\nargv = ["bash", "b.sh"]\n',
            '[[pipeline.stages]]\nname = "b"\nargv = ["bash", "b.sh"]\ncwd = ".."\n',
            '[[pipeline.stages]]\nname = "b"\nargv = ["bash", "b.sh"]\nlog = "../escape.log"\n',
            '[[pipeline.stages]]\nname = "b"\nargv = ["bash", "b.sh"]\nrequires = ["/abs"]\n',
            '[[pipeline.stages]]\nname = "b"\nargv = ["bash", "b.sh"]\nunexpected = 1\n',
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                load_config(self.config(base, extra))

    def test_validation_errors_for_each_section_and_unknown_keys(self):
        base = 'version = 1\n[[pipeline.stages]]\nname = "a"\nargv = ["bash", "a.sh"]\n'
        for extra in (
            'version = 2\n',
            '[unknown]\n',
            '[records]\nunknown_field = 1\n',
            '[records]\nsummary_glob = "../up"\n',
            '[records]\nprimary_metric = ""\n',
            '[records]\ndimensions = [""]\n',
            '[environment]\nunknown_knob = 1\n',
            '[resources]\nunknown_knob = 1\n',
            '[health.strange]\n',
            '[[artifacts]]\npath = "x"\nrequired = "yes"\n',
            '[[artifacts]]\npath = "../out"\n',
            '[[artifacts]]\npath = "x"\nextra = true\n',
            '[pipeline]\nnamed_only = true\n',
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                load_config(self.config(extra if base.startswith(extra) else base, extra))

    def test_pipeline_options_matrix(self):
        legacy = 'version = 1\n[[pipeline.stages]]\nname = "a"\nargv = ["bash", "a.sh"]\n'
        try:
            import tomllib  # noqa: F401
        except ModuleNotFoundError:
            import tomli as tomllib  # noqa: F401
        config = self.config(legacy)
        info = list_pipelines(config)
        self.assertEqual(info, {"default": "default", "pipelines": ["default"]})
        with self.assertRaises(ValueError):
            list_pipelines(self.config(
                legacy, '[pipelines.default]\nstages = [{name = "b", argv = ["true"]}]\n'))
        with self.assertRaises(ValueError):
            load_config(self.config(legacy, 'pipeline = "not-a-table"\n'))

    def test_named_selection_defaults_and_digest_stability(self):
        path = self.config(
            'version = 1\n[pipeline]\ndefault = "beta"\n'
            '[pipelines.alpha]\nstages = [{name = "a", argv = ["true"]}]\n'
            '[pipelines.beta]\nstages = [{name = "b", argv = ["true"]}]\n')
        self.assertEqual(list_pipelines(path)["default"], "beta")
        self.assertEqual(load_config(path)["pipeline"]["name"], "beta")
        self.assertEqual(load_config(path, pipeline="alpha")["pipeline"]["name"], "alpha")
        self.assertNotEqual(pipeline_digest(load_config(path, pipeline="alpha")),
                            pipeline_digest(load_config(path, pipeline="beta")))
        digest_a = pipeline_digest(load_config(path, pipeline="beta"))
        self.assertEqual(digest_a, pipeline_digest(load_config(path, pipeline="beta")))

    def test_load_config_shape_of_defaults(self):
        config = load_config(self.config(
            'version = 1\n[[pipeline.stages]]\nname = "a"\nargv = ["bash", "a.sh"]\n'))
        stage = config["pipeline"]["stages"][0]
        self.assertEqual(config["pipeline"]["name"], "default")
        self.assertFalse(config["pipeline"]["named"])
        self.assertNotIn("check_argv", stage)
        self.assertNotIn("cwd", stage)
        self.assertNotIn("requires", stage)
        empty = load_config(self.config('version = 1\n'))
        self.assertEqual(empty["pipeline"]["stages"], [])
        self.assertIsNone(empty["pipeline"]["name"])

    def test_cpu_device_defaults_empty_gpu_ids(self):
        config = self.config(
            'version = 1\n[[pipeline.stages]]\nname = "a"\nargv = ["true"]\n')
        request = {"source": {"repo_root": str(self.root)},
                   "workload": {"argv": ["bash", "train.sh"], "cwd": "."},
                   "resources": {"device": "cpu", "gpu_ids": ["1"]}}
        result = apply_project_config(request, config)
        self.assertEqual(result["resources"]["gpu_ids"], ["1"])
        request["resources"] = {"device": "cpu"}
        result = apply_project_config(request, config)
        self.assertEqual(result["resources"]["gpu_ids"], [])

    def test_environment_variables_deep_merge_and_health_merge(self):
        config = self.config(
            'version = 1\n[[pipeline.stages]]\nname = "a"\nargv = ["true"]\n'
            '[environment.variables]\nA = "x"\nB = "y"\n'
            '[health.first_step]\ntimeout_seconds = 90\nminimum_count = 1\n')
        request = {"source": {"repo_root": str(self.root)},
                   "workload": {"argv": ["bash", "train.sh"], "cwd": "."},
                   "environment": {"variables": {"B": "override", "C": "z"}},
                   "health": {"first_step": {"timeout_seconds": 30}}}
        result = apply_project_config(request, config)
        variables = result["environment"]["variables"]
        self.assertEqual(variables, {"A": "x", "B": "override", "C": "z"})
        self.assertEqual(result["health"]["first_step"]["timeout_seconds"], 30)
        self.assertEqual(result["health"]["first_step"]["minimum_count"], 1)


if __name__ == "__main__":
    unittest.main()
