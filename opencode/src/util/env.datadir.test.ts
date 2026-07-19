/**
 * Tests for global data-dir resolution and project slugging.
 *
 * Origin: reported by @cawa0505 in #90 (data was written into the user's project
 * directory, polluting repos and preventing cross-project trend aggregation).
 * This implementation is independent; the tests pin the behaviours that review
 * of that report identified as risky.
 */
import { test, expect, afterEach } from "bun:test";
import { homedir, platform } from "node:os";
import { join } from "node:path";
import { mkdtempSync, mkdirSync, writeFileSync, existsSync, readFileSync, readdirSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolveDataDir, hashProjectDir, resolveConfig } from "./env.js";
import { migrateLegacyDataDir } from "./migrate.js";

const ENV_KEYS = ["TOKEN_OPTIMIZER_DATA_DIR", "XDG_DATA_HOME", "LOCALAPPDATA"];
const saved: Record<string, string | undefined> = {};
for (const k of ENV_KEYS) saved[k] = process.env[k];

afterEach(() => {
  for (const k of ENV_KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
});

// --- resolveDataDir: precedence -------------------------------------------

test("explicit config.dataDir beats the env var", () => {
  process.env.TOKEN_OPTIMIZER_DATA_DIR = "/from/env";
  expect(resolveDataDir({ dataDir: "/from/config" })).toBe("/from/config");
});

test("env var is used when config.dataDir is unset", () => {
  process.env.TOKEN_OPTIMIZER_DATA_DIR = "/from/env";
  expect(resolveDataDir({})).toBe("/from/env");
  expect(resolveDataDir()).toBe("/from/env");
});

test("blank values fall through to the platform default", () => {
  process.env.TOKEN_OPTIMIZER_DATA_DIR = "   ";
  const resolved = resolveDataDir({ dataDir: "  " });
  expect(resolved).not.toBe("   ");
  expect(resolved.startsWith(homedir()) || resolved.length > 0).toBe(true);
});

test("platform default is the documented global location", () => {
  delete process.env.TOKEN_OPTIMIZER_DATA_DIR;
  delete process.env.XDG_DATA_HOME;
  const resolved = resolveDataDir({});
  if (platform() === "darwin") {
    expect(resolved).toBe(join(homedir(), "Library", "Application Support"));
  } else if (platform() === "win32") {
    expect(resolved.length).toBeGreaterThan(0);
  } else {
    expect(resolved).toBe(join(homedir(), ".local", "share"));
  }
});

test("XDG_DATA_HOME is honoured on non-darwin platforms", () => {
  if (platform() === "darwin" || platform() === "win32") return;
  delete process.env.TOKEN_OPTIMIZER_DATA_DIR;
  process.env.XDG_DATA_HOME = "/xdg/data";
  expect(resolveDataDir({})).toBe("/xdg/data");
});

// --- resolveDataDir: trailing-segment stripping (M1 regression) ------------

test("a trailing token-optimizer segment is stripped to avoid double nesting", () => {
  expect(resolveDataDir({ dataDir: "/data/token-optimizer" })).toBe("/data");
  expect(resolveDataDir({ dataDir: "/data/token-optimizer/" })).toBe("/data");
});

test("REGRESSION: a directory merely ENDING in token-optimizer is left intact", () => {
  // A naive /\/?token-optimizer\/?$/ truncates these to "/foo/bar-" and "/srv/my-".
  expect(resolveDataDir({ dataDir: "/foo/bar-token-optimizer" })).toBe("/foo/bar-token-optimizer");
  expect(resolveDataDir({ dataDir: "/srv/my-token-optimizer" })).toBe("/srv/my-token-optimizer");
  expect(resolveDataDir({ dataDir: "/opt/xtoken-optimizer" })).toBe("/opt/xtoken-optimizer");
});

test("a path that is only token-optimizer is not stripped to nothing", () => {
  expect(resolveDataDir({ dataDir: "token-optimizer" })).toBe("token-optimizer");
});

// --- hashProjectDir --------------------------------------------------------

test("slug keeps a readable prefix derived from the project path", () => {
  expect(hashProjectDir("/Users/alex/my project")).toMatch(/^-Users-alex-my-project-[0-9a-f]{10}$/);
  expect(hashProjectDir("D:\\Code\\my app")).toMatch(/^D--Code-my-app-[0-9a-f]{10}$/);
});

test("slug is stable for the same input", () => {
  expect(hashProjectDir("/a/one")).toBe(hashProjectDir("/a/one"));
});

test("REGRESSION: punctuation variants must not collide onto one slug", () => {
  // The readable part collapses every non-alphanumeric to "-", so these three
  // share a prefix. projectSlug is the ONLY boundary between projects in a
  // shared global data dir, so a bare slug would let ~/clients/acme-corp read
  // ~/clients/acme.corp's checkpoints. The digest is what keeps them apart.
  const variants = ["/Users/alex/client-a", "/Users/alex/client.a", "/Users/alex/client_a"];
  const slugs = variants.map(hashProjectDir);
  expect(new Set(slugs).size).toBe(3);
});

test("slug contains no path separators", () => {
  for (const p of ["/a/b", "..\\..\\etc", "/x/../../y"]) {
    const slug = hashProjectDir(p);
    expect(slug).not.toContain("/");
    expect(slug).not.toContain("\\");
  }
});

test("empty input yields a named bucket, not an empty segment", () => {
  // "" would make join(base, slug) collapse back onto base, unscoping everything.
  expect(hashProjectDir("")).toMatch(/^unknown-project-[0-9a-f]{10}$/);
  expect(hashProjectDir("///")).toMatch(/^unknown-project-[0-9a-f]{10}$/);
  // Different "empty-ish" inputs still resolve to distinct buckets.
  expect(hashProjectDir("")).not.toBe(hashProjectDir("///"));
});

// --- resolveConfig wiring (H1: dataDir must not be dead code) --------------

test("resolveConfig carries opts.dataDir through to resolveDataDir", () => {
  const config = resolveConfig({ dataDir: "/opt/custom" });
  expect(config.dataDir).toBe("/opt/custom");
  expect(resolveDataDir(config)).toBe("/opt/custom");
});

test("resolveConfig leaves dataDir undefined when not supplied", () => {
  expect(resolveConfig({}).dataDir).toBeUndefined();
});

// --- migration (H2) --------------------------------------------------------

const SLUG = "-Users-alex-proj";

function seedLegacy(root: string, opts: { wal?: boolean } = {}): string {
  const project = join(root, "project");
  const legacy = join(project, "token-optimizer");
  mkdirSync(join(legacy, "sessions"), { recursive: true });
  writeFileSync(join(legacy, "trends.db"), "TRENDS");
  writeFileSync(join(legacy, "sessions", "abc.db"), "SESSION");
  if (opts.wal) {
    writeFileSync(join(legacy, "trends.db-wal"), "TRENDS_WAL");
    writeFileSync(join(legacy, "sessions", "abc.db-wal"), "SESSION_WAL");
  }
  return project;
}

test("legacy project-local data is copied, not moved", () => {
  const root = mkdtempSync(join(tmpdir(), "to-migrate-"));
  const project = seedLegacy(root);
  const newBase = join(root, "global");

  expect(migrateLegacyDataDir(project, newBase, SLUG)).toBe(true);
  expect(readFileSync(join(newBase, "token-optimizer", "trends.db"), "utf8")).toBe("TRENDS");
  // Originals survive so a bad migration stays recoverable.
  expect(existsSync(join(project, "token-optimizer", "trends.db"))).toBe(true);
});

test("REGRESSION: migrated sessions land where the scoped reader actually looks", () => {
  // The readers in restore.ts / resume-lean.ts always scan sessions/{slug}/,
  // because hashProjectDir never returns an empty slug. Migrating into a flat
  // sessions/ therefore copies files somewhere nothing ever reads: data loss
  // that reports itself as success. Assert reachability, not just arrival.
  const root = mkdtempSync(join(tmpdir(), "to-migrate-scoped-"));
  const project = seedLegacy(root);
  const newBase = join(root, "global");

  expect(migrateLegacyDataDir(project, newBase, SLUG)).toBe(true);

  const readerPath = join(newBase, "token-optimizer", "sessions", SLUG, "abc.db");
  expect(existsSync(readerPath)).toBe(true);
  expect(readFileSync(readerPath, "utf8")).toBe("SESSION");
  // And specifically NOT stranded in the unscoped parent.
  expect(existsSync(join(newBase, "token-optimizer", "sessions", "abc.db"))).toBe(false);
});

test("REGRESSION: WAL sidecars migrate with their database", () => {
  // Both stores run PRAGMA journal_mode=WAL, so committed-but-uncheckpointed
  // rows live in the -wal file. Copying only the .db silently drops them.
  const root = mkdtempSync(join(tmpdir(), "to-migrate-wal-"));
  const project = seedLegacy(root, { wal: true });
  const newBase = join(root, "global");

  expect(migrateLegacyDataDir(project, newBase, SLUG)).toBe(true);
  const to = join(newBase, "token-optimizer");
  expect(readFileSync(join(to, "trends.db-wal"), "utf8")).toBe("TRENDS_WAL");
  expect(readFileSync(join(to, "sessions", SLUG, "abc.db-wal"), "utf8")).toBe("SESSION_WAL");
});

test("migration runs at most once and never clobbers newer data", () => {
  const root = mkdtempSync(join(tmpdir(), "to-migrate-once-"));
  const project = seedLegacy(root);
  const newBase = join(root, "global");
  expect(migrateLegacyDataDir(project, newBase, SLUG)).toBe(true);

  writeFileSync(join(newBase, "token-optimizer", "trends.db"), "NEW");
  expect(migrateLegacyDataDir(project, newBase, SLUG)).toBe(false);
  expect(readFileSync(join(newBase, "token-optimizer", "trends.db"), "utf8")).toBe("NEW");
});

test("no temp files survive a successful migration", () => {
  // Copies go to .tmp-<pid> then rename, so a crash leaves a stray temp rather
  // than a truncated DB that existsSync would later skip as "already done".
  const root = mkdtempSync(join(tmpdir(), "to-migrate-tmp-"));
  const project = seedLegacy(root, { wal: true });
  const newBase = join(root, "global");
  expect(migrateLegacyDataDir(project, newBase, SLUG)).toBe(true);

  const to = join(newBase, "token-optimizer");
  const strays = readdirSync(to).filter((f) => f.includes(".tmp-"));
  expect(strays).toEqual([]);
  // And the cross-process lock directory is released.
  expect(existsSync(join(to, ".migrate.lock"))).toBe(false);
});

test("SECURITY: a symlinked database is skipped, not read through", () => {
  // A repo can ship token-optimizer/trends.db as a symlink to ~/.ssh/id_ed25519.
  // Migration runs unprompted on plugin init, so following that link would copy
  // an arbitrary file into the global data dir, and session copies land exactly
  // where the continuity readers scan.
  const root = mkdtempSync(join(tmpdir(), "to-migrate-link-"));
  const secret = join(root, "secret.txt");
  writeFileSync(secret, "PRIVATE KEY");

  const project = join(root, "project");
  const legacy = join(project, "token-optimizer");
  mkdirSync(join(legacy, "sessions"), { recursive: true });
  symlinkSync(secret, join(legacy, "trends.db"));
  symlinkSync(secret, join(legacy, "sessions", "leak.db"));

  const newBase = join(root, "global");
  migrateLegacyDataDir(project, newBase, SLUG);

  const to = join(newBase, "token-optimizer");
  expect(existsSync(join(to, "trends.db"))).toBe(false);
  expect(existsSync(join(to, "sessions", SLUG, "leak.db"))).toBe(false);
});

test("SECURITY: a symlinked legacy directory is not traversed", () => {
  const root = mkdtempSync(join(tmpdir(), "to-migrate-linkdir-"));
  const elsewhere = join(root, "elsewhere");
  mkdirSync(elsewhere, { recursive: true });
  writeFileSync(join(elsewhere, "trends.db"), "NOT YOURS");

  const project = join(root, "project");
  mkdirSync(project, { recursive: true });
  symlinkSync(elsewhere, join(project, "token-optimizer"));

  const newBase = join(root, "global");
  expect(migrateLegacyDataDir(project, newBase, SLUG)).toBe(false);
  expect(existsSync(join(newBase, "token-optimizer", "trends.db"))).toBe(false);
});

test("migration is a no-op with nothing to migrate and never throws", () => {
  const root = mkdtempSync(join(tmpdir(), "to-migrate-none-"));
  expect(migrateLegacyDataDir(join(root, "absent"), join(root, "global"), SLUG)).toBe(false);
  expect(migrateLegacyDataDir("", "", SLUG)).toBe(false);
  // A missing slug must not silently fall back to an unscoped write.
  expect(migrateLegacyDataDir(seedLegacy(mkdtempSync(join(tmpdir(), "to-migrate-noslug-"))), join(root, "g2"), "")).toBe(false);
});
