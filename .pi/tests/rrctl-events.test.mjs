import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { buildWaitCommand, registerRrctlEvents, resetRrctlEventsBroker, rrctlEventsBroker } from "../extensions/rrctl-events.ts";

class FakeChild extends EventEmitter {
	constructor(pid = 4000 + Math.floor(Math.random() * 1000)) {
		super();
		this.pid = pid;
		this.exitCode = null;
		this.signalCode = null;
		this.stdout = new PassThrough();
		this.stderr = new PassThrough();
	}

	kill() {
		return true;
	}

	finish(code = 0, signal = null) {
		this.exitCode = code;
		this.signalCode = signal;
		this.emit("exit", code, signal);
	}

	fail(error) {
		this.emit("error", error);
	}
}

function fakePi(sessionId) {
	const handlers = new Map();
	const messages = [];
	const progress = [];
	const pi = {
		sessionId,
		handlers,
		messages,
		progress,
		tool: undefined,
		stale: false,
		registerTool(tool) {
			pi.tool = tool;
		},
		on(event, handler) {
			const list = handlers.get(event) ?? [];
			list.push(handler);
			handlers.set(event, list);
			return () => {};
		},
		sendUserMessage(text, options) {
			if (pi.stale) throw new Error("This extension ctx is stale after session replacement or reload.");
			messages.push({ text, options });
		},
		sendMessage(message, options) {
			if (pi.stale) throw new Error("This extension ctx is stale after session replacement or reload.");
			progress.push({ message, options });
		},
		async emit(event, context) {
			for (const handler of handlers.get(event) ?? []) await handler({ type: event }, context);
		},
	};
	return pi;
}

function sessionContext(root, sessionId) {
	return { cwd: root, sessionManager: { getSessionId: () => sessionId } };
}

async function fixture(t) {
	const root = await mkdtemp(join(tmpdir(), "pi-rrctl-events-"));
	t.after(() => rm(root, { recursive: true, force: true }));
	await mkdir(join(root, ".agents/harness/remote"), { recursive: true });
	await writeFile(join(root, ".agents/harness/remote/remote_run.py"), "# fixture\n");
	await writeFile(join(root, "runspec.json"), JSON.stringify({ schema_version: "rrctl.run.v1", run_id: "RUN-A" }));
	return root;
}

/** 启动一个 extension 实例并触发它的 session_start，返回该实例、spawn 记录。 */
async function boot(root, sessionId, spawns, existing) {
	const pi = existing ?? fakePi(sessionId);
	const spawnChild = (command, args, options) => {
		const child = new FakeChild();
		spawns.push({ child, command, args, options });
		return child;
	};
	registerRrctlEvents(pi, { spawnChild });
	await pi.emit("session_start", sessionContext(root, sessionId));
	return pi;
}

async function waitOn(pi, root, params = { runspec: "runspec.json" }) {
	return await pi.tool.execute("call", params, undefined, undefined, sessionContext(root, pi.sessionId));
}

function eventPathFor(root, sessionId) {
	const runspec = resolve(root, "runspec.json");
	const digest = createHash("sha256").update(`${sessionId}\0${runspec}`).digest("hex").slice(0, 20);
	return join(root, ".pi/rrctl-events", `${digest}.event.json`);
}

/** 断言唤醒没有经过 send 失败兜底：绑定必须始终指向当前活跃 session。 */
function assertNoNotificationFailures(logPath) {
	assert.doesNotMatch(readFileSync(logPath, "utf8"), /notification failed/);
}

test("Pi rrctl wait uses one indefinite terminal/attention observer", async (t) => {
	const root = await fixture(t);
	const command = buildWaitCommand(root, { runspec: "runspec.json", resume: true });
	assert.equal(command.runId, "RUN-A");
	assert.equal(command.argv[command.argv.indexOf("--max-wait-seconds") + 1], "0");
	assert.ok(command.argv.includes("--resume"));
	assert.ok(command.argv.includes("--observe-only"));
});

test("Pi rrctl wait rejects paths outside the project", async (t) => {
	const root = await fixture(t);
	assert.throws(() => buildWaitCommand(root, { runspec: "../outside.json" }), /inside the project root/);
});

test("Pi ends the current turn and wakes exactly once on terminal state", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	await writeFile(join(root, ".agents/harness/remote/remote_run.py"), "raise SystemExit(0)\n");
	const pi = fakePi("session-real");
	registerRrctlEvents(pi);
	const result = await pi.tool.execute("call", { runspec: "runspec.json" }, undefined, undefined, sessionContext(root, "session-real"));
	assert.equal(result.terminate, true);
	for (let index = 0; index < 50 && pi.messages.length === 0; index++) {
		await new Promise((resolvePromise) => setTimeout(resolvePromise, 10));
	}
	assert.equal(pi.messages.length, 1);
	assert.match(pi.messages[0].text, /terminal event for RunID RUN-A/);
	assert.equal(rrctlEventsBroker().waits.size, 1);
	assert.equal([...rrctlEventsBroker().waits.values()][0].settled, true);
});

