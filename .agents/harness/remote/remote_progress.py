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
import zoneinfo
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
AGENT_DIR = REPO_ROOT / ".agents"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from harness.remote.remote_run import rrctl_call, resolve_rrctl  # noqa: E402


def local_timezone() -> datetime.tzinfo:
    """操作者本地时区：显式配置优先，否则取宿主本地时区。

    宿主 TZ 缺失时 ``astimezone()`` 会静默给出 UTC，读起来像"世界时间"；
    因此允许 ``HARNESS_TZ`` / ``TZ``（如 ``Asia/Shanghai``）显式指定。
    """
    for name in (os.environ.get("HARNESS_TZ"), os.environ.get("TZ")):
        if not name:
            continue
        try:
            return zoneinfo.ZoneInfo(name.lstrip(":"))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            continue
    return datetime.datetime.now().astimezone().tzinfo or datetime.timezone.utc


def to_local(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(local_timezone())


def timezone_label(tz: datetime.tzinfo | None = None) -> str:
    """显示为 UTC+8 这类无歧义偏移，不用 CST 等可指多地的缩写。"""
    tz = tz or local_timezone()
    offset = datetime.datetime.now(tz).utcoffset() or datetime.timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def format_duration(seconds: float) -> str:
    """人类可读时长：6h6m / 41m / 2d3h。"""
    total = max(0, int(round(seconds)))
    minutes = total // 60
    if minutes < 1:
        return f"{total}s"
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins}m"
    return f"{mins}m"


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


def _extract_total_from_meta(meta: dict[str, Any]) -> int | None:
    """从 RunSpec 元数据中泛化提取总规模 (total_steps / total_episodes / total_epochs)。"""
    for key in (
        "completion_exact_count",
        "completion_min_count",
        "total_steps",
        "total_episodes",
        "episode_count",
        "max_steps",
        "max_epochs",
        "total_epochs",
        "epochs",
        "total",
    ):
        val = meta.get(key)
        if isinstance(val, int) and val > 0:
            return val
    ac = meta.get("adapter_contract")
    if isinstance(ac, dict):
        for key in ("completion_exact_count", "completion_min_count"):
            val = ac.get(key)
            if isinstance(val, int) and val > 0:
                return val
    # 递归查找任意子契约字典中的总步数字段 (如 custom contract 中的 episode_count, total_steps)
    for v in meta.values():
        if isinstance(v, dict):
            for sub_key in ("episode_count", "total_steps", "total_episodes", "max_steps", "completion_count", "count"):
                val = v.get(sub_key)
                if isinstance(val, int) and val > 0:
                    return val
    return None


def _refresh_cached_eta(cached: dict[str, Any]) -> None:
    speed = cached.get("speed_seconds_per_step")
    curr = cached.get("current_step")
    total = cached.get("total_steps")
    state = cached.get("state")
    if state in {"running", "launched", "workload_started"} and speed and curr is not None and total and total > curr:
        rem_sec = max(0.0, (total - curr) * speed)
        cached["remaining_seconds"] = rem_sec
        now = datetime.datetime.now(datetime.timezone.utc)
        cached["eta_datetime"] = (now + datetime.timedelta(seconds=rem_sec)).isoformat()


