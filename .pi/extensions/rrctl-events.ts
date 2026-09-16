import { createHash } from "node:crypto";
import { closeSync, mkdirSync, openSync, readFileSync, renameSync, writeFileSync } from "node:fs";
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
type Wait = { child: ChildProcess; eventPath: string; runId: string };

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

export function registerRrctlEvents(pi: ExtensionAPI): void {
	const waits = new Map<string, Wait>();
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
			try {
				const command = buildWaitCommand(root, params);
				const existing = waits.get(command.runspec);
				if (existing && existing.child.exitCode === null) {
					return {
						content: [{ type: "text" as const, text: `Already waiting for RunID ${existing.runId}; no model polling is required.` }],
						details: { run_id: existing.runId, event_path: existing.eventPath, reused: true },
						terminate: true as const,
					};
				}
				const key = createHash("sha256").update(`${ctx.sessionManager.getSessionId()}\0${command.runspec}`).digest("hex").slice(0, 20);
				const directory = join(root, ".pi/rrctl-events");
				mkdirSync(directory, { recursive: true, mode: 0o700 });
				const logPath = join(directory, `${key}.log`);
				const eventPath = join(directory, `${key}.event.json`);
				const log = openSync(logPath, "a", 0o600);
				const child = spawn("python3", command.argv, {
					cwd: root,
					stdio: ["ignore", log, log],
					env: process.env,
				});
				closeSync(log);
				waits.set(command.runspec, { child, eventPath, runId: command.runId });
				child.once("exit", (code, signal) => {
					waits.delete(command.runspec);
					const kind = code === 0 ? "terminal" : "attention";
					atomicJson(eventPath, {
						schema_version: "rrctl.agent-event.v1", run_id: command.runId,
						kind, exit_code: code, signal, log_path: logPath,
						created_at: new Date().toISOString(),
					});
					pi.sendUserMessage(
						`rrctl ${kind} event for RunID ${command.runId}. Inspect ${eventPath} and continue the existing workflow; do not relaunch the RunID.`,
						{ deliverAs: "followUp" },
					);
				});
				child.once("error", (error) => {
					waits.delete(command.runspec);
					atomicJson(eventPath, {
						schema_version: "rrctl.agent-event.v1", run_id: command.runId,
						kind: "attention", error: String(error), log_path: logPath,
						created_at: new Date().toISOString(),
					});
					pi.sendUserMessage(`rrctl attention event for RunID ${command.runId}. Inspect ${eventPath}.`, { deliverAs: "followUp" });
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
