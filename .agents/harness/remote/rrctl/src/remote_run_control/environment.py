"""运行环境与无副作用的导入预检；本文件可独立经 SSH 执行。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any


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


IMPORT_PROBE = r"""
import contextlib, importlib, io, json, os, sys
request = json.load(sys.stdin)
errors = []
if sys.version_info < (3, 10):
    errors.append({"code": "python_version", "required": ">=3.10"})
for name in request.get("required_modules", []):
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
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
    try:
        result = subprocess.run(
            activation_argv(
                ["python", "-c", IMPORT_PROBE],
                overrides={**settings.get("variables", {}), "CUDA_VISIBLE_DEVICES": ""},
            ),
            input=json.dumps({"required_modules": settings.get("required_modules", [])}),
            capture_output=True,
            text=True,
            env=make_environment(settings),
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "errors": [{"code": "environment_probe_failed", "type": type(exc).__name__}],
        }
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        return {
            "ok": False,
            "errors": [{"code": "environment_activation_failed", "exit_code": result.returncode}],
        }
    if result.returncode or not isinstance(value, dict) or value.get("ok") is not True:
        return (
            value
            if isinstance(value, dict)
            else {"ok": False, "errors": [{"code": "environment_probe_invalid"}]}
        )
    return value


if __name__ == "__main__":
    answer = preflight(json.load(sys.stdin))
    print(json.dumps(answer, separators=(",", ":")))
    raise SystemExit(0 if answer.get("ok") else 2)
