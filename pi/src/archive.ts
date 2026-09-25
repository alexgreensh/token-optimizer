import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync, readFileSync, readdirSync, statSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { redact } from "./redact.ts";
import { dataDir } from "./state.ts";
/** Archive redacted content only; generated IDs are opaque and never derive from user paths. */
export function archive(text: string, root = dataDir()): string {
  const path = join(root, "archives"); mkdirSync(path, { recursive: true, mode: 0o700 });
  const id = `${Date.now()}-${randomUUID()}`;
  const body = redact(text);
  const hash = createHash("sha256").update(body).digest("hex");
  writeFileSync(join(path, `${id}.txt`), body, { mode: 0o600, flag: "wx" });
  return `${id}:${hash.slice(0, 12)}`;
}
export function recover(pointer: string, root = dataDir()): string {
  const [id, hash] = pointer.split(":");
  if (!/^[0-9]{10,16}-[0-9a-f-]{36}$/.test(id) || !/^[0-9a-f]{12}$/.test(hash)) throw new Error("Invalid recovery pointer");
  const body = readFileSync(join(root, "archives", `${id}.txt`), "utf8");
  if (!createHash("sha256").update(body).digest("hex").startsWith(hash)) throw new Error("Archive checksum mismatch");
  return body;
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
