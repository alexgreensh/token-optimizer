// Data gathering and re-keying for the band.
//
// The engine refuses `$` passed across an import ("$ is followed only into a
// function declared in this same file"), and a plugin has exactly one hooks
// module. So this file never sees `$`: it works over `DataIo`, a port that
// register.tsx builds from `$` in one top-level function (`dataIo($)`), each
// member spelled `$.noun.method(...)` there. That keeps every noun call where
// the engine's scan looks for it, and lets this logic run under plain Node.
//
// Every lookup is best effort: a figure that cannot be read is left null and
// the rest still fill. Nothing here registers a hook; register.tsx wires the
// events and the cadence below.
import type { TokenOptimizerDesktopSession } from '../types/index.d.ts'
import type { Limit, Quality } from '../src/contracts.ts'
import {
  parseQualityCache,
  parseStatusBar,
  parseUsage,
  resolveTokenOptimizerRoot,
  type StatusBar,
  type TokenOptimizerRoot,
} from '../src/parse.ts'

/** Redraw once a second, but only inside the cache warning window. */
export const TICK_WARNING_MS = 1_000
/** Redraw otherwise, beside the event-driven redraws. */
export const TICK_IDLE_MS = 60_000
/** Re-read the quality cache this often, and after each turn. */
export const QUALITY_REFRESH_MS = 60_000
/** Run the status command this long after a turn ends. */
export const STATUS_AFTER_TURN_MS = 5_000
/** The status command answers from its cache in well under a second warm. */
export const STATUS_TIMEOUT_MS = 4_000
/** `git branch --show-current` is local and instant; anything slower is skipped. */
export const GIT_TIMEOUT_MS = 1_000

/** Shown as the savings reason when no Token Optimizer install is found. */
export const NOT_FOUND = 'Token Optimizer not found'
/** No Python 3 launcher starts: Token Optimizer's scripts cannot run. */
export const NO_PYTHON = 'Token Optimizer needs Python 3 to measure savings.'
/** The installed Token Optimizer predates the status command this band reads. */
export const OUTDATED = 'Update Token Optimizer to see savings.'

/**
 * Python launchers in the order tried. The mod cannot see the host's OS, so a
 * launcher that cannot start (or Windows' Store stub, exit 9009) moves on to
 * the next: python3 on macOS and Linux, python or the `py -3` launcher on
 * Windows. The first that starts is remembered for the module's life.
 */
export const PYTHON_LAUNCHERS: readonly (readonly string[])[] = [['python3'], ['python'], ['py', '-3']]

/** Windows' "app execution alias" stub for a missing python exits with this. */
const WINDOWS_NOT_FOUND = 9009

let launcherIndex = 0

/** Forgets which launcher worked (tests, and nothing else). */
export function resetLauncher(): void {
  launcherIndex = 0
}

/**
 * What the gatherer needs from the engine. register.tsx answers each member
 * with the `$` call named beside it; any member may reject.
 */
export type DataIo = {
  /** `$.clock.now()` (ms) */
  now: () => Promise<number>
  /** `$.session.id()` */
  sessionId: () => Promise<string>
  /** `$.session.cwd()` */
  cwd: () => Promise<string>
  /** `$.env.get('HOME')` (the validator wants a literal variable name) */
  envHome: () => Promise<string | undefined>
  /** `$.env.get('CLAUDE_CONFIG_DIR')`: a relocated Claude folder, as Token Optimizer honours it */
  envConfigDir?: () => Promise<string | undefined>
  /** `$.env.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW')`: the raw auto-compaction window override */
  envAutoCompactWindow?: () => Promise<string | undefined>
  /** `$.env.get('USERPROFILE')`, the Windows home */
  envUserProfile: () => Promise<string | undefined>
  /** `$.session.usage()` */
  usage: () => Promise<unknown>
  /** `$.fs.list(path)` */
  list: (path: string) => Promise<readonly { name: string }[]>
  /** `$.fs.stat(path)` */
  stat: (path: string) => Promise<{ kind: string; mtimeMs: number }>
  /** `$.fs.read(path)` */
  read: (path: string) => Promise<string>
  /** `$.plugin.root`: this plugin's own folder */
  pluginRoot?: () => string
  /** `$.process.run(argv, init)` */
  run: (argv: string[], init: { cwd?: string; timeoutMs: number; stdin?: string }) => Promise<{ exitCode: number; stdout: string; stderr?: string }>
  /** `$.ui.log(text, { to: 'debug' })`: a line in Claude Code's debug log */
  log?: (text: string) => Promise<void>
}

