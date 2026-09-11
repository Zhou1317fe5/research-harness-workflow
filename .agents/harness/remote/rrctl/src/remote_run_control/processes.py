"""Linux 进程身份与本次运行归属；不依赖终端服务。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import RRCError
from .jsonutil import sha256_json


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_identity(pid: int) -> dict[str, Any]:
    try:
        root = Path("/proc") / str(pid)
        stat = (root / "stat").read_text()
        command = (root / "cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError) as exc:
        raise RRCError("process_missing", "process no longer exists", "ownership") from exc
    except (PermissionError, OSError) as exc:
        raise RRCError(
            "process_unavailable", "process identity cannot be read", "ownership"
        ) from exc
    _, separator, tail = stat.rpartition(")")
    fields = tail.split()
    if not separator or len(fields) < 20:
        raise RRCError("process_unavailable", "process stat is invalid", "ownership")
    argv = [item.decode("utf-8", errors="replace") for item in command.split(b"\0") if item]
    return {
        "pid": pid,
        "state": fields[0],
        "parent_pid": int(fields[1]),
        "process_group_id": int(fields[2]),
        "session_id": int(fields[3]),
        "start_ticks": int(fields[19]),
        "cmdline_sha256": sha256_json(argv),
        "argv": argv,
    }


def has_run_marker(pid: int, run_id: str, control_root: str | Path) -> bool:
    try:
        values = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (PermissionError, OSError) as exc:
        raise RRCError(
            "process_unavailable", "process run marker cannot be read", "ownership"
        ) from exc
    return (
        f"RRCTL_RUN_ID={run_id}".encode() in values
        and f"RRCTL_CONTROL_ROOT={control_root}".encode() in values
    )


def owned_processes(run_id: str, control_root: str | Path) -> list[dict[str, Any]]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            identity = process_identity(int(entry.name))
            if identity["state"] not in {"Z", "X"} and has_run_marker(
                identity["pid"], run_id, control_root
            ):
                found.append(identity)
        except (OSError, RRCError):
            continue
    return sorted(found, key=lambda item: item["pid"])


def probe_bound_process(
    binding: dict[str, Any], prefix: str, run_id: str, control_root: Path
) -> dict[str, Any]:
    pid = binding.get(f"{prefix}_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {"state": "pending", "pid": None}
    try:
        current = process_identity(pid)
        if current["state"] in {"Z", "X"}:
            return {"state": "missing", "pid": pid}
        if binding.get("boot_id") != boot_id() or current["start_ticks"] != binding.get(
            f"{prefix}_start_ticks"
        ):
            return {"state": "mismatch", "pid": pid}
        if current["process_group_id"] != binding.get(
            f"{prefix}_process_group_id"
        ) or not has_run_marker(pid, run_id, control_root):
            return {"state": "mismatch", "pid": pid}
        # exec 会合法改变 argv；启动身份、会话和运行标识不会随 exec 改变。
        return {
            "state": "alive",
            "pid": pid,
            "command_changed": current["cmdline_sha256"] != binding.get(f"{prefix}_cmdline_sha256"),
        }
    except RRCError as exc:
        return {"state": "missing" if exc.code == "process_missing" else "unavailable", "pid": pid}


def bound_group(
    binding: dict[str, Any], run_id: str, control_root: Path
) -> tuple[int, list[dict[str, Any]]]:
    pgid = binding.get("executor_process_group_id")
    sid = binding.get("executor_session_id")
    executor = binding.get("executor_pid")
    if (
        binding.get("boot_id") != boot_id()
        or not isinstance(pgid, int)
        or pgid <= 1
        or pgid == os.getpgrp()
        or pgid != sid
        or pgid != executor
    ):
        raise RRCError(
            "abort_process_group_invalid",
            "bound independent process session cannot be verified",
            "ownership",
        )
    for prefix in ("executor", "workload"):
        if probe_bound_process(binding, prefix, run_id, control_root)["state"] == "mismatch":
            raise RRCError(
                "abort_identity_mismatch",
                "a bound PID was reused or its ownership changed",
                "ownership",
            )
    members = owned_processes(run_id, control_root)
    if not members:
        raise RRCError("abort_process_missing", "no owned live process remains", "ownership")
    earliest = binding.get("executor_start_ticks")
    if not isinstance(earliest, int) or any(
        item["process_group_id"] != pgid
        or item["session_id"] != sid
        or item["start_ticks"] < earliest
        for item in members
    ):
        raise RRCError(
            "abort_group_mismatch", "owned processes escaped the bound session", "ownership"
        )
    return pgid, members
