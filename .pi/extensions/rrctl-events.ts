import { createHash } from "node:crypto";
import process from "node:process";
import { mkdirSync, readFileSync, renameSync, writeFileSync, appendFileSync } from "node:fs";
import { StringDecoder } from "node:string_decoder";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const Parameters = {
	type: "object",
	additionalProperties: false,
	properties: {
		runspec: { type: "string", minLength: 1, description: "Path to the existing rrctl RunSpec." },
		runspecs: { type: "array", items: { type: "string" }, description: "List of rrctl RunSpec paths for concurrent multi-GPU monitoring." },
		profiles: { type: "string", minLength: 1, description: "Optional rrctl profiles path." },
		resume: { type: "boolean", description: "Observe an already launched RunID without relaunching it." },
		poll_seconds: { type: "number", exclusiveMinimum: 0, description: "Remote observation interval; defaults to 600." },
	},
} as any;

type Params = { runspec?: string; runspecs?: string[]; profiles?: string; resume?: boolean; poll_seconds?: number };

type Waiter = {
	child: ChildProcess;
	eventPath: string;
	logPath: string;
	runId: string;
	root: string;
	key: string;
	settled: boolean;
	settledAt?: number;
	/** 子进程退出码：`0` 为 terminal，非 `0`/spawn 失败为 attention。 */
	exitCode?: number;
};

type Notification = { root: string; message: string; logPath: string; progressKey?: string };

/** 当前活跃 session 的绑定；只在 `session_start` 建立、`session_shutdown` 同步解除。 */
type Binding = {
	root: string;
	sessionId: string;
	token: symbol;
	send: (message: string) => void;
	progress: (message: string) => void;
};

type Broker = {
	waits: Map<string, Waiter>;
	pending: Notification[];
	binding?: Binding;
};

type SessionContext = { cwd: string; sessionManager: { getSessionId(): string } };

