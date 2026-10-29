#!/usr/bin/env python3
"""数据集 / 权重 / checkpoint 等输入资产的本地取回 → 上传远端 staging 工具。

设计原则（重要）：
- **不做大文件哈希**。数据集图像、掩码、样本与 GB 级权重一律不做全量 sha256；
  完整性以文件大小（st_size）为准，必要时配合远端 stat 回读比对。只有显式传
  ``--sha256`` 时才计算并校验（适用于小文件或明确要求的场合）。
- 走流式传输（rsync/scp 直接由磁盘喂网络），绝不把大文件 read_bytes() 进内存。
- 远端网络差而本地快时，正是本工具的用途：本地下载（可断点）后上传到远端资产目录。
- 幂等：远端已存在且大小一致则跳过；rsync 优先（--partial --inplace 断点续传）。

用法：
  python3 .agents/harness/remote/stage_asset.py \
      --profile adam --remote-path /home/zhoujiale/cdfss-dinov3-workspace/weights/x.pth \
      --url https://.../x.pth --local /tmp/x.pth        # 本地下载再上传
  python3 .agents/harness/remote/stage_asset.py \
      --profile eve --remote-path /srv/.../data.tgz --local /tmp/data.tgz   # 已有本地文件直接上传
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
RRCTL_SRC = Path(__file__).resolve().parent / "rrctl" / "src"
if str(RRCTL_SRC) not in sys.path:
    sys.path.insert(0, str(RRCTL_SRC))

from remote_run_control.profiles import ProfileStore  # noqa: E402
from remote_run_control.transport import transport_for  # noqa: E402


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return ""  # unreachable


def _download(url: str, dest: Path, *, force: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0 and not force:
        sys.stderr.write(f"[stage-asset] local file already present, skip download: {dest}\n")
        return
    sys.stderr.write(f"[stage-asset] downloading {url} -> {dest} (resume enabled)...\n")
    cmd = ["curl", "-fL", "--retry", "5", "--retry-delay", "2",
           "--continue-at", "-", "-o", str(dest), url]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SystemExit(f"[stage-asset] download failed (rc={result.returncode}): {url}")
    size = dest.stat().st_size
    if size == 0:
        raise SystemExit(f"[stage-asset] downloaded file is empty: {dest}")


def _split_ssh_base(base: list[str]) -> tuple[list[str], str, list[str], str]:
    """从 SSHTransport._base_command() 拆出 (prefix, ssh_bin, ssh_opts, destination)。"""
    b = list(base)
    prefix: list[str] = []
    if b and Path(b[0]).name == "sshpass":
        prefix = b[:2]
        b = b[2:]
    if not b or Path(b[0]).name != "ssh":
        raise SystemExit("[stage-asset] profile is not an ssh transport")
    ssh_bin = b[0]
    destination = b[-1]
    opts = b[1:-1]
    return prefix, ssh_bin, opts, destination


def _remote_size(transport, remote_path: str) -> int | None:
    result = transport.run(
        ["bash", "-c", 'if [ -e "$1" ]; then stat -c %s -- "$1"; fi', "stage-asset", remote_path]
    )
    text = result.stdout.decode("utf-8", errors="replace").strip()
    return int(text) if text.isdigit() else None


def _ensure_remote_dir(transport, remote_dir: str) -> None:
    transport.run(["bash", "-c", 'mkdir -p -- "$1"', "stage-asset", remote_dir])


def _upload(transport, profile, local: Path, remote_path: str, *, use_rsync: bool) -> None:
    base, env = transport._base_command()
    prefix, ssh_bin, ssh_opts, destination = _split_ssh_base(base)
    if use_rsync and not profile.password_env:
        rsh = shlex.join([ssh_bin, *ssh_opts])
        argv = [*prefix, "rsync", "-e", rsh, "--partial", "--inplace", "--compress",
                str(local), f"{destination}:{remote_path}"]
    else:
        if use_rsync and profile.password_env:
            sys.stderr.write("[stage-asset] password profile -> fall back to scp (rsync+sshpass unreliable)\n")
        argv = [*prefix, "scp", *ssh_opts, "-C", str(local), f"{destination}:{remote_path}"]
    sys.stderr.write(f"[stage-asset] uploading {local.name} ({_human(local.stat().st_size)}) via "
                     f"{Path(argv[len(prefix)]).name} -> {destination}:{remote_path}\n")
    result = subprocess.run(argv, env=env)
    if result.returncode != 0:
        raise SystemExit(f"[stage-asset] upload failed (rc={result.returncode})")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="profiles.json 中的 profile 名（目标主机）")
    parser.add_argument("--profiles", type=Path, help="profiles.json 路径（默认 ~/.config/rrctl/profiles.json）")
    parser.add_argument("--remote-path", required=True, help="远端资产绝对落盘路径")
    parser.add_argument("--local", required=True, type=Path, help="本地暂存文件路径")
    parser.add_argument("--url", help="若给出则先本地下载到 --local（支持断点续传）")
    parser.add_argument("--force", action="store_true", help="强制重下载/重上传")
    parser.add_argument("--sha256", help="可选：显式提供的 sha256，给出才校验（默认不哈希大文件）")
    parser.add_argument("--no-rsync", action="store_true", help="禁用 rsync，改用 scp")
    args = parser.parse_args()

    profile = ProfileStore(args.profiles).load(args.profile)
    transport = transport_for(profile)

    if args.url:
        _download(args.url, args.local, force=args.force)
    if not args.local.is_file():
        raise SystemExit(f"[stage-asset] local file missing: {args.local}")
    local_size = args.local.stat().st_size

    if args.sha256:
        got = _sha256(args.local)
        if got != args.sha256:
            raise SystemExit(f"[stage-asset] local sha256 mismatch: {got} != {args.sha256}")

    remote_dir = str(Path(args.remote_path).parent)
    _ensure_remote_dir(transport, remote_dir)
    existing = _remote_size(transport, args.remote_path)
    if existing == local_size and not args.force:
        if args.sha256:
            # 仅在用户显式要求时才回读远端哈希
            remote_sha = transport.run(
                ["bash", "-c", 'sha256sum -- "$1" | awk \'{print $1}\'', "stage-asset", args.remote_path]
            ).stdout.decode().strip()
            if remote_sha != args.sha256:
                raise SystemExit(f"[stage-asset] remote sha256 mismatch: {remote_sha}")
        sys.stdout.write(json.dumps({
            "ok": True, "skipped": True, "reason": "remote size matches",
            "profile": profile.name, "remote_path": args.remote_path,
            "size": local_size, "sha256_checked": bool(args.sha256),
        }, ensure_ascii=False) + "\n")
        return 0

    use_rsync = (not args.no_rsync) and shutil.which("rsync") is not None
    _upload(transport, profile, args.local, args.remote_path, use_rsync=use_rsync)

    final = _remote_size(transport, args.remote_path)
    if final != local_size:
        raise SystemExit(f"[stage-asset] remote size mismatch after upload: {final} != {local_size}")
    sys.stdout.write(json.dumps({
        "ok": True, "skipped": False, "profile": profile.name,
        "remote_path": args.remote_path, "size": local_size,
        "sha256_checked": bool(args.sha256),
    }, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