async function attempt<T>(work: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await work()
  } catch {
    return fallback
  }
}

/** Session ids become file names; keep only what Token Optimizer's own sanitizer keeps. */
export function cleanId(id: string): string {
  const clean = id.replace(/[^a-zA-Z0-9_-]/g, '')
  // Token Optimizer files an id shorter than 6 under "unknown": read nothing for it either.
  return clean.length >= 6 ? clean : ''
}

/** True when the stored atom belongs to another session (or to none) and must be reset. */
export function shouldReset(stored: TokenOptimizerDesktopSession | null, liveSessionId: string): boolean {
  return stored === null || stored.sessionId !== liveSessionId
}

/** The user's home, as the engine's process sees it. */
export async function readHome(io: DataIo): Promise<string> {
  return (await attempt(() => io.envHome(), undefined)) || (await attempt(() => io.envUserProfile(), undefined)) || ''
}

/** The Claude folder: CLAUDE_CONFIG_DIR when set (as Token Optimizer's claude_home()), else ~/.claude. */
export async function claudeDir(io: DataIo, home: string): Promise<string> {
  const set = io.envConfigDir ? await attempt(() => io.envConfigDir!(), undefined) : undefined
  const dir = set ? set.trim().replace(/[\\/]+$/, '') : ''
  // Absolute only, as Token Optimizer requires: a relative one would point the band elsewhere.
  const absolute = dir.startsWith('/') || /^[A-Za-z]:[\\/]/.test(dir)
  return absolute ? dir : home ? `${home}/.claude` : ''
}

/**
 * The freshest `quality-cache-<sid>.json` across Token Optimizer's storage
 * directories: each plugin install's data dir, then the legacy
 * `~/.claude/token-optimizer`. Newest modification time wins.
 */
export async function readQuality(io: DataIo, home: string, sid: string): Promise<Quality | null> {
  if (!home || !sid) {
    return null
  }

  const claude = await claudeDir(io, home)
  const dataRoot = `${claude}/plugins/data`
  const entries = await attempt(() => io.list(dataRoot), [])
  const dirs = entries
    .filter(entry => entry.name.includes('token-optimizer'))
    .map(entry => `${dataRoot}/${entry.name}/token-optimizer`)
  dirs.push(`${claude}/token-optimizer`)

  let freshest: { path: string; mtimeMs: number } | null = null

  for (const dir of dirs) {
    const path = `${dir}/quality-cache-${sid}.json`
    const stat = await attempt(() => io.stat(path), null)

    if (stat && stat.kind === 'file' && (!freshest || stat.mtimeMs > freshest.mtimeMs)) {
      freshest = { path, mtimeMs: stat.mtimeMs }
    }
  }

  if (!freshest) {
    return null
  }

  const { path } = freshest
  const raw = await attempt(() => io.read(path), null)
  const nowMs = await attempt(() => io.now(), Date.now())

  return raw === null ? null : parseQualityCache(raw, nowMs / 1000)
}

/**
 * Token Optimizer's scripts: the installed-plugins registry first, then
 * the skill install; the first whose measure.py exists. null when neither does.
 */
