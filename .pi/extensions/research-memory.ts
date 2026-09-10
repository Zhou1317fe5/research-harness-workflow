import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

type HookAction = "context" | "prompt" | "scan" | "checkpoint" | "stop" | "sync";
type HookPayload = Record<string, string>;
type HookResult = { hookSpecificOutput?: { additionalContext?: string; snapshotRevision?: string; generatedAt?: string }; systemMessage?: string };
type HookRunner = (root: string, action: HookAction, payload: HookPayload) => Promise<HookResult>;

const CONTEXT_TYPE = "research-memory-context";
const MAX_INPUT = 4 * 1024 * 1024;
const MAX_OUTPUT = 64 * 1024;

export function memoryProjectRoot(cwd: string): string | undefined {
	let directory = resolve(cwd);
	while (true) {
		if (existsSync(join(directory, ".agents/harness/memory/research_memory.py"))) return directory;
		const parent = dirname(directory);
		if (parent === directory || existsSync(join(directory, ".git"))) return undefined;
		directory = parent;
	}
}

/** 与现有 Codex hook 一样，仅在子进程内加载项目环境；凭据不经过 TS 或命令参数。 */
export function runMemoryHook(root: string, action: HookAction, payload: HookPayload): Promise<HookResult> {
	const input = JSON.stringify(payload);
	if (Buffer.byteLength(input) > MAX_INPUT) return Promise.reject(new Error("memory_input_too_large"));
	return new Promise((resolveResult, reject) => {
		const shell = 'if [ -f "$1" ]; then set -a; . "$1"; set +a; fi; exec python3 "$2" --repo-root "$3" hook --action "$4" --host pi --binding research-memory-v1';
		const child = spawn("bash", ["-c", shell, "research-memory-pi",
			join(root, ".agents/harness/config/.env"),
			join(root, ".agents/harness/memory/research_memory.py"), root, action],
		{ cwd: root, stdio: ["pipe", "pipe", "ignore"] });
		const output: Buffer[] = [];
		let outputBytes = 0;
		let failure: string | undefined;
		const timer = setTimeout(() => {
			failure = "memory_hook_timeout";
			child.kill("SIGKILL");
		}, 20_000);
		child.stdout.on("data", (chunk: Buffer) => {
			outputBytes += chunk.length;
			if (outputBytes > MAX_OUTPUT) {
				failure = "memory_output_too_large";
				child.kill("SIGKILL");
			} else output.push(chunk);
		});
		child.on("error", () => {
			clearTimeout(timer);
			reject(new Error("memory_hook_spawn_failed"));
		});
		child.stdin.on("error", (error: NodeJS.ErrnoException) => {
			// hooks 关闭时 Python 不读取 stdin，正常退出也可能产生 EPIPE。
			if (error.code !== "EPIPE") failure ??= "memory_hook_input_failed";
		});
		child.on("close", (code) => {
			clearTimeout(timer);
			if (failure || code !== 0) return reject(new Error(failure ?? `memory_hook_exit_${code}`));
			try {
				const result: unknown = JSON.parse(Buffer.concat(output).toString("utf8"));
				if (!result || typeof result !== "object" || Array.isArray(result)) throw new Error();
				resolveResult(result as HookResult);
			} catch { reject(new Error("memory_hook_invalid_json")); }
		});
		child.stdin.end(input);
	});
}

