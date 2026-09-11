"""Daemonless local and OpenSSH transport backends."""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import RRCError
from .jsonutil import sha256_file
from .models import EnvironmentSpec
from .profiles import Profile
from .security import redact


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    def json_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


class Transport:
    @property
    def secret_values(self) -> tuple[str, ...]:
        return ()

    def run(
        self, argv: list[str], *, input_data: bytes | None = None, timeout_seconds: float = 180
    ) -> CommandResult:
        raise NotImplementedError

    def mkdir_exclusive(self, path: str) -> None:
        raise NotImplementedError

    def upload(self, local_path: Path, remote_path: str, expected_sha256: str) -> None:
        raise NotImplementedError

    def download(self, remote_path: str) -> bytes:
        raise NotImplementedError

    def preflight(self, *, python: str, environment: EnvironmentSpec) -> dict:
        # 直接执行同一份标准库实现，避免 SSH 和 worker 各维护一套环境规则。
        source = Path(__file__).with_name("environment.py").read_text(encoding="utf-8")
        result = self.run(
            [python, "-c", source],
            input_data=json.dumps(asdict(environment)).encode(),
        )
        try:
            value = json.loads(result.stdout)
        except ValueError:
            value = {"ok": False, "errors": [{"code": "bootstrap_python_failed"}]}
        if result.returncode != 0 or not isinstance(value, dict) or value.get("ok") is not True:
            raise RRCError(
                "remote_preflight",
                "selected Conda environment failed its Python/import preflight",
                "transport",
                details=value if isinstance(value, dict) else {},
            )
        return value


class LocalTransport(Transport):
    def run(
        self, argv: list[str], *, input_data: bytes | None = None, timeout_seconds: float = 180
    ) -> CommandResult:
        _validate_timeout(timeout_seconds)
        try:
            result = subprocess.run(
                argv, input=input_data, check=False, capture_output=True, timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            raise RRCError(
                "transport_timeout",
                "control command timed out; detached workload ownership is unchanged",
                "observer",
                details={"remote_state_unknown": True},
            ) from exc
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def mkdir_exclusive(self, path: str) -> None:
        try:
            Path(path).mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise RRCError(
                "stage_collision", f"stage directory already exists: {path}", "collision"
            ) from exc

    def upload(self, local_path: Path, remote_path: str, expected_sha256: str) -> None:
        destination = Path(remote_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.rrctl-upload")
        if destination.exists() or temporary.exists():
            raise RRCError(
                "upload_collision", f"upload destination already exists: {destination}", "transport"
            )
        shutil.copyfile(local_path, temporary)
        temporary.chmod(0o600)
        actual = sha256_file(temporary)
        if actual != expected_sha256:
            temporary.unlink(missing_ok=True)
            raise RRCError(
                "upload_sha_mismatch",
                f"uploaded file SHA mismatch: {remote_path}",
                "transport",
            )
        os.replace(temporary, destination)

    def download(self, remote_path: str) -> bytes:
        try:
            return Path(remote_path).read_bytes()
        except OSError as exc:
            raise RRCError(
                "download_failed", f"cannot read remote path: {remote_path}", "transport"
            ) from exc


class SSHTransport(Transport):
    def __init__(self, profile: Profile):
        if profile.kind != "ssh":
            raise ValueError("SSHTransport requires an ssh profile")
        self.profile = profile

    @property
    def secret_values(self) -> tuple[str, ...]:
        return self.profile.secret_values

    def _base_command(self) -> tuple[list[str], dict[str, str]]:
        environment = os.environ.copy()
        command = list(self.profile.ssh_argv)
        if self.profile.password_env:
            password = environment[self.profile.password_env]
            environment["SSHPASS"] = password
            command = ["sshpass", "-e", *command]
        return command, environment

    def run(
        self, argv: list[str], *, input_data: bytes | None = None, timeout_seconds: float = 180
    ) -> CommandResult:
        _validate_timeout(timeout_seconds)
        base, environment = self._base_command()
        remote_command = shlex.join(argv)
        try:
            result = subprocess.run(
                [*base, remote_command],
                input=input_data,
                check=False,
                capture_output=True,
                env=environment,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RRCError(
                "transport_timeout",
                "SSH control command timed out; detached workload ownership is unchanged",
                "observer",
                details={"remote_state_unknown": True},
            ) from exc
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def mkdir_exclusive(self, path: str) -> None:
        result = self.run(
            [
                "bash",
                "-c",
                (
                    'set -eu; parent=$(dirname -- "$1"); '
                    'mkdir -p -m 700 -- "$parent"; '
                    'mkdir -m 700 -- "$1"'
                ),
                "rrctl-mkdir-exclusive",
                path,
            ]
        )
        if result.returncode != 0:
            message = redact(
                result.stderr.decode("utf-8", errors="replace"), self.profile.secret_values
            )
            raise RRCError(
                "stage_collision",
                f"failed to create exclusive stage directory {path}: {message.strip()}",
                "collision",
            )

    def upload(self, local_path: Path, remote_path: str, expected_sha256: str) -> None:
        temporary = f"{remote_path}.rrctl-upload"
        script = (
            "set -eu; umask 077; "
            f"test ! -e {shlex.quote(remote_path)}; test ! -e {shlex.quote(temporary)}; "
            f"cat > {shlex.quote(temporary)}; "
            f"test \"$(sha256sum {shlex.quote(temporary)} | awk '{{print $1}}')\" = "
            f"{shlex.quote(expected_sha256)}; "
            f"mv {shlex.quote(temporary)} {shlex.quote(remote_path)}"
        )
        result = self.run(["bash", "-c", script], input_data=local_path.read_bytes())
        if result.returncode != 0:
            message = redact(
                result.stderr.decode("utf-8", errors="replace"), self.profile.secret_values
            )
            raise RRCError(
                "upload_failed",
                f"upload failed for {remote_path}: {message.strip()}",
                "transport",
            )

    def download(self, remote_path: str) -> bytes:
        result = self.run(["cat", remote_path])
        if result.returncode != 0:
            message = redact(
                result.stderr.decode("utf-8", errors="replace"), self.profile.secret_values
            )
            raise RRCError(
                "download_failed",
                f"download failed for {remote_path}: {message.strip()}",
                "transport",
            )
        return result.stdout


def transport_for(profile: Profile) -> Transport:
    if profile.kind == "local":
        return LocalTransport()
    return SSHTransport(profile)


def _validate_timeout(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise RRCError(
            "transport_budget", "transport timeout must be positive and finite", "transport"
        )