test("Pi settles a fake child exactly once and delivers to the active session", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const pi = await boot(root, "session-a", spawns);
	const result = await waitOn(pi, root);
	assert.equal(result.terminate, true);
	assert.equal(spawns.length, 1);
	assert.equal(spawns[0].command, "python3");
	assert.ok(spawns[0].args.includes("--max-wait-seconds"));
	spawns[0].child.finish(0, null);
	assert.equal(pi.messages.length, 1);
	assert.match(pi.messages[0].text, /terminal event for RunID RUN-A/);
	assert.deepEqual(JSON.parse(readFileSync(result.details.event_path, "utf8")).kind, "terminal");
	assert.equal(rrctlEventsBroker().waits.size, 1);
	assert.equal([...rrctlEventsBroker().waits.values()][0].settled, true);
});

test("Pi settles once when error and exit both fire", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const pi = await boot(root, "session-a", spawns);
	const result = await waitOn(pi, root);
	spawns[0].child.fail(new Error("spawn failed"));
	spawns[0].child.finish(1, null);
	assert.equal(pi.messages.length, 1);
	assert.match(pi.messages[0].text, /attention event for RunID RUN-A/);
	const event = JSON.parse(readFileSync(result.details.event_path, "utf8"));
	assert.equal(event.kind, "attention");
	assert.match(event.error, /spawn failed/);
	assert.equal(rrctlEventsBroker().waits.size, 1);
	assert.equal([...rrctlEventsBroker().waits.values()][0].settled, true);
});

for (const reason of ["new", "resume", "fork", "reload"]) {
	for (const timing of ["gap", "after"]) {
		test(`Pi rebinds a running waiter across session ${reason} (${timing}) without stale ctx`, async (t) => {
			resetRrctlEventsBroker();
			const root = await fixture(t);
			const spawns = [];
			const first = await boot(root, "session-a", spawns);
			const result = await waitOn(first, root);
			await first.emit("session_shutdown", sessionContext(root, "session-a"));
			first.stale = true; // 模拟 Pi 在 replacement 后使旧 pi 失效：任何调用都会抛。
			assert.equal(rrctlEventsBroker().binding, undefined);

			let second;
			if (timing === "after") {
				second = await boot(root, "session-b", spawns);
			}
			spawns[0].child.finish(0, null);
			if (timing === "gap") {
				second = await boot(root, "session-b", spawns);
			}
			assert.equal(first.messages.length, 0);
			assert.equal(second.messages.length, 1);
			assert.match(second.messages[0].text, /terminal event for RunID RUN-A/);
			assertNoNotificationFailures(result.details.log_path);
			assert.equal(rrctlEventsBroker().pending.length, 0);
			assert.equal(rrctlEventsBroker().waits.size, 1);
	assert.equal([...rrctlEventsBroker().waits.values()][0].settled, true);
		});
	}
}

test("Pi keeps the wake pending after quit and flushes it in the next session", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const first = await boot(root, "session-a", spawns);
	const result = await waitOn(first, root);
	await first.emit("session_shutdown", sessionContext(root, "session-a"));
	first.stale = true;
	spawns[0].child.finish(2, null);
	assert.equal(first.messages.length, 0);
	assert.equal(rrctlEventsBroker().pending.length, 1);
	assert.equal(JSON.parse(readFileSync(result.details.event_path, "utf8")).exit_code, 2);
	const second = await boot(root, "session-b", spawns);
	assert.equal(second.messages.length, 1);
	assert.match(second.messages[0].text, /attention event for RunID RUN-A/);
	assertNoNotificationFailures(result.details.log_path);
	assert.equal(rrctlEventsBroker().pending.length, 0);
});

test("Pi reuses a running waiter after reload instead of spawning a second observer", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const first = await boot(root, "session-a", spawns);
	await waitOn(first, root);
	await first.emit("session_shutdown", sessionContext(root, "session-a"));
	first.stale = true;
	const second = await boot(root, "session-b", spawns);
	const reused = await waitOn(second, root);
	assert.equal(reused.details.reused, true);
	assert.equal(spawns.length, 1);
	spawns[0].child.finish(0, null);
	assert.equal(first.messages.length, 0);
	assert.equal(second.messages.length, 1);
	assertNoNotificationFailures(reused.details.event_path.replace(/\.event\.json$/, ".log"));
});

