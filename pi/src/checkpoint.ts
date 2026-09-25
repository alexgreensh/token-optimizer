import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { dataDir } from "./state.ts";
import { redact } from "./redact.ts";
export type Checkpoint = { session: string; branchLeaf: string; goals: string[]; files: string[]; createdAt: string };
const hash = (text: string) => createHash("sha256").update(text).digest("hex").slice(0, 24);
/** Keep branch-specific, redacted lightweight state. Pi remains the compactor. */
export function writeCheckpoint(value: Checkpoint, root = dataDir()): string {
  const dir = join(root, "checkpoints"); mkdirSync(dir, { recursive: true, mode: 0o700 });
  const id = hash(`${value.session}\0${value.branchLeaf}`);
  const contents = JSON.stringify({ ...value, goals: value.goals.map(redact), files: value.files.map(redact) });
  writeFileSync(join(dir, `${id}.json`), contents, { mode: 0o600 });
  return id;
}
export function readCheckpoint(session: string, branchLeaf: string, root = dataDir()): Checkpoint | undefined {
  try {
    const id = hash(`${session}\0${branchLeaf}`);
    const parsed: unknown = JSON.parse(readFileSync(join(root, "checkpoints", `${id}.json`), "utf8"));
    if (!parsed || typeof parsed !== "object") return;
    const c = parsed as Checkpoint;
    if (c.session !== session || c.branchLeaf !== branchLeaf || !Array.isArray(c.goals) || !Array.isArray(c.files)) return;
    return c;
  } catch { return; }
}
export function checkpointFromBranch(session: string, branchLeaf: string, messages: string[]): Checkpoint {
  // Extract bounded, explicit markers only: no LLM summarizer or raw transcript dump.
  const goals = messages.filter(m => /\b(?:todo|next|goal|blocker|decision)\s*:/i.test(m)).slice(-8).map(m => m.slice(0, 500));
  const files = [...new Set(messages.flatMap(m => (m.match(/(?:^|\s)(?:\.?\.?\/)?[\w./-]+\.(?:ts|tsx|js|py|md|json)(?=\s|$)/g) ?? []).map(x => x.trim())))].slice(-25);
  return { session, branchLeaf, goals, files, createdAt: new Date().toISOString() };
}
