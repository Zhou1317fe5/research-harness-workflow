"""运行环境与只读导入预检；与控制通道共享输出预算。"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Any

from .output_limits import run_bounded
from .security import StreamRedactor


def make_environment(
    settings: dict[str, Any], extra: dict[str, str] | None = None
) -> dict[str, str]:
    # 只继承用户身份和区域信息；动态库、Python 和 shell 启动钩子由本次运行显式决定。
    env = {
        key: os.environ[key]
        for key in ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ")
        if key in os.environ
    }
    env.update({"PATH": os.defpath, "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1"})
    env.update(settings.get("variables", {}))
    env["RRCTL_CONDA_SH"] = settings["conda_sh"]
    env["RRCTL_CONDA_ENV"] = settings["name"]
    if extra:
        env.update(extra)
    return env


def activation_argv(
    argv: list[str] | tuple[str, ...], *, overrides: dict[str, str] | None = None
) -> list[str]:
    script = 'source "$RRCTL_CONDA_SH" && conda activate "$RRCTL_CONDA_ENV" && exec "$@"'
    # activate.d 也可能写入旧路径；激活后再次执行声明的隔离策略和 GPU 绑定。
    command = ["/usr/bin/env"]
    for key in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME", "BASH_ENV", "ENV"):
        command += ["-u", key]
    values = {"PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1", **(overrides or {})}
    command += [f"{key}={value}" for key, value in values.items()]
    return [
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        script,
        "rrctl-environment",
        *command,
        *argv,
    ]


STAGING_WRITE_PROBE_NAME = ".rrctl-write-probe"

# 通过控制通道在远程 stage 根执行真实 write/fsync 探针，避免只读 preflight 放行
# 最终一定写不下的运行。探针以外来输入条件触发 ENOSPC，不替代磁盘配额检查。
def staging_write_probe(settings: dict[str, Any]) -> dict[str, Any]:
    import hashlib
    import os
    import tempfile
    from pathlib import Path

    stage_root = settings.get("stage_root") if isinstance(settings, dict) else None
    if not isinstance(stage_root, str) or not stage_root:
        return {"ok": False, "errors": [{"code": "staging_probe_invalid"}]}
    stage_path = Path(stage_root)
    if ".." in stage_path.parts:
        return {"ok": False, "errors": [{"code": "staging_probe_invalid"}]}
    path = stage_path / STAGING_WRITE_PROBE_NAME
    # probe 直接在 stage_root 里写入；为了同时在“parent 不存在”的满盘 FS 上给出
    # 可诊断的失败和真正的写探针，探针语义需要 stage parent 已存在或为可创建空链路。
    probe_parent = stage_path
    existed_before = probe_parent.exists()
    parent_chain: list[Path] = []
    if not existed_before:
        walk = probe_parent
        while not walk.exists() and walk != walk.parent:
            parent_chain.append(walk)
            walk = walk.parent
        if not walk.is_dir():
            return {"ok": False, "errors": [{"code": "staging_probe_invalid"}]}
        anchor = walk
        relative_parts = [candidate.name for candidate in reversed(parent_chain)]
        # dirfd 方式逐个创建目录，在已存在 parent 上遇到 ENOSPC 时也能立即失败。

    def _cleanup_created_parents() -> None:
        for candidate in parent_chain:
            try:
                candidate.rmdir()
            except OSError:
                break

    try:
        payload = hashlib.sha256(stage_root.encode()).digest() * 32  # 1 KiB 占位
        if parent_chain:
            fd = os.open(anchor, os.O_RDONLY)
            try:
                for part in relative_parts:
                    os.mkdir(part, dir_fd=fd)
                    next_fd = os.open(part, os.O_RDONLY, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
            finally:
                os.close(fd)
        try:
            with tempfile.NamedTemporaryFile(
                dir=stage_root, prefix=STAGING_WRITE_PROBE_NAME, delete=False
            ) as handle:
                probe_file = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            probe_file.unlink()
        finally:
            _cleanup_created_parents()
        return {"ok": True, "path": str(path)}
    except OSError as exc:
        _cleanup_created_parents()
        leftovers = (stage_path.glob(STAGING_WRITE_PROBE_NAME + "*")
                     if stage_path.is_dir() else [])
        for leftover in leftovers:
            with contextlib.suppress(OSError):
                leftover.unlink()
        _cleanup_created_parents()
        return {
            "ok": False,
            "errors": [
                {
                    "code": "staging_write_probe_failed",
                    "errno": getattr(exc, "errno", None),
                    "type": type(exc).__name__,
                }
            ]
        }
    except Exception as exc:  # 系统性失败不能误装成磁盘满；直接失败更可恢复。
        _cleanup_created_parents()
        return {
            "ok": False,
            "errors": [{"code": "staging_write_probe_error", "type": type(exc).__name__}],
        }


IMPORT_PROBE = r"""
import contextlib, importlib, json, os, sys
request = json.load(sys.stdin)
errors = []
if sys.version_info < (3, 10):
    errors.append({"code": "python_version", "required": ">=3.10"})