test("Pi does not let a late shutdown clear the successor binding", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const first = await boot(root, "session-a", spawns);
	const result = await waitOn(first, root);
	await first.emit("session_shutdown", sessionContext(root, "session-a"));
	const second = await boot(root, "session-b", spawns);
	await first.emit("session_shutdown", sessionContext(root, "session-a")); // 重复触发也必须幂等。
	assert.equal(rrctlEventsBroker().binding.sessionId, "session-b");
	spawns[0].child.finish(0, null);
	assert.equal(second.messages.length, 1);
	assert.equal(first.messages.length, 0);
	assertNoNotificationFailures(result.details.log_path);
});

test("Pi suspends a wake for another project root until that root starts", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const other = await fixture(t);
	const spawns = [];
	const first = await boot(root, "session-a", spawns);
	await waitOn(first, root);
	await first.emit("session_shutdown", sessionContext(root, "session-a"));
	first.stale = true;
	spawns[0].child.finish(0, null);
	assert.equal(rrctlEventsBroker().pending.length, 1);
	const foreign = await boot(other, "session-other", spawns);
	assert.equal(foreign.messages.length, 0);
	assert.equal(rrctlEventsBroker().pending.length, 1);
	const same = await boot(root, "session-b", spawns);
	assert.equal(same.messages.length, 1);
	assert.equal(rrctlEventsBroker().pending.length, 0);
});

test("Pi still wakes once when the event file cannot be written", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const eventPath = eventPathFor(root, "session-a");
	mkdirSync(eventPath, { recursive: true }); // 目标位置是目录，原子落盘必然失败。
	const spawns = [];
	const pi = await boot(root, "session-a", spawns);
	await waitOn(pi, root);
	spawns[0].child.finish(0, null);
	assert.equal(pi.messages.length, 1);
	assert.match(pi.messages[0].text, /terminal event for RunID RUN-A/);
	assert.match(pi.messages[0].text, /Event file write failed/);
	assert.equal(rrctlEventsBroker().waits.size, 1);
	assert.equal([...rrctlEventsBroker().waits.values()][0].settled, true);
});

test("Pi never lets a send failure escape the child callback", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const first = await boot(root, "session-a", spawns);
	const result = await waitOn(first, root);
	first.sendUserMessage = () => {
		throw new Error("send failed");
	};
	spawns[0].child.finish(0, null); // 发送失败不能让 EventEmitter callback 逃逸成 uncaughtException。
	assert.equal(rrctlEventsBroker().pending.length, 1);
	assert.match(readFileSync(result.details.log_path, "utf8"), /notification failed: Error: send failed/);
	await first.emit("session_shutdown", sessionContext(root, "session-a"));
	first.stale = true;
	const second = await boot(root, "session-b", spawns);
	assert.equal(second.messages.length, 1);
	assert.match(second.messages[0].text, /terminal event for RunID RUN-A/);
	assert.equal(rrctlEventsBroker().pending.length, 0);
});

test("Pi preserves queued notifications when a flush send fails", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	await writeFile(join(root, "runspec-b.json"), JSON.stringify({ schema_version: "rrctl.run.v1", run_id: "RUN-B" }));
	const spawns = [];
	const first = await boot(root, "session-a", spawns);
	await waitOn(first, root);
	await waitOn(first, root, { runspec: "runspec-b.json" });
	await first.emit("session_shutdown", sessionContext(root, "session-a"));
	first.stale = true;
	spawns[0].child.finish(0, null);
	spawns[1].child.finish(0, null);
	assert.equal(rrctlEventsBroker().pending.length, 2);

	const second = fakePi("session-b");
	let sends = 0;
	const original = second.sendUserMessage.bind(second);
	second.sendUserMessage = (text, options) => {
		sends += 1;
		if (sends === 1) throw new Error("flush failed");
		original(text, options);
	};
	await boot(root, "session-b", spawns, second);
	assert.equal(second.messages.length, 0);
	assert.equal(rrctlEventsBroker().pending.length, 2); // 失败项和其后的通知都保留。
	await second.emit("session_start", sessionContext(root, "session-b"));
	assert.equal(second.messages.length, 2);
	assert.match(second.messages[0].text, /RunID RUN-A/);
	assert.match(second.messages[1].text, /RunID RUN-B/);
	assert.equal(rrctlEventsBroker().pending.length, 0);
});

