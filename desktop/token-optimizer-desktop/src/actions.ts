// The three buttons' pure parts. register.tsx owns
// every `$` call; this file decides. Nothing here imports from 'claude-code'.
import { tokens } from './format.ts'

/** Start fresh waits this long for its second click. */
export const FRESH_ARM_MS = 5_000
/** A busy state ends by itself after this, so no step label outlives its work. */
export const BUSY_TIMEOUT_MS = 120_000
/** How long an outcome note replaces "All clear." */
export const NOTE_MS = 8_000
/** Keep warm's one-line fork prompt. */
export const WARM_PROMPT = 'Reply with exactly: ok'
/** The `$.store` key of a pending Start fresh hand-off (survives restarts). */
export const HANDOFF_KEY = 'handoff'
/** compact-capture reads a transcript; generous, still bounded. */
export const CAPTURE_TIMEOUT_MS = 30_000
/** The budget compact-capture is given (`--budget-seconds`), under CAPTURE_TIMEOUT_MS so it answers first. */
export const CAPTURE_BUDGET_SECONDS = 25
/** A saved hand-off joins a new session only this soon after it was saved; older ones are dropped. */
export const HANDOFF_TTL_MS = 10 * 60_000
/** resume-lean is a token-free read of checkpoints and the session log. */
export const RESUME_TIMEOUT_MS = 20_000

/**
 * `measure.py dashboard` regenerates the page and opens it in the browser (whatever the OS
 * opener is), so it can take a while on a long history. Bounded, and under BUSY_TIMEOUT_MS so
 * the runner answers before the busy state gives up on its own. Measured cold on a synthetic
 * 800-session / 1.1 GB history: 38 s (25 s warm); 90 s is more than twice that.
 */
export const DASHBOARD_TIMEOUT_MS = 90_000
/**
 * The arguments of that command. Never `--quiet`: quiet regenerates without opening anything.
 * `--user` marks the run as a person's click: the runner cannot pass env and its stdin is not a tty,
 * so without it measure.py reads the run as a hook and cuts a heavy rebuild off at its 20 s hook budget.
 */
export const DASHBOARD_ARGS = ['dashboard', '--user'] as const
/** The band's note line while the dashboard opens. */
export const DASHBOARD_OPENING = 'Opening the dashboard.'
/** The note after it failed, shown for NOTE_MS. */
export const DASHBOARD_FAILED = 'Could not open the dashboard.'

export type Busy = 'clean' | 'fresh-capture' | 'fresh-clear' | 'dashboard' | null

/** The band's own UI state: what a button is doing, the last outcome, the Start fresh arm. Plain JSON. */
export type UiState = {
  busy: Busy
  /** When `busy` was set (ms); the timeout counts from here. */
  busySince: number | null
  note: string | null
  /** The note shows until this time (ms). */
  noteUntil: number
  /** First Start fresh click (ms); null when disarmed. */
  freshArmedAt: number | null
}

/** Start fresh's saved hand-off: who saved it, where, when (`$.clock.now()` ms), and the text it carries. */
export type Handoff = {
  fromSessionId: string
  cwd: string
  checkpointPath: string
  text: string
  createdAt: number
  /** While the save is being stamped: createdAt is the far-future placeholder, this the press time. */
  pendingSince?: number
}

/** A hand-off still being stamped this long after its press was cut off by a crash. */
const PENDING_MAX_MS = 60_000

export type HandoffFate = 'attach' | 'skip' | { drop: string }

/**
 * What the session `at.sessionId` in `at.cwd` does with a held hand-off.
 * It joins a different session than the one that saved it, in the
 * same project, that started at or after it was saved (`startedAt`, null when
 * unknown), within 10 minutes of the save. No marker ties it to one clear, so
 * a typed /clear, a reload or a lost start event cannot strand or steal it.
 * Another project's hand-off is never this session's to touch; an
 * expired one in this project is dropped (the reason ends a one-line note).
 */
