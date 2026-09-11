"""Daemonless local and OpenSSH transport backends."""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import RRCError
from .jsonutil import sha256_file
from .models import EnvironmentSpec
from .output_limits import CONTROL_OUTPUT_LIMIT, OutputCapture, run_bounded
from .profiles import Profile
from .security import StreamRedactor, redact, redact_data


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    output: dict = field(default_factory=dict)

    def json_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stdout_truncated(self) -> bool:
        return self.output.get("streams", {}).get("stdout", {}).get("truncated", False)

    @property
    def truncated(self) -> bool:
        return self.output.get("truncated", False)

    def diagnostics(self, secret_values: tuple[str, ...] = ()) -> dict:
        if self.output:
            return self.output
        captures = []
        for data in (self.stdout, self.stderr):
            capture = OutputCapture(CONTROL_OUTPUT_LIMIT, StreamRedactor(secret_values))
            capture.feed(data)
            capture.finish()
            captures.append(capture)
        return _output_details(*captures, complete=True)


def _output_details(stdout: OutputCapture, stderr: OutputCapture, *, complete: bool) -> dict:
    return {
        "truncated": stdout.raw.truncated or stderr.raw.truncated,
        "stdout": stdout.preview().decode("utf-8", errors="replace")
        if stdout.raw.limit is not None
        else "",
        "stderr": stderr.preview().decode("utf-8", errors="replace"),
        "streams": {
            "stdout": stdout.metadata(complete=complete),
            "stderr": stderr.metadata(complete=complete),
        },
    }


def _capture_command(
    argv: list[str],
    *,
    input_data: bytes | None,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
    secret_values: tuple[str, ...] = (),
    stdout_limit: int | None = CONTROL_OUTPUT_LIMIT,
) -> CommandResult:
    _validate_timeout(timeout_seconds)
    try:
        captured = run_bounded(
            argv,
            input_data=input_data,
            env=env,
            timeout_seconds=timeout_seconds,
            stdout_limit=stdout_limit,
            redactors=(
                StreamRedactor(secret_values) if stdout_limit is not None else None,
                StreamRedactor(secret_values),
            ),
        )
    except OSError as exc:
        raise RRCError(
            "transport_spawn",
            "control executable could not start; check installed tools and profile",
            "transport",
            details={"type": type(exc).__name__},
            retryable=False,
        ) from exc
    output = _output_details(captured.stdout, captured.stderr, complete=not captured.timed_out)
    if captured.timed_out:
        raise RRCError(
            "transport_timeout",
            "control response timed out; remote outcome is not confirmed",
            "observer",
            details={"remote_state_unknown": True, "control_output": output},
        )
    return CommandResult(
        captured.returncode,
        captured.stdout.preview() if captured.stdout.raw.truncated else captured.stdout.raw.bytes(),
        captured.stderr.preview(),
        output,
    )


def _check_connection(result: CommandResult) -> None:
    if result.returncode == 255 or result.returncode < 0:
        raise RRCError(
            "transport_connection",
            "connection ended before the remote outcome was confirmed",
            "observer",
            details={"remote_state_unknown": True, "control_output": result.diagnostics()},
        )


class Transport:
    last_control_output: dict | None = None
    redaction_values: tuple[str, ...] = ()

    @property
    def secret_values(self) -> tuple[str, ...]:
        return self.redaction_values

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
        # 在内存中加载 canonical 探针及输出预算；不上传文件或创建运行目录。
        sources = {
            name: Path(__file__).with_name(name + ".py").read_text(encoding="utf-8")
            for name in ("errors", "security", "output_limits", "environment")
        }
        source = (
            "import json,sys,types\n"
            "if sys.version_info < (3,10):\n"
            " print(json.dumps({'ok':False,'errors':[{'code':'python_version',"
            "'required':'>=3.10'}]}));sys.exit(2)\n"
            "package=types.ModuleType('_rrctl_preflight');package.__path__=[]\n"
            "sys.modules[package.__name__]=package\n"
            f"for name,source in {sources!r}.items():\n"
            " module=types.ModuleType(package.__name__+'.'+name)\n"
            " module.__package__=package.__name__;sys.modules[module.__name__]=module\n"
            " exec(compile(source,name,'exec'),module.__dict__)\n"
            "answer=module.preflight(json.load(sys.stdin))\n"
            "print(json.dumps(answer,separators=(',',':')))\n"
            "sys.exit(0 if answer.get('ok') else 2)\n"
        )
        result = self.run(
            [python, "-c", source],
            input_data=json.dumps(asdict(environment)).encode(),
        )
        if result.stdout_truncated:
            raise RRCError(
                "control_output_truncated",
                "preflight response exceeded the control output budget",
                "transport",
                details={"control_output": result.diagnostics()},
                retryable=False,
            )
        try:
            value = redact_data(json.loads(result.stdout), self.secret_values)
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
        result = _capture_command(
            argv,
            input_data=input_data,
            timeout_seconds=timeout_seconds,
            secret_values=self.secret_values,
        )
        if result.truncated:
            self.last_control_output = result.diagnostics()
        _check_connection(result)
        return result

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
        return (*self.profile.secret_values, *self.redaction_values)

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
        base, environment = self._base_command()
        remote_command = shlex.join(argv)
        result = _capture_command(
            [*base, remote_command],
            input_data=input_data,
            env=environment,
            timeout_seconds=timeout_seconds,
            secret_values=self.secret_values,
        )
        if result.truncated:
            self.last_control_output = result.diagnostics()
        _check_connection(result)
        return result

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
        # 文件传输保留完整二进制 stdout；只有诊断 stderr 使用控制预算。
        base, environment = self._base_command()
        result = _capture_command(
            [*base, shlex.join(["cat", remote_path])],
            input_data=None,
            env=environment,
            timeout_seconds=180,
            secret_values=self.secret_values,
            stdout_limit=None,
        )
        _check_connection(result)
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
