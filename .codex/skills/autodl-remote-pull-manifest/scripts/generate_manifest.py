#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shlex
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = Path(__file__).resolve().parent


def _load_env_config() -> dict[str, str]:
    """Load configuration from .env file if it exists."""
    env_path = SCRIPT_DIR / ".env"
    config = {}
    if not env_path.exists():
        return config

    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key and value:
                    config[key] = value
    except Exception as e:
        print(f"[WARN] Failed to read .env file: {e}", file=sys.stderr)

    return config


def _bash_sq(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _parse_remote_host(remote_host_arg: str) -> tuple[str, list[str]]:
    """
    Support:
      - "user@host"
      - "ssh -p 36485 user@host"
    Returns: (host, ssh_opts)
    """
    s = (remote_host_arg or "").strip()
    if not s:
        return ("<user@host>", [])
    if " " not in s and "\t" not in s:
        return (s, [])
    try:
        tokens = shlex.split(s)
    except Exception:
        return (s, [])
    if not tokens:
        return (s, [])
    if tokens[0] == "ssh":
        tokens = tokens[1:]
    if not tokens:
        return (s, [])
    host = tokens[-1]
    ssh_opts = tokens[:-1]
    return (host, ssh_opts)


def _has_strict_host_key_checking(ssh_opts: list[str]) -> bool:
    return any("StrictHostKeyChecking" in t for t in ssh_opts)


def main() -> int:
    # Load .env configuration
    env_config = _load_env_config()
    default_remote_host = env_config.get("REMOTE_HOST", "<user@host>")
    default_password = env_config.get("SSH_PASSWORD", None)

    # Filter out placeholder passwords
    if default_password in ("your_password_here", ""):
        default_password = None

    parser = argparse.ArgumentParser(description="Generate a minimal remote artifact pull script for a given EXP_ROOT.")
    parser.add_argument("--expid", required=True, help="ExpID, e.g. E20251225-01")
    parser.add_argument("--exp-root", required=True, help="Remote EXP_ROOT (absolute path).")
    parser.add_argument("--remote-host", default=default_remote_host, help="SSH target (user@host or SSH config alias). Default from .env if available.")
    parser.add_argument(
        "--strict-host-key-checking",
        default="accept-new",
        choices=["accept-new", "yes", "no"],
        help="SSH StrictHostKeyChecking policy to avoid interactive prompts (default: accept-new).",
    )
    parser.add_argument(
        "--eval-dir",
        default=None,
        help="Optional remote eval dir (absolute, like $EXP_ROOT/eval/<eval_name>/<run_id>); default: pull all eval dirs under $EXP_ROOT/eval/*/*.",
    )
    parser.add_argument(
        "--write-script",
        action="store_true",
        help="Write the generated script to research_workspace/experiments/<ExpID>/commands/pull_remote_artifacts.sh",
    )
    parser.add_argument(
        "--password",
        default=default_password,
        help="SSH password for sshpass authentication. Default from .env if available. WARNING: Password will be stored in the generated script with chmod 700 (owner-only). Requires --write-script.",
    )
    args = parser.parse_args()

    expid = args.expid.strip()
    remote_exp_root = args.exp_root.rstrip("/")
    remote_host_raw = args.remote_host.strip()
    password = args.password

    # Validate password usage
    if password is not None:
        if password == "":
            print("[ERROR] Empty password not allowed. Omit --password for key-based auth.", file=sys.stderr)
            return 1
        if "\n" in password or "\r" in password:
            print("[ERROR] Password contains newline characters; unsupported.", file=sys.stderr)
            return 1
        if not args.write_script:
            print("[ERROR] --password requires --write-script to avoid exposing password in stdout.", file=sys.stderr)
            return 1

    remote_host, ssh_opts = _parse_remote_host(remote_host_raw)
    if args.strict_host_key_checking and not _has_strict_host_key_checking(ssh_opts):
        ssh_opts = [*ssh_opts, "-o", f"StrictHostKeyChecking={args.strict_host_key_checking}"]

    local_exp_dir = REPO_ROOT / "research_workspace" / "experiments" / expid
    local_commands_dir = local_exp_dir / "commands"
    local_artifacts_dir = local_exp_dir / "remote_artifacts"
    local_commands_dir.mkdir(parents=True, exist_ok=True)
    local_artifacts_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "expid": expid,
        "remote_host": remote_host if remote_host != "<user@host>" else "",
        "ssh_opts": ssh_opts,
        "remote_exp_root": remote_exp_root,
        "local_experiment_dir": local_exp_dir.as_posix(),
        "created_at": _dt.datetime.now().isoformat(),
        "note": "Generated locally; execute the pull script on the local machine with SSH access to the remote.",
    }
    meta_path = local_artifacts_dir / "remote_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    bash_ssh_opts = " ".join(_bash_sq(x) for x in ssh_opts)
    script_lines: list[str] = []
    script_lines += [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
    ]

    if password:
        script_lines += [
            "# Password-based authentication via sshpass",
            f"export SSHPASS={_bash_sq(password)}",
            "",
            "# Check sshpass availability",
            'if ! command -v sshpass >/dev/null 2>&1; then',
            '  echo "[ERROR] sshpass not found. Install: apt-get install -y sshpass or brew install sshpass" >&2',
            '  exit 3',
            "fi",
            "",
            "# Cleanup on exit",
            "cleanup() { unset SSHPASS; }",
            "trap cleanup EXIT",
            "",
        ]

    script_lines += [
        f'REMOTE_HOST={_bash_sq(remote_host)}',
        f"SSH_OPTS=({bash_ssh_opts})" if ssh_opts else "SSH_OPTS=()",
    ]

    if password:
        script_lines += [
            'RSYNC_RSH="sshpass -e ssh ${SSH_OPTS[*]}"',
            'SSH_CMD=(sshpass -e ssh "${SSH_OPTS[@]}")',
        ]
    else:
        script_lines += [
            'RSYNC_RSH="ssh ${SSH_OPTS[*]}"',
            'SSH_CMD=(ssh "${SSH_OPTS[@]}")',
        ]

    script_lines += [
        f'EXP_ROOT={_bash_sq(remote_exp_root)}',
        f'DEST_DIR={_bash_sq(local_artifacts_dir.as_posix())}',
        "",
        'mkdir -p "$DEST_DIR"',
        "",
        'echo "[INFO] DEST_DIR=$DEST_DIR"',
        'echo "[INFO] EXP_ROOT=$EXP_ROOT"',
        'echo "[INFO] REMOTE_HOST=$REMOTE_HOST"',
        'echo "[INFO] SSH_OPTS=${SSH_OPTS[*]:-}"',
        "",
        "# 1) results.json (optional)",
        'if "${SSH_CMD[@]}" "$REMOTE_HOST" "test -f \\"$EXP_ROOT/results.json\\""; then',
        '  rsync -av -e "$RSYNC_RSH" "$REMOTE_HOST:$EXP_ROOT/results.json" "$DEST_DIR/results.json"',
        "else",
        '  echo "[WARN] Missing on remote: $EXP_ROOT/results.json (skip)"',
        "fi",
        "",
        "# 1.1) console logs (optional but useful)",
        'if "${SSH_CMD[@]}" "$REMOTE_HOST" "test -f \\"$EXP_ROOT/train_console.log\\""; then',
        '  rsync -av -e "$RSYNC_RSH" "$REMOTE_HOST:$EXP_ROOT/train_console.log" "$DEST_DIR/train_console.log"',
        "fi",
        'if "${SSH_CMD[@]}" "$REMOTE_HOST" "test -f \\"$EXP_ROOT/eval_console.log\\""; then',
        '  rsync -av -e "$RSYNC_RSH" "$REMOTE_HOST:$EXP_ROOT/eval_console.log" "$DEST_DIR/eval_console.log"',
        "fi",
        "",
        "# 1.2) configs/ (optional but useful for reproducibility)",
        'if "${SSH_CMD[@]}" "$REMOTE_HOST" "test -d \\"$EXP_ROOT/configs\\""; then',
        '  rsync -av -e "$RSYNC_RSH" "$REMOTE_HOST:$EXP_ROOT/configs/" "$DEST_DIR/configs/"',
        "else",
        '  echo "[WARN] Missing on remote: $EXP_ROOT/configs/ (skip)"',
        "fi",
        "",
        "# 2) train/logs (optional)",
        'mkdir -p "$DEST_DIR/train"',
        'if "${SSH_CMD[@]}" "$REMOTE_HOST" "test -d \\"$EXP_ROOT/train/logs\\""; then',
        '  rsync -av -e "$RSYNC_RSH" "$REMOTE_HOST:$EXP_ROOT/train/logs/" "$DEST_DIR/train/logs/"',
        "else",
        '  echo "[WARN] Missing on remote: $EXP_ROOT/train/logs/ (skip)"',
        "fi",
        "",
        "# 3) eval (logs/metrics/vis)",
    ]

    if args.eval_dir:
        script_lines += [
            f'EVAL_DIRS=({_bash_sq(args.eval_dir.rstrip("/"))})',
            'echo "[INFO] Using user-specified eval dir: ${EVAL_DIRS[0]}"',
        ]
    else:
        script_lines += [
            'mapfile -t EVAL_DIRS < <("${SSH_CMD[@]}" "$REMOTE_HOST" "ls -dt \\"$EXP_ROOT/eval\\"/*/* 2>/dev/null || true")',
            'if [ "${#EVAL_DIRS[@]}" -eq 0 ]; then',
            '  echo "[ERROR] No eval dir found under $EXP_ROOT/eval/*/*" >&2',
            "  exit 2",
            "fi",
            'if [ "${#EVAL_DIRS[@]}" -eq 1 ]; then',
            '  echo "[INFO] Found 1 eval dir: ${EVAL_DIRS[0]}"',
            "else",
            '  echo "[INFO] Found ${#EVAL_DIRS[@]} eval dirs; pulling all."',
            "fi",
        ]

    script_lines += [
        'for EVAL_DIR in "${EVAL_DIRS[@]}"; do',
        '  EVAL_NAME=$(basename "$(dirname "$EVAL_DIR")")',
        '  RUN_ID=$(basename "$EVAL_DIR")',
        '  LOCAL_EVAL_DIR="$DEST_DIR/eval/$EVAL_NAME/$RUN_ID"',
        '  mkdir -p "$LOCAL_EVAL_DIR"',
        "",
        '  rsync -av -e "$RSYNC_RSH" "$REMOTE_HOST:$EVAL_DIR/logs/" "$LOCAL_EVAL_DIR/logs/"',
        '  rsync -av -e "$RSYNC_RSH" "$REMOTE_HOST:$EVAL_DIR/metrics/" "$LOCAL_EVAL_DIR/metrics/"',
        '  rsync -av --exclude "_cache/" -e "$RSYNC_RSH" "$REMOTE_HOST:$EVAL_DIR/vis/" "$LOCAL_EVAL_DIR/vis/"',
        "",
        '  echo "[INFO] Pulled eval into: $LOCAL_EVAL_DIR"',
        "done",
        'echo "[INFO] Done."',
        "",
    ]

    script_text = "\n".join(script_lines)

    if args.write_script:
        out_path = local_commands_dir / "pull_remote_artifacts.sh"
        out_path.write_text(script_text, encoding="utf-8")
        try:
            os.chmod(out_path, 0o700 if password else 0o755)
        except Exception:
            pass
        if password:
            print(f"[WARN] Password stored in script. File permissions set to 700 (owner-only).", file=sys.stderr)
        print(out_path.as_posix())
    else:
        print(script_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
