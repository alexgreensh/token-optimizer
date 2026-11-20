/**
 * Context-fill estimate must follow the session instead of freezing at the
 * first (near-zero) value written to quality_cache.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { SessionStore } from "./storage/session-store.js";
import { TokenOptimizerPlugin } from "./index.js";
import { hashProjectDir } from "./util/env.js";

const SID = "fill-estimate-session";
const MODEL = "claude-sonnet-4-5"; // 200k window
const realNow = Date.now;
let clock = 0;
let plugin: any;
let dir: string;

beforeEach(async () => {
  clock = realNow();
  Date.now = () => clock;
  dir = `${process.env.TMPDIR || "/tmp"}/to-fill-estimate-${crypto.randomUUID()}`;
  plugin = await TokenOptimizerPlugin(
    { directory: process.cwd(), project: { id: "fill-test", worktree: process.cwd() } } as any,
    { dataDir: dir } as any,
  );
});

afterEach(async () => {
  Date.now = realNow;
  await plugin?.dispose?.();
});

async function userTurn(text = "please continue with the task at hand, thanks a lot") {
  await plugin["chat.message"](
    { sessionID: SID, model: { modelID: MODEL } },
    { parts: [{ type: "text", text }] },
  );
}

async function toolCall(chars: number, n = 1) {
  for (let i = 0; i < n; i++) {
    await plugin["tool.execute.after"](
      { sessionID: SID, tool: "bash", args: { command: "echo" } },
      { output: "x".repeat(chars) },
    );
  }
}

async function fillPercent(): Promise<number> {
  const out: string = (await plugin.statusForSession(SID, {})).output;
  const m = /Context Fill\*\*: ~(\d+)%/.exec(out);
  if (!m) throw new Error(`no fill in status:\n${out}`);
  return Number(m[1]);
}

function cachedFill(): number | null {
  const probe = new SessionStore(dir, SID, hashProjectDir(process.cwd()));
  return probe.getQualityCache()?.fill_pct ?? null;
}

describe("OpenCode context-fill estimate", () => {
  test("rises with the session instead of locking at the first estimate", async () => {
    await userTurn();
    expect(await fillPercent()).toBe(0);
    // 20 tool results x 20k chars = 400k chars ~ 100k tokens ~ 50% of 200k.
    await toolCall(20_000, 20);
    clock += 3 * 60 * 1000; // past the 2-minute quality throttle
    await toolCall(10);
    const fill = await fillPercent();
    expect(fill).toBeGreaterThanOrEqual(45);
    expect(fill).toBeLessThanOrEqual(56);
  });

  test("a tool result is counted once, not once per bookkeeping row", async () => {
    await userTurn();
    // 10 results x 40k chars = 400k chars ~ 100k tokens ~ 50%.
    await toolCall(40_000, 10);
    clock += 3 * 60 * 1000;
    await toolCall(10);
    expect(await fillPercent()).toBeLessThanOrEqual(56);
  });

  test("fill covers the whole session since compaction, not just the last rows", async () => {
    await userTurn();
    // 150 results x 5k chars = 750k chars ~ 187k tokens ~ 94% of 200k.
    await toolCall(5_000, 150);
    clock += 3 * 60 * 1000;
    await toolCall(10);
    expect(await fillPercent()).toBeGreaterThanOrEqual(88);
  });

  test("the persisted cache follows the estimate", async () => {
    await userTurn();
    await toolCall(20_000, 20);
    clock += 3 * 60 * 1000;
    await toolCall(10);
    expect(cachedFill()!).toBeGreaterThan(0.4);
  });

  test("fill drops after compaction clears the signal tables", async () => {
    await userTurn();
    await toolCall(20_000, 20);
    clock += 3 * 60 * 1000;
    await toolCall(10);
    expect(await fillPercent()).toBeGreaterThan(40);
    await plugin["experimental.compaction.autocontinue"]({ sessionID: SID }, {});
    expect(await fillPercent()).toBeLessThan(5);
  });

  test("fill-based warnings fire for a genuinely full session", async () => {
    await userTurn();
    await toolCall(20_000, 45);
    clock += 3 * 60 * 1000;
    await toolCall(10);
    const out: string = (await plugin.statusForSession(SID, {})).output;
    expect(out).toContain("context fill, compact now");
    expect(out).toContain("tool calls, instruction adherence severely degraded");
    expect(await fillPercent()).toBeGreaterThanOrEqual(90);
  });

  test("fill-based signals stay quiet for a small session", async () => {
    await userTurn();
    await toolCall(2_000, 10);
    clock += 3 * 60 * 1000;
    await toolCall(10);
    const out: string = (await plugin.statusForSession(SID, {})).output;
    expect(out).not.toContain("context fill");
    expect(await fillPercent()).toBeLessThanOrEqual(5);
  });

  test("row-cap pruning does not shrink cumulative fill", async () => {
    await userTurn();
    // 3000 results x 300 chars = 900k chars ~ 225k tokens > 200k window. The
    // signal tables are pruned to 2000 rows along the way (every 200 calls).
    await toolCall(300, 3000);
    const probe = new SessionStore(dir, SID, hashProjectDir(process.cwd()));
    expect(probe.getFillChars()).toBe(3000 * 300 + "please continue with the task at hand, thanks a lot".length);
    clock += 3 * 60 * 1000;
    await toolCall(10);
    expect(await fillPercent()).toBeGreaterThanOrEqual(95);
    expect(await fillPercent()).toBeLessThanOrEqual(100);
  });

  test("counter is idempotent per recorded row, role-filtered and reset on compaction", () => {
    const store = new SessionStore(dir, "counter-session", hashProjectDir(process.cwd()));
    expect(store.getFillChars()).toBe(0);
    store.recordMessage(1, "user", 100, true);
    store.recordMessage(2, "tool_result", 400, true);
    store.recordMessage(3, "assistant", 400, true);
    expect(store.getFillChars()).toBe(500);
    store.capSignalTables(1);
    expect(store.getFillChars()).toBe(500);
    expect(store.getFillChars()).toBe(500); // reads do not change it
    store.resetSignalAccumulators();
    expect(store.getFillChars()).toBe(0);
    store.recordMessage(4, "tool_result", 70, true);
    expect(store.getFillChars()).toBe(70);
  });

  test("a failed insert leaves the counter unchanged, also inside an outer transaction", () => {
    const store = new SessionStore(dir, "atomic-session", hashProjectDir(process.cwd()));
    store.recordMessage(1, "user", 100, true);
    expect(() => store.recordMessage(null as any, "user", 50, true)).toThrow();
    expect(store.getFillChars()).toBe(100);
    const db = store.connect();
    expect(() =>
      db.transaction(() => {
        store.recordMessage(2, "tool_result", 70, true);
        store.recordMessage(null as any, "tool_result", 30, true);
      })(),
    ).toThrow();
    expect(store.getFillChars()).toBe(100); // the whole outer transaction rolled back
    db.transaction(() => {
      store.recordMessage(3, "tool_result", 70, true);
      store.recordMessage(4, "assistant", 70, true);
    })();
    expect(store.getFillChars()).toBe(170);
  });

  test("steady-state recordMessage cost does not grow with the messages table", () => {
    function timed(name: string, preRows: number): { ms: number; store: SessionStore } {
      const store = new SessionStore(dir, name, hashProjectDir(process.cwd()));
      const db = store.connect();
      db.transaction(() => {
        for (let i = 0; i < preRows; i++) db.run("INSERT INTO messages (idx, role, text_length, is_substantive, timestamp) VALUES (?, 'user', 1, 0, 0)", [i]);
      })();
      store.recordMessage(1, "user", 1, false); // seeds the counter from the table once
      const t0 = performance.now();
      for (let i = 0; i < 300; i++) store.recordMessage(i, "tool_result", 1, false);
      return { ms: performance.now() - t0, store };
    }
    timed("warmup-session", 10);
    const small = timed("perf-small", 10);
    const big = timed("perf-big", 100000);
    // A SUM over the big table on every call would cost ~3 ms per call, many
    // times the small-table cost. Allow generous noise headroom.
    expect(big.ms).toBeLessThan(small.ms * 3 + 40);
    expect(big.store.getFillChars()).toBe(100001 + 300);
  });

  test("a session recorded before the counter existed carries its history forward", () => {
    const store = new SessionStore(dir, "legacy-session", hashProjectDir(process.cwd()));
    store.recordMessage(1, "user", 100, true);
    store.recordMessage(2, "tool_result", 400, true);
    // Simulate the old schema state: rows exist, no counter key.
    store.connect().run("DELETE FROM session_meta WHERE key = 'fill_chars'");
    expect(store.getFillChars()).toBe(500); // fallback to the table sum
    store.recordMessage(3, "tool_result", 50, true);
    expect(store.getFillChars()).toBe(550); // seeded from history, then counted
    store.recordMessage(4, "user", 5, true);
    expect(store.getFillChars()).toBe(555);
  });

  test("within the quality throttle window the estimator does no extra reads", async () => {
    await userTurn();
    await toolCall(1_000); // first computation, starts the throttle window
    const names = ["getRecentMessages", "getRecentToolResults", "getFillChars"] as const;
    const calls: Record<string, number> = {};
    const originals: Array<() => void> = [];
    for (const name of names) {
      const proto: any = SessionStore.prototype;
      if (typeof proto[name] !== "function") continue;
      const orig = proto[name];
      calls[name] = 0;
      proto[name] = function (this: unknown, ...args: unknown[]) { calls[name]++; return orig.apply(this, args); };
      originals.push(() => { proto[name] = orig; });
    }
    try {
      clock += 10_000;
      await toolCall(1_000, 5);
      await userTurn();
      expect(Object.values(calls).reduce((a, b) => a + b, 0)).toBe(0);
    } finally {
      for (const restore of originals) restore();
    }
  });
});
