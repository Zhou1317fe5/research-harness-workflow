#!/usr/bin/env python3
"""为当前项目安装本地科研记忆钩子，保留宿主的其他配置。"""
from __future__ import annotations

# 直接运行脚本和通过 Python 包导入时使用同一实现。
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from harness.memory.install_memory_hooks import main
    raise SystemExit(main())

import argparse
import json
import shlex
import sys
from pathlib import Path

from harness.memory.research_memory import ROOT, atomic

MARKER = "research-memory-v1"
EVENTS = {
    "SessionStart": "context",
    "UserPromptSubmit": "prompt",
    "PostToolUse": "scan",
    "PreCompact": "checkpoint",
    "Stop": "stop",
}


def command(root, action, host):
    script = root / ".agents/harness/memory/research_memory.py"
    return shlex.join([sys.executable, str(script), "--repo-root", str(root),
                       "hook", "--action", action, "--host", host, "--binding", MARKER])


def _tokens_look_like_install(parts):
    """判断 token 序列是否为本 install() 生成的 hook 形式。

    支持两种形态：
    (a) install() 生成：['bash','-c', <shell字符串（内含 --binding MARKER）>, 'research-memory', ...]
        shell 字符串里包含 '--binding <MARKER>'。
    (b) 手工/旧版：包含 'research_memory.py' 作为脚本路径 token
        且后面紧跟 '--binding' '<MARKER>' 两个连续 token（或嵌在 token 字符串中）。
    """
    # 形态 (a)：bash -c <shell> research-memory ...，其中 shell 里含 marker token
    if len(parts) >= 4 and parts[0] == "bash" and parts[1] == "-c":
        shell = parts[2]
        if f"--binding {MARKER}" in shell:
            return True
    # 形态 (b)：找到 research_memory.py 作为路径 basename；
    # 同一 token 含 marker，或后跟 --binding <MARKER>。
    for index, part in enumerate(parts):
        if Path(part).name == "research_memory.py":
            if f"--binding {MARKER}" in part:
                return True
            for j in range(index + 1, len(parts) - 1):
                if parts[j] == "--binding" and parts[j + 1] == MARKER:
                    return True
            break
    return False


def owned(handler):
    value = handler.get("command", "")
    if not isinstance(value, str):
        return False
    try:
        parts = shlex.split(value)
    except ValueError:
        return False
    return _tokens_look_like_install(parts)


def _validate_hosts(hosts):
    unknown = set(hosts) - {"codex", "claude"}
    if unknown:
        raise ValueError(f"unknown host(s): {', '.join(sorted(unknown))}; expected codex or claude")


def install(root=ROOT, *, remove=False, hosts=("codex", "claude")):
    _validate_hosts(hosts)
    root = Path(root).resolve()
    if not all((root / ".agents/harness/memory" / name).is_file()
               for name in ("research_memory.py", "memory_hooks.py")):
        raise ValueError("请先复制 .agents/harness 到目标项目")
    planned = []
    for host, path in (("codex", root / ".codex/hooks.json"),
                       ("claude", root / ".claude/settings.local.json")):
        if host not in hosts:
            continue
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("宿主配置不能是符号链接")
        if remove and not path.exists():
            continue
        data = json.loads(path.read_text()) if path.is_file() else {}
        if not isinstance(data, dict):
            raise ValueError("现有宿主配置不是对象")
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("现有 hooks 配置不是对象")
        for event, action in EVENTS.items():
            groups = []
            old_groups = hooks.get(event, [])
            if not isinstance(old_groups, list):
                raise ValueError("现有 hook event 不是数组")
            for group in old_groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list) or any(
                    not isinstance(h, dict) for h in group["hooks"]
                ):
                    raise ValueError("现有 hook group 格式无效")
                kept = [h for h in group.get("hooks", []) if not owned(h)]
                if kept or not group["hooks"]:
                    groups.append({**group, "hooks": kept})
            if not remove:
                # 同事件的 handlers 会并发执行；由一个回调先落盘，再提供本地上下文。
                group = {"hooks": [{"type": "command", "command": command(root, action, host),
                                    "timeout": 20}]}
                if event in {"PreToolUse", "PostToolUse"}:
                    group["matcher"] = ".*"
                groups.append(group)
            if groups:
                hooks[event] = groups
            else:
                hooks.pop(event, None)
        if not path.exists() or json.loads(path.read_text()) != data:
            planned.append((path, data))
    changed = []
    for path, data in planned:
        atomic(path, data)
        changed.append(str(path))
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--host", choices=("all", "codex", "claude"), default="all")
    args = parser.parse_args()
    try:
        paths = install(args.repo_root, remove=args.remove,
                        hosts=("codex", "claude") if args.host == "all" else (args.host,))
    except (ValueError, OSError) as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__}))
        return 2
    print(json.dumps({"ok": True, "paths": paths,
                      "note": "本地科研记忆钩子已安装；Codex 新定义需在 /hooks 中由宿主完成信任"}, ensure_ascii=False))
    return 0
