/** OpenCode 2 adapter. Keep the scoring/storage engine shared with the V1 plugin. */
import type { Context, Cleanup } from "@opencode/plugin/promise/plugin";
import { TokenOptimizerPlugin } from "./index.js";

type ContentPart = { type: string; text?: string };

/** The text a tool result carries. Structured `output` counts at its JSON size. */
export function textOf(result: unknown): string {
  if (typeof result === "string") return result;
  if (!result || typeof result !== "object") return "";
  const r = result as { output?: unknown; content?: string | readonly ContentPart[] };
  if (typeof r.output === "string") return r.output;
  if (typeof r.content === "string") return r.content;
  if (Array.isArray(r.content)) {
    const text = r.content.filter((c) => c?.type === "text").map((c) => c.text ?? "").join("\n");
    if (text) return text;
  }
  if (r.output !== undefined && r.output !== null) {
    try { return JSON.stringify(r.output) ?? ""; } catch { return ""; }
  }
  return "";
}

type Usage = { input?: number; output?: number; cache?: { read?: number; write?: number } } | undefined;

/**
 * One key per compaction's usage, whichever event reports it. OpenCode can send
 * the same compaction's usage on both `session.usage.recorded` and
 * `session.compaction.ended`; the same numbers collapse to one record.
 */
function compactionKey(sessionID: unknown, tokens: Usage): string {
  return `compaction:${String(sessionID)}:${tokens?.input ?? 0}:${tokens?.output ?? 0}:${tokens?.cache?.read ?? 0}:${tokens?.cache?.write ?? 0}`;
}

const BOUND = 1024;
function remember(set: Set<string>, key: string): boolean {
  if (set.has(key)) return false;
  set.add(key);
  if (set.size > BOUND) set.delete(set.values().next().value!);
  return true;
}

