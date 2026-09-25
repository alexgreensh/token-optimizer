import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync, readFileSync, readdirSync, statSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { redact, suspiciousSecret } from "./redact.ts";
import { dataDir } from "./state.ts";

export const MAX_RESULT_BYTES = 64 * 1024;
export const MAX_ARCHIVE_BYTES = 10 * 1024 * 1024;
/** Refuse unrecognized secret-shaped data rather than persisting it. */
export function archive(text: string, root = dataDir()): { pointer: string; path: string } | undefined {
  if (Buffer.byteLength(text, "utf8") > MAX_RESULT_BYTES || suspiciousSecret(text)) return;
  const body = redact(text);
  if (suspiciousSecret(body)) return;
  const dir = join(root, "archives"); mkdirSync(dir, { recursive: true, mode: 0o700 });
  let bytes = 0;
  for (const name of readdirSync(dir)) {
    if (!/^[0-9]{10,16}-[0-9a-f-]{36}\.txt$/.test(name)) continue;
    bytes += statSync(join(dir, name)).size;
  }
  if (bytes + Buffer.byteLength(body, "utf8") > MAX_ARCHIVE_BYTES) return;
  const id = `${Date.now()}-${randomUUID()}`;
  const hash = createHash("sha256").update(body).digest("hex").slice(0, 12);
  const path = join(dir, `${id}.txt`);
  writeFileSync(path, body, { mode: 0o600, flag: "wx" });
  return { pointer: `${id}:${hash}`, path };
}
export function recover(pointer: string, root = dataDir()): string {
  const [id, hash] = pointer.split(":");
  if (!/^[0-9]{10,16}-[0-9a-f-]{36}$/.test(id) || !/^[0-9a-f]{12}$/.test(hash)) throw new Error("Invalid recovery pointer");
  const body = readFileSync(join(root, "archives", `${id}.txt`), "utf8");
  if (!createHash("sha256").update(body).digest("hex").startsWith(hash)) throw new Error("Archive checksum mismatch");
  return body;
}
export function purge(kind: "archives" | "checkpoints", root = dataDir()): number {
  const dir = join(root, kind); let n = 0;
  try {
    for (const name of readdirSync(dir)) {
      if (!(kind === "archives" ? /^[0-9]{10,16}-[0-9a-f-]{36}\.txt$/ : /^[0-9a-f]{24}\.json$/).test(name)) continue;
      unlinkSync(join(dir, name)); n++;
    }
  } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  return n;
}
export function pruneArchives(retainDays: number, root = dataDir(), now = Date.now()): number {
  const dir = join(root, "archives"); let count = 0;
  try {
    for (const file of readdirSync(dir)) {
      if (!/^[0-9]{10,16}-[0-9a-f-]{36}\.txt$/.test(file)) continue;
      const target = join(dir, file);
      if (now - statSync(target).mtimeMs > retainDays * 86400000) { unlinkSync(target); count++; }
    }
  } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  return count;
}

export function listArchives(root = dataDir()): string[] {
  try { return readdirSync(join(root, "archives")).filter(name => /^[0-9]{10,16}-[0-9a-f-]{36}\.txt$/.test(name)).sort().reverse().slice(0, 20).map(name => {
    const body = readFileSync(join(root, "archives", name), "utf8");
    return `${name.slice(0, -4)}:${createHash("sha256").update(body).digest("hex").slice(0, 12)}`;
  }); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; return []; }
}
