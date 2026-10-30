/** Read-time exclusions for the append-only savings ledgers. */
export function countedEvents<T extends object>(rows: T[], kind: "savings" | "compression"): T[] {
  const seen = new Map<string, number[]>();
  return rows.filter((item) => {
    const row = item as Record<string, unknown>;
    const session = typeof row.session_id === "string" ? row.session_id : "";
    if (/^(?:test-session-|failwire-|f3bench-sess|unit-c-never|torture-|sess_test)/.test(session) || /^codex-sess-\d+$/.test(session)) return false;
    const type = String(kind === "savings" ? row.event_type ?? "" : row.feature ?? "");
    if (kind === "savings" && /^tool_archive(?:_refetch_block)?$/.test(type) &&
        /claude-in-chrome__computer|computer_use__|[Ss]creenshot/.test([row.detail, row.tool_name, row.tool].map((value) => String(value ?? "")).join(" "))) return false;
    if (row.tier === "opportunity") return false;
    const saved = kind === "savings" ? Number(row.tokens_saved) : Number(row.original_tokens) - Number(row.compressed_tokens);
    const ts = Date.parse(String(row.timestamp ?? ""));
    if (!session || !(saved > 0) || !Number.isFinite(ts)) return true;
    const parts = kind === "savings"
      ? [session, type, row.tokens_saved, row.detail ?? ""]
      : [session, type, row.command_pattern ?? "", row.original_tokens, row.compressed_tokens];
    const key = JSON.stringify(parts);
    const prior = seen.get(key) ?? [];
    const twin = prior.some((time) => Math.abs(ts - time) <= 3000);
    prior.push(ts);
    seen.set(key, prior);
    return !twin;
  });
}