export async function setupV2(ctx: Context): Promise<Cleanup> {
  // V1 hashed the session's own worktree; `project.directory` is that same path.
  const projectDir = ctx.location.project.directory || ctx.location.project.canonical;
  const legacy = await TokenOptimizerPlugin({
    directory: ctx.location.directory,
    project: { id: ctx.location.project.id, worktree: projectDir },
  } as unknown as Parameters<typeof TokenOptimizerPlugin>[0], ctx.options);
  const abort = new AbortController();
  let closed = false;
  // Admission may retry the same message, and replayed events may repeat. Both sets are bounded.
  const admitted = new Set<string>();
  const handled = new Set<string>();

  const registrations: { dispose: () => Promise<void> }[] = [];
  const register = async (promise: Promise<{ dispose: () => Promise<void> }>) => { registrations.push(await promise); };
  const disposeAll = async () => {
    for (const registration of registrations.reverse()) {
      try { await registration.dispose(); } catch (error) { console.warn("[Token Optimizer] V2 dispose failed:", error); }
    }
    registrations.length = 0;
  };

  let loop: Promise<void> = Promise.resolve();
  try {
    await register(ctx.tool.transform((editor) => {
      for (const name of ["token_status", "token_dashboard"] as const) {
        const tool = legacy.tool?.[name];
        if (!tool) continue;
        editor.add({
          name,
          description: tool.description,
          input: { type: "object", properties: name === "token_status" ? { detail: { type: "boolean" } } : { days: { type: "number" } }, additionalProperties: false },
          async execute(input, context) {
            const result = name === "token_status"
              ? await (legacy as typeof legacy & { statusForSession: (id: string, args: { detail?: boolean }) => Promise<{ output: string }> }).statusForSession(context.sessionID, input as { detail?: boolean })
              : await tool.execute(input as never, {} as never);
            return { content: typeof result === "string" ? result : result.output ?? "" };
          },
        });
      }
    }));
    await register(ctx.shell.hook("create.before", async (event) => {
      if (!event?.env) return;
      const env: Record<string, string> = Object.fromEntries(Object.entries(event.env).filter((entry): entry is [string, string] => typeof entry[1] === "string"));
      await legacy["shell.env"]?.({} as never, { env });
      Object.assign(event.env, env);
    }));
    await register(ctx.session.hook("prompt", async (event) => {
      const text = event?.prompt?.text;
      if (typeof text !== "string" || !event.sessionID) return;
      // Dedup only a known message; a missing id must not swallow later prompts.
      if (event.messageID && !remember(admitted, `${event.sessionID}:${event.messageID}`)) return;
      await legacy["chat.message"]?.({ sessionID: event.sessionID } as never, {
        parts: [{ type: "text", text }],
      } as never);
    }));
    await register(ctx.tool.hook("execute.before", async (event) => {
      await legacy["tool.execute.before"]?.({ tool: event.tool, sessionID: event.sessionID } as never, { args: event.input } as never);
    }));
    await register(ctx.tool.hook("execute.after", async (event) => {
      // A failed tool is recorded as one: the engine reads failures from the text.
      const output = event.status === "completed"
        ? textOf(event.result)
        : `Error: ${String(event.error?.message ?? "tool failed")}`;
      await legacy["tool.execute.after"]?.({ tool: event.tool, sessionID: event.sessionID, args: event.input } as never, { output } as never);
    }));
    await register(ctx.session.hook("context", async (event) => {
      // This is model-visible only; never mutate the durable conversation.
      const system: string[] = [];
      await legacy["experimental.chat.system.transform"]?.({
        sessionID: event.sessionID, model: { id: event.model?.id },
      } as never, { system } as never);
      for (const text of system) event.system.push({ type: "text", text });
    }));
    await register(ctx.session.hook("compaction", async (event) => {
      // Native compaction still owns summarization. We contribute only instructions.
      const context: string[] = [];
      await legacy["experimental.session.compacting"]?.({ sessionID: event.sessionID } as never, { context } as never);
      for (const text of context) event.system.push({ type: "text", text });
    }));
  } catch (error) {
    // A half-registered plugin would track some events and miss others: undo it all.
    await disposeAll();
    await (legacy as typeof legacy & { dispose?: () => Promise<void> }).dispose?.();
    throw error;
  }

  const usage = async (id: string, sessionID: unknown, tokens: Usage, cost: unknown, modelID?: string, activityAt?: number) => {
    await legacy.event?.({ event: {
      type: "message.updated", properties: { info: {
        role: "assistant", id, sessionID, tokens, cost, ...(modelID ? { modelID } : {}),
        ...(activityAt !== undefined ? { time: { created: activityAt } } : {}),
      } },
    } } as never);
  };

  loop = (async () => {
    try {
      for await (const event of ctx.event.subscribe({ signal: abort.signal })) {
        if (abort.signal.aborted) break;
        // Durable events can be replayed; each is handled once.
        if (event.id && !remember(handled, event.id)) continue;
        try {
          const data = event.data as Record<string, unknown>;
          const rawTime = (event as unknown as { timestamp?: unknown }).timestamp;
          const eventTime = typeof rawTime === "string" ? Date.parse(rawTime) : typeof rawTime === "number" ? rawTime : NaN;
          const activityAt = Number.isFinite(eventTime) ? eventTime : undefined;
          if (event.type === "session.created") {
            await legacy.event?.({ event: { type: "session.created", properties: { info: { id: data.sessionID } } } } as never);
          } else if (event.type === "session.step.ended" || event.type === "session.step.failed") {
            // One final usage per assistant step; a failed step can still have been billed.
            if (data.tokens || data.cost) await usage(event.id, data.sessionID, data.tokens as Usage, data.cost, undefined, activityAt);
          } else if ((event.type as string) === "session.usage.recorded") {
            // Auxiliary requests (title, compaction). Not in 2.0.18's plugin event
            // list; handled for hosts that deliver it, deduped against compaction.ended.
            const tokens = data.tokens as Usage;
            const id = data.source === "compaction" ? compactionKey(data.sessionID, tokens) : String((event as { id: string }).id);
            await usage(id, data.sessionID, tokens, data.cost, undefined, activityAt);
          } else if (event.type === "session.compaction.ended") {
            if (data.tokens || data.cost) {
              const tokens = data.tokens as Usage;
              await usage(compactionKey(data.sessionID, tokens), data.sessionID, tokens, data.cost,
                (data.model as { id?: string } | undefined)?.id, activityAt);
            }
            await legacy["experimental.compaction.autocontinue"]?.({ sessionID: data.sessionID } as never, {} as never);
          } else if (event.type === "session.idle") {
            await legacy.event?.({ event: { type: "session.idle", properties: { sessionID: data.sessionID } } } as never);
          } else if (event.type === "session.deleted") {
            await legacy.event?.({ event: { type: "session.deleted", properties: { info: { id: data.sessionID } } } } as never);
          }
        } catch (error) { console.warn("[Token Optimizer] V2 event error:", error); }
      }
    } catch (error) {
      if (!abort.signal.aborted) console.warn("[Token Optimizer] V2 event subscription failed:", error);
    }
  })();

  return async () => {
    if (closed) return;
    closed = true;
    abort.abort();
    // Let an in-flight event finish before the stores close, but never hang on it.
    await Promise.race([loop, new Promise((resolve) => setTimeout(resolve, 1000))]);
    await disposeAll();
    await (legacy as typeof legacy & { dispose?: () => Promise<void> }).dispose?.();
  };
}
