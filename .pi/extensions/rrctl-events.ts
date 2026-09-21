import { createHash } from "node:crypto";
import { closeSync, mkdirSync, openSync, readFileSync, renameSync, writeFileSync, appendFileSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const Parameters = {
	type: "object",
	additionalProperties: false,
	required: ["runspec"],
	properties: {
		runspec: { type: "string", minLength: 1, description: "Path to the existing rrctl RunSpec." },
		profiles: { type: "string", minLength: 1, description: "Optional rrctl profiles path." },
		resume: { type: "boolean", description: "Observe an already launched RunID without relaunching it." },
		poll_seconds: { type: "number", exclusiveMinimum: 0, description: "Remote observation interval; defaults to 600." },
	},
} as any;

type Params = { runspec: string; profiles?: string; resume?: boolean; poll_seconds?: number };

type Waiter = {
	child: ChildProcess;
	eventPath: string;
	logPath: string;
	runId: string;
	root: string;
	key: string;
	settled: boolean;
};

type Notification = { root: string; message: string; logPath: string };

/** 当前活跃 session 的绑定；只在 `session_start` 建立、`session_shutdown` 同步解除。 */
type Binding = {
	root: string;
	sessionId: string;
	token: symbol;
	send: (message: string) => void;
};

type Broker = {
	waits: Map<string, Waiter>;
	pending: Notification[];
	binding?: Binding;
};

type SessionContext = { cwd: string; sessionManager: { getSessionId(): string } };

export const RRCTL_BROKER_KEY = "__researchHarnessRrctlEventsV1";

/**
 * 进程级 broker：跨 session 保留未结束的 waiter。
 *
 * session replacement/reload 会重建 extension 实例并使旧 `pi` stale，因此子进程回调
 * 绝不捕获 `pi`/`ctx`，只向 broker 交付普通字符串；唤醒始终由当前活跃 session 的绑定发出。
 */
export function rrctlEventsBroker(): Broker {
	const holder = globalThis as unknown as Record<string, unknown>;
	const current = holder[RRCTL_BROKER_KEY] as Broker | undefined;
	if (current && current.waits instanceof Map && Array.isArray(current.pending)) return current;
	const created: Broker = { waits: new Map(), pending: [] };
	holder[RRCTL_BROKER_KEY] = created;
	return created;
}

export function resetRrctlEventsBroker(): void {
	delete (globalThis as unknown as Record<string, unknown>)[RRCTL_BROKER_KEY];
}

function projectRoot(cwd: string): string | undefined {
	let current = resolve(cwd);
	while (true) {
		try {
			readFileSync(join(current, ".agents/harness/remote/remote_run.py"));
			return current;
		} catch {}
		const parent = dirname(current);
		if (parent === current) return undefined;
		current = parent;
	}
}

function inside(root: string, path: string): boolean {
	const value = relative(root, path);
	return value === "" || (!value.startsWith("..") && !isAbsolute(value));
}

function atomicJson(path: string, value: unknown): void {
	const temporary = `${path}.${process.pid}.tmp`;
	writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
	renameSync(temporary, path);
}

export function buildWaitCommand(root: string, params: Params): { argv: string[]; runId: string; runspec: string } {
	const runspec = resolve(root, params.runspec);
	if (!inside(root, runspec)) throw new Error("runspec must stay inside the project root");
	const spec = JSON.parse(readFileSync(runspec, "utf8"));
	if (spec?.schema_version !== "rrctl.run.v1" || typeof spec.run_id !== "string" || !spec.run_id) {
		throw new Error("expected a valid rrctl.run.v1 RunSpec");
	}
	const argv = [
		join(root, ".agents/harness/remote/remote_run.py"), runspec, "--execute",
		"--poll-seconds", String(params.poll_seconds ?? 600), "--max-wait-seconds", "0",
	];
	if (params.resume) argv.push("--resume");
	if (params.profiles) {
		const profiles = resolve(root, params.profiles);
		if (!inside(root, profiles)) throw new Error("profiles must stay inside the project root");
		argv.push("--profiles", profiles);
	}
	return { argv, runId: spec.run_id, runspec };
}

export type RrctlEventsOptions = { spawnChild?: typeof spawn };

export function registerRrctlEvents(pi: ExtensionAPI, options: RrctlEventsOptions = {}): void {
	const broker = rrctlEventsBroker();
	// extension 实例标识：旧实例的 shutdown 只能解除自己的绑定，不能清掉后继实例的绑定。
	const token = Symbol("rrctl-events-instance");
	const spawnChild: typeof spawn = options.spawnChild ?? spawn;

	function bindingFor(ctx: SessionContext): Binding | undefined {
		const root = projectRoot(ctx.cwd);
		if (!root) return undefined;
		return {
			root,
			sessionId: ctx.sessionManager.getSessionId(),
			token,
			send: (message) => pi.sendUserMessage(message, { deliverAs: "followUp" }),
		};
	}

	/** 非权威绑定不覆盖其他实例的绑定；`session_start` 才是当前活跃 session 的权威来源。 */
	function bind(ctx: SessionContext, authoritative: boolean): Binding | undefined {
		const binding = bindingFor(ctx);
		if (!binding) return undefined;
		if (!authoritative && broker.binding && broker.binding.token !== token) return broker.binding;
		broker.binding = binding;
		return binding;
	}

	function send(binding: Binding, notification: Notification): boolean {
		try {
			binding.send(notification.message);
			return true;
		} catch (error) {
			// 异步 callback 绝不允许异常逃逸成 uncaughtException；失败项保留在 pending，
			// 并在 waiter 日志留下证据，等待下一次 session 绑定重试。
			broker.pending.push(notification);
			try {
				appendFileSync(notification.logPath, `${new Date().toISOString()} rrctl_event_wait notification failed: ${String(error)}\n`, { encoding: "utf8", mode: 0o600 });
			} catch {}
			return false;
		}
	}

	function deliver(notification: Notification): void {
		const binding = broker.binding;
		if (binding && binding.root === notification.root) {
			send(binding, notification);
			return;
		}
		// 无活跃绑定（replacement 间隙、quit、其他项目）时暂存，由同 root 的 session_start 冲刷。
		broker.pending.push(notification);
	}

	function flush(root: string): void {
		const binding = broker.binding;
		if (!binding || binding.root !== root) return;
		const matching: Notification[] = [];
		const remaining: Notification[] = [];
		for (const notification of broker.pending) {
			(notification.root === root ? matching : remaining).push(notification);
		}
		broker.pending = remaining;
		for (let index = 0; index < matching.length; index++) {
			if (!send(binding, matching[index])) {
				// 失败项已回到 pending；保留其后的通知顺序，等待下一次绑定。
				broker.pending.push(...matching.slice(index + 1));
				return;
			}
		}
	}

	function settle(entry: Waiter, kind: "terminal" | "attention", detail: Record<string, unknown>, message: string): void {
		if (entry.settled) return;
		entry.settled = true;
		broker.waits.delete(entry.key);
		let writeFailure: string | undefined;
		try {
			atomicJson(entry.eventPath, {
				schema_version: "rrctl.agent-event.v1", run_id: entry.runId,
				kind, ...detail, log_path: entry.logPath,
				created_at: new Date().toISOString(),
			});
		} catch (error) {
			// 证据落盘失败也必须唤醒一次，且不能让文件系统异常从 EventEmitter callback 逃逸成 uncaughtException。
			writeFailure = String(error);
		}
		deliver({ root: entry.root, message: writeFailure ? `${message} Event file write failed: ${writeFailure}.` : message, logPath: entry.logPath });
	}

	pi.on("session_start", (_event, ctx) => {
		const binding = bind(ctx, true);
		if (binding) flush(binding.root);
	});
	pi.on("session_shutdown", () => {
		// 必须同步、幂等地解除绑定：此后旧实例的 pi 不再被任何 callback 使用。
		if (broker.binding?.token === token) broker.binding = undefined;
	});

	pi.registerTool({
		name: "rrctl_event_wait",
		label: "rrctl Event Wait",
		description: "Launch or resume one rrctl RunID and end this model turn. The extension waits without model polling and wakes Pi exactly once when rrctl reports terminal state or required attention. Do not call periodic bash/write_stdin observers for the same RunID.",
		promptSnippet: "Wait for rrctl terminal/attention events without model polling.",
		promptGuidelines: [
			"For long remote train/eval runs, call rrctl_event_wait alone and do not emit periodic progress messages.",
			"Set resume=true for an existing RunID; never relaunch an already bound RunID.",
		],
		parameters: Parameters,
		executionMode: "sequential",
		async execute(_toolCallId, raw, _signal, _onUpdate, ctx) {
			const params = raw as Params;
			const root = projectRoot(ctx.cwd);
			if (!root) return { content: [{ type: "text" as const, text: "rrctl_event_wait rejected: project harness not found." }], isError: true };
			// 工具只可能在当前活跃实例里执行；确保 settlement 的唤醒能回到当前 session。
			bind(ctx, false);
			try {
				const command = buildWaitCommand(root, params);
				const key = `${root}\u0000${command.runspec}`;
				const existing = broker.waits.get(key);
				if (existing && !existing.settled && existing.child.exitCode === null) {
					return {
						content: [{ type: "text" as const, text: `Already waiting for RunID ${existing.runId}; no model polling is required.` }],
						details: { run_id: existing.runId, event_path: existing.eventPath, reused: true },
						terminate: true as const,
					};
				}
				if (existing) broker.waits.delete(key);
				const sessionId = ctx.sessionManager.getSessionId();
				const digest = createHash("sha256").update(`${sessionId}\0${command.runspec}`).digest("hex").slice(0, 20);
				const directory = join(root, ".pi/rrctl-events");
				mkdirSync(directory, { recursive: true, mode: 0o700 });
				const logPath = join(directory, `${digest}.log`);
				const eventPath = join(directory, `${digest}.event.json`);
				const log = openSync(logPath, "a", 0o600);
				let child: ChildProcess;
				try {
					child = spawnChild("python3", command.argv, {
						cwd: root,
						stdio: ["ignore", log, log],
						env: process.env,
					});
				} finally {
					closeSync(log);
				}
				const entry: Waiter = { child, eventPath, logPath, runId: command.runId, root, key, settled: false };
				broker.waits.set(key, entry);
				child.once("exit", (code, signal) => {
					settle(
						entry,
						code === 0 ? "terminal" : "attention",
						{ exit_code: code, signal },
						`rrctl ${code === 0 ? "terminal" : "attention"} event for RunID ${entry.runId}. Inspect ${entry.eventPath} and continue the existing workflow; do not relaunch the RunID.`,
					);
				});
				child.once("error", (error) => {
					settle(
						entry,
						"attention",
						{ error: String(error) },
						`rrctl attention event for RunID ${entry.runId}. Inspect ${entry.eventPath}.`,
					);
				});
				return {
					content: [{ type: "text" as const, text: `Waiting for RunID ${command.runId}; Pi will wake only for terminal state or required attention.` }],
					details: { run_id: command.runId, event_path: eventPath, log_path: logPath, reused: false },
					terminate: true as const,
				};
			} catch (error) {
				return { content: [{ type: "text" as const, text: `rrctl_event_wait rejected: ${String(error)}` }], isError: true };
			}
		},
	});
}

export default registerRrctlEvents;