export function registerResearchMemory(pi: ExtensionAPI, runHook: HookRunner = runMemoryHook): void {
	if (Number.parseInt(process.env.PI_SUB_AGENT_DEPTH ?? "0", 10) > 0 || process.env.MAGIC_CONTEXT_PI_SUBAGENT === "1") return;
	let context = "";
	let contextRevision = "";
	let contextTimestamp = 0;
	let needsRefresh = false;
	let turnId = "";
	let inputCaptured = false;
	let finalReply: { text: string; timestamp: string } | undefined;
	let queue: Promise<unknown> = Promise.resolve();
	let warned = false;

	function call(ctx: ExtensionContext, action: HookAction, eventName: string, fields: HookPayload = {}): Promise<void> {
		const root = memoryProjectRoot(ctx.cwd);
		if (!root) return Promise.resolve();
		const payload: HookPayload = {
			hook_event_name: eventName,
			session_id: ctx.sessionManager.getSessionId(),
			...(turnId ? { turn_id: turnId } : {}),
			context_revision: contextRevision,
			memory_protocol: "2",
			timestamp: new Date().toISOString(),
			...fields,
		};
		const pending = queue.then(async () => {
			try {
				const result = await runHook(root, action, payload);
				if (result.systemMessage) throw new Error("memory_hook_reported_failure");
				const snapshot = result.hookSpecificOutput;
				const additional = snapshot?.additionalContext;
				if (typeof additional === "string") {
					context = additional;
					contextRevision = snapshot?.snapshotRevision ?? "";
					const generated = snapshot?.generatedAt ? Date.parse(snapshot.generatedAt) : Number.NaN;
					contextTimestamp = Number.isFinite(generated) ? generated : Date.now();
				} else if (!snapshot?.snapshotRevision || snapshot.snapshotRevision !== contextRevision) {
					if (["context", "prompt", "scan", "checkpoint"].includes(action)) {
						context = "";
						contextRevision = "";
					}
				}
				needsRefresh = action === "stop" || action === "sync";
			} catch {
				context = "";
				contextRevision = "";
				needsRefresh = true;
				if (!warned) {
					warned = true;
					pi.appendEntry("research-memory-error", { action, event: eventName });
					if (ctx.hasUI) ctx.ui.notify("科研记忆回调未完成，请检查项目 Python 和本地记忆队列。", "warning");
				}
			}
		});
		queue = pending;
		return pending;
	}

	pi.on("session_start", async (_event, ctx) => {
		context = "";
		contextRevision = "";
		contextTimestamp = 0;
		needsRefresh = false;
		turnId = "";
		inputCaptured = false;
		finalReply = undefined;
		warned = false;
		await call(ctx, "context", "SessionStart");
	});
	pi.on("input", async (event, ctx) => {
		inputCaptured = true;
		// 扩展产生的自动 follow-up 不能作为用户决定的来源。
		if (event.source === "extension") return;
		turnId = randomUUID();
		finalReply = undefined;
		await call(ctx, "prompt", "UserPromptSubmit", { prompt: event.text });
	});
	pi.on("before_agent_start", async (event, ctx) => {
		finalReply = undefined;
		// input 还覆盖运行中的用户 steering；SDK 跳过 input 时才在此补收。
		if (!inputCaptured) {
			turnId = randomUUID();
			await call(ctx, "prompt", "UserPromptSubmit", { prompt: event.prompt });
		}
		inputCaptured = false;
	});
	pi.on("tool_result", () => {
		// 合并同一批工具完成事件；下一次模型调用前只扫描一次，不读取工具内容。
		needsRefresh = true;
	});
	pi.on("session_before_compact", async (_event, ctx) => {
		await call(ctx, "checkpoint", "PreCompact");
	});
	pi.on("session_compact", async (_event, ctx) => {
		await call(ctx, "context", "SessionStart");
	});
	pi.on("message_end", (event) => {
		const message = event.message;
		if (message.role !== "assistant") return;
		// 中间工具调用、推理块、错误或中断回复不当作最终回答。
		finalReply = message.stopReason === "stop" ? {
			text: message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n"),
			timestamp: new Date(message.timestamp).toISOString(),
		} : undefined;
	});
	pi.on("agent_settled", async (_event, ctx) => {
		if (!turnId) return;
		await call(ctx, "stop", "Stop", finalReply
			? { last_assistant_message: finalReply.text, timestamp: finalReply.timestamp } : {});
		inputCaptured = false;
	});
	pi.on("session_shutdown", async (_event, ctx) => {
		await call(ctx, "sync", "SessionEnd");
	});
	pi.on("context", async (event, ctx) => {
		await queue;
		if (needsRefresh) await call(ctx, "scan", "PostToolUse");
		const messages = event.messages.filter((message) =>
			message.role !== "custom" || message.customType !== CONTEXT_TYPE);
		if (!context) return { messages };
		// Pi 把 custom 转为 user；历史快照放在真实会话之前，不能冒充最新用户指令。
		return { messages: [{
			role: "custom" as const, customType: CONTEXT_TYPE, content: context,
			display: false, timestamp: contextTimestamp,
		}, ...messages] };
	});
}

export default function researchMemory(pi: ExtensionAPI): void {
	registerResearchMemory(pi);
}
