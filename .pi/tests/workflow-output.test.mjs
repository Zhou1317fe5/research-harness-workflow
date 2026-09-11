import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { boundOutput, registerWorkflowOutput } from "../extensions/workflow-output.ts";

async function project(t) {
  const root = await mkdtemp(join(tmpdir(), "workflow-output-"));
  await mkdir(join(root, ".agents/harness/memory"), { recursive: true });
  await writeFile(join(root, ".agents/harness/memory/research_memory.py"), "# fixture\n");
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

test("短输出保持原样", async (t) => {
  const root = await project(t);
  assert.equal(await boundOutput({ toolName: "bash", content: [{ type: "text", text: "ok" }], isError: false }, root, "s"), undefined);
});

test("长报错保留首尾、错误状态、图片和完整私有文件", async (t) => {
  const root = await project(t);
  const text = "START\n" + "line\n".repeat(4000) + "ERROR-END";
  const picture = { type: "image", data: "fixture", mimeType: "image/png" };
  const result = await boundOutput({ toolName: "bash", content: [{ type: "text", text }, picture], isError: true, details: { exitCode: 2 } }, root, "s");
  assert.equal(result.isError, true);
  assert.equal(result.details.exitCode, 2);
  assert.ok(result.content[0].text.startsWith("START"));
  assert.ok(result.content[0].text.includes("ERROR-END"));
  assert.ok(result.content[0].text.length < 10000);
  assert.equal(result.content[1], picture);
  assert.equal(await readFile(result.details.workflowOutput.path, "utf8"), text);
  assert.equal((await stat(result.details.workflowOutput.path)).mode & 0o777, 0o600);
});

test("上下文只折叠仍有后续副本的相同工具结果", async (t) => {
  const root = await project(t);
  const handlers = new Map();
  registerWorkflowOutput({ on: (name, callback) => handlers.set(name, callback) });
  const ctx = { cwd: root, sessionManager: { getSessionId: () => "s" } };
  const text = "same data\n".repeat(2000);
  const user = { role: "user", content: [{ type: "text", text: "instruction ".repeat(2000) }] };
  const memory = { role: "custom", customType: "research-memory-context", content: "important decision ".repeat(2000) };
  const message = (id) => ({ role: "toolResult", toolName: "bash", toolCallId: id, content: [{ type: "text", text }], isError: false });
  const result = await handlers.get("context")({ messages: [memory, user, message("1"), message("2")] }, ctx);
  assert.equal(result.messages[0], memory);
  assert.equal(result.messages[1], user);
  assert.ok(result.messages[2].content[0].text.length < 500);
  assert.ok(result.messages[3].content[0].text.includes("same data"));
  assert.ok(result.messages[3].content[0].text.length < 10000);
});

test("变化后的输出不被判为重复，按需读取短片段不受影响", async (t) => {
  const root = await project(t);
  const first = await boundOutput({ toolName: "read", content: [{ type: "text", text: "a".repeat(15000) }] }, root, "s");
  const second = await boundOutput({ toolName: "read", content: [{ type: "text", text: "b".repeat(15000) }] }, root, "s");
  assert.notEqual(first.details.workflowOutput.path, second.details.workflowOutput.path);
  assert.equal(await boundOutput({ toolName: "read", content: [{ type: "text", text: "a".repeat(1000) }] }, root, "s"), undefined);
});

test("归档不可写时保留全部结果", async (t) => {
  const root = await project(t);
  await writeFile(join(root, ".pi"), "fixture blocks directory");
  assert.equal(await boundOutput({ toolName: "bash", content: [{ type: "text", text: "x".repeat(15000) }] }, root, "s"), undefined);
});

test("首次读取指令文件保持完整，代码大输出仍按预算处理", async (t) => {
  const root = await project(t);
  const content = [{ type: "text", text: "instruction\n".repeat(2000) }];
  for (const path of ["AGENTS.md", "/project/.codex/skills/mission/SKILL.md"]) {
    const result = await boundOutput({ toolName: "read", input: { path }, content }, root, "s");
    assert.equal(result.content, content);
  }
  const result = await boundOutput({ toolName: "read", input: { path: "/project/code.py" }, content }, root, "s");
  assert.ok(result.content[0].text.length < 10000);
});