def get_progress(
    run_id: str,
    repo_root: Path,
    profiles: Path | None = None,
    spec_path: Path | None = None,
    force_remote: bool = False,
    max_cache_age_seconds: float = 60.0,
) -> dict[str, Any]:
    """查询指定 RunID 的状态、最新进度与推算的 ETA。

    默认优先读取本地状态快照或终态状态，毫秒级响应，避免弱网下每次查询执行 2 次 SSH 挂起；
    force_remote=True 或缓存超期时穿透查询远端。
    """
    local_state_dir = Path.home() / f".local/state/rrctl/runs/{run_id}"
    cache_path = local_state_dir / "latest_progress.json"
    status_path = local_state_dir / "last_status.json"

    # 1. 尝试从本地高速快照直读
    if not force_remote and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("run_id") == run_id:
                state = cached.get("state")
                cached_at = cached.get("cached_at")
                is_fresh = False
                if state in {"completed", "failed", "aborted"}:
                    is_fresh = True
                elif cached_at:
                    age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(cached_at)).total_seconds()
                    if 0 <= age <= max_cache_age_seconds:
                        is_fresh = True
                if is_fresh:
                    _refresh_cached_eta(cached)
                    return cached
        except Exception:
            pass

    # 2. 本地已是终态且无缓存文件时，快速直读 last_status.json 避免不必要的远端 SSH
    if not force_remote and status_path.is_file():
        try:
            st = json.loads(status_path.read_text(encoding="utf-8"))
            st_state = st.get("state")
            if st_state in {"completed", "failed", "aborted"}:
                exit_code = st.get("detail", {}).get("exit_code")
                tot = None
                if spec_path and spec_path.is_file():
                    try:
                        tot = _extract_total_from_meta(json.loads(spec_path.read_text(encoding="utf-8")).get("metadata", {}))
                    except Exception:
                        pass
                return {
                    "run_id": run_id,
                    "state": st_state,
                    "exit_code": exit_code,
                    "started_at": None,
                    "updated_at": st.get("updated_at"),
                    "elapsed_seconds": None,
                    "current_step": tot,
                    "step_field": "step",
                    "total_steps": tot,
                    "pct": 100.0 if st_state == "completed" else None,
                    "speed_seconds_per_step": None,
                    "remaining_seconds": 0.0 if st_state == "completed" else None,
                    "eta_datetime": None,
                    "metrics": {},
                    "estimate_source": "local_status",
                    "eta_scope": "compute_only",
                }
        except Exception:
            pass

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
                        t = _extract_total_from_meta(meta)
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

    if current_step is not None and total_steps is not None and total_steps > 0:
        pct = min(100.0, (current_step / total_steps) * 100.0)
    if current_step is not None and current_step > 0 and elapsed_seconds is not None:
        speed_seconds_per_step = elapsed_seconds / current_step
        if total_steps is not None and total_steps > 0:
            if state in {"running", "launched", "workload_started"}:
                remaining_steps = max(0, total_steps - current_step)
                remaining_seconds = remaining_steps * speed_seconds_per_step
                eta_datetime = now + datetime.timedelta(seconds=remaining_seconds)

    info = {
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
        "estimate_source": "observed_mean_including_initialization" if speed_seconds_per_step is not None else "unknown",
        "eta_scope": "compute_only",
    }
    info["cached_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        if local_state_dir.is_dir():
            cache_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return info


def estimate_runtime(
    total_steps: int,
    smoke_summary_path: Path | None = None,
    marginal_seconds: float | None = None,
    init_seconds: float | None = None,
    fallback_marginal_seconds: float | None = None,
) -> dict[str, Any]:
    """根据前置 smoke 产物或指定耗时推算全量运行的预计耗时与完成时刻 (时间沙盘)。"""
    t_init = init_seconds if init_seconds is not None else 0.0
    t_marg = marginal_seconds
    estimate_source = "explicit_input" if marginal_seconds is not None else None

    if smoke_summary_path and smoke_summary_path.is_file():
        try:
            sdata = json.loads(smoke_summary_path.read_text(encoding="utf-8"))
            # 优先从 smoke_summary 中提取耗时指标
            # 常见字段: marginal_seconds_per_step, init_seconds, elapsed_seconds, steps
            if init_seconds is None and "init_seconds" in sdata:
                t_init = float(sdata["init_seconds"])
            if t_marg is None:
                if "marginal_seconds_per_step" in sdata:
                    t_marg = float(sdata["marginal_seconds_per_step"])
                    estimate_source = "smoke_marginal"
                elif "speed_seconds_per_step" in sdata:
                    t_marg = float(sdata["speed_seconds_per_step"])
                    estimate_source = "smoke_speed"
                elif "elapsed_seconds" in sdata and "steps" in sdata and int(sdata["steps"]) > 0:
                    steps = int(sdata["steps"])
                    elapsed = float(sdata["elapsed_seconds"])
                    if t_init > 0 and elapsed > t_init:
                        t_marg = (elapsed - t_init) / steps
                    else:
                        t_marg = elapsed / steps
                    estimate_source = "smoke_elapsed_mean"
        except Exception:
            pass

    if t_marg is None and fallback_marginal_seconds is not None and fallback_marginal_seconds > 0:
        t_marg = float(fallback_marginal_seconds)
        estimate_source = "fallback_heuristic"

    if (not isinstance(total_steps, int) or isinstance(total_steps, bool) or total_steps <= 0
            or not math.isfinite(t_init) or t_init < 0):
        raise ValueError("总步数必须为正整数，初始化耗时必须为有限非负数")
    if t_marg is None or not math.isfinite(t_marg) or t_marg <= 0:
        raise ValueError("无法推算耗时：缺少单步/单样本边际耗时 (marginal_seconds 或有效的 smoke_summary)")

    total_seconds = t_init + total_steps * t_marg
    now = datetime.datetime.now(datetime.timezone.utc)
    eta_datetime = now + datetime.timedelta(seconds=total_seconds)

    return {
        "total_steps": total_steps,
        "init_seconds": t_init,
        "marginal_seconds_per_step": t_marg,
        "estimated_total_seconds": total_seconds,
        "eta_datetime": eta_datetime.isoformat(),
        "estimate_source": estimate_source,
        "eta_scope": "compute_only",
    }


def format_estimate(est: dict[str, Any]) -> str:
    """将时间沙盘推算格式化为清晰的人类可读文本。"""
    total = est["total_steps"]
    t_init = est["init_seconds"]
    t_marg = est["marginal_seconds_per_step"]
    tot_sec = est["estimated_total_seconds"]
    eta_iso = est["eta_datetime"]

    dt = datetime.datetime.fromisoformat(eta_iso)
    local_dt = to_local(dt)
    tz_label = timezone_label()
    eta_local_str = f"{local_dt.strftime('%Y-%m-%d %H:%M')} {tz_label}"

    lines = [
        "[启动前耗时沙盘推算]",
        f"- 全量规模: {total} 步/样本",
        f"- 单步边际耗时: {t_marg:.2f} 秒/步 (核心算子实测，外围残差一律归入冷启动)",
    ]
    if t_init > 0:
        lines.append(f"- 一次性初始化/冷启动耗时: {t_init:.1f} 秒 (含模型加载与首次 CUDA 建立)")
    lines.append(f"- 预计总耗时: {format_duration(tot_sec)}")
    lines.append(f"- 预计完成时刻: {eta_local_str}")
    lines.append(f"- 估计来源: {est.get('estimate_source', 'unknown')}；仅计算阶段，不含回传、核验与审查")
    lines.append("- 裁定原则: 只要核心步单耗合法即推定放行，严禁因外围残差争议额外增开 GPU 计时探针")
    return "\n".join(lines)


def format_progress(info: dict[str, Any], mode: str = "detailed") -> str:
    """将进度字典格式化为人类可读文本。

    mode: 'detailed' (用于对话或独立查询), 'heartbeat' (用于 30 分钟单行心跳).
    """
    run_id = info["run_id"]
    state = info["state"]
    step = info.get("current_step")
    total = info.get("total_steps")
    pct = info.get("pct")
    if pct is None and step is not None and isinstance(total, (int, float)) and total > 0:
        pct = min(100.0, step / total * 100.0)
    speed = info.get("speed_seconds_per_step")
    elapsed_sec = info.get("elapsed_seconds")
    rem_sec = info.get("remaining_seconds")
    eta_iso = info.get("eta_datetime")
    metrics = info.get("metrics") or {}

    # 本地时刻显示
    tz_label = timezone_label()
    eta_local_str = ""
    eta_local_full = ""
    if eta_iso:
        dt = to_local(datetime.datetime.fromisoformat(eta_iso))
        eta_local_str = f"{dt.strftime('%H:%M')} {tz_label}"
        eta_local_full = f"{dt.strftime('%Y-%m-%d %H:%M')} {tz_label}"

    started_local_str = ""
    if info.get("started_at"):
        s_dt = to_local(datetime.datetime.fromisoformat(info["started_at"]))
        started_local_str = f"{s_dt.strftime('%H:%M')} {tz_label}"

    metrics_str = " | ".join(
        f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in metrics.items()
    )

    if mode == "heartbeat":
        parts = [f"[GPU 进度心跳] RunID: {run_id}"]
        if step is not None:
            if total and pct is not None:
                parts.append(f"进度: {step}/{total} ({pct:.1f}%)")
            else:
                parts.append(f"当前步: {step}")
        if speed:
            parts.append(f"速度: {speed:.1f}s/步")
        if elapsed_sec is not None:
            parts.append(f"已用: {format_duration(elapsed_sec)}")
        if rem_sec is not None and eta_local_str:
            parts.append(f"预计剩余: {format_duration(rem_sec)} (预计完成: {eta_local_str})")
        if metrics_str:
            parts.append(metrics_str)
        return " | ".join(parts)

    # detailed 模式
    lines = [f"[GPU 任务进度] RunID: {run_id}"]
    if state in {"running", "launched", "workload_started"}:
        lines.append(f"- 运行状态: 运行中 ({state})")
        if step is not None:
            total_text = f"/{total} ({pct:.1f}%)" if total and pct is not None else ""
            lines.append(f"- 当前进度: {step}{total_text} [{info.get('step_field', 'step')}]")
        if speed:
            lines.append(f"- 执行速度: {speed:.1f} 秒/步")
        if elapsed_sec is not None:
            start_hint = f" (启动于 {started_local_str})" if started_local_str else ""
            lines.append(f"- 已用时长: {format_duration(elapsed_sec)}{start_hint}")
        if rem_sec is not None and eta_local_str:
            lines.append(f"- 预计剩余: {format_duration(rem_sec)}")
            lines.append(f"- 预计完成时刻: {eta_local_full}")
        if metrics_str:
            lines.append(f"- 阶段指标: {metrics_str}")
    elif state == "completed":
        lines.append(f"- 运行状态: 已完成 (completed, exit_code {info.get('exit_code', 0)})")
        if total:
            lines.append(f"- 最终进度: {total}/{total} (100.0%)")
        if elapsed_sec is not None:
            lines.append(f"- 实际总耗时: {format_duration(elapsed_sec)}")
        if metrics_str:
            lines.append(f"- 最终指标: {metrics_str}")
    else:
        lines.append(f"- 运行状态: {state} (exit_code {info.get('exit_code')})")
        if step is not None:
            total_text = f"/{total}" if total else ""
            lines.append(f"- 中断进度: {step}{total_text}")
        if elapsed_sec is not None:
            lines.append(f"- 运行耗时: {format_duration(elapsed_sec)}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", help="RunID, runspec.json 路径或 issue 目录；省略则自动探测最新运行")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help="项目根目录")
    parser.add_argument("--profiles", type=Path, help="凭据 profile 文件路径")
    parser.add_argument("--remote", action="store_true", help="强制穿透查询远端（绕过本地快照缓存）")
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 输出")
    parser.add_argument("--mode", choices=["detailed", "heartbeat"], default="detailed", help="显示模式")
    parser.add_argument("--estimate", action="store_true", help="执行启动前耗时沙盘推算")
    parser.add_argument("--total-steps", type=int, help="全量运行规模总步数/样本数")
    parser.add_argument("--smoke-summary", type=Path, help="前置 smoke_summary.json 路径")
    parser.add_argument("--marginal-seconds", type=float, help="单步边际耗时（秒）")
    parser.add_argument("--init-seconds", type=float, default=None, help="一次性初始化/冷启动耗时（秒）；省略时读取smoke记录")
    args = parser.parse_args()

    if args.estimate:
        if not args.total_steps:
            print("[错误] 推算耗时必须指定 --total-steps", file=sys.stderr)
            return 1
        try:
            est = estimate_runtime(
                total_steps=args.total_steps,
                smoke_summary_path=args.smoke_summary,
                marginal_seconds=args.marginal_seconds,
                init_seconds=args.init_seconds,
            )
        except Exception as exc:
            if args.json:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"[错误] {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"ok": True, "result": est}, ensure_ascii=False, indent=2))
        else:
            print(format_estimate(est))
        return 0

    profiles = args.profiles
    if profiles is None:
        default_profiles = args.repo_root / ".agents/harness/config/profiles.json"
        if default_profiles.is_file():
            profiles = default_profiles

    try:
        run_id, spec_path = resolve_run_target(args.target, args.repo_root)
        info = get_progress(
            run_id, args.repo_root, profiles=profiles, spec_path=spec_path,
            force_remote=args.remote,
        )
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
