import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { ExtensionAPI, ToolResultEvent } from "@earendil-works/pi-coding-agent";
import { memoryProjectRoot } from "./research-memory.ts";

const TOOLS = new Set(["bash", "read", "grep", "ffgrep", "find", "fffind", "ls"]);
const LIMIT = 10_000;
type Output = Pick<ToolResultEvent, "toolName" | "content"> & Partial<Pick<ToolResultEvent, "toolCallId" | "input" | "details" | "isError">>;
type BoundOutput = { content: ToolResultEvent["content"]; details?: unknown; isError?: boolean };
type Archive = { sha256: string; path: string; originalChars: number };

function metadata(details: unknown): Archive | undefined {
	if (!details || typeof details !== "object" || Array.isArray(details)) return undefined;
	const value = (details as Record<string, unknown>).workflowOutput;
	if (!value || typeof value !== "object") return undefined;
	const item = value as Archive;
	return typeof item.sha256 === "string" && typeof item.path === "string" ? item : undefined;
}

export async function boundOutput(output: Output, root: string, sessionId: string, limit = LIMIT): Promise<BoundOutput | undefined> {
	if (!TOOLS.has(output.toolName)) return undefined;
	if (metadata(output.details)) return undefined;
	const text = output.content.filter((item) => item.type === "text").map((item) => item.text ?? "").join("\n\n");
	if (text.length <= limit) return undefined;
	const sha256 = createHash("sha256").update(text).digest("hex");
	const session = createHash("sha256").update(sessionId).digest("hex").slice(0, 20);
	const directory = join(root, ".pi", "workflow-output", session);
	const path = join(directory, `${sha256}.txt`);
	try {
		await mkdir(directory, { recursive: true, mode: 0o700 });
		await writeFile(path, text, { encoding: "utf8", mode: 0o600, flag: "wx" }).catch((error: NodeJS.ErrnoException) => {
			if (error.code !== "EEXIST") throw error;
		});
	} catch {
		// 无法保存完整输出时保留原结果，避免丢失证据。
		return undefined;
	}
	const archive: Archive = { sha256, path, originalChars: text.length };
	const sourcePath = output.input?.path;
	// 指令文件首次读取保持完整；来源未知的旧 read 结果也保守保留。
	const preserveInstructions = output.toolName === "read" && (
		typeof sourcePath !== "string" || /(?:^|\/)(?:AGENTS|SKILL)\.md$/.test(sourcePath)
	);
	const head = Math.floor(limit * 0.55);
	const tail = Math.floor(limit * 0.20);
	const excerpt = `${text.slice(0, head)}\n\n[输出中间部分已省略]\n\n${text.slice(-tail)}\n\n完整工具文本：${path}\n原始长度：${text.length} 字符。请按需读取对应行或提取字段；这段摘录不代表完整输出。`;
	const details = output.details && typeof output.details === "object" && !Array.isArray(output.details)
		? { ...output.details, workflowOutput: archive }
		: output.details === undefined ? { workflowOutput: archive } : undefined;
	return {
		content: preserveInstructions ? output.content : [{ type: "text", text: excerpt }, ...output.content.filter((item) => item.type !== "text")],
		...(details === undefined ? {} : { details }),
		isError: output.isError,
	};
}

export function registerWorkflowOutput(pi: ExtensionAPI): void {
	pi.on("tool_result", async (event, ctx) => {
		const root = memoryProjectRoot(ctx.cwd);
		if (!root) return;
		return await boundOutput(event, root, ctx.sessionManager.getSessionId());
	});
	pi.on("context", async (event, ctx) => {
		const root = memoryProjectRoot(ctx.cwd);
		if (!root) return;
		const messages = [...event.messages];
		const inputs = new Map<string, Record<string, unknown>>();
		for (const message of messages) {
			if (message.role !== "assistant" || !Array.isArray(message.content)) continue;
			for (const block of message.content) {
				if (block.type === "toolCall") inputs.set(block.id, block.arguments);
			}
		}
		const seen = new Set<string>();
		let changed = false;
		for (let index = messages.length - 1; index >= 0; index--) {
			const message = messages[index];
			if (message.role !== "toolResult") continue;
			const limited = await boundOutput({ ...message, input: inputs.get(message.toolCallId) } as unknown as Output, root, ctx.sessionManager.getSessionId());
			const current = limited ? { ...message, ...limited } : message;
			const archive = metadata(current.details);
			if (archive && seen.has(archive.sha256)) {
				messages[index] = { ...current, content: [
					{ type: "text", text: `同一上下文稍后已有相同工具输出。完整文本：${archive.path}` },
					...current.content.filter((item) => item.type !== "text"),
				] } as typeof message;
				changed = true;
			} else {
				if (archive) seen.add(archive.sha256);
				if (limited) {
					messages[index] = current as typeof message;
					changed = true;
				}
			}
		}
		if (changed) return { messages };
	});
}

export default registerWorkflowOutput;