export async function findTokenOptimizerRoot(io: DataIo, home: string): Promise<TokenOptimizerRoot | null> {
  const claude = await claudeDir(io, home)
  const registry = claude ? await attempt(() => io.read(`${claude}/plugins/installed_plugins.json`), null) : null
  // Shipped inside Token Optimizer, the plugin root is Token Optimizer itself; loaded on its
  // own from a checkout (desktop/<this plugin>), the scripts two folders up are the matching version.
  const root = io.pluginRoot ? await attempt(async () => io.pluginRoot!(), '') : ''
  const sibling = [root.replace(/[\\/]+$/, ''), trimTwo(root)]
    .filter(dir => dir !== '')
    .map(dir => ({ scriptsDir: `${dir}/skills/token-optimizer/scripts`, runner: `${dir}/hooks/module_runner.py` }))

  // Only scripts inside the Claude folder run (or beside this plugin in a checkout): a
  // tampered or stale registry entry pointing elsewhere is skipped, never executed.
  // Compared with one separator and one case of drive letter, so a Windows home
  // (C:\\Users\\me) and a registry path (C:\\Users\\me\\.claude\\...) agree.
  const norm = (p: string) => p.replace(/\\/g, '/').replace(/^([a-zA-Z]):/, (_m: string, d: string) => `${d.toLowerCase()}:`)
  // Windows paths compare without case, as the filesystem does.
  const fold = (p: string) => (/^[a-z]:\//.test(norm(claude)) ? norm(p).toLowerCase() : norm(p))
  const base = fold(claude)
  const inside = (p: string) => base !== '' && (fold(p) === base || fold(p).startsWith(`${base}/`))
  const listed = resolveTokenOptimizerRoot(registry, claude).filter(r => inside(r.scriptsDir))
  for (const root of [...sibling, ...listed]) {
    if (!(await attempt(() => io.stat(`${root.scriptsDir}/measure.py`), null))) {
      continue
    }

    const { runner } = root
    const hasRunner = runner !== null && (await attempt(() => io.stat(runner), null)) !== null

    return { scriptsDir: root.scriptsDir, runner: hasRunner ? runner : null }
  }

  return null
}

function newer(a: number | null, b: number | null): number | null {
  return a === null ? b : b === null ? a : Math.max(a, b)
}

/** A path two folders up, or '' when it has fewer. */
function trimTwo(path: string): string {
  const parts = path.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts.length > 2 ? parts.slice(0, -2).join('/') : ''
}

/** The argv for `measure.py <args>`, through module_runner when the install has it (bytecode reuse). */
export function measureArgv(root: TokenOptimizerRoot, args: readonly string[], python: readonly string[] = PYTHON_LAUNCHERS[0] ?? ['python3']): string[] {
  const launch = root.runner ? [...python, root.runner, root.scriptsDir, 'measure'] : [...python, `${root.scriptsDir}/measure.py`]

  return [...launch, ...args]
}

/** The arguments of `measure.py status-bar` for one session. */
function statusBarArgs(sid: string, transcript?: string): string[] {
  return ['status-bar', '--session', sid, '--json', ...(transcript ? ['--transcript', transcript] : [])]
}

function isTimeout(error: unknown): boolean {
  const name = error instanceof Error ? error.name : ''
  return name === 'TimeoutError' || /timed? ?out|deadline/i.test(error instanceof Error ? error.message : String(error))
}

/** No Python launcher could start at all (every one was tried). */
function noLauncher(error: unknown): boolean {
  return /no python launcher|python launcher not found|ENOENT|not found/i.test(error instanceof Error ? error.message : String(error))
}

/**
 * Runs `measure.py <args>` with the first Python launcher that starts. A
 * timeout is the command's own answer and is never retried, so a slow read
 * costs one timeout, not three. Rejects as the last attempt did.
 */
export async function runMeasure(
  io: DataIo,
  root: TokenOptimizerRoot,
  args: readonly string[],
  init: { timeoutMs: number; stdin?: string },
): Promise<{ exitCode: number; stdout: string; stderr?: string }> {
  let lastError: unknown = new Error('no python launcher')

  for (let i = launcherIndex; i < PYTHON_LAUNCHERS.length; i++) {
    try {
      const result = await io.run(measureArgv(root, args, PYTHON_LAUNCHERS[i]), init)

      if (result.exitCode === WINDOWS_NOT_FOUND && i < PYTHON_LAUNCHERS.length - 1) {
        lastError = new Error('python launcher not found')
        continue
      }

      launcherIndex = i
      return result
    } catch (error) {
      if (isTimeout(error)) {
        throw error
      }

      lastError = error
    }
  }

  throw lastError
}

/**
 * `measure.py status-bar --json`; 'outdated' when the install has no
 * such command (it prints usage, not JSON), null when it fails, times out or
 * prints something else.
 */
export async function readStatusBar(
  io: DataIo,
  root: TokenOptimizerRoot,
  sid: string,
  transcript?: string,
): Promise<StatusBar | 'outdated' | 'nopython' | null> {
  const args = statusBarArgs(sid, transcript)
  let result: { exitCode: number; stdout: string; stderr?: string } | 'nopython' | null = null
  try {
    result = await runMeasure(io, root, args, { timeoutMs: STATUS_TIMEOUT_MS })
  } catch (error) {
    result = noLauncher(error) ? 'nopython' : null
  }

  if (result === 'nopython') return 'nopython'
  if (!result) return null
  if (result.exitCode !== 0 && io.log) {
    // Why the status read failed, where `claude --debug` shows it.
    void attempt(() => io.log!(`token-optimizer status bar: status-bar exited ${result.exitCode}: ${(result.stderr ?? '').trim().slice(0, 300)}`), undefined)
  }
  // The JSON is the last line that is one (a stray line printed before it is not a reason to fail).
  const json = result.stdout.split(/\r?\n/).map(l => l.trim()).filter(l => l.startsWith('{')).pop()
  // An install without the command prints its usage instead of JSON (or exits 2).
  // Usage text instead of JSON is an older install; empty output is just a failed read.
  if (!json) return (result.exitCode === 0 || result.exitCode === 2) && result.stdout.trim() !== '' ? 'outdated' : null
  return result.exitCode === 0 ? parseStatusBar(json) : null
}

/** The current git branch; null outside a repository, on a detached HEAD, or without git. */
export async function readBranch(io: DataIo, cwd: string): Promise<string | null> {
  const init = cwd ? { cwd, timeoutMs: GIT_TIMEOUT_MS } : { timeoutMs: GIT_TIMEOUT_MS }
  const result = await attempt(() => io.run(['git', 'branch', '--show-current'], init), null)
  const branch = result && result.exitCode === 0 ? result.stdout.trim() : ''

  return branch === '' ? null : branch
}

export type GatherOptions = {
  /** The live session id when the caller knows it better than `$.session.id()` (a classic SessionStart's `session_id`). */
  sessionId?: string
  /** Start over even when the id matches (a clear). */
  reset?: boolean
  /** Run the status command (start, 5 s after a turn, row opened). */
  savings?: boolean
  /** The session's transcript, so the status command need not look it up. */
  transcript?: string
}

/**
 * The per-session part of a Snapshot. Starts from `previous` only when it
 * belongs to this session, so nothing carries over a clear. Savings and
 * the clock facts come from the status command when asked for; when it is not
 * asked, fails or times out, the last known values stay.
 */
export async function gather(
  io: DataIo,
  previous: TokenOptimizerDesktopSession | null,
  options: GatherOptions = {},
): Promise<TokenOptimizerDesktopSession> {
  const now = await attempt(() => io.now(), Date.now())
  const sid = cleanId(options.sessionId ?? (await attempt(() => io.sessionId(), '')))
  const base = options.reset || shouldReset(previous, sid) ? null : previous
  const cwd = await attempt(() => io.cwd(), '')
  const home = await readHome(io)
  const autoCompactWindow = io.envAutoCompactWindow ? await attempt(() => io.envAutoCompactWindow!(), undefined) : undefined
  const reported = parseUsage(await attempt(() => io.usage(), null), autoCompactWindow)
  // A limit does not vanish mid-session: a refresh that comes back without one (right
  // after a compact, say) keeps the last known value; once that window has renewed, 0%.
  const keep = (fresh: Limit | null, last: Limit | null | undefined): Limit | null => {
    if (fresh) return fresh
    // Kept only until its own renewal: after that the old figure is wrong, and an unreadable
    // renewal time is no renewal time (either could otherwise stay on screen for good).
    if (!last || last.resetsAt === null) return null
    const renews = Date.parse(last.resetsAt)
    return Number.isFinite(renews) && renews > now ? last : null
  }
  const usage = { ...reported, fiveHour: keep(reported.fiveHour, base?.fiveHour), week: keep(reported.week, base?.week) }
  const sawLimits = Boolean(base?.sawLimits || reported.fiveHour || reported.week)
  const branch = await readBranch(io, cwd)
  const quality = await readQuality(io, home, sid)

  let status: StatusBar | 'outdated' | 'nopython' | null = null
  let notFound = false

  if (options.savings && sid) {
    const root = await findTokenOptimizerRoot(io, home)
    notFound = root === null
    status = root ? await readStatusBar(io, root, sid, options.transcript) : null
  }

  const kept = {
    savings: base?.savings ?? null,
    savingsState: base?.savingsState ?? ('unavailable' as const),
    savingsReason: base?.savingsReason ?? null,
    lastRequestEpoch: base?.lastRequestEpoch ?? null,
    cacheLifetime: base?.cacheLifetime ?? null,
    checkpointEpoch: base?.checkpointEpoch ?? null,
    earlierCheckpoint: base?.earlierCheckpoint ?? null,
    compactions: base?.compactions ?? null,
  }
  const seen = { toolCallsSeen: base?.toolCallsSeen ?? 0, compactionsSeen: base?.compactionsSeen ?? 0 }

  const facts = notFound
    ? { ...kept, savings: null, savingsState: 'unavailable' as const, savingsReason: NOT_FOUND, checkpointEpoch: null }
    : status === 'outdated'
      ? { ...kept, savings: null, savingsState: 'unavailable' as const, savingsReason: OUTDATED }
    : status === 'nopython'
      ? { ...kept, savings: null, savingsState: 'unavailable' as const, savingsReason: NO_PYTHON }
    : status
      ? {
          // While savings load, the last known figures stay on show.
          savings: status.savings ?? (status.savingsState === 'loading' ? kept.savings : null),
          savingsState: status.savingsState,
          savingsReason: status.savings ? null : status.savingsReason,
          lastRequestEpoch: status.lastRequestEpoch ?? kept.lastRequestEpoch,
          cacheLifetime: status.cacheLifetime ?? kept.cacheLifetime,
          checkpointEpoch: status.checkpointEpoch,
          earlierCheckpoint: status.earlierCheckpoint,
          compactions: status.compactions ?? kept.compactions,
        }
      : kept

  // The higher count wins: the quality cache can lag a compaction that just landed.
  // Never lower than the band already knew this session (it may have seen the compaction itself).
  const known = Math.max(
    'compactions' in facts && facts.compactions != null ? facts.compactions : 0,
    base?.quality?.compactions ?? 0,
  )
  const counted = quality && known > quality.compactions ? { ...quality, compactions: known } : quality
  // A status answer has already checked the checkpoint file is still on disk, so it wins
  // over the quality cache, which keeps naming a save after retention removes it.
  const answered = status !== null && typeof status === 'object'
  const checkpointEpoch = notFound ? null : answered ? facts.checkpointEpoch : newer(quality?.checkpointEpoch ?? null, facts.checkpointEpoch)
  const merged = counted && answered ? { ...counted, checkpointEpoch } : counted

  return {
    sessionId: sid,
    gatheredAt: now,
    quality: merged,
    ...usage,
    branch,
    ...facts,
    // Without a status answer, the newer of the two: the quality cache knows quality saves
    // the moment they land, the status command also knows stop and compaction saves.
    ...seen,
    sawLimits,
    checkpointEpoch,
    sheetOpen: base?.sheetOpen ?? false,
  }
}

/**
 * What to store when `fresh` lands on top of `current` (read inside
 * `update`): keeps a row toggle made while gathering, never across sessions
 * or a forced reset.
 */
export function mergeStored(
  current: TokenOptimizerDesktopSession | null,
  fresh: TokenOptimizerDesktopSession,
  reset = false,
  savingsRead = true,
): TokenOptimizerDesktopSession {
  const sameSession = !reset && current !== null && current.sessionId === fresh.sessionId

  // A refresh that began before a compaction landed must not write its count back down.
  const counted = sameSession ? Math.max(current.quality?.compactions ?? 0, fresh.quality?.compactions ?? 0) : null
  const quality = fresh.quality && counted !== null && counted > fresh.quality.compactions ? { ...fresh.quality, compactions: counted } : fresh.quality

  // The band's own counts only grow; a refresh that read them earlier cannot undo a later one.
  const seen = sameSession
    ? {
        toolCallsSeen: Math.max(current.toolCallsSeen ?? 0, fresh.toolCallsSeen ?? 0),
        compactionsSeen: Math.max(current.compactionsSeen ?? 0, fresh.compactionsSeen ?? 0),
      }
    : {}

  // A refresh that did not run the status command only carried its older copy of what that
  // command reports: a slower savings refresh that landed meanwhile keeps its figures.
  const statusFacts =
    sameSession && !savingsRead
      ? {
          savings: current.savings,
          savingsState: current.savingsState,
          savingsReason: current.savingsReason,
          earlierCheckpoint: current.earlierCheckpoint,
          cacheLifetime: current.cacheLifetime ?? fresh.cacheLifetime,
          lastRequestEpoch: newer(current.lastRequestEpoch, fresh.lastRequestEpoch),
          checkpointEpoch: newer(current.checkpointEpoch, fresh.checkpointEpoch),
          compactions:
            current.compactions == null ? fresh.compactions : fresh.compactions == null ? current.compactions : Math.max(current.compactions, fresh.compactions),
        }
      : {}

  return { ...fresh, ...statusFacts, ...seen, quality, sheetOpen: sameSession ? current.sheetOpen : fresh.sheetOpen }
}
