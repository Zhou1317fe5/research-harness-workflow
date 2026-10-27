"""Daemonless local and OpenSSH transport backends."""

from __future__ import annotations

import json
import math
import os
import selectors
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import RRCError
from .jsonutil import sha256_file
from .models import EnvironmentSpec
from .output_limits import CONTROL_OUTPUT_LIMIT, OutputCapture, run_bounded
from .profiles import Profile
from .security import StreamRedactor, redact, redact_data


def _check_download_size(count: int, expected_size: int | None) -> None:
    if expected_size is not None and count > expected_size:
        raise RRCError("pull_integrity", "artifact exceeds declared size", "artifact",
                       details={"expected_size": expected_size})


def download_timeout_seconds() -> float:
    """Each download gets this wall-clock budget, not a fresh budget per chunk."""
    try:
        value = float(os.environ.get("RRCTL_DOWNLOAD_TIMEOUT_SECONDS", "300"))
    except ValueError as exc:
        raise RRCError("transport_budget", "invalid RRCTL_DOWNLOAD_TIMEOUT_SECONDS", "transport") from exc
    if not math.isfinite(value) or not 0 < value <= 86400:
        raise RRCError(
            "transport_budget",
            "RRCTL_DOWNLOAD_TIMEOUT_SECONDS must be finite and in (0, 86400]",
            "transport",
        )
    return value


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

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        expected_sha256: str,
        *,
        timeout_seconds: float = 300,
    ) -> None:
        raise NotImplementedError

    def download(self, remote_path: str) -> bytes:
        raise NotImplementedError

    def download_to_file(
        self, remote_path: str, destination: Path, *,
        timeout_seconds: float, progress: Callable[[int], None], expected_size: int | None = None,
    ) -> None:
        # Compatibility for small/custom transports that only implement download().
        data = self.download(remote_path)
        _check_download_size(len(data), expected_size)
        destination.write_bytes(data)
        progress(destination.stat().st_size)

    def download_tar_stream(
        self, remote_root: str, relative_paths: list[str], destination_dir: Path, *, timeout_seconds: float = 300
    ) -> bool:
        return False

    def preflight(
        self, *, python: str, environment: EnvironmentSpec, stage_root: str | None = None
    ) -> dict:
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
        request = dict(asdict(environment))
        if stage_root is not None:
            request["stage_root"] = stage_root
        result = self.run(
            [python, "-c", source],
            input_data=json.dumps(request).encode(),
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

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        expected_sha256: str,
        *,
        timeout_seconds: float = 300,
    ) -> None:
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

    def download_to_file(
        self, remote_path: str, destination: Path, *,
        timeout_seconds: float, progress: Callable[[int], None], expected_size: int | None = None,
    ) -> None:
        _validate_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        count = 0
        with Path(remote_path).open("rb") as source, destination.open("xb") as target:
            while chunk := source.read(1024 * 1024):
                _check_download_size(count + len(chunk), expected_size)
                target.write(chunk)
                count += len(chunk)
                progress(count)
                if time.monotonic() >= deadline:
                    raise RRCError("transport_timeout", "artifact download timed out", "artifact",
                                   details={"timeout_seconds": timeout_seconds}, retryable=True)


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
        if command and Path(command[0]).name == "ssh":
            existing_opts = {command[i] for i in range(len(command) - 1) if command[i] == "-o"}
            extra = []
            if not any("ConnectTimeout" in opt for opt in existing_opts):
                extra.extend(["-o", "ConnectTimeout=15"])
            if not any("ServerAliveInterval" in opt for opt in existing_opts):
                extra.extend(["-o", "ServerAliveInterval=15"])
            if not any("ServerAliveCountMax" in opt for opt in existing_opts):
                extra.extend(["-o", "ServerAliveCountMax=3"])
            if not any("TCPKeepAlive" in opt for opt in existing_opts):
                extra.extend(["-o", "TCPKeepAlive=yes"])
            socket_dir = Path.home() / ".ssh" / "sockets"
            try:
                socket_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not any("ControlMaster" in opt for opt in existing_opts):
                    extra.extend([
                        "-o", "ControlMaster=auto",
                        "-o", f"ControlPath={socket_dir}/%r@%h:%p",
                        "-o", "ControlPersist=10m",
                    ])
            except OSError:
                pass
            if extra:
                command = [command[0], *extra, *command[1:]]
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

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        expected_sha256: str,
        *,
        timeout_seconds: float = 300,
    ) -> None:
        size_bytes = local_path.stat().st_size
        size_str = (
            f"{size_bytes / 1024 / 1024:.2f} MB"
            if size_bytes >= 1024 * 1024
            else f"{size_bytes / 1024:.1f} KB"
        )
        sys.stderr.write(f"[rrctl] uploading {local_path.name} ({size_str}) -> {remote_path}...\n")
        sys.stderr.flush()
        t0 = time.monotonic()
        temporary = f"{remote_path}.rrctl-upload"
        # 允许自动清理因网络异常残留的 temporary 碎片，但不覆盖已有的正式 remote_path
        script = (
            "set -eu; umask 077; "
            f"test ! -e {shlex.quote(remote_path)}; "
            f"rm -f {shlex.quote(temporary)}; "
            f"cat > {shlex.quote(temporary)}; "
            f"test \"$(sha256sum {shlex.quote(temporary)} | awk '{{print $1}}')\" = "
            f"{shlex.quote(expected_sha256)}; "
            f"mv {shlex.quote(temporary)} {shlex.quote(remote_path)}"
        )
        max_attempts = 3
        last_error = ""
        payload = local_path.read_bytes()
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                sys.stderr.write(f"[rrctl] retrying upload for {local_path.name} (attempt {attempt}/{max_attempts})...\n")
                sys.stderr.flush()
                time.sleep(1.5)
            result = self.run(
                ["bash", "-c", script],
                input_data=payload,
                timeout_seconds=timeout_seconds,
            )
            if result.returncode == 0:
                elapsed = time.monotonic() - t0
                sys.stderr.write(
                    f"[rrctl] uploaded {local_path.name} ({size_str}) in {elapsed:.1f}s\n"
                )
                sys.stderr.flush()
                return
            last_error = redact(
                result.stderr.decode("utf-8", errors="replace"), self.profile.secret_values
            ).strip()

        raise RRCError(
            "upload_failed",
            f"upload failed for {remote_path} after {max_attempts} attempts: {last_error}",
            "transport",
        )

    def download(self, remote_path: str) -> bytes:
        # 文件传输保留完整二进制 stdout；只有诊断 stderr 使用控制预算。
        base, environment = self._base_command()
        result = _capture_command(
            [*base, shlex.join(["cat", remote_path])],
            input_data=None,
            env=environment,
            timeout_seconds=download_timeout_seconds(),
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

    def download_to_file(
        self, remote_path: str, destination: Path, *,
        timeout_seconds: float, progress: Callable[[int], None], expected_size: int | None = None,
    ) -> None:
        """Stream binary stdout to disk; drain bounded/redacted stderr separately."""
        _validate_timeout(timeout_seconds)
        base, environment = self._base_command()
        stderr = OutputCapture(CONTROL_OUTPUT_LIMIT, StreamRedactor(self.secret_values))
        deadline = time.monotonic() + timeout_seconds
        with destination.open("xb", buffering=0) as target:
            try:
                process = subprocess.Popen(
                    [*base, shlex.join(["cat", "--", remote_path])],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=environment,
                )
            except OSError as exc:
                raise RRCError("transport_spawn", "download executable could not start",
                               "transport", retryable=False) from exc
            try:
                with selectors.DefaultSelector() as selector:
                    os.set_blocking(process.stderr.fileno(), False)
                    selector.register(process.stderr, selectors.EVENT_READ)
                    os.set_blocking(process.stdout.fileno(), False)
                    selector.register(process.stdout, selectors.EVENT_READ)
                    count = 0
                    while selector.get_map() or process.poll() is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RRCError(
                                "transport_timeout", "artifact download timed out; verified files are retained",
                                "artifact", details={"timeout_seconds": timeout_seconds}, retryable=True,
                            )
                        for key, _ in selector.select(min(remaining, 0.25)):
                            try:
                                data = os.read(key.fd, 65536)
                            except BlockingIOError:
                                continue
                            if data:
                                if key.fileobj is process.stdout:
                                    _check_download_size(count + len(data), expected_size)
                                    target.write(data)
                                    count += len(data)
                                else:
                                    stderr.feed(data)
                            else:
                                selector.unregister(key.fileobj)
                        progress(os.fstat(target.fileno()).st_size)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stderr.close()
                process.stdout.close()
                stderr.finish()
            result = CommandResult(process.returncode, b"", stderr.preview())
            _check_connection(result)
            if result.returncode != 0:
                raise RRCError(
                    "download_failed", "artifact download failed",
                    "transport", details={"control_output": result.diagnostics(self.secret_values)},
                    retryable=True,
                )

    def download_tar_stream(
        self,
        remote_root: str,
        relative_paths: list[str],
        destination_dir: Path,
        *,
        timeout_seconds: float = 300,
    ) -> bool:
        """Download multiple files via a single remote tar gzip stream into destination_dir."""
        if not relative_paths:
            return True
        import tarfile
        base, environment = self._base_command()
        quoted_paths = " ".join(shlex.quote(p) for p in relative_paths)
        remote_cmd = f"tar -czf - -C {shlex.quote(remote_root)} -- {quoted_paths}"
        cmd = [*base, remote_cmd]
        deadline = time.monotonic() + timeout_seconds
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
        except OSError:
            return False

        try:
            with tarfile.open(fileobj=process.stdout, mode="r|gz") as tar:
                for member in tar.getmembers():
                    target_path = (destination_dir / member.name).resolve()
                    if not target_path.is_relative_to(destination_dir.resolve()):
                        return False
                tar.extractall(destination_dir)
            stdout, stderr = process.communicate(timeout=max(5.0, deadline - time.monotonic()))
            return process.returncode == 0
        except Exception:
            if process.poll() is None:
                process.kill()
            process.wait()
            return False


def transport_for(profile: Profile) -> Transport:
    if profile.kind == "local":
        return LocalTransport()
    return SSHTransport(profile)


def _validate_timeout(value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise RRCError(
            "transport_budget", "transport timeout must be positive and finite", "transport"
        )
