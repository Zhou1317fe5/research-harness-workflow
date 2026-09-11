"""同一远端用户的 GPU 占用登记；生命周期与活进程绑定。"""

from __future__ import annotations

import csv
import fcntl
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import RRCError
from .jsonutil import atomic_write_json, sha256_json, utc_now
from .models import RunSpec
from .processes import boot_id, owned_processes


def _query(fields: str, *, applications: bool = False) -> list[list[str]]:
    option = "--query-compute-apps" if applications else "--query-gpu"
    try:
        result = subprocess.run(
            ["nvidia-smi", f"{option}={fields}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": os.defpath, "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RRCError("gpu_probe_failed", "GPU inventory is unavailable", "resources") from exc
    if result.returncode:
        raise RRCError("gpu_probe_failed", "GPU inventory command failed", "resources")
    return [
        [field.strip() for field in row] for row in csv.reader(result.stdout.splitlines()) if row
    ]


def available_devices(spec: RunSpec) -> list[str]:
    resources = spec.resources
    if resources is None:
        raise RRCError(
            "resources_missing", "new runs require an explicit resources declaration", "resources"
        )
    if resources.device == "cpu":
        return []
    inventory = {}
    aliases = {}
    try:
        for index, uuid, free in _query("index,uuid,memory.free"):
            inventory[uuid] = float(free)
            aliases[index] = uuid
            aliases[uuid] = uuid
    except (ValueError, TypeError) as exc:
        raise RRCError(
            "gpu_inventory_invalid",
            "GPU inventory did not contain usable memory values",
            "resources",
        ) from exc
    if not inventory:
        raise RRCError("gpu_unavailable", "no GPU is available", "resources")
    requested = list(resources.gpu_ids) or list(inventory)
    if any(item not in aliases for item in requested):
        raise RRCError("gpu_unknown", "requested GPU index/UUID does not exist", "resources")
    selected = [aliases[item] for item in requested]
    if len(set(selected)) != len(selected):
        raise RRCError("gpu_duplicate", "GPU aliases refer to the same device", "resources")
    busy = []
    for row in _query("gpu_uuid,pid", applications=True):
        if len(row) != 2:
            raise RRCError("gpu_inventory_invalid", "GPU process inventory is invalid", "resources")
        if row[0] in selected:
            busy.append({"gpu_uuid": row[0], "pid": row[1]})
    low = [uuid for uuid in selected if inventory[uuid] < resources.minimum_free_mib]
    if busy or low:
        raise RRCError(
            "gpu_busy",
            "requested GPUs are occupied or below the free-memory budget",
            "resources",
            details={
                "processes": busy,
                "below_memory_budget": low,
                "minimum_free_mib": resources.minimum_free_mib,
            },
        )
    return selected


@contextmanager
def _locked(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _active(record: dict[str, Any]) -> bool:
    if record.get("boot_id") != boot_id():
        return False
    return bool(owned_processes(record["run_id"], record["control_root"]))


def acquire(spec: RunSpec, control_root: Path, *, lease_root: Path | None = None) -> dict[str, Any]:
    root = lease_root or Path.home() / ".local/state/rrctl/gpu-leases"
    if spec.resources is not None and spec.resources.device == "cpu":
        return {"device": "cpu", "gpu_ids": [], "lease_path": None}
    with _locked(root):
        live = []
        for path in sorted(root.glob("*.json")):
            try:
                record = json.loads(path.read_text())
                if (
                    not isinstance(record, dict)
                    or record.get("schema_version") != "rrctl.gpu-lease.v1"
                    or not isinstance(record.get("run_id"), str)
                    or not isinstance(record.get("control_root"), str)
                    or not Path(record["control_root"]).is_absolute()
                    or not isinstance(record.get("boot_id"), str)
                    or not isinstance(record.get("gpu_ids"), list)
                    or not record["gpu_ids"]
                    or any(not isinstance(item, str) or not item for item in record["gpu_ids"])
                ):
                    raise ValueError("invalid lease schema")
                active = _active(record)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RRCError(
                    "gpu_lease_invalid",
                    "GPU lease registry requires repair",
                    "resources",
                    details={"path": str(path)},
                ) from exc
            if active:
                live.append(record)
            else:
                path.unlink()
        # 空 GPU 列表代表整机独占；显式 UUID/索引在真实库存中统一规范化。
        selected = available_devices(spec)
        if any(set(record["gpu_ids"]) & set(selected) for record in live):
            raise RRCError(
                "gpu_lease_busy", "requested GPUs belong to another live rrctl run", "resources"
            )
        path = root / f"{sha256_json(str(control_root))}.json"
        record = {
            "schema_version": "rrctl.gpu-lease.v1",
            "run_id": spec.run_id,
            "control_root": str(control_root),
            "boot_id": boot_id(),
            "gpu_ids": selected,
            "created_at": utc_now(),
        }
        atomic_write_json(path, record)
        return {"device": "gpu", "gpu_ids": selected, "lease_path": str(path)}


def release(lease: dict[str, Any], spec: RunSpec, control_root: Path) -> None:
    path_text = lease.get("lease_path")
    if not path_text:
        return
    path = Path(path_text)
    with _locked(path.parent):
        # 父 worker 退出而训练子进程仍存活时继续保留登记。
        remaining = [
            item
            for item in owned_processes(spec.run_id, control_root)
            if item["pid"] != os.getpid()
        ]
        if not remaining and path.is_file():
            record = json.loads(path.read_text())
            if record.get("run_id") == spec.run_id and record.get("control_root") == str(
                control_root
            ):
                path.unlink()
