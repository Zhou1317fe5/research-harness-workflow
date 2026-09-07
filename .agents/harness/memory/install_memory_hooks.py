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
    env = root / ".agents/harness/config/.env"
    # 凭据只在钩子子进程中加载；不写入配置，不返回环境变量内容。
    shell = ('if [ -f "$1" ]; then set -a; . "$1"; set +a; fi; '
             'exec "$2" "$3" --repo-root "$5" hook --action "$4" --host "$6" --binding ' + MARKER)
    return shlex.join(["bash", "-c", shell, "research-memory", str(env), sys.executable,
                       str(script), action, str(root), host])


def owned(handler):
    value = handler.get("command", "")
    if not isinstance(value, str):
        return False
    try:
        parts = shlex.split(value)
    except ValueError:
        return False
    return (any(Path(part).name == "research_memory.py" for part in parts)
            and any("--binding " + MARKER in part for part in parts))


def install(root=ROOT, *, remove=False, hosts=("codex", "claude")):
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
                # 同事件的 handlers 会并发执行；由一个回调先落盘，再安排可选同步。
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
    print(json.dumps({"ok": True, "paths": paths, "hindsight_configuration": "unchanged",
                      "note": "Hindsight 默认关闭；Codex 新定义需在 /hooks 中由宿主完成信任"}, ensure_ascii=False))
    return 0
