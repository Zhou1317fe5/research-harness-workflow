#!/usr/bin/env python3
"""把一个由 Pi 扩展注册的 provider 转写进 agent 级 models.json。

审查会话固定以 ``--no-extensions`` 启动（审查进程不执行任何扩展代码），因此
扩展注册的 provider 在审查里不可见。本工具把这类 provider 转写成 models.json
条目，让 ``/model`` 与审查门禁看到同一个 provider，同时保持审查侧的硬隔离。
相比在审查会话里加载扩展，它换来的是确定性：审查时的 provider 集合不随机器上
装了哪些扩展而漂移。

它只搬运非敏感信息：

- ``baseUrl`` 来自 ``<agent-dir>/extension-settings/*.json``；
- 模型清单与能力（context / max-out / thinking / images）来自 ``pi --list-models``。

凭据一律不搬运：不读取 ``auth.json``，不复制 apiKey。需要凭据时用
``--api-key-ref NAME`` 写入 ``"apiKey": "$NAME"`` 环境变量引用，其余情况交由 Pi
自己的凭据解析顺序（运行时 ``--api-key`` → ``auth.json`` → models.json ``apiKey``
→ 环境变量）。注意 ``/model`` 与 ``--list-models`` 只展示**凭据可用**的 provider：
只有 models.json 条目、没有凭据时，provider 仍然不会被列出，审查也用不上。
因此本工具会顺带用 ``pi auth check``（只读状态，不打印凭据）报出每个目标
provider 的凭据状态，并在未就绪时提示 ``/login <provider>`` 或 ``--api-key-ref``。

默认只做 dry-run；必须显式 ``--write`` 才落盘，落盘前备份原文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"
DEFAULT_AGENT_DIR = Path.home() / ".pi" / "agent"
MODELS_FILE = "models.json"
EXTENSION_SETTINGS_DIR = "extension-settings"
TABLE_COLUMNS = ("provider", "model", "context", "max-out", "thinking", "images")
_SIZE_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


class ProviderSyncError(ValueError):
    """输入不可解析或不足，需要用户干预。"""


def agent_dir(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    configured = os.environ.get(AGENT_DIR_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_AGENT_DIR


def parse_size(value: str) -> int:
    """把 ``128K`` / ``32.8K`` / ``1M`` 这类展示值转成整数 token 数。"""
    text = value.strip()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KkMmBb]?)", text)
    if match is None:
        raise ProviderSyncError(f"无法解析大小: {value!r}")
    number, suffix = match.group(1), match.group(2).upper()
    return int(round(float(number) * _SIZE_SUFFIXES.get(suffix, 1)))


def parse_models_table(text: str) -> list[dict[str, str]]:
    """解析 ``pi --list-models`` 的定宽表格。"""
    header_index = None
    lines = text.splitlines()
    for index, line in enumerate(lines):
        columns = re.split(r"\s{2,}", line.strip())
        if tuple(columns) == TABLE_COLUMNS:
            header_index = index
            break
    if header_index is None:
        raise ProviderSyncError("找不到 `pi --list-models` 的表头，输出格式可能已变化")
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 1:]:
        if not line.strip():
            continue
        values = re.split(r"\s{2,}", line.strip())
        if len(values) != len(TABLE_COLUMNS):
            continue
        rows.append(dict(zip(TABLE_COLUMNS, values)))
    return rows


def providers_without_extensions(
    with_extensions: list[dict[str, str]], without_extensions: list[dict[str, str]]
) -> list[str]:
    """扩展注册的 provider = 启用扩展时可见、禁用扩展后消失的那些。"""
    enabled = {row["provider"] for row in with_extensions}
    disabled = {row["provider"] for row in without_extensions}
    return sorted(enabled - disabled)


def read_extension_base_urls(settings_dir: Path) -> dict[str, str]:
    """从 ``extension-settings/*.json`` 收集 ``providers.<name>.baseUrl``。

    这些文件是各扩展自己的配置，形状不是 Pi 的公开契约，因此只取明确的
    ``baseUrl`` 字段，缺字段或不可读的文件直接跳过。
    """
    found: dict[str, str] = {}
    if not settings_dir.is_dir():
        return found
    for path in sorted(settings_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        providers = data.get("providers") if isinstance(data, dict) else None
        if not isinstance(providers, dict):
            continue
        for name, config in providers.items():
            base_url = config.get("baseUrl") if isinstance(config, dict) else None
            if isinstance(base_url, str) and base_url.strip():
                found.setdefault(name, base_url.strip())
    return found


def build_provider_entry(
    provider: str, rows: list[dict[str, str]], base_url: str, api_key_ref: str | None
) -> dict[str, object]:
    """按 models.json 的字段名构造条目（不含任何凭据值）。"""
    models: list[dict[str, object]] = []
    for row in rows:
        if row["provider"] != provider:
            continue
        entry: dict[str, object] = {"id": row["model"], "name": row["model"]}
        entry["input"] = ["text", "image"] if row["images"].strip() == "yes" else ["text"]
        entry["contextWindow"] = parse_size(row["context"])
        entry["maxTokens"] = parse_size(row["max-out"])
        entry["reasoning"] = row["thinking"].strip() == "yes"
        models.append(entry)
    if not models:
        raise ProviderSyncError(f"{provider} 在 --list-models 输出里没有任何模型")
    providers_entry: dict[str, object] = {
        "baseUrl": base_url,
        "api": "openai-completions",
        "models": models,
    }
    if api_key_ref:
        providers_entry["apiKey"] = f"${api_key_ref}"
    return providers_entry


def merge_providers(
    existing: dict[str, object], entries: dict[str, dict[str, object]]
) -> tuple[dict[str, object], list[str]]:
    """把新条目并入现有 models.json，保留其它 provider 原样。"""
    merged = dict(existing)
    providers = merged.get("providers")
    providers = dict(providers) if isinstance(providers, dict) else {}
    changes: list[str] = []
    for name, entry in entries.items():
        current = providers.get(name)
        if current == entry:
            changes.append(f"{name}: 已是最新")
        elif current is None:
            changes.append(f"{name}: 新增")
        else:
            changes.append(f"{name}: 更新")
        providers[name] = entry
    merged["providers"] = providers
    return merged, changes


def run_list_models(pi: str, *, extensions: bool, workdir: Path) -> str:
    argv = [pi] + ([] if extensions else ["--no-extensions"]) + ["--list-models"]
    completed = subprocess.run(argv, capture_output=True, text=True, cwd=workdir, check=False)
    if completed.returncode != 0:
        raise ProviderSyncError(f"`{' '.join(argv)}` 退出码 {completed.returncode}: {completed.stderr.strip()}")
    return completed.stdout


def provider_auth_status(pi: str, provider: str, *, workdir: Path) -> str:
    """只读“凭据是否就绪”，不打印任何凭据内容。

    ``pi auth check`` 用退出码表示结果（未就绪时非 0），因此只看 stdout。
    """
    completed = subprocess.run(
        [pi, "auth", "check", "--provider", provider, "--no-refresh"],
        capture_output=True, text=True, cwd=workdir, check=False,
    )
    lines = completed.stdout.strip().splitlines()
    return lines[0].strip() if lines else "unknown"


def load_existing(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderSyncError(f"{path} 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderSyncError(f"{path} 的顶层必须是对象")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, default=None, help=f"默认 $${AGENT_DIR_ENV} 或 ~/.pi/agent")
    parser.add_argument("--pi", default="pi", help="Pi 可执行文件")
    parser.add_argument("--provider", action="append", default=None,
                        help="只处理指定 provider（可重复）；默认自动发现所有扩展注册的 provider")
    parser.add_argument("--api-key-ref", default=None,
                        help="写入 apiKey 为 $NAME 环境变量引用；不传则不写 apiKey")
    parser.add_argument("--write", action="store_true", help="落盘（默认 dry-run）")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    directory = agent_dir(args.agent_dir)
    models_path = directory / MODELS_FILE
    try:
        with_extensions = parse_models_table(run_list_models(args.pi, extensions=True, workdir=args.workdir))
        if args.provider:
            targets = sorted(set(args.provider))
            missing = [name for name in targets if name not in {row["provider"] for row in with_extensions}]
            if missing:
                raise ProviderSyncError(f"未知 provider: {', '.join(missing)}")
        else:
            without_extensions = parse_models_table(run_list_models(args.pi, extensions=False, workdir=args.workdir))
            targets = providers_without_extensions(with_extensions, without_extensions)
        if not targets:
            print("没有发现扩展注册的 provider：审查会话与当前配置一致，无需转写。")
            return 0

        base_urls = read_extension_base_urls(directory / EXTENSION_SETTINGS_DIR)
        entries: dict[str, dict[str, object]] = {}
        for name in targets:
            base_url = base_urls.get(name)
            if base_url is None:
                print(f"跳过 {name}: 在 {directory / EXTENSION_SETTINGS_DIR} 里找不到它的 baseUrl", file=sys.stderr)
                continue
            entries[name] = build_provider_entry(name, with_extensions, base_url, args.api_key_ref)
        if not entries:
            raise ProviderSyncError("没有任何 provider 可转写")

        merged, changes = merge_providers(load_existing(models_path), entries)
        print("将写入以下 provider（凭据不搬运）：")
        for name, entry in entries.items():
            model_ids = [model["id"] for model in entry["models"]]  # type: ignore[index]
            status = provider_auth_status(args.pi, name, workdir=args.workdir)
            print(f"  {name}: baseUrl={entry['baseUrl']} models={len(model_ids)} first={model_ids[0]} auth={status}")
            if status != "ready":
                print(f"    凭据未就绪：provider 不会出现在 /model 里，审查也用不上。"
                      f"先 `/login {name}`（凭据归 Pi 保存），或加 --api-key-ref <ENV_NAME>。")
        for change in changes:
            print(f"  {change}")
        if not args.write:
            print(f"\n(dry-run) 目标文件 {models_path}；加 --write 落盘")
            return 0

        if models_path.is_file():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = models_path.with_suffix(f".json.bak-{stamp}")
            shutil.copy2(models_path, backup)
            print(f"已备份 {backup}")
        models_path.parent.mkdir(parents=True, exist_ok=True)
        models_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入 {models_path}")
        if args.api_key_ref is None:
            print("注意：未写入 apiKey。若该 provider 没有 auth.json/环境变量凭据，"
                  "请加 --api-key-ref <ENV_NAME> 或自行补充。")
        return 0
    except ProviderSyncError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