export function handoffFate(h: Handoff, at: { sessionId: string; cwd: string; startedAt: number | null; now: number }): HandoffFate {
  if (h.cwd !== at.cwd) return 'skip'
  if (h.pendingSince !== undefined) {
    // Being stamped right now; one a crash left half-saved is dropped after a minute.
    return at.now - h.pendingSince > PENDING_MAX_MS ? { drop: 'its save never finished' } : 'skip'
  }
  if (at.now - h.createdAt > HANDOFF_TTL_MS) return { drop: 'it is more than 10 minutes old' }
  if (at.sessionId === '' || at.sessionId === h.fromSessionId) return 'skip'
  return at.startedAt !== null && at.startedAt >= h.createdAt ? 'attach' : 'skip'
}

export function initialUi(): UiState {
  return { busy: null, busySince: null, note: null, noteUntil: 0, freshArmedAt: null }
}

export function isArmed(ui: UiState, now: number): boolean {
  return ui.freshArmedAt !== null && now - ui.freshArmedAt < FRESH_ARM_MS
}

/** The busy state as it stands at `now`: a stale one has timed out. */
export function busyNow(ui: UiState, now: number): Busy {
  if (ui.busy === null || ui.busySince === null) return null
  return now - ui.busySince < BUSY_TIMEOUT_MS ? ui.busy : null
}

/** The "Full dashboard" link is ignored while anything else is busy (a second click included). */
export function canOpenDashboard(ui: UiState, now: number): boolean {
  return busyNow(ui, now) === null
}

/** measure.py prints this and still exits 0 when the OS opener failed. */
const BROWSER_FAILED = 'Could not auto-open browser'

/** The dashboard run failed: it threw or timed out (`null`), exited non-zero, or could not open the browser. */
export function dashboardFailed(result: { exitCode: number; stdout: string } | null): boolean {
  return result === null || result.exitCode !== 0 || result.stdout.includes(BROWSER_FAILED)
}

/** What the dashboard link's own status line says: while it opens, and after it failed; null otherwise. */
export function dashboardStatus(busy: string | null, note: string | null): string | null {
  if (busy === 'dashboard') return DASHBOARD_OPENING
  return note === DASHBOARD_FAILED ? DASHBOARD_FAILED : null
}

export function withBusy(ui: UiState, busy: Busy, now: number): UiState {
  return { ...ui, busy, busySince: busy === null ? null : now, freshArmedAt: null }
}

export function withNote(ui: UiState, note: string, now: number): UiState {
  return { ...ui, note, noteUntil: now + NOTE_MS }
}

export function noteNow(ui: UiState, now: number): string | null {
  return ui.note !== null && now < ui.noteUntil ? ui.note : null
}

/** The path compact-capture printed (`[Token Optimizer] Checkpoint saved: <path>`), or null. */
export function checkpointPathFrom(stdout: string): string | null {
  const match = /Checkpoint saved: (.+?)\s*$/m.exec(stdout)
  return match?.[1] ? match[1] : null
}

/** compact_capture writes this note when it found no transcript to read. */
export function isStubCheckpoint(contents: string): boolean {
  if (!contents.includes('No transcript data available')) return false
  // Empty means the phrase with almost nothing else; a real checkpoint that quotes it is not.
  const rest = contents.replace('No transcript data available', '').replace(/^#.*$/gm, '').replace(/\s+/g, '')
  return rest.length < STUB_MAX_CHARS
}

/** A checkpoint with less than this much besides its headings and the empty notice saved nothing. */
const STUB_MAX_CHARS = 200

const POINTER = /^.*Cross-session checkpoint.*$/

/**
 * Removes Token Optimizer's "Cross-session checkpoint" pointer lines from a
 * SessionStart's context: after Start fresh the held hand-off replaces it.
 * Every other line stays; an entry left empty is dropped.
 */
export function stripCrossSessionPointer(entries: readonly string[] | undefined): string[] | undefined {
  if (!entries) return undefined
  const out: string[] = []
  for (const entry of entries) {
    const kept = entry.split('\n').filter(line => !POINTER.test(line)).join('\n')
    if (kept.trim() !== '') out.push(kept)
  }
  return out
}

type Json = Record<string, unknown>
const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0)

/**
 * The lifetime one request's cache writes used, when its usage carries the
 * 1h/5m split (transcript rows do; `TurnUsage` may not). Null when unknown.
 */