with open(os.devnull, "w") as sink:
    for name in request.get("required_modules", []):
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                importlib.import_module(name)
        except Exception as exc:
            errors.append({"code": "import_failed", "module": name,
                           "type": type(exc).__name__, "message": str(exc)[:500]})
result = {"ok": not errors, "python": sys.executable, "version": list(sys.version_info[:3]),
          "required_modules": request.get("required_modules", []), "errors": errors}
print(json.dumps(result, separators=(",", ":")))
raise SystemExit(0 if result["ok"] else 2)
"""


def preflight(settings: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "linux" or not os.path.isfile("/proc/sys/kernel/random/boot_id"):
        return {"ok": False, "errors": [{"code": "linux_process_identity_required"}]}
    if isinstance(settings, dict) and settings.get("stage_root"):
        # 只在 launch 传入 stage_root 时执行写探针；doctor 等调用方不提供
        # stage，这里只保留只读环境探测。
        stage_probe = staging_write_probe(settings)
        if not stage_probe.get("ok"):
            return stage_probe
    try:
        result = run_bounded(
            activation_argv(
                ["python", "-c", IMPORT_PROBE],
                overrides={**settings.get("variables", {}), "CUDA_VISIBLE_DEVICES": ""},
            ),
            input_data=json.dumps(
                {"required_modules": settings.get("required_modules", [])}
            ).encode(),
            env=make_environment(settings),
            timeout_seconds=90,
            redactors=(StreamRedactor(), StreamRedactor()),
        )
    except OSError as exc:
        return {
            "ok": False,
            "errors": [{"code": "environment_probe_failed", "type": type(exc).__name__}],
        }
    if result.timed_out or result.stdout.raw.truncated:
        return {
            "ok": False,
            "errors": [
                {
                    "code": "environment_probe_timeout"
                    if result.timed_out
                    else "environment_output_truncated"
                }
            ],
            "control_output": {
                "stdout": result.stdout.metadata(complete=not result.timed_out),
                "stderr": result.stderr.metadata(complete=not result.timed_out),
                "stderr_tail": result.stderr.preview().decode("utf-8", errors="replace"),
            },
        }
    try:
        value = json.loads(result.stdout.raw.bytes())
    except (ValueError, TypeError):
        return {
            "ok": False,
            "errors": [{"code": "environment_activation_failed", "exit_code": result.returncode}],
            "control_output": {
                "stderr": result.stderr.metadata(complete=True),
                "stderr_tail": result.stderr.preview().decode("utf-8", errors="replace"),
            },
        }
    if result.returncode or not isinstance(value, dict) or value.get("ok") is not True:
        if isinstance(value, dict):
            return {**value, "ok": False}
        return {"ok": False, "errors": [{"code": "environment_probe_invalid"}]}
    if result.stderr.raw.truncated:
        value["control_output"] = {
            "truncated": True,
            "stderr": result.stderr.metadata(complete=True),
        }
    return value


if __name__ == "__main__":
    answer = preflight(json.load(sys.stdin))
    print(json.dumps(answer, separators=(",", ":")))
    raise SystemExit(0 if answer.get("ok") else 2)