/** settle 后保留 tombstone 的窗口：足以覆盖同 event 重复推送的分钟级窗口。 */
export const RRCTL_SETTLED_TOMBSTONE_MS = 60 * 60 * 1000;

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
	if (!params.runspec) throw new Error("runspec is required");
	const runspec = resolve(root, params.runspec);
	if (!inside(root, runspec)) throw new Error("runspec must stay inside the project root");
	const spec = JSON.parse(readFileSync(runspec, "utf8"));
	if (spec?.schema_version !== "rrctl.run.v1" || typeof spec.run_id !== "string" || !spec.run_id) {
		throw new Error("expected a valid rrctl.run.v1 RunSpec");
	}
	const argv = [
		join(root, ".agents/harness/remote/remote_run.py"), runspec, "--execute", "--observe-only",
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

function attachObserver(
	child: ChildProcess,
	entry: Waiter,
	logPath: string,
	root: string,
	key: string,
	deliver: (notification: Notification) => void,
	settle: (entry: Waiter, kind: "terminal" | "attention", detail: Record<string, unknown>, message: string) => void,
): void {
	let lastProgress: string | undefined;
	let lastProgressAt = 0;
	for (const stream of [child.stdout, child.stderr]) {
		if (!stream) continue;
		const decoder = new StringDecoder("utf8");
		let line = "";
		let dropping = false;
		const parseLine = (value: string) => {
			if (entry.settled || !value.startsWith("[rrctl-progress] ")) return;
			try {
				const event = JSON.parse(value.slice("[rrctl-progress] ".length));
				const allowedStages = new Set(["wait", "estimate", "metrics", "completed"]);
				if (event.schema_version !== "rrctl.progress.v1" || event.run_id !== entry.runId || !allowedStages.has(event.stage) || typeof event.message !== "string" || !event.message.trim()) return;
				if (value === lastProgress && Date.now() - lastProgressAt < 1000) return;
				lastProgress = value;
				lastProgressAt = Date.now();
				deliver({ root, message: event.message, logPath, progressKey: key });
			} catch {}
		};
		stream.on("data", (chunk: any) => {
			try {
				appendFileSync(logPath, chunk, { mode: 0o600 });
			} catch (error) {
				settle(entry, "attention", { error: String(error) }, `rrctl attention event for RunID ${entry.runId}: observer log write failed. Do not relaunch the RunID.`);
			}
			const parts = decoder.write(chunk).split("\n");
			for (let index = 0; index < parts.length; index++) {
				if (!dropping) {
					if (line.length + parts[index].length > 65536) { line = ""; dropping = true; }
					else line += parts[index];
				}
				if (index < parts.length - 1) {
					if (!dropping) parseLine(line);
					line = "";
					dropping = false;
				}
			}
		});
		stream.once("end", () => { if (!dropping) parseLine(line + decoder.end()); });
		stream.once("error", (error: any) => settle(entry, "attention", { error: String(error) }, `rrctl attention event for RunID ${entry.runId}: observer stream failed. Do not relaunch the RunID.`));
	}
	child.once("exit", (code: any, signal: any) => {
		settle(
			entry,
			code === 0 ? "terminal" : "attention",
			{ exit_code: code, signal },
			`rrctl ${code === 0 ? "terminal" : "attention"} event for RunID ${entry.runId}. Inspect ${entry.eventPath}. ${code === 0 ? "Compute is terminal; continue pull/ingest now (artifacts have not been pulled)." : "Diagnose the existing run and pull diagnostic artifacts as needed. If the run failed, repair the defect; clean relaunch is supported."}`,
		);
	});
	child.once("error", (error: any) => {
		settle(
			entry,
			"attention",
			{ error: String(error) },
			`rrctl attention event for RunID ${entry.runId}. Inspect ${entry.eventPath}.`,
		);
	});
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
			// Pi ExtensionAPI: display renders in the transcript; triggerTurn:false never wakes the model.
			progress: (message) => pi.sendMessage({ customType: "rrctl-progress", content: message, display: true }, { triggerTurn: false }),
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
			if (notification.progressKey) binding.progress(notification.message);
			else binding.send(notification.message);
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
		// Replacement gaps retain only the latest heartbeat per waiter, not an unbounded history.
		if (notification.progressKey) {
			broker.pending = broker.pending.filter((item) => item.progressKey !== notification.progressKey);
		}
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
		entry.settledAt = Date.now();
		entry.exitCode = entry.child.exitCode;
		broker.pending = broker.pending.filter((item) => item.progressKey !== entry.key);
		// 不立即从 broker.waits 删除：把本 waiter 留作 tombstone，同 key 的同 event
		// 后续 execute 会命中该 tombstone 并复用其结果，而不是 spawn 新 child
		// 再推送一次。清理在 execute 的 getOrCreate 路径上做。
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
			"Never use sleep or polling loops in bash. Call rrctl_event_wait alone; it waits silently and wakes you upon completion or required attention.",
			"Set resume=true for an active running RunID; clean relaunch is supported for terminated/failed runs.",
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
				const runspecList: string[] = [];
				if (typeof params.runspec === "string" && params.runspec.trim()) {
					runspecList.push(params.runspec.trim());
				}
				if (Array.isArray(params.runspecs)) {
					for (const r of params.runspecs) {
						if (typeof r === "string" && r.trim() && !runspecList.includes(r.trim())) {
							runspecList.push(r.trim());
						}
					}
				}
				if (runspecList.length === 0) {
					return { content: [{ type: "text" as const, text: "rrctl_event_wait rejected: expected runspec or runspecs." }], isError: true };
				}

				const commandList = runspecList.map((rs) => buildWaitCommand(root, { ...params, runspec: rs }));

				if (commandList.length === 1) {
					const command = commandList[0];
					const key = `${root}\0${command.runspec}`;
					const existing = broker.waits.get(key);
					if (existing) {
						if (existing.settled) {
							const age = Date.now() - (existing.settledAt ?? 0);
							if (existing.exitCode === 0 && age < RRCTL_SETTLED_TOMBSTONE_MS) {
								return {
									content: [{ type: "text" as const, text: `Event for RunID ${existing.runId} already delivered; no new observer is spawned.` }],
									details: { run_id: existing.runId, event_path: existing.eventPath, reused: true },
									terminate: true as const,
								};
							}
							broker.waits.delete(key);
						} else if (existing.child.exitCode === null) {
							return {
								content: [{ type: "text" as const, text: `Already waiting for RunID ${existing.runId}; no model polling is required.` }],
								details: { run_id: existing.runId, event_path: existing.eventPath, reused: true },
								terminate: true as const,
							};
						} else {
							broker.waits.delete(key);
						}
					}
					const sessionId = ctx.sessionManager.getSessionId();
					const digest = createHash("sha256").update(`${sessionId}\0${command.runspec}`).digest("hex").slice(0, 20);
					const directory = join(root, ".pi/rrctl-events");
					mkdirSync(directory, { recursive: true, mode: 0o700 });
					const logPath = join(directory, `${digest}.log`);
					const eventPath = join(directory, `${digest}.event.json`);
					appendFileSync(logPath, "", { mode: 0o600 });
					const child = spawnChild("python3", command.argv, {
						cwd: root,
						stdio: ["ignore", "pipe", "pipe"],
						env: process.env,
					});
					const entry: Waiter = { child, eventPath, logPath, runId: command.runId, root, key, settled: false };
					broker.waits.set(key, entry);
					attachObserver(child, entry, logPath, root, key, deliver, settle);
					return {
						content: [{ type: "text" as const, text: `Waiting for RunID ${command.runId}; Pi will wake only for terminal state or required attention.` }],
						details: { run_id: command.runId, event_path: eventPath, log_path: logPath, reused: false },
						terminate: true as const,
					};
				}

				// 多任务并发监听 (multi-runspec concurrent monitoring)
				for (const command of commandList) {
					const key = `${root}\0${command.runspec}`;
					const existing = broker.waits.get(key);
					if (existing && existing.settled) {
						const age = Date.now() - (existing.settledAt ?? 0);
						if (existing.exitCode === 0 && age < RRCTL_SETTLED_TOMBSTONE_MS) {
							return {
								content: [{ type: "text" as const, text: `Event for RunID ${existing.runId} already delivered; no new observer is spawned.` }],
								details: { run_id: existing.runId, event_path: existing.eventPath, reused: true },
								terminate: true as const,
							};
						}
					}
				}

				const spawnedRunIds: string[] = [];
				const reusedRunIds: string[] = [];
				const eventPaths: string[] = [];
				const logPaths: string[] = [];
				const sessionId = ctx.sessionManager.getSessionId();
				const directory = join(root, ".pi/rrctl-events");
				mkdirSync(directory, { recursive: true, mode: 0o700 });

				for (const command of commandList) {
					const key = `${root}\0${command.runspec}`;
					const existing = broker.waits.get(key);
					if (existing) {
						if (!existing.settled && existing.child.exitCode === null) {
							reusedRunIds.push(command.runId);
							eventPaths.push(existing.eventPath);
							logPaths.push(existing.logPath);
							continue;
						}
						broker.waits.delete(key);
					}

					const digest = createHash("sha256").update(`${sessionId}\0${command.runspec}`).digest("hex").slice(0, 20);
					const logPath = join(directory, `${digest}.log`);
					const eventPath = join(directory, `${digest}.event.json`);
					appendFileSync(logPath, "", { mode: 0o600 });
					const child = spawnChild("python3", command.argv, {
						cwd: root,
						stdio: ["ignore", "pipe", "pipe"],
						env: process.env,
					});
					const entry: Waiter = { child, eventPath, logPath, runId: command.runId, root, key, settled: false };
					broker.waits.set(key, entry);
					spawnedRunIds.push(command.runId);
					eventPaths.push(eventPath);
					logPaths.push(logPath);
					attachObserver(child, entry, logPath, root, key, deliver, settle);
				}

				const allRunIds = commandList.map((c) => c.runId);
				return {
					content: [{ type: "text" as const, text: `Waiting concurrently for ${allRunIds.length} RunIDs: [${allRunIds.join(", ")}]; Pi will wake on terminal state or required attention from any task.` }],
					details: { run_ids: allRunIds, event_paths: eventPaths, log_paths: logPaths, spawned: spawnedRunIds, reused: reusedRunIds },
					terminate: true as const,
				};
			} catch (error) {
				return { content: [{ type: "text" as const, text: `rrctl_event_wait rejected: ${String(error)}` }], isError: true };
			}
		},
	});
}

export default registerRrctlEvents;