export function lifetimeFromUsage(usage: unknown): '1h' | '5m' | null {
  if (!usage || typeof usage !== 'object') return null
  const split = (usage as Json).cache_creation
  if (!split || typeof split !== 'object') return null
  if (num((split as Json).ephemeral_1h_input_tokens) > 0) return '1h'
  if (num((split as Json).ephemeral_5m_input_tokens) > 0) return '5m'
  return null
}

/** Prompt tokens one request carried: what the next request re-reads (per request, never the turn's sum). */
export function requestContextTokens(usage: unknown): number {
  if (!usage || typeof usage !== 'object') return 0
  const u = usage as Json
  return num(u.input_tokens) + num(u.cache_read_input_tokens) + num(u.cache_creation_input_tokens)
}

export type WarmOutcome =
  | { ok: true; lapsed: boolean; contextTokens: number; cacheRead?: number; lifetimeS?: number }
  | { ok: false; reason: string }

/** One line for the Keep warm outcome; a lapsed cache names what the warm-up cost. */
export function warmToast(o: WarmOutcome): string {
  if (!o.ok) return `Keep warm did not run: ${o.reason}.`
  if (o.lapsed) return `The cache had already dropped, so the warm-up re-read ${tokens(o.contextTokens)} tokens at full price.`
  // The receipt: what the warm-up read from the cache, and that the clock restarted.
  const read = o.cacheRead && o.cacheRead > 0 ? `: re-read ${tokens(o.cacheRead)} tokens from the cache at a tenth of the price` : ''
  const clock = o.lifetimeS ? ` The clock is back to ${Math.round(o.lifetimeS / 60)}m.` : ''
  return `Cache kept warm${read}.${clock}`
}

const TYPED = new Set(['composer', 'sdk', 'bridge', 'unclassified'])

/** The hand-off joins a prompt the person sent, never a notification, peer or automatic one. */
export function attachesHandoff(originKind: string | undefined): boolean {
  return originKind === undefined || TYPED.has(originKind)
}

/** What Start fresh needs from the host: measure.py by arguments (stdin optional), and a file read. */
export type HandoffPort = {
  run: (args: string[], stdin?: string) => Promise<{ exitCode: number; stdout: string }>
  read: (path: string) => Promise<string>
}

export type HandoffResult = { ok: true; handoff: Handoff } | { ok: false; reason: string }

/**
 * Start fresh up to the clear: save a checkpoint, check it is real,
 * build the lean resume text. Any failure returns a one-line reason and
 * nothing is cleared.
 */
export async function prepareHandoff(
  port: HandoffPort,
  input: { sessionId: string; transcriptPath: string | null; cwd: string; now: number },
): Promise<HandoffResult> {
  const fail = (reason: string): HandoffResult => ({ ok: false, reason })
  const stdin = JSON.stringify(input.transcriptPath ? { session_id: input.sessionId, transcript_path: input.transcriptPath } : { session_id: input.sessionId })

  let captured: { exitCode: number; stdout: string }
  try {
    captured = await port.run(['compact-capture', '--trigger', 'start-fresh', '--budget-seconds', String(CAPTURE_BUDGET_SECONDS)], stdin)
  } catch {
    return fail('the checkpoint could not be saved')
  }
  if (captured.exitCode !== 0) return fail('the checkpoint could not be saved')
  const path = checkpointPathFrom(captured.stdout)
  if (!path) return fail('the checkpoint could not be saved')

  let contents: string
  try {
    contents = await port.read(path)
  } catch {
    return fail('the saved checkpoint could not be read')
  }
  if (isStubCheckpoint(contents)) return fail('the checkpoint came out empty')

  let lean: { exitCode: number; stdout: string }
  try {
    lean = await port.run(['resume-lean', input.sessionId, '--print'])
  } catch {
    return fail('the hand-off text could not be built')
  }
  const text = lean.exitCode === 0 ? lean.stdout.trim() : ''
  if (!text) return fail('the hand-off text came out empty')

  return { ok: true, handoff: { fromSessionId: input.sessionId, cwd: input.cwd, checkpointPath: path, text, createdAt: input.now } }
}
