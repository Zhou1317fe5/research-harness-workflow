"""Daemonless local and OpenSSH transport backends."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import RRCError
from .jsonutil import sha256_file
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

    def run(self, argv: list[str], *, input_data: bytes | None = None) -> CommandResult:
        raise NotImplementedError

    def mkdir_exclusive(self, path: str) -> None:
        raise NotImplementedError

    def upload(self, local_path: Path, remote_path: str, expected_sha256: str) -> None:
        raise NotImplementedError

    def download(self, remote_path: str) -> bytes:
        raise NotImplementedError

    def preflight(self, *, python: str, conda_sh: str) -> dict[str, str]:
        result = self.run(
            [
                "bash",
                "-c",
                '"$1" --version && command -v tmux && test -f "$2"',
                "rrctl-preflight",
                python,
                conda_sh,
            ]
        )
        if result.returncode != 0:
            raise RRCError(
                "remote_preflight",
                "remote Python/tmux/conda preflight failed",
                "transport",
                details={"stderr": result.stderr.decode("utf-8", errors="replace")[-4000:]},
            )
        return {"stdout": result.stdout.decode("utf-8", errors="replace").strip()}


class LocalTransport(Transport):
    def run(self, argv: list[str], *, input_data: bytes | None = None) -> CommandResult:
        result = subprocess.run(argv, input=input_data, check=False, capture_output=True)
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

    def run(self, argv: list[str], *, input_data: bytes | None = None) -> CommandResult:
        base, environment = self._base_command()
        remote_command = shlex.join(argv)
        result = subprocess.run(
            [*base, remote_command],
            input=input_data,
            check=False,
            capture_output=True,
            env=environment,
        )
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
