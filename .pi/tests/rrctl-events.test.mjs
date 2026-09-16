import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { buildWaitCommand, registerRrctlEvents } from "../extensions/rrctl-events.ts";

test("Pi rrctl wait uses one indefinite terminal/attention observer", async (t) => {
	const root = await mkdtemp(join(tmpdir(), "pi-rrctl-events-"));
	t.after(() => rm(root, { recursive: true, force: true }));
	await mkdir(join(root, ".agents/harness/remote"), { recursive: true });
	await writeFile(join(root, ".agents/harness/remote/remote_run.py"), "# fixture\n");
	await writeFile(join(root, "runspec.json"), JSON.stringify({ schema_version: "rrctl.run.v1", run_id: "RUN-A" }));
	const command = buildWaitCommand(root, { runspec: "runspec.json", resume: true });
	assert.equal(command.runId, "RUN-A");
	assert.equal(command.argv[command.argv.indexOf("--max-wait-seconds") + 1], "0");
	assert.ok(command.argv.includes("--resume"));
});

test("Pi rrctl wait rejects paths outside the project", async (t) => {
	const root = await mkdtemp(join(tmpdir(), "pi-rrctl-events-"));
	t.after(() => rm(root, { recursive: true, force: true }));
	assert.throws(() => buildWaitCommand(root, { runspec: "../outside.json" }), /inside the project root/);
});

test("Pi ends the current turn and wakes exactly once on terminal state", async (t) => {
	const root = await mkdtemp(join(tmpdir(), "pi-rrctl-events-"));
	t.after(() => rm(root, { recursive: true, force: true }));
	await mkdir(join(root, ".agents/harness/remote"), { recursive: true });
	await writeFile(join(root, ".agents/harness/remote/remote_run.py"), "raise SystemExit(0)\n");
	await writeFile(join(root, "runspec.json"), JSON.stringify({ schema_version: "rrctl.run.v1", run_id: "RUN-A" }));
	let tool;
	const messages = [];
	registerRrctlEvents({
		registerTool(value) { tool = value; },
		sendUserMessage(value) { messages.push(value); },
	});
	const result = await tool.execute("call", { runspec: "runspec.json" }, undefined, undefined, {
		cwd: root,
		sessionManager: { getSessionId: () => "session-a" },
	});
	assert.equal(result.terminate, true);
	for (let index = 0; index < 50 && messages.length === 0; index++) {
		await new Promise((resolve) => setTimeout(resolve, 10));
	}
	assert.equal(messages.length, 1);
	assert.match(messages[0], /terminal event for RunID RUN-A/);
});
