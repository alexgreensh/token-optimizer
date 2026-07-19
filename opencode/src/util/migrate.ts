import {
  existsSync,
  mkdirSync,
  copyFileSync,
  readdirSync,
  lstatSync,
  writeFileSync,
  renameSync,
  rmSync,
  rmdirSync,
} from "node:fs";
import { join } from "node:path";

const DATA_FOLDER = "token-optimizer";
const MARKER = ".migrated-from-project-dir";
const LOCK = ".migrate.lock";

/** SQLite in WAL mode keeps committed-but-uncheckpointed rows in the sidecars.
 *  Copying only the .db silently drops whatever never got checkpointed. */
const SQLITE_SIDECARS = ["", "-wal", "-shm"];

/**
 * One-time, non-destructive migration from the old project-local data location
 * (`{project}/token-optimizer/`) to the platform-global one.
 *
 * Deliberately COPIES rather than moves. A move of a live SQLite file across a
 * plugin upgrade is how trends history gets lost for real; leaving the original
 * in place means a bad migration is recoverable by pointing
 * TOKEN_OPTIMIZER_DATA_DIR back at the project.
 *
 * Never throws. A migration failure must not take the plugin down with it: the
 * worst acceptable outcome is starting fresh with a warning, not a crash.
 *
 * @param projectSlug Session DBs MUST land in the same per-project subdirectory
 *   the readers scan (`sessions/{slug}/`). Legacy data was project-local, so it
 *   unambiguously belongs to this project's slug. Writing it flat would copy the
 *   files somewhere no reader ever looks, which is data loss wearing a success
 *   message.
 * @returns true when files were copied on this call, false otherwise.
 */
export function migrateLegacyDataDir(
  projectDir: string,
  newBase: string,
  projectSlug: string,
): boolean {
  if (!projectDir || !newBase || !projectSlug) return false;

  const legacy = join(projectDir, DATA_FOLDER);
  const target = join(newBase, DATA_FOLDER);
  if (legacy === target) return false;

  let lockDir = "";
  try {
    if (!isRealDirectory(legacy)) return false;
    // Marker lives in the target, so migration is attempted at most once even if
    // the legacy directory is never cleaned up by the user.
    if (existsSync(join(target, MARKER))) return false;

    mkdirSync(target, { recursive: true });

    // mkdir is atomic and fails with EEXIST if another process already holds the
    // lock, which makes it a usable cross-process claim without extra deps.
    lockDir = join(target, LOCK);
    try {
      mkdirSync(lockDir);
    } catch {
      // A lock left behind by a process that died would otherwise disable
      // migration forever, so an old one is treated as abandoned and reclaimed.
      if (!reclaimStaleLock(lockDir)) {
        // Another instance is migrating this target right now. Let it finish;
        // whichever process wins, the data ends up in the same place.
        lockDir = "";
        return false;
      }
    }

    // Re-check under the lock: the holder may have completed between our first
    // marker check and acquiring the lock.
    if (existsSync(join(target, MARKER))) return false;

    let copied = 0;
    copied += copyDatabase(legacy, target, "trends.db");

    const legacySessions = join(legacy, "sessions");
    if (isRealDirectory(legacySessions)) {
      // Scoped to match the readers in restore.ts / resume-lean.ts.
      const targetSessions = join(target, "sessions", projectSlug);
      mkdirSync(targetSessions, { recursive: true });
      for (const entry of readdirSync(legacySessions)) {
        // Only flat .db files migrate. Sidecars are pulled in by copyDatabase,
        // and an already-scoped subdirectory means a newer layout is present.
        if (!entry.endsWith(".db")) continue;
        if (!isRealFile(join(legacySessions, entry))) continue;
        copied += copyDatabase(legacySessions, targetSessions, entry);
      }
    }

    writeMarker(target);

    if (copied > 0) {
      console.warn(
        `[Token Optimizer] Moved data storage to ${target}. ` +
          `Copied ${copied} database(s) from ${legacy}. ` +
          `The originals were left in place and can be deleted once you have confirmed your history looks right.`,
      );
    }
    return copied > 0;
  } catch (err) {
    console.warn(
      `[Token Optimizer] Could not migrate legacy data from ${projectDir}: ${err instanceof Error ? err.message : String(err)}. ` +
        `Continuing with the new location; your old data is untouched.`,
    );
    return false;
  } finally {
    if (lockDir) {
      try {
        rmdirSync(lockDir);
      } catch {
        // A stranded lock only blocks a future migration that has already run.
      }
    }
  }
}

