---
id: "0001"
title: "使用独立进程与显式资源管理远程实验"
status: "accepted"
date: "2026-09-11"
recorded: "2026-09-11"
supersedes: null
superseded-by: null
tags: ["remote-execution", "process-lifecycle", "gpu-resources"]
aliases: ["rrctl", "tmux", "pipelines"]
paths: [".agents/harness/remote/rrctl", ".agents/harness/pipeline"]
---

# 0001. 使用独立进程与显式资源管理远程实验

## Context

远程执行曾依赖 tmux 的会话名与继承环境。实际使用中出现过 CUDA 动态库冲突，审计还复现了会话名改写和健康误判。用户于 2026-09-11 明确要求移除 tmux 并修复通用工作流。运行仍需在 SSH 或本地观察结束后继续，并保留可核验的归属和退出状态。

## Decision

rrctl 0.2 使用 Linux 独立进程会话启动 worker，stdio 指向本次 control root。通过 RunID、control root、boot ID、PID 启动时间和 PGID 验证归属。GPU 任务在启动前取得按设备登记的独占资源，Conda 激活前后应用声明的环境。观察预算与任务生命周期分离；首步观察失败不自动停止活任务。命名 pipelines 选择项目 Bash 脚本组合，并把阶段定义及摘要固定到 RunSpec。

## Rejected

- 保留 tmux 后端：会话与真实任务生命周期不一致，且用户明确要求移除。
- 强制 systemd：会增加服务器服务管理与权限前提；当前 Linux 进程机制足以满足这次需求。
- 失败后临时 SSH/nohup 启动：无法延续同一 RunID 的归属和证据，违背统一控制面的要求。

## Consequences

远端继续要求 Linux /proc 和项目 Conda 环境；不增加本地常驻服务。旧后端运行可读取和拉取，不能自动迁移为新进程。GPU 约束覆盖同一用户的 rrctl 任务，并检查已有 CUDA 占用；外部程序自行启动仍需宿主资源管理配合。当前已有独立进程生命周期、观察器终止、supervisor 丢失后停止、GPU 冲突和命名 pipeline 的本地行为回归；验证命令位于 rrctl/tests 与 remote/tests。