test("Pi rejects a missing project harness without spawning", async (t) => {
	resetRrctlEventsBroker();
	const root = await mkdtemp(join(tmpdir(), "pi-rrctl-events-"));
	t.after(() => rm(root, { recursive: true, force: true }));
	const spawns = [];
	const pi = await boot(root, "session-a", spawns);
	const result = await waitOn(pi, root);
	assert.equal(result.isError, true);
	assert.equal(spawns.length, 0);
	assert.equal(existsSync(join(root, ".pi/rrctl-events")), false);
});

test("Pi reuses a settled tombstone instead of respawning for the same event", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const pi = await boot(root, "session-a", spawns);
	const first = await waitOn(pi, root);
	assert.equal(first.details.reused, false);
	spawns[0].child.finish(0, null);
	assert.equal(pi.messages.length, 1);
	// 同一 RunID、同一 session、同一 runspec 的第二次 execute：命中 tombstone，
	// 不再 spawn 新 observer，也不重复向用户推送同一事件。
	const second = await waitOn(pi, root);
	assert.equal(second.details.reused, true);
	assert.equal(second.details.run_id, "RUN-A");
	assert.equal(second.details.event_path, first.details.event_path);
	assert.equal(spawns.length, 1);
	assert.equal(pi.messages.length, 1);
	assert.match(second.content[0].text, /already delivered/);
});

test("Pi respawns within the window when the settled event was attention, not terminal", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const pi = await boot(root, "session-a", spawns);
	const first = await waitOn(pi, root);
	assert.equal(first.details.reused, false);
	// attention: 非零退出表示控制面仍在推进终态，允许在 tombstone 窗口内重等新 observer。
	spawns[0].child.finish(2, null);
	assert.equal(pi.messages.length, 1);
	assert.match(pi.messages[0].text, /attention event for RunID RUN-A/);
	const second = await waitOn(pi, root);
	assert.equal(second.details.reused, false);
	assert.equal(spawns.length, 2);
	// 第二个 observer 到 terminal 后正常推送一次。
	spawns[1].child.finish(0, null);
	assert.equal(pi.messages.length, 2);
	assert.match(pi.messages[1].text, /terminal event for RunID RUN-A/);
});

test("Pi respawns after the settled tombstone has expired", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const pi = await boot(root, "session-a", spawns);
	const first = await waitOn(pi, root);
	spawns[0].child.finish(0, null);
	assert.equal(pi.messages.length, 1);
	// 手工把 tombstone 的 settledAt 推到窗口之外，模拟过期。
	const entry = [...rrctlEventsBroker().waits.values()][0];
	entry.settledAt = Date.now() - (60 * 60 * 1000 + 1);
	const second = await waitOn(pi, root);
	assert.equal(second.details.reused, false);
	assert.equal(spawns.length, 2);
	spawns[1].child.finish(0, null);
	assert.equal(pi.messages.length, 2);
});

function heartbeat(message = "训练进度：10/100，预计剩余 30 分钟", extra = {}) {
	return Buffer.from(`[rrctl-progress] ${JSON.stringify({ schema_version: "rrctl.progress.v1", run_id: "RUN-A", stage: "wait", message, ...extra })}\n`);
}

test("Pi streams chunked UTF-8 heartbeat to a visible non-waking message and keeps both logs", async (t) => {
	resetRrctlEventsBroker();
	let clock = Date.now();
	t.mock.method(Date, "now", () => clock);
	const root = await fixture(t);
	const spawns = [];
	const pi = await boot(root, "session-a", spawns);
	const result = await waitOn(pi, root);
	assert.deepEqual(spawns[0].options.stdio, ["ignore", "pipe", "pipe"]);
	const frame = heartbeat();
	const split = frame.indexOf(Buffer.from("训练")) + 1;
	spawns[0].child.stdout.write("ordinary stdout\n");
	spawns[0].child.stderr.write(frame.subarray(0, split));
	assert.equal(pi.progress.length, 0);
	spawns[0].child.stderr.write(frame.subarray(split));
	spawns[0].child.stderr.write(frame);
	assert.equal(pi.progress.length, 1);
	clock += 30 * 60 * 1000;
	spawns[0].child.stderr.write(frame); // An unchanged scheduled heartbeat is still user-visible.
	assert.equal(pi.progress.length, 2);
	assert.equal(pi.messages.length, 0);
	assert.equal(pi.progress[0].message.display, true);
	assert.equal(pi.progress[0].options.triggerTurn, false);
	assert.match(pi.progress[0].message.content, /训练进度/);
	assert.match(readFileSync(result.details.log_path, "utf8"), /ordinary stdout/);
	assert.match(readFileSync(result.details.log_path, "utf8"), /rrctl.progress.v1/);
	spawns[0].child.finish();
	assert.match(pi.messages[0].text, /continue pull\/ingest now/);
});

