/** OpenCode 2 adapter. Keep the scoring/storage engine shared with the V1 plugin. */
import type { Context, Cleanup } from "@opencode/plugin/promise/plugin";
import { TokenOptimizerPlugin } from "./index.js";

function textOf(result: unknown): string {
  if (!result || typeof result !== "object") return "";
  const r = result as { output?: string; content?: string | readonly { type: string; text?: string }[] };
  return typeof r.output === "string" ? r.output : typeof r.content === "string" ? r.content : (r.content ?? []).filter((c) => c.type === "text").map((c) => c.text ?? "").join("\n");
}

export async function setupV2(ctx: Context): Promise<Cleanup> {
  const projectDir = ctx.location.project.canonical || ctx.location.project.directory;
  const legacy = await TokenOptimizerPlugin({
    directory: ctx.location.directory,
    project: { id: ctx.location.project.id, worktree: projectDir },
  } as unknown as Parameters<typeof TokenOptimizerPlugin>[0], ctx.options);
  const abort = new AbortController();
  let closed = false;
  // Admission may retry the same message. Keep only a bounded set of IDs.
  const admitted = new Set<string>();

  const registrations: { dispose: () => Promise<void> }[] = [];
  const register = async (promise: Promise<{ dispose: () => Promise<void> }>) => { registrations.push(await promise); };

  await register(ctx.tool.transform((editor) => {
    for (const name of ["token_status", "token_dashboard"] as const) {
      const tool = legacy.tool?.[name];
      if (!tool) continue;
      editor.add({
        name,
        description: name === "token_status" ? "Report context health, warnings, fill, and activity." : "Generate the local Token Optimizer dashboard.",
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
    const env: Record<string, string> = Object.fromEntries(Object.entries(event.env).filter((entry): entry is [string, string] => typeof entry[1] === "string"));
    await legacy["shell.env"]?.({} as never, { env });
    Object.assign(event.env, env);
  }));
  await register(ctx.session.hook("prompt", async (event) => {
    if (admitted.has(event.messageID)) return;
    admitted.add(event.messageID);
    if (admitted.size > 1024) admitted.delete(admitted.values().next().value!);
    await legacy["chat.message"]?.({ sessionID: event.sessionID } as never, {
      parts: [{ type: "text", text: event.prompt.text }],
    } as never);
  }));
  await register(ctx.tool.hook("execute.before", async (event) => {
    await legacy["tool.execute.before"]?.({ tool: event.tool, sessionID: event.sessionID } as never, { args: event.input } as never);
  }));
  await register(ctx.tool.hook("execute.after", async (event) => {
    const output = event.status === "completed" ? textOf(event.result) : String(event.error?.message ?? "Tool error");
    await legacy["tool.execute.after"]?.({ tool: event.tool, sessionID: event.sessionID, args: event.input } as never, { output } as never);
  }));
  await register(ctx.session.hook("context", async (event) => {
    // This is model-visible only; never mutate the durable conversation.
    const system: string[] = [];
    await legacy["experimental.chat.system.transform"]?.({
      sessionID: event.sessionID, model: { id: event.model.id },
    } as never, { system } as never);
    for (const text of system) event.system.push({ type: "text", text });
  }));
  await register(ctx.session.hook("compaction", async (event) => {
    // Native compaction still owns summarization. We contribute only instructions.
    const context: string[] = [];
    await legacy["experimental.session.compacting"]?.({ sessionID: event.sessionID } as never, { context } as never);
    for (const text of context) event.system.push({ type: "text", text });
  }));

  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({ signal: abort.signal })) {
        if (abort.signal.aborted) break;
        try {
          const data = event.data as Record<string, unknown>;
          if (event.type === "session.created") {
            await legacy.event?.({ event: { type: "session.created", properties: { info: { id: data.sessionID } } } } as never);
          } else if (event.type === "session.step.ended") {
            // One final usage per assistant step; unlike streaming deltas, this is final.
            await legacy.event?.({ event: {
              type: "message.updated", properties: { info: {
                role: "assistant", id: event.id, sessionID: data.sessionID,
                tokens: data.tokens, cost: data.cost,
              } },
            } } as never);
          } else if (event.type === "session.compaction.ended") {
            // Compaction is a separate model request, with its own usage/cost.
            if (data.tokens || data.cost) {
              await legacy.event?.({ event: {
                type: "message.updated", properties: { info: {
                  role: "assistant", id: event.id, sessionID: data.sessionID,
                  tokens: data.tokens, cost: data.cost,
                  modelID: (data.model as { id?: string } | undefined)?.id,
                } },
              } } as never);
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
    for (const registration of registrations.reverse()) await registration.dispose();
    await (legacy as typeof legacy & { dispose?: () => Promise<void> }).dispose?.();
  };
}