/**
 * Copy a SQLite database and its WAL sidecars as a set.
 *
 * Each file goes to a temp name first and is then renamed into place. rename is
 * atomic within a filesystem, so a crash mid-copy leaves a stray .tmp rather
 * than a truncated database that `existsSync` would later mistake for a
 * completed migration and skip forever.
 *
 * @returns 1 if the main .db was copied, else 0.
 */
function copyDatabase(srcDir: string, destDir: string, name: string): number {
  // Skip the whole set if the destination DB is already there; a rerun must
  // never clobber data that is newer than the legacy copy.
  if (existsSync(join(destDir, name))) return 0;
  if (!isRealFile(join(srcDir, name))) return 0;

  let mainCopied = 0;
  for (const suffix of SQLITE_SIDECARS) {
    const src = join(srcDir, `${name}${suffix}`);
    const dest = join(destDir, `${name}${suffix}`);
    if (!isRealFile(src)) continue;
    const tmp = `${dest}.tmp-${process.pid}`;
    try {
      copyFileSync(src, tmp);
      renameSync(tmp, dest);
      if (suffix === "") mainCopied = 1;
    } catch (err) {
      try {
        rmSync(tmp, { force: true });
      } catch {
        /* best effort */
      }
      throw err;
    }
  }
  return mainCopied;
}

/** A lock older than this is assumed to belong to a process that died. */
const LOCK_STALE_MS = 5 * 60 * 1000;

/**
 * Reclaim an abandoned lock directory. Returns true if this call now owns it.
 *
 * Without this, one crash between acquiring the lock and the finally-block
 * release strands the directory and every future launch silently no-ops.
 */
function reclaimStaleLock(lockDir: string): boolean {
  try {
    const age = Date.now() - lstatSync(lockDir).mtimeMs;
    if (age < LOCK_STALE_MS) return false;
    rmdirSync(lockDir);
    mkdirSync(lockDir);
    return true;
  } catch {
    // Lost the race to another reclaimer, or the lock vanished underneath us.
    return false;
  }
}

/**
 * Symlink-safe existence checks.
 *
 * Everything under `{project}/token-optimizer/` is untrusted: it is whatever
 * repository the user happened to open, and git stores symlinks natively. A
 * hostile repo can ship `token-optimizer/trends.db -> ../../../../.ssh/id_ed25519`
 * and, because migration runs unprompted on plugin init, a following copy would
 * pull that file into the global data dir. Worse, session copies land in the very
 * directory the continuity readers scan, so a copied file that parses as a
 * checkpoint DB could reach the model's context.
 *
 * lstat does not follow the final symlink, so a link is seen as a link and
 * skipped rather than read through.
 */
function isRealDirectory(path: string): boolean {
  try {
    return lstatSync(path).isDirectory();
  } catch {
    return false;
  }
}

function isRealFile(path: string): boolean {
  try {
    return lstatSync(path).isFile();
  } catch {
    return false;
  }
}

function writeMarker(target: string): void {
  try {
    const path = join(target, MARKER);
    // An empty marker file is enough; its presence is the whole signal.
    if (!existsSync(path)) writeFileSync(path, "");
  } catch {
    // A missing marker only costs a redundant no-op migration attempt later.
  }
}
