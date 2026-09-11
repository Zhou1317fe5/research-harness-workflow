"""只读连接诊断；不创建 RunID，不上传、启动或停止任务。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .errors import RRCError
from .models import EnvironmentSpec
from .profiles import ProfileStore
from .security import secure_file_mode
from .transport import transport_for

_PYTHON_AND_PATHS = r"""
import json,os,sys
request=json.load(sys.stdin)
paths=[os.path.expanduser(value) for value in request['directories']]
checks=[{'index':i,'accessible':os.path.isdir(path) and os.access(path,os.R_OK|os.X_OK)}
        for i,path in enumerate(paths)]
print(json.dumps({'python_version':list(sys.version_info[:3]),'directories':checks}))
"""


def connection_doctor(
    store: ProfileStore,
    profile_name: str,
    *,
    env_file: Path | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    result = {
        "profile": profile_name,
        "checks": checks,
        "connection_checked": False,
        "environment_checked": False,
        "server_health_checked": False,
    }

    def add(check_name: str, passed: bool | None, code: str, hint: str = "", **details):
        checks.append(
            {
                "check": check_name,
                "status": "skipped" if passed is None else "passed" if passed else "failed",
                "code": code,
                **({"hint": hint} if hint else {}),
                **details,
            }
        )

    def finish():
        result["ok"] = not any(item["status"] == "failed" for item in checks)
        return result

    try:
        profile = store.load(profile_name, require_credentials=False)
    except RRCError as exc:
        add(
            "profile",
            False,
            exc.code,
            "Check the named profile and its credential variable references.",
        )
        return finish()
    add("profile", True, "profile_valid")
    add(
        "profile_permissions",
        True,
        "profile_permissions_valid",
        private_required=bool(profile.password_env),
    )
    checked_env = env_file or store.path.parent / ".env"
    try:
        if checked_env.is_file():
            private = secure_file_mode(checked_env)
            add(
                "env_file_permissions",
                private,
                "private_file" if private else "env_file_permissions_insecure",
                "Restrict the env file to its owner; doctor does not read or source it.",
            )
        else:
            add(
                "env_file_permissions",
                False if env_file is not None else None,
                "env_file_missing",
                "Supply variables through the calling environment; "
                "an explicit env file must exist.",
            )
    except OSError:
        add(
            "env_file_permissions",
            False,
            "env_file_unreadable",
            "Check env file metadata permissions.",
        )
    if profile.kind == "ssh":
        for program in (profile.ssh_argv[0], *(["sshpass"] if profile.password_env else [])):
            present = shutil.which(program) is not None
            add(
                "tool:" + Path(program).name,
                present,
                "tool_available" if present else "tool_missing",
                "Install the required SSH executable in the calling environment.",
            )
    settings = profile.diagnostics
    variables = [settings.conda_env_var, settings.conda_sh_var]
    if profile.password_env:
        variables.insert(0, profile.password_env)
    for name in dict.fromkeys(variables):
        supplied = bool(os.environ.get(name))
        add(
            "environment_variable",
            supplied,
            "variable_set" if supplied else "variable_unset",
            "Load the named variable into the calling environment before retrying doctor.",
            name=name,
            state="set" if supplied else "unset",
        )
    conda_name = os.environ.get(settings.conda_env_var, "")
    conda_sh = os.environ.get(settings.conda_sh_var, "")
    valid_environment = bool(conda_name and conda_sh.startswith("/")) and not any(
        char in conda_name + conda_sh for char in "\0\r\n"
    )
    add(
        "environment_references",
        valid_environment,
        "environment_references_valid" if valid_environment else "environment_references_invalid",
        "Provide a nonempty Conda environment and an absolute initialization path "
        "through the configured variable names.",
    )
    if offline or not finish()["ok"]:
        add("remote", None, "offline" if offline else "local_checks_failed")
        return finish()
    transport = transport_for(profile)
    transport.redaction_values = (conda_name, conda_sh)

    def parse(reply):
        if reply.stdout_truncated:
            raise RRCError(
                "control_output_truncated", "diagnostic response exceeded its budget", "transport"
            )
        try:
            value = json.loads(reply.stdout)
        except ValueError as exc:
            raise RRCError(
                "doctor_response_invalid", "diagnostic response is not valid JSON", "transport"
            ) from exc
        if reply.returncode or not isinstance(value, dict):
            raise RRCError(
                "doctor_remote_failed", "read-only diagnostic command failed", "transport"
            )
        return value

    try:
        login = parse(
            transport.run(
                [
                    "/bin/sh",
                    "-c",
                    'if [ -x "$1" ] && [ ! -d "$1" ]; then '
                    "printf '{\"python_executable\":true}\\n'; "
                    "else printf '{\"python_executable\":false}\\n'; fi",
                    "rrctl-doctor",
                    settings.python,
                ],
                timeout_seconds=30,
            )
        )
        result["connection_checked"] = True
        add("connection", True, "connection_available")
        executable = login.get("python_executable") is True
        add(
            "remote_python_executable",
            executable,
            "python_executable" if executable else "python_not_executable",
            "Set diagnostics.python to an existing absolute executable path.",
        )
        if not executable:
            return finish()
        remote = parse(
            transport.run(
                [settings.python, "-c", _PYTHON_AND_PATHS],
                input_data=json.dumps({"directories": settings.directories}).encode(),
                timeout_seconds=30,
            )
        )
        version = remote.get("python_version")
        valid_python = (
            isinstance(version, list)
            and len(version) == 3
            and all(isinstance(part, int) for part in version)
            and version >= [3, 10, 0]
        )
        add(
            "remote_python_version",
            valid_python,
            "python_supported" if valid_python else "python_version_unsupported",
            "Use Python 3.10 or newer for the remote control process.",
            version=version if valid_python else None,
        )
        directories = remote.get("directories")
        if not isinstance(directories, list) or len(directories) != len(settings.directories):
            raise RRCError(
                "doctor_response_invalid", "directory diagnostic response is invalid", "transport"
            )
        for index, entry in enumerate(directories):
            accessible = (
                isinstance(entry, dict)
                and entry.get("index") == index
                and entry.get("accessible") is True
            )
            add(
                f"directory:{index}",
                accessible,
                "directory_accessible" if accessible else "directory_inaccessible",
                f"Check diagnostics.directories[{index}] and its read/traverse permissions.",
            )
        if not valid_python:
            return finish()
        result["environment_checked"] = True
        environment = transport.preflight(
            python=settings.python,
            environment=EnvironmentSpec(
                kind="conda",
                name=conda_name,
                conda_sh=conda_sh,
            ),
        )
        add("conda_environment", True, "conda_available", version=environment.get("version"))
        if environment.get("control_output"):
            result["environment_output"] = environment["control_output"]
    except RRCError as exc:
        add(
            "conda_environment" if result["environment_checked"] else "remote",
            False,
            exc.code,
            "Check SSH connectivity, the configured Python path and the named Conda variables.",
        )
        if exc.details.get("control_output"):
            result["control_output"] = exc.details["control_output"]
        if isinstance(exc.details.get("errors"), list):
            result["remote_error_codes"] = [
                item.get("code") for item in exc.details["errors"] if isinstance(item, dict)
            ]
    if transport.last_control_output:
        result["control_output"] = transport.last_control_output
    return finish()
