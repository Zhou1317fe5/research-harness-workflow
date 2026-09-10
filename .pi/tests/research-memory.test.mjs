import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath, pathToFileURL } from "node:url";
import { registerResearchMemory } from "../extensions/research-memory.ts";

const root = fileURLToPath(new URL("../../", import.meta.url));

function fixture(runner) {
  const handlers = new Map();
  const calls = [];
  const entries = [];
  const pi = { on(name, callback) { handlers.set(name, callback); }, appendEntry(...args) { entries.push(args); } };
  registerResearchMemory(pi, async (project, action, payload) => {
    calls.push({ project, action, payload });
    return runner(action, payload);
  });
  const ctx = { cwd: root, sessionManager: { getSessionId: () => "fixture-session" }, hasUI: false };
  const emit = async (name, event = {}) => handlers.get(name)?.(event, ctx);
  return { handlers, calls, entries, emit };
}

function snapshot(revision, text) {
  return { hookSpecificOutput: { additionalContext: text, snapshotRevision: revision,
    generatedAt: "2026-09-10T12:00:00Z" } };
}

test("内部 Historian 和普通子代理均不注册回调", () => {
  for (const key of ["MAGIC_CONTEXT_PI_SUBAGENT", "PI_SUB_AGENT_DEPTH"]) {
    const original = process.env[key];
    try {
      process.env[key] = "1";
      assert.equal(fixture(async () => ({})).handlers.size, 0);
    } finally {
      if (original === undefined) delete process.env[key];
      else process.env[key] = original;
    }
  }
});

test("工具完成后读取新版本，历史数据位于真实用户消息之前", async () => {
  let revision = "old";
  const f = fixture(async () => snapshot(revision, revision === "old" ? "旧快照" : "已更新的快照"));
  await f.emit("session_start");
  await f.emit("input", { source: "interactive", text: "授权新任务" });
  await f.emit("before_agent_start", { prompt: "授权新任务" });
  revision = "new";
  await f.emit("tool_result");
  await f.emit("tool_result");
  await f.emit("tool_result");
  const message = { role: "user", content: [{ type: "text", text: "授权新任务" }], timestamp: 1 };
  const result = await f.emit("context", { messages: [message] });
  assert.equal(result.messages[0].content, "已更新的快照");
  assert.equal(result.messages.at(-1), message);
  assert.equal(f.calls.filter((call) => call.action === "scan").length, 1);
  assert.equal(f.calls.at(-1).payload.context_revision, "old");
});

test("同一版本不重复传正文，也不会删除有效缓存", async () => {
  const f = fixture(async (_action, payload) => payload.context_revision === "v1"
    ? { hookSpecificOutput: { snapshotRevision: "v1" } } : snapshot("v1", "稳定快照"));
  await f.emit("session_start");
  await f.emit("tool_result");
  const first = await f.emit("context", { messages: [] });
  await f.emit("tool_result");
  const second = await f.emit("context", { messages: [] });
  assert.equal(first.messages[0].content, "稳定快照");
  assert.deepEqual(first.messages[0], second.messages[0]);
});

test("停用回调时清除现存快照", async () => {
  let enabled = true;
  const f = fixture(async () => enabled ? snapshot("v1", "旧内容") : snapshot("disabled", ""));
  await f.emit("session_start");
  enabled = false;
  await f.emit("tool_result");
  const user = { role: "user", content: "当前指令", timestamp: 1 };
  const result = await f.emit("context", { messages: [
    { role: "custom", customType: "research-memory-context", content: "陈旧内容" }, user,
  ] });
  assert.deepEqual(result.messages, [user]);
});

test("扩展自动 follow-up 不会成为用户来源", async () => {
  const f = fixture(async () => snapshot("v1", "数据"));
  await f.emit("session_start");
  await f.emit("input", { source: "extension", text: "自动继续" });
  await f.emit("before_agent_start", { prompt: "自动继续" });
  assert.equal(f.calls.filter((call) => call.action === "prompt").length, 0);
});

test("回调错误清除缓存并允许下轮恢复", async () => {
  let failing = false;
  const f = fixture(async () => {
    if (failing) throw new Error("fixture failure");
    return snapshot("ok", "有效历史数据");
  });
  await f.emit("session_start");
  failing = true;
  await f.emit("tool_result");
  assert.deepEqual((await f.emit("context", { messages: [] })).messages, []);
  failing = false;
  assert.equal((await f.emit("context", { messages: [] })).messages[0].content, "有效历史数据");
  assert.equal(f.entries.length, 1);
});

test("真实 Pi 消息转换仍保持用户指令在历史数据之后", { skip: !process.env.PI_MESSAGES_MODULE }, async () => {
  const { convertToLlm } = await import(pathToFileURL(process.env.PI_MESSAGES_MODULE));
  const f = fixture(async () => snapshot("v1", "历史数据"));
  await f.emit("session_start");
  const user = { role: "user", content: [{ type: "text", text: "最新任务" }], timestamp: 2 };
  const result = await f.emit("context", { messages: [user] });
  const wire = convertToLlm(result.messages);
  assert.equal(wire[0].content[0].text, "历史数据");
  assert.equal(wire.at(-1).content[0].text, "最新任务");
});
