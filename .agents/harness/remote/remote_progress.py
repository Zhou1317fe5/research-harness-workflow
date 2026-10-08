#!/usr/bin/env python3
"""远程工作负载进度与预计剩余时间 (ETA) 探针。

支持秒级主动查询、周期性心跳与启动前耗时推算。
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
AGENT_DIR = REPO_ROOT / ".agents"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from harness.remote.remote_run import rrctl_call, resolve_rrctl  # noqa: E402


def resolve_run_target(target: str | None, repo_root: Path) -> tuple[str, Path | None]:
    """根据输入参数解析目标 RunID 与 RunSpec 路径。若未指定则自动探测最近任务。"""
    if target:
        target_path = Path(target)
        if target_path.is_file() and target_path.name.endswith(".json"):
            try:
                data = json.loads(target_path.read_text(encoding="utf-8"))
                run_id = data.get("run_id")
                if isinstance(run_id, str):
                    return run_id, target_path.resolve()
            except Exception:
                pass
        if target_path.is_dir():
            runspecs = sorted(target_path.glob("**/runspec.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            if runspecs:
                try:
                    data = json.loads(runspecs[0].read_text(encoding="utf-8"))
                    run_id = data.get("run_id")
                    if isinstance(run_id, str):
                        return run_id, runspecs[0].resolve()
                except Exception:
                    pass
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", target):
            spec_matches = sorted(repo_root.glob(f"issues/**/runs/{target}/runspec.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            spec_path = spec_matches[0].resolve() if spec_matches else None
            return target, spec_path

    # 未指定时：从当前仓库查找最近的 RunSpec
    repo_runspecs = sorted(repo_root.glob("issues/**/runs/*/runspec.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if repo_runspecs:
        try:
            data = json.loads(repo_runspecs[0].read_text(encoding="utf-8"))
            run_id = data.get("run_id")
            if isinstance(run_id, str):
                return run_id, repo_runspecs[0].resolve()
        except Exception:
            pass

    # 次选：从本用户本地 rrctl 状态目录中查找最近的运行
    user_state_runs = Path.home() / ".local/state/rrctl/runs"
    if user_state_runs.is_dir():
        candidates = []
        for rdir in user_state_runs.iterdir():
            status_file = rdir / "last_status.json"
            if status_file.is_file():
                try:
                    mtime = status_file.stat().st_mtime
                    candidates.append((mtime, rdir.name, rdir / "run_spec.json"))
                except Exception:
                    pass
        if candidates:
            candidates.sort(reverse=True)
            chosen_mtime, chosen_id, chosen_spec = candidates[0]
            spec_p = chosen_spec.resolve() if chosen_spec.is_file() else None
            return chosen_id, spec_p

    raise ValueError("未能自动发现有效的运行目标，请指定 RunID 或 runspec.json 路径")


def _parse_iso_datetime(dt_str: str | None) -> datetime.datetime | None:
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        # 支持各种标准 ISO 格式
        return datetime.datetime.fromisoformat(dt_str)
    except Exception:
        return None


def get_progress(run_id: str, repo_root: Path, profiles: Path | None = None, spec_path: Path | None = None) -> dict[str, Any]:
    """查询指定 RunID 的状态、最新进度与推算的 ETA。"""
    prefix = [resolve_rrctl(), "--json"]
    if profiles:
        prefix += ["--profiles", str(profiles.resolve())]

    # 1. 调用 rrctl inspect
    res_inspect = rrctl_call([*prefix, "inspect", run_id], repo_root)
    if res_inspect.returncode != 0:
        raise ValueError(f"无法检查任务状态: {res_inspect.stderr or res_inspect.stdout}")
    try:
        inspect_data = json.loads(res_inspect.stdout).get("result", {})
    except Exception as exc:
        raise ValueError(f"inspect 结果解析失败: {exc}") from exc

    binding = inspect_data.get("binding", {})
    status = inspect_data.get("status", {})
    state = status.get("state", "unknown")
    exit_code = status.get("detail", {}).get("exit_code")

    started_at = _parse_iso_datetime(binding.get("workload_started_at") or binding.get("created_at"))
    updated_at = _parse_iso_datetime(status.get("updated_at"))

    # 2. 尝试读取健康检查（获取进度快照）
    current_step: int | None = None
    step_field: str = "step"
    total_steps: int | None = None
    metrics: dict[str, Any] = {}

    res_health = rrctl_call([*prefix, "health", run_id, "--phase", "periodic"], repo_root)
    if res_health.returncode == 0:
        try:
            hdata = json.loads(res_health.stdout).get("result", {})
            adapter_prog = hdata.get("adapter", {}).get("progress", {})
            if isinstance(adapter_prog, dict):
                count_val = adapter_prog.get("count")
                if isinstance(count_val, int) and count_val >= 0:
                    current_step = count_val
                field_val = adapter_prog.get("count_field")
                if isinstance(field_val, str) and field_val:
                    step_field = field_val
                total_val = adapter_prog.get("total")
                if isinstance(total_val, int) and total_val > 0:
                    total_steps = total_val
                m_val = adapter_prog.get("metrics")
                if isinstance(m_val, dict):
                    metrics = m_val
        except Exception:
            pass

    # 3. 补充从 RunSpec 元数据中获取总规模 (total_steps)
    if total_steps is None:
        lookups = []
        if spec_path and spec_path.is_file():
            lookups.append(spec_path)
        lookups.extend([
            repo_root / f"issues/**/runs/{run_id}/runspec.json",
            Path.home() / f".local/state/rrctl/runs/{run_id}/run_spec.json",
        ])
        for pat in lookups:
            paths = [pat] if pat.is_file() else list(repo_root.glob(str(pat.relative_to(repo_root)))) if pat.is_relative_to(repo_root) else []
            for p in paths:
                if p.is_file():
                    try:
                        sdata = json.loads(p.read_text(encoding="utf-8"))
                        meta = sdata.get("metadata", {})
                        t = (
                            meta.get("full600_contract", {}).get("episode_count")
                            or meta.get("adapter_contract", {}).get("completion_exact_count")
                            or meta.get("adapter_contract", {}).get("completion_min_count")
                            or meta.get("total_steps")
                            or meta.get("total_episodes")
                        )
                        if isinstance(t, int) and t > 0:
                            total_steps = t
                            break
                    except Exception:
                        pass
            if total_steps is not None:
                break

    # 4. 计算用时与 ETA
    now = datetime.datetime.now(datetime.timezone.utc)
    elapsed_seconds: float | None = None
    if started_at:
        ref_time = updated_at if state in {"completed", "failed", "aborted"} and updated_at else now
        elapsed_seconds = max(0.0, (ref_time - started_at).total_seconds())

    pct: float | None = None
    speed_seconds_per_step: float | None = None
    remaining_seconds: float | None = None
    eta_datetime: datetime.datetime | None = None

    if current_step is not None and current_step > 0 and elapsed_seconds is not None:
        speed_seconds_per_step = elapsed_seconds / current_step
        if total_steps is not None and total_steps > 0:
            pct = min(100.0, (current_step / total_steps) * 100.0)
            if state in {"running", "launched", "workload_started"}:
                remaining_steps = max(0, total_steps - current_step)
                remaining_seconds = remaining_steps * speed_seconds_per_step
                eta_datetime = now + datetime.timedelta(seconds=remaining_seconds)

    return {
        "run_id": run_id,
        "state": state,
        "exit_code": exit_code,
        "started_at": started_at.isoformat() if started_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "elapsed_seconds": elapsed_seconds,
        "current_step": current_step,
        "step_field": step_field,
        "total_steps": total_steps,
        "pct": pct,
        "speed_seconds_per_step": speed_seconds_per_step,
        "remaining_seconds": remaining_seconds,
        "eta_datetime": eta_datetime.isoformat() if eta_datetime else None,
        "metrics": metrics,
    }


def format_progress(info: dict[str, Any], mode: str = "detailed") -> str:
    """将进度字典格式化为人类可读文本。

    mode: 'detailed' (用于对话或独立查询), 'heartbeat' (用于 30 分钟单行心跳).
    """
    run_id = info["run_id"]
    state = info["state"]
    step = info.get("current_step")
    total = info.get("total_steps")
    pct = info.get("pct")
    speed = info.get("speed_seconds_per_step")
    elapsed_sec = info.get("elapsed_seconds")
    rem_sec = info.get("remaining_seconds")
    eta_iso = info.get("eta_datetime")
    metrics = info.get("metrics") or {}

    # 本地时刻显示
    eta_local_str = ""
    if eta_iso:
        dt = datetime.datetime.fromisoformat(eta_iso)
        eta_local_str = dt.astimezone().strftime("%H:%M %Z")

    started_local_str = ""
    if info.get("started_at"):
        s_dt = datetime.datetime.fromisoformat(info["started_at"])
        started_local_str = s_dt.astimezone().strftime("%H:%M")

    metrics_str = " | ".join(
        f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in metrics.items()
    )

    if mode == "heartbeat":
        parts = [f"[GPU 进度心跳] RunID: {run_id}"]
        if step is not None:
            if total:
                parts.append(f"进度: {step}/{total} ({pct:.1f}%)")
            else:
                parts.append(f"当前步: {step}")
        if speed:
            parts.append(f"速度: {speed:.1f}s/步")
        if elapsed_sec is not None:
            parts.append(f"已用: {elapsed_sec/60:.0f}m")
        if rem_sec is not None and eta_local_str:
            parts.append(f"预计剩余: {rem_sec/60:.0f}m (预计完成: {eta_local_str})")
        if metrics_str:
            parts.append(metrics_str)
        return " | ".join(parts)

    # detailed 模式
    lines = [f"[GPU 任务进度] RunID: {run_id}"]
    if state in {"running", "launched", "workload_started"}:
        lines.append(f"- 运行状态: 运行中 ({state})")
        if step is not None:
            total_text = f"/{total} ({pct:.1f}%)" if total else ""
            lines.append(f"- 当前进度: {step}{total_text} [{info.get('step_field', 'step')}]")
        if speed:
            lines.append(f"- 执行速度: {speed:.1f} 秒/步")
        if elapsed_sec is not None:
            start_hint = f" (启动于 {started_local_str})" if started_local_str else ""
            lines.append(f"- 已用时长: {elapsed_sec/60:.1f} 分钟{start_hint}")
        if rem_sec is not None and eta_local_str:
            hours = rem_sec / 3600
            lines.append(f"- 预计剩余: {rem_sec/60:.1f} 分钟 ({hours:.2f} 小时)")
            lines.append(f"- 预计完成时刻: {eta_local_str}")
        if metrics_str:
            lines.append(f"- 阶段指标: {metrics_str}")
    elif state == "completed":
        lines.append(f"- 运行状态: 已完成 (completed, exit_code {info.get('exit_code', 0)})")
        if total:
            lines.append(f"- 最终进度: {total}/{total} (100.0%)")
        if elapsed_sec is not None:
            lines.append(f"- 实际总耗时: {elapsed_sec/60:.1f} 分钟 ({elapsed_sec/3600:.2f} 小时)")
        if metrics_str:
            lines.append(f"- 最终指标: {metrics_str}")
    else:
        lines.append(f"- 运行状态: {state} (exit_code {info.get('exit_code')})")
        if step is not None:
            total_text = f"/{total}" if total else ""
            lines.append(f"- 中断进度: {step}{total_text}")
        if elapsed_sec is not None:
            lines.append(f"- 运行耗时: {elapsed_sec/60:.1f} 分钟")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="RunID, runspec.json 路径或 issue 目录；省略则自动探测最新运行")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="项目根目录")
    parser.add_argument("--profiles", type=Path, help="凭据 profile 文件路径")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    parser.add_argument("--mode", choices=["detailed", "heartbeat"], default="detailed", help="显示模式")
    args = parser.parse_args()

    profiles = args.profiles
    if profiles is None:
        default_profiles = args.repo_root / ".agents/harness/config/profiles.json"
        if default_profiles.is_file():
            profiles = default_profiles

    try:
        run_id, spec_path = resolve_run_target(args.target, args.repo_root)
        info = get_progress(run_id, args.repo_root, profiles=profiles, spec_path=spec_path)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[错误] {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"ok": True, "result": info}, ensure_ascii=False, indent=2))
    else:
        print(format_progress(info, mode=args.mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())