test("Pi bounds malformed/oversized frames and rejects wrong run or stage", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const pi = await boot(root, "session-a", spawns);
	await waitOn(pi, root);
	const stream = spawns[0].child.stderr;
	stream.write("[rrctl-progress] {bad json}\n[rrctl-progress] " + "x".repeat(70000));
	stream.write(heartbeat()); // Still part of the oversized line.
	stream.write(heartbeat("wrong", { run_id: "RUN-B" }));
	stream.write(heartbeat("wrong", { stage: "pull" }));
	assert.equal(pi.progress.length, 0);
	stream.write(heartbeat("有效进度"));
	assert.equal(pi.progress.length, 1);
	spawns[0].child.finish();
});

test("Pi heartbeat rebinds across reload and coalesces the replacement gap", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const first = await boot(root, "session-a", spawns);
	const result = await waitOn(first, root);
	await first.emit("session_shutdown", sessionContext(root, "session-a"));
	first.stale = true;
	spawns[0].child.stderr.write(heartbeat("旧进度"));
	spawns[0].child.stderr.write(heartbeat("最新进度"));
	assert.equal(rrctlEventsBroker().pending.length, 1);
	const second = await boot(root, "session-b", spawns);
	assert.equal(first.progress.length, 0);
	assert.equal(second.progress.length, 1);
	assert.equal(second.progress[0].message.content, "最新进度");
	spawns[0].child.stderr.write(heartbeat("新会话进度"));
	assert.equal(second.progress.length, 2);
	assert.equal(second.messages.length, 0);
	spawns[0].child.finish();
	assertNoNotificationFailures(result.details.log_path);
});

test("Pi keeps failed heartbeat pending without waking and discards it at terminal", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	const spawns = [];
	const first = await boot(root, "session-a", spawns);
	await waitOn(first, root);
	first.sendMessage = () => { throw new Error("heartbeat send failed"); };
	spawns[0].child.stderr.write(heartbeat());
	assert.equal(first.messages.length, 0);
	assert.equal(rrctlEventsBroker().pending.length, 1);
	await first.emit("session_shutdown", sessionContext(root, "session-a"));
	spawns[0].child.finish();
	const second = await boot(root, "session-b", spawns);
	assert.equal(second.progress.length, 0);
	assert.equal(second.messages.length, 1);
});

test("Pi real Python observer delivers heartbeat live and terminal without slow pull", async (t) => {
	resetRrctlEventsBroker();
	const root = await fixture(t);
	await writeFile(join(root, ".agents/harness/remote/remote_run.py"), `
import json, pathlib, sys, time
root = pathlib.Path.cwd()
frame = '[rrctl-progress] ' + json.dumps({'schema_version':'rrctl.progress.v1','run_id':'RUN-A','stage':'wait','message':'训练进度 10/100，预计剩余 30 分钟'}, ensure_ascii=False) + '\\n'
data = frame.encode()
for piece in (data[:31], data[31:]):
    sys.stderr.buffer.write(piece)
    sys.stderr.buffer.flush()
    time.sleep(0.02)
print('stdout evidence', flush=True)
deadline = time.monotonic() + 5
while not (root / 'release').exists() and time.monotonic() < deadline:
    time.sleep(0.01)
(root / 'compute-terminal').touch()
if '--observe-only' not in sys.argv:
    (root / 'pull-started').touch()
    time.sleep(3)
    (root / 'pull-done').touch()
`);
	const pi = fakePi("session-real");
	registerRrctlEvents(pi);
	await pi.emit("session_start", sessionContext(root, pi.sessionId));
	const result = await waitOn(pi, root);
	const entry = [...rrctlEventsBroker().waits.values()][0];
	t.after(() => { if (entry.child.exitCode === null) entry.child.kill(); });
	for (let index = 0; index < 200 && pi.progress.length === 0; index++) await new Promise((r) => setTimeout(r, 10));
	assert.equal(pi.progress.length, 1);
	assert.equal(pi.messages.length, 0);
	assert.equal(entry.child.exitCode, null);
	assert.match(pi.progress[0].message.content, /预计剩余/);
	await writeFile(join(root, "release"), "go");
	for (let index = 0; index < 100 && pi.messages.length === 0; index++) await new Promise((r) => setTimeout(r, 10));
	assert.equal(pi.messages.length, 1);
	assert.ok(existsSync(join(root, "compute-terminal")));
	assert.equal(existsSync(join(root, "pull-started")), false);
	assert.match(pi.messages[0].text, /continue pull\/ingest now/);
	assert.match(readFileSync(result.details.log_path, "utf8"), /stdout evidence/);
});
