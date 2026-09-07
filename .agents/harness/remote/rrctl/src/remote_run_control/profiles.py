"""Local-only credential and transport profiles."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RRCError
from .jsonutil import load_json
from .security import contains_secret, secure_file_mode

DEFAULT_PROFILE_PATH = Path.home() / ".config" / "rrctl" / "profiles.json"


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    kind: str
    ssh_argv: tuple[str, ...] = ()
    password_env: str | None = None

    @property
    def secret_values(self) -> tuple[str, ...]:
        if not self.password_env:
            return ()
        value = os.environ.get(self.password_env)
        return (value,) if value else ()


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_PROFILE_PATH

    def load(self, name: str) -> Profile:
        if not self.path.is_file():
            raise RRCError(
                "profile_file_missing",
                f"profile file does not exist: {self.path}",
                "credential",
            )
        raw = load_json(self.path)
        profiles = raw.get("profiles") if isinstance(raw, dict) else None
        if not isinstance(profiles, dict) or name not in profiles:
            raise RRCError("profile_missing", f"profile not found: {name}", "credential")
        item = profiles[name]
        if not isinstance(item, dict):
            raise RRCError("profile_invalid", f"profile {name} must be an object", "credential")
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
                or any(not isinstance(arg, str) or not arg for arg in value)
            ):
                raise RRCError(
                    "profile_ssh_argv_invalid",
                    f"profile {name}.ssh_argv must be a non-empty string array",
                    "credential",
                )
            ssh_argv = tuple(value)
            if contains_secret(" ".join(ssh_argv)):
                raise RRCError(
                    "profile_secret_in_argv",
                    f"profile {name}.ssh_argv must not contain credentials",
                    "credential",
                )
        password_env = item.get("password_env")
        if password_env is not None and (
            not isinstance(password_env, str) or not password_env.strip()
        ):
            raise RRCError(
                "profile_password_env_invalid",
                f"profile {name}.password_env must be non-empty text",
                "credential",
            )
        if password_env and not secure_file_mode(self.path):
            raise RRCError(
                "profile_permissions_insecure",
                f"password-enabled profile file must have mode 0600: {self.path}",
                "credential",
            )
        if password_env and not os.environ.get(password_env):
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
