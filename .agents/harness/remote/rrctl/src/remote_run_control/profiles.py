"""Local-only credential and transport profiles."""

from __future__ import annotations

import getopt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import RRCError
from .jsonutil import load_json
from .security import SECRET_KEY, contains_secret, secure_file_mode

DEFAULT_PROFILE_PATH = Path.home() / ".config" / "rrctl" / "profiles.json"


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    python: str = "/usr/bin/python3"
    conda_env_var: str = "REMOTE_CONDA_ENV"
    conda_sh_var: str = "REMOTE_CONDA_SH"
    directories: tuple[str, ...] = ("~",)


def _variable_name(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RRCError(
            "profile_environment_reference",
            f"{field_name} must be an environment variable name",
            "credential",
        )
    return value


def _diagnostics(value: Any) -> ConnectionSettings:
    if not isinstance(value, dict):
        raise RRCError("profile_diagnostics", "profile.diagnostics must be an object", "credential")
    python = value.get("python", "/usr/bin/python3")
    if (
        not isinstance(python, str)
        or not python.startswith("/")
        or any(char in python for char in "\0\r\n")
    ):
        raise RRCError(
            "profile_python", "diagnostics.python must be an absolute executable path", "credential"
        )
    directories = value.get("directories", ["~"])
    if not isinstance(directories, list) or any(
        not isinstance(path, str)
        or not (path.startswith("/") or path == "~" or path.startswith("~/"))
        or any(char in path for char in "\0\r\n")
        for path in directories
    ):
        raise RRCError(
            "profile_directories",
            "diagnostics.directories must contain absolute or ~/ paths",
            "credential",
        )
    return ConnectionSettings(
        python=python,
        conda_env_var=_variable_name(
            value.get("conda_env_var", "REMOTE_CONDA_ENV"), "diagnostics.conda_env_var"
        ),
        conda_sh_var=_variable_name(
            value.get("conda_sh_var", "REMOTE_CONDA_SH"), "diagnostics.conda_sh_var"
        ),
        directories=tuple(directories),
    )


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    kind: str
    ssh_argv: tuple[str, ...] = ()
    password_env: str | None = None
    diagnostics: ConnectionSettings = field(default_factory=ConnectionSettings)

    @property
    def secret_values(self) -> tuple[str, ...]:
        if not self.password_env:
            return ()
        value = os.environ.get(self.password_env)
        return (value,) if value else ()


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_PROFILE_PATH

    def load(self, name: str, *, require_credentials: bool = True) -> Profile:
        if not self.path.is_file():
            raise RRCError(
                "profile_file_missing",
                f"profile file does not exist: {self.path}",
                "credential",
            )
        try:
            raw = load_json(self.path)
        except (OSError, ValueError) as exc:
            raise RRCError(
                "profile_file_invalid", "profile file is unreadable or invalid JSON", "credential"
            ) from exc
        profiles = raw.get("profiles") if isinstance(raw, dict) else None
        if not isinstance(profiles, dict) or name not in profiles:
            raise RRCError("profile_missing", f"profile not found: {name}", "credential")
        item = profiles[name]
        if not isinstance(item, dict):
            raise RRCError("profile_invalid", f"profile {name} must be an object", "credential")
        if any(isinstance(key, str) and SECRET_KEY.fullmatch(key) for key in item):
            raise RRCError(
                "profile_inline_credential",
                "profile must reference credential variable names only",
                "credential",
            )
        kind = item.get("kind")
        if kind not in {"ssh", "local"}:
            raise RRCError(
                "profile_kind_invalid",
                f"profile {name} kind must be ssh or local",
                "credential",
            )
        ssh_argv: tuple[str, ...] = ()
        if kind == "ssh":
            value = item.get("ssh_argv")
            if (
                not isinstance(value, list)
                or not value
                or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in value)
            ):
                raise RRCError(
                    "profile_ssh_argv_invalid",
                    f"profile {name}.ssh_argv must be a non-empty string array",
                    "credential",
                )
            ssh_argv = tuple(value)
            try:
                _, destinations = getopt.getopt(
                    value[1:], "46AaCfGgKkMNnqsTtVvXxYyB:b:c:D:E:e:F:I:i:J:L:l:m:O:o:p:R:S:W:w:"
                )
                if Path(value[0]).name != "ssh" or len(destinations) != 1:
                    raise ValueError("expected one SSH destination without a remote command")
            except (getopt.GetoptError, ValueError) as exc:
                raise RRCError(
                    "profile_ssh_argv_invalid",
                    "ssh_argv must contain ssh options and one destination, "
                    "without an appended command",
                    "credential",
                ) from exc
            if contains_secret(" ".join(ssh_argv)):
                raise RRCError(
                    "profile_secret_in_argv",
                    f"profile {name}.ssh_argv must not contain credentials",
                    "credential",
                )
        password_env = item.get("password_env")
        if password_env is not None:
            password_env = _variable_name(password_env, "profile.password_env")
        if password_env and not secure_file_mode(self.path):
            raise RRCError(
                "profile_permissions_insecure",
                f"password-enabled profile file must have mode 0600: {self.path}",
                "credential",
            )
        if require_credentials and password_env and not os.environ.get(password_env):
            raise RRCError(
                "credential_env_missing",
                f"profile {name} requires environment variable {password_env}",
                "credential",
            )
        return Profile(
            name=name,
            kind=kind,
            ssh_argv=ssh_argv,
            password_env=password_env,
            diagnostics=_diagnostics(item.get("diagnostics", {})),
        )


def write_example(path: Path) -> None:
    from .jsonutil import atomic_write_json

    value: dict[str, Any] = {
        "profiles": {
            "local-test": {"kind": "local"},
            "remote-key": {"kind": "ssh", "ssh_argv": ["ssh", "host-alias"]},
            "remote-password": {
                "kind": "ssh",
                "ssh_argv": ["ssh", "host-alias"],
                "password_env": "RRCTL_SSH_PASSWORD",
            },
        }
    }
    atomic_write_json(path, value)
