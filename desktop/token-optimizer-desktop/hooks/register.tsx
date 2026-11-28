// The Token Optimizer band above the prompt in the desktop app.
//
// This is the plugin's one hooks module and the only file that touches `$`.
// Every decision lives in ../src (pure, Node-tested) and ./data.ts (the data
// gatherer over a port). The engine allows `$` only into functions declared
// at the top of this file, so each `$` helper below is one, and closures made
// inside hooks only ever hand `$` on to them.
//
// State the drawing reads lives in `$.state` atoms (types/index.d.ts) so a hot
// reload keeps it; the pose reducer also runs from a module copy so streaming
// chunks do not write on every piece.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'
import type { TokenOptimizerDesktopSession } from '../types/index.d.ts'
import {
  BUSY_TIMEOUT_MS,
  CAPTURE_TIMEOUT_MS,
  DASHBOARD_ARGS,
  DASHBOARD_FAILED,
  DASHBOARD_TIMEOUT_MS,
  HANDOFF_KEY,
  HANDOFF_TTL_MS,
  RESUME_TIMEOUT_MS,
  WARM_PROMPT,
  attachesHandoff,
  busyNow,
  busyTimeoutMs,
  canOpenDashboard,
  cleanUpOutcome,
  dashboardFailed,
  handoffFate,
  initialUi,
  isArmed,
  isHandoff,
  lifetimeFromUsage,
  liveTokens,
  noteNow,
  prepareHandoff,
  requestContextTokens,
  stripCrossSessionPointer,
  warmToast,
  withBusy,
  withNote,
  type Busy,
  type Handoff,
  type HandoffPort,
  type UiState,
} from '../src/actions.ts'
import { canKeepWarm, initialClock, reduceClock, view, type ClockEvent, type ClockState } from '../src/clock.ts'
import { LIGHT, clawdSvg } from '../src/clawd.ts'
import type { CacheView, Snapshot } from '../src/contracts.ts'
import { drawBand, drawSlim, tonesFor, type Model, type Handlers, type ClawdLayer } from '../src/band.tsx'
import { allowsHeadlessFallback, decideArgs, liveMeters, parseDecision, parseHeadlessResult, proofOk, shouldAsk,
  HEADLESS_STOP, HEADLESS_TIMEOUT_MS, PROOF_STOP } from '../src/autowarm.ts'
import { moodOf, type ActionId } from '../src/ladder.ts'
import { envSize, storedSize, type BandSize } from '../src/size.ts'
import { switchedOff, timeOptions } from '../src/switches.ts'
import { DEBOUNCE_MS, initialPose, reducePose, type PoseEvent, type PoseState } from '../src/pose.ts'
import {
  QUALITY_REFRESH_MS,
  STATUS_AFTER_TURN_MS,
  STATUS_TIMEOUT_MS,
  TICK_IDLE_MS,
  TICK_WARNING_MS,
  cleanId,
  findTokenOptimizerRoot,
  gather,
  mergeStored,
  readHome,
  runMeasure, bookModAction, logForkFailure,
  type DataIo,
  type GatherOptions,
} from './data.ts'

const sessionAtom = atom({ plugin: 'token-optimizer', key: 'session' } as const, null)
const handoffAtom = atom({ plugin: 'token-optimizer', key: 'handoff' } as const, null)
const clockAtom = atom({ plugin: 'token-optimizer', key: 'clock' } as const, null)
const poseAtom = atom({ plugin: 'token-optimizer', key: 'pose' } as const, null)
const engineCallAtom = atom({ plugin: 'token-optimizer', key: 'engineCall' } as const, null)
const uiAtom = atom({ plugin: 'token-optimizer', key: 'ui' } as const, null)
const frameAtom = atom({ plugin: 'token-optimizer', key: 'frame' } as const, 0)

// ---- module state: plain variables, rebuilt from the atoms after a reload ----

let enabled = true
let animate = true
/** Hover cards on the marks; TOKEN_OPTIMIZER_STATUS_BAR_CARDS=0 turns them off (#367). */
let hoverCards = true
let active = false
/** Whether the TOKEN_OPTIMIZER_STATUS_BAR switches have been read in this environment. */
let switchesRead = false
let timers: { cancel: () => void }[] = []
let statusTimer: { cancel: () => void } | null = null
let poseLive: PoseState | null = null
let poseSig = ''
let settleQueued = false
/** The cache coldness last told to Clawd; null until the first tick tells him, so a cold pose kept across a reload never sticks. */
let lastCold: boolean | null = null
let frameText = ''
/** The transcript Claude Code named for one session: never handed to another session's read. */
let transcript: { sessionId: string; path: string } | null = null

function noteTranscript(sessionId: unknown, path: unknown): void {
  if (typeof path === 'string' && path && typeof sessionId === 'string' && sessionId) transcript = { sessionId: cleanId(sessionId), path }
}

function transcriptFor(sid: string): string | undefined {
  return transcript !== null && sid !== '' && transcript.sessionId === sid ? transcript.path : undefined
}
let warmInFlight = false
let lastAutoAsk: { sid: string; anchor: number | null } | null = null
const autoStopped = new Set<string>()
let compactedAt = 0
let autoCompacting = false
let autoTurnRunning = false
/**
 * Which session the band is serving: bumped when one starts, is cleared or is
 * swapped in. Work begun under one generation drops its result in the next.
 */
let sessionGen = 0
/**
 * A press is being claimed: a second press meanwhile is ignored (no double
 * compaction). A claim older than the busy timeout is stale: a stuck press
 * never holds the buttons past it.
 */
let claim: { at: number } | null = null
/**
 * The one engine call the buttons may have running: our `$.session.compact()`
 * or our `$.command.run({ command: 'clear' })`. Set before the call's first
 * await, cleared only when it settles, whatever the busy timeout says: a
 * second compaction or clear never overlaps it. `engineCallAtom` records it
 * too, so a reload mid-call still refuses (see runningCall).
 */
let engineCall: 'compact' | 'clear' | null = null
/** When engineCall was claimed: a call that never settles stops blocking after HANDOFF_TTL_MS. */
let engineCallAt = 0
/** The live session a render last asked to re-read after finding another session's figures. */
let resyncFor = ''
/** Compactions that landed in this process, so Clean up can tell one happened during its /compact. */
let compactionsLanded = 0; let modCompactEvidence: { usage?: unknown; before?: number; after?: number } | null = null

/** The store key for the user's band size: the user's choice, so it survives sessions and restarts. */
const SIZE_KEY = 'status-bar-size'
/** The env default (TOKEN_OPTIMIZER_STATUS_BAR_SIZE), read once in readSwitches. */
let sizeEnvDefault: BandSize = 'full'
let timeOpts = timeOptions(undefined)
/** The stored size, once read or pressed: null while only the env default applies. */
let sizeClicked: BandSize | null = null
/** Whether $.store was consulted for the size successfully (a failed read retries next render). */
let sizeRead = false

const attempt = async <T,>(work: () => Promise<T>, fallback: T): Promise<T> => {
  try {
    return await work()
  } catch {
    return fallback
  }
}

/** Session ids become file names; keep what Token Optimizer's sanitizer keeps (as data.ts does). */

// ---- data ----

/** The gatherer's port over `$` (data.ts never sees `$`). */
function dataIo($: EngineInterface): DataIo {
  return {
    now: () => $.clock.now(),
    sessionId: () => $.session.id(),
    cwd: () => $.session.cwd(),
    envHome: () => $.env.get('HOME'),
    envUserProfile: () => $.env.get('USERPROFILE'),
    envConfigDir: () => $.env.get('CLAUDE_CONFIG_DIR'),
    envAutoCompactWindow: () => $.env.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW'),
    usage: () => $.session.usage(),
    list: path => $.fs.list(path),
    stat: path => $.fs.stat(path),
    read: async path => {
      const text = await $.fs.read(path)
      return typeof text === 'string' ? text : ''
    },
    run: (argv, init) => $.process.run(argv, init),
    pluginRoot: () => $.plugin.root,
    log: async text => {
      await $.ui.log(text, { to: 'debug' })
    },
  }
}

/** Gather and store the session's figures, then let the cache clock learn from them. */
async function refresh($: EngineInterface, options: GatherOptions = {}): Promise<void> {
  let gen = sessionGen
  const current = await attempt(() => read($, sessionAtom), null)
  const liveSid = cleanId(options.sessionId ?? (await attempt(() => $.session.id(), '')))
  const path = options.transcript ?? transcriptFor(liveSid)
  const fresh = await gather(dataIo($), current, path ? { ...options, transcript: path } : options)
  // Begun before a clear or a session change: its figures belong to the old session.
  if (gen !== sessionGen) return
  if (!options.reset && current !== null && fresh.sessionId !== '' && current.sessionId !== fresh.sessionId) {
    // Another session (a new one, or a resume): nothing of the last one carries over.
    sessionGen += 1
    gen = sessionGen
    toolsPending = 0
    await feedClock($, { type: 'clear' })
    await setUi($, () => initialUi())
    lastCold = null
    await feedPose($, { type: 'session-start' })
  }
  const latest = await attempt(() => read($, sessionAtom), null)
  if (gen !== sessionGen) return
  const merged = mergeStored(latest, fresh, options.reset, options.savings === true)
  // Nothing shown changed (gatheredAt always does): no write, so no redraw.
  if (latest !== null && sameShown(latest, merged)) return void (await syncClock($, fresh, gen))
  await update($, sessionAtom, cur => gen === sessionGen ? mergeStored(cur, fresh, options.reset, options.savings === true) : cur)
  if (gen !== sessionGen) return
  await syncClock($, fresh, gen)
}

function sameShown(a: TokenOptimizerDesktopSession, b: TokenOptimizerDesktopSession): boolean {
  return JSON.stringify({ ...a, gatheredAt: 0 }) === JSON.stringify({ ...b, gatheredAt: 0 })
}

/**
 * Runs work off the current event (the status command can take seconds). Where no
 * clock can schedule it, it runs now rather than not at all.
 */
async function later($: EngineInterface, work: () => Promise<void>): Promise<void> {
  // Work for the session as it is now: a clear or session change before it runs drops it.
  const gen = sessionGen
  const guarded = async () => {
    if (gen === sessionGen) await work()
  }
  try {
    $.clock.after(0, () => void attempt(guarded, undefined))
  } catch {
    await attempt(guarded, undefined)
  }
}

/** A prompt is taking the hand-off: a second prompt in the same moment does not. */
let takingHandoff = false

/** Tool calls the band counted since the last write: written alongside a redraw that happens anyway. */
let toolsPending = 0

async function flushTools($: EngineInterface): Promise<void> {
  if (toolsPending === 0) return
  const n = toolsPending
  toolsPending = 0
  await attempt(() => update($, sessionAtom, cur => (cur ? { ...cur, toolCallsSeen: (cur.toolCallsSeen ?? 0) + n } : cur)), undefined)
}

/** The status command's anchor and measured lifetime feed the clock when they are newer than what it holds. */
async function syncClock($: EngineInterface, s: TokenOptimizerDesktopSession, gen?: number): Promise<void> {
  if (gen !== undefined && gen !== sessionGen) return
  await attempt(
    () =>
      update($, clockAtom, cur => {
        if (gen !== undefined && gen !== sessionGen) return cur
        let c: ClockState = cur ?? initialClock()
        if (s.cacheLifetime && c.lifetime !== s.cacheLifetime) c = reduceClock(c, { type: 'lifetime-measured', lifetime: s.cacheLifetime })
        if (s.lastRequestEpoch !== null) {
          const at = s.lastRequestEpoch * 1000
          if (c.anchor === null || at > c.anchor) {
            c = reduceClock(c, { type: 'request-done', at, lifetime: s.cacheLifetime, contextTokens: s.contextTokens ?? c.contextTokens ?? 0 })
          }
        }
        if (c.contextTokens === null && s.contextTokens !== null) c = { ...c, contextTokens: s.contextTokens }
        return c
      }),
    undefined,
  )
}

async function feedClock($: EngineInterface, event: ClockEvent, now?: number): Promise<void> {
  await attempt(() => update($, clockAtom, cur => reduceClock(cur ?? initialClock(), event, now)), undefined)
}

/**
 * One pose event. The reducer runs on the module copy; the atom is written
 * only when something the drawing or a reload needs has changed.
 */
async function feedPose($: EngineInterface, event: PoseEvent): Promise<void> {
  try {
    const now = await $.clock.now()
    if (!poseLive) poseLive = (await read($, poseAtom)) ?? initialPose(now)
    poseLive = reducePose(poseLive, event, now)
    const s = poseLive
    const sig = JSON.stringify([s.pose, s.since, s.working, s.sub, s.agents, s.permissions, s.questions, s.compacting, s.cold, s.until])
    if (sig !== poseSig) {
      poseSig = sig
      await update($, poseAtom, () => s)
    }
    // A sub-pose waiting out its debounce settles on a tick of its own.
    if (s.rawSub !== s.sub && !settleQueued) {
      settleQueued = true
      $.clock.after(DEBOUNCE_MS, () => {
        settleQueued = false
        void feedPose($, { type: 'tick', now: 0 })
      })
    }
  } catch {
    // A pose that cannot be stored never breaks the event it rode on.
  }
}

/** Permission prompts and questions have no closing event of their own: close them on the next sign of life. */
async function closeAsks($: EngineInterface): Promise<void> {
  // After a reload the module copy is empty until the first pose event: read the stored one.
  if (!poseLive) poseLive = await attempt(() => read($, poseAtom), null)
  for (let i = poseLive?.permissions ?? 0; i > 0; i--) await feedPose($, { type: 'permission-closed' })
  for (let i = poseLive?.questions ?? 0; i > 0; i--) await feedPose($, { type: 'question-closed' })
}

function planDefault(s: TokenOptimizerDesktopSession | null): 3600 | 300 {
  // Rate limits mean a Claude plan: an hour of cache; the API keeps five minutes.
  // A limit seen once this session keeps it a plan, even if usage briefly stops reporting one.
  return s && (s.fiveHour || s.week || s.sawLimits) ? 3600 : 300
}

/**
 * Keeps Clawd's transients and naps moving and redraws when what the band
 * shows changes: by the second near a deadline or during a hold, otherwise
 * every 5 s, with at most one redraw a minute while nothing moves.
 */
/** Last tick that did its full work: when nothing moves by the second, every 5 s is enough. */
let fullTickAt = 0
const QUIET_TICK_MS = 5000
/** Something on screen changes by the second (the last cache minutes, a note, a pending press). */
let nearDeadline = false

async function tick($: EngineInterface): Promise<void> {
  try {
    const now = await $.clock.now()
    // By the second only while a countdown is near its end, a transient pose holds, or a
    // press is pending; otherwise every 5 s (each tick is several engine calls).
    // A hold still running at the last full tick keeps ticking by the second until it ends.
    const urgent = poseLive !== null && Object.values(poseLive.until).some(t => t > fullTickAt)
    if (!urgent && !settleQueued && now - fullTickAt < QUIET_TICK_MS && !nearDeadline) return
    fullTickAt = now
    await feedPose($, { type: 'tick', now })
    const clock = (await read($, clockAtom)) ?? initialClock()
    const session = await read($, sessionAtom)
    const ui = (await read($, uiAtom)) ?? initialUi()
    const v = view(clock, now, planDefault(session))
    void attempt(() => maybeAutoWarm($, clock, v, session, now), undefined)
    const cold = v.state === 'cold'
    if (cold !== lastCold) {
      lastCold = cold
      await feedPose($, { type: 'cache-cold-changed', cold })
    }
    // Only a change the band shows redraws it, at most once a minute: the desktop restarts
    // Clawd's picture on every redraw, so a per-second countdown made him blink each second.
    // One minute clock: the cache countdown's when it runs, else the wall clock's (for "27m ago").
    const minute = v.secondsLeft != null ? `c${Math.ceil(v.secondsLeft / 60)}` : `w${Math.floor(now / TICK_IDLE_MS)}`
    const text = [v.state, minute, busyNow(ui, now), noteNow(ui, now), isArmed(ui, now)].join('|')
    nearDeadline = v.state === 'warning' || v.state === 'warming' || busyNow(ui, now) !== null || noteNow(ui, now) !== null || isArmed(ui, now)
    if (text !== frameText) await flushTools($)
    if (text !== frameText) {
      frameText = text
      await update($, frameAtom, n => (n ?? 0) + 1)
    }
  } catch {
    // The next tick tries again.
  }
}

function startCadence($: EngineInterface): void {
  for (const t of timers) t.cancel()
  timers = []
  try {
    timers.push($.clock.every(TICK_WARNING_MS, () => void tick($)))
    timers.push($.clock.every(QUALITY_REFRESH_MS, () => void attempt(() => refresh($), undefined)))
  } catch {
    // No timers: the band still redraws on events.
  }
}

/** Everything a desktop session needs once: a pending hand-off from disk, figures, the cadence. */
async function start($: EngineInterface): Promise<void> {
  sessionGen += 1
  // A warm-up marked running with no fork in flight here was cut off by a reload.
  const clock = await attempt(() => read($, clockAtom), null)
  if (clock?.warming && !warmInFlight) await feedClock($, { type: 'warm-failed' })
  const sizing = attempt(() => readSize($), undefined)
  const held = await heldHandoff($)
  if (held) await handoffThatFits($, held)
  await sizing
  await feedPose($, { type: 'session-start' })
  // The quick reads now, so the band fills at once; the status command (seconds, at worst)
  // runs off the start event, so the session never waits on it.
  await attempt(() => refresh($), undefined)
  await later($, () => refresh($, { savings: true }))
  startCadence($)
}

/**
 * The pending hand-off. `$.store` is the truth (it survives restarts and is
 * deleted when one session takes it); the atom only mirrors it for drawing. A
 * store that cannot be read holds nothing: no stale mirror is ever attached.
 */
async function heldHandoff($: EngineInterface): Promise<Handoff | null> {
  let stored: unknown
  try {
    stored = await $.store.get(HANDOFF_KEY)
  } catch {
    return null
  }
  const held = isHandoff(stored) ? stored : null
  if (stored != null && !held) await attempt(() => $.store.delete(HANDOFF_KEY), undefined)
  const mirror = await attempt(() => read($, handoffAtom), null)
  if (JSON.stringify(mirror) !== JSON.stringify(held)) await attempt(() => update($, handoffAtom, () => held), undefined)
  return held
}

/** When the live session began (`$.clock.now()` ms), or null when the engine cannot say. */
async function sessionStartedAt($: EngineInterface): Promise<number | null> {
  const usage = await attempt(() => $.session.usage(), null)
  return typeof usage?.startedAt === 'number' && Number.isFinite(usage.startedAt) ? usage.startedAt : null
}

/**
 * The held hand-off when it joins this session: another session than
 * the one that saved it, in its project, started since the save, within 10
 * minutes of it. An expired one in this project is dropped with a one-line
 * note; another project's is left alone. `as` names the session when the
 * caller knows its id better than the engine does yet (a clear's own start event).
 */
async function handoffThatFits($: EngineInterface, h: Handoff, as?: { sessionId: string; startedAt: number | null }): Promise<Handoff | null> {
  const sessionId = as?.sessionId ?? cleanId(await attempt(() => $.session.id(), ''))
  const cwd = await attempt(() => $.session.cwd(), '')
  const now = await $.clock.now()
  const startedAt = as ? as.startedAt : await sessionStartedAt($)
  const fate = handoffFate(h, { sessionId, cwd, startedAt, now })
  if (fate === 'attach') return h
  if (fate === 'skip') return null
  await dropHandoff($)
  const line = `Start fresh's saved hand-off was discarded: ${fate.drop}.`
  await setUi($, u => withNote(u, line, now))
  toast($, line)
  return null
}

/** The refusal for a press while our compaction or clear still runs. */
function stillRunning(kind: 'compact' | 'clear'): string {
  return kind === 'compact' ? 'Still finishing the last clean-up.' : 'Still clearing.'
}

/**
 * Our compaction or clear still running: this module's own, or one recorded
 * before a reload that is under 10 minutes old (an older record is cleared).
 */
async function runningCall($: EngineInterface): Promise<'compact' | 'clear' | null> {
  if (engineCall) {
    const at = await attempt(() => $.clock.now(), null)
    if (at === null || at - engineCallAt < HANDOFF_TTL_MS) return engineCall
    // Never settled: the record's own expiry below decides, as after a reload.
    engineCall = null
  }
  const held = await attempt(() => read($, engineCallAtom), null)
  if (engineCall) return engineCall
  if (!held) return null
  // A clock that cannot be read keeps the record: refuse rather than overlap.
  const now = await attempt(() => $.clock.now(), null)
  if (now === null || now - held.startedAt < HANDOFF_TTL_MS) return held.kind
  await attempt(() => update($, engineCallAtom, cur => (cur && cur.startedAt === held.startedAt && cur.kind === held.kind ? null : cur)), undefined)
  return null
}

/**
 * Claims the one engine call (the caller checked runningCall with no await
 * since) and records it for a reload. False when it cannot be recorded: the
 * call is not made.
 */
async function claimCall($: EngineInterface, kind: 'compact' | 'clear'): Promise<{ kind: 'compact' | 'clear'; startedAt: number } | null> {
  // Never over a call already in flight, whatever the caller checked before.
  if (engineCall !== null) return null
  engineCall = kind
  try {
    const mine = { kind, startedAt: await $.clock.now() }
    engineCallAt = mine.startedAt
    await update($, engineCallAtom, () => mine)
    return mine
  } catch {
    engineCall = null
    return null
  }
}

/** The call settled: release it here and in the record (only our own record). */
async function releaseCall($: EngineInterface, mine: { kind: 'compact' | 'clear'; startedAt: number }): Promise<void> {
  engineCall = null
  await attempt(() => update($, engineCallAtom, cur => (cur && cur.kind === mine.kind && cur.startedAt === mine.startedAt ? null : cur)), undefined)
}

// ---- buttons ----

function toast($: EngineInterface, text: string): void {
  try {
    $.ui.toast(text)
  } catch {
    // A toast that cannot show is not worth failing a press over.
  }
}

async function setUi($: EngineInterface, fn: (ui: UiState) => UiState): Promise<void> {
  await attempt(() => update($, uiAtom, cur => fn(cur ?? initialUi())), undefined)
}

async function disarm($: EngineInterface): Promise<void> {
  const ui = await attempt(() => read($, uiAtom), null)
  if (ui?.freshArmedAt != null) await setUi($, u => ({ ...u, freshArmedAt: null }))
}

const BUSY_WORDS: Record<Exclude<Busy, null>, string> = {
  clean: 'Clean up',
  'fresh-capture': 'Start fresh',
  'fresh-clear': 'Start fresh',
  dashboard: 'Opening the dashboard',
}

/** A busy state ends by itself: a step label never outlives its work. */
function armBusyTimeout($: EngineInterface, busy: Exclude<Busy, null>, since: number): void {
  try {
    $.clock.after(busyTimeoutMs(busy), () => void expireBusy($, busy, since))
  } catch {
    // busyNow() still treats it as over after the timeout.
  }
}

async function expireBusy($: EngineInterface, busy: Exclude<Busy, null>, since: number): Promise<void> {
  const ui = await attempt(() => read($, uiAtom), null)
  if (!ui || ui.busy !== busy || ui.busySince !== since) return
  const now = await $.clock.now()
  // The clear waits for the turn to end and can still land: the hand-off stays
  // for the conversation it creates, within 10 minutes of the save.
  const line =
    busy === 'fresh-clear' ? 'Start fresh clears when the current turn ends.' : busy === 'dashboard' ? DASHBOARD_FAILED : `${BUSY_WORDS[busy]} timed out.`
  await setUi($, u => withNote(withBusy(u, null, now), line, now))
  // The dashboard link fails by its note alone.
  if (busy !== 'dashboard') toast($, line)
}

/** Deletes the held hand-off, store first; true only when the delete succeeded (the mirror follows it). */
async function dropHandoff($: EngineInterface): Promise<boolean> {
  try {
    await $.store.delete(HANDOFF_KEY)
  } catch {
    return false
  }
  await attempt(() => update($, handoffAtom, () => null), undefined)
  return true
}

async function isTurnRunning($: EngineInterface): Promise<boolean> {
  const clock = await attempt(() => read($, clockAtom), null)
  return Boolean(clock?.working || poseLive?.working)
}

/** Clean up: compaction with Token Optimizer's own PreCompact guidance, run outside the press. */
async function cleanUp($: EngineInterface, kind: 'clean' | 'clean-first'): Promise<void> {
  const now = await $.clock.now()
  const ui = (await attempt(() => read($, uiAtom), null)) ?? initialUi()
  if (busyNow(ui, now) !== null) return
  await disarm($)
  const running = await runningCall($)
  if (running) {
    toast($, stillRunning(running))
    return
  }
  if (await isTurnRunning($)) {
    toast($, 'Clean up waits until the turn finishes.')
    return
  }
  await setUi($, u => withBusy(u, 'clean', now))
  armBusyTimeout($, 'clean', now)
  $.clock.after(0, () => void attempt(() => runCompact($, now, kind), undefined))
}

async function runCompact($: EngineInterface, since: number, kind: 'clean' | 'clean-first'): Promise<void> {
  // A turn that started since the press would be cut short.
  if (await isTurnRunning($)) {
    const ui = await attempt(() => read($, uiAtom), null)
    if (!ui || ui.busy !== 'clean' || ui.busySince !== since) return
    const now = await $.clock.now()
    await setUi($, u => withBusy(u, null, now))
    toast($, 'Clean up waits until the turn finishes.')
    return
  }
  const running = await runningCall($)
  if (running || engineCall) {
    const ui = await attempt(() => read($, uiAtom), null)
    if (!ui || ui.busy !== 'clean' || ui.busySince !== since) return
    const now = await $.clock.now()
    await setUi($, u => withBusy(u, null, now))
    toast($, stillRunning(running ?? engineCall ?? 'compact'))
    return
  }
  /** The note when no compaction can be confirmed; null after a confirmed one. */
  let skip: string | null = null; modCompactEvidence = null
  const skipped = (reason: string) => `Clean up skipped: ${reason.replace(/\.$/, '')}.`
  // Claimed with no await between the check and the claim; recorded before the call.
  const mine = await claimCall($, 'compact')
  if (!mine) {
    skip = skipped('it could not be recorded')
  } else {
    try {
      // The command, as if typed: $.session.compact() is refused in a headless
      // session, and the desktop app runs its sessions headless.
      // Success is decided from evidence, never from the reply's words (#207):
      // Claude Code answers a refused /compact ("Not enough messages to compact.",
      // a hook's veto) without throwing, and a reply may be empty or not English.
      const landedBefore = compactionsLanded
      const beforeUsage = await attempt(() => $.session.usage(), null); const tokensBefore = liveTokens(beforeUsage); const compactSid = cleanId(await attempt(() => $.session.id(), '')); const compactPath = transcriptFor(compactSid); const compactTtl = (await attempt(() => read($, sessionAtom), null))?.cacheLifetime
      const answer = await $.command.run({ command: 'compact' })
      const afterUsage = await attempt(() => $.session.usage(), null); const tokensAfter = liveTokens(afterUsage)
      const outcome = cleanUpOutcome({
        landed: compactionsLanded > landedBefore,
        tokensBefore,
        tokensAfter,
        said: answer?.text ?? '',
      })
      if (!outcome.ok) skip = outcome.line; else { const evidence = modCompactEvidence as { usage?: unknown; before?: number; after?: number } | null; const finished = await $.clock.now(); const receipt = { action: kind, action_id: `${compactSid}-${since}-compact`, session_id: compactSid, started_at: since / 1000, timestamp: finished / 1000, transcript_path: compactPath, before_tokens: evidence?.before ?? tokensBefore, after_tokens: evidence?.after ?? tokensAfter, usage: evidence?.usage, observed_manual: evidence !== null, observed_action: true, cost_usd: beforeUsage?.cost?.usd != null && afterUsage?.cost?.usd != null ? afterUsage.cost.usd - beforeUsage.cost.usd : null, ttl_kind: compactTtl }; void attempt(() => bookModAction(dataIo($), receipt), false); $.clock.after(STATUS_AFTER_TURN_MS, () => void attempt(async () => { if (!await bookModAction(dataIo($), receipt)) toast($, 'Clean-up savings could not be recorded in Token Optimizer.') }, undefined)) }
    } catch (error) {
      skip = skipped(error instanceof Error && error.message ? error.message.split('\n')[0] ?? 'it failed' : 'it failed')
    } finally {
      await releaseCall($, mine)
    }
  }
  const ui = await attempt(() => read($, uiAtom), null)
  // Timed out meanwhile, or another step took over: nothing to report here.
  if (!ui || ui.busy !== 'clean' || ui.busySince !== since) return
  const now = await $.clock.now()
  if (skip !== null) {
    const line = skip
    await setUi($, u => withNote(withBusy(u, null, now), line, now))
    toast($, line)
  } else {
    await setUi($, u => withNote(withBusy(u, null, now), 'Cleaned up.', now))
  }
  $.clock.after(0, () => void attempt(() => refresh($), undefined))
}

/** Manual Keep warm uses the same fork and booking path as the timer. */
async function keepWarm($: EngineInterface): Promise<void> {
  const now = await $.clock.now()
  await disarm($)
  const clock = (await attempt(() => read($, clockAtom), null)) ?? initialClock()
  if (warmInFlight || clock.warming) {
    toast($, 'A warm-up is already running.')
    return
  }
  if (!canKeepWarm(clock, now)) {
    toast($, clock.working || (await isTurnRunning($)) ? 'Keep warm waits until the turn finishes.' : 'The cache is too close to dropping to keep it warm.')
    return
  }
  warmInFlight = true
  const gen = sessionGen
  await feedClock($, { type: 'warm-start' }, now)
  $.clock.after(0, () => void attempt(() => runWarm($, clock.contextTokens, gen), undefined))
}

type ForkReply = Awaited<ReturnType<EngineInterface['model']['fork']>>

/** The warm-up fork, or null once `ms` pass without an answer; a late answer is then ignored. */
function forkWithin($: EngineInterface, ms: number, receipt: (reply: ForkReply) => Promise<void>): Promise<ForkReply | null> {
  return new Promise<ForkReply | null>((resolve, reject) => {
    let timer: { cancel: () => void } | null = null
    try {
      timer = $.clock.after(ms, () => { void logForkFailure(dataIo($), 'fork timed out'); resolve(null) })
    } catch {
      // No timer: the fork alone decides.
    }
    $.model.fork({ prompt: WARM_PROMPT }).then(
      async reply => {
        timer?.cancel(); if (!reply.isAnswered) void logForkFailure(dataIo($), `fork: ${reply.reason}`); await receipt(reply).catch(error => logForkFailure(dataIo($), `receipt: ${error instanceof Error ? error.name : 'Error'}`))
        resolve(reply)
      },
      error => {
        timer?.cancel()
        void logForkFailure(dataIo($), `fork: ${error instanceof Error ? error.name : 'Error'}`); reject(error)
      },
    )
  })
}

type AutoWarm = { sid: string; anchor: number; headless: boolean; args: string[] }

async function autoStillIdle($: EngineInterface, auto: AutoWarm, gen: number, started = false): Promise<boolean> {
  const clock = await read($, clockAtom)
  const now = await $.clock.now()
  const sid = cleanId(await $.session.id())
  return gen === sessionGen && sid === auto.sid && clock?.anchor === auto.anchor
    && auto.anchor > compactedAt && !autoTurnRunning && !autoCompacting && !poseLive?.working && !poseLive?.compacting
    && canKeepWarm({ ...clock, warming: started ? false : clock.warming }, now)
}

async function maybeAutoWarm($: EngineInterface, clock: ClockState, cache: CacheView,
                             session: TokenOptimizerDesktopSession | null, now: number): Promise<void> {
  if (!active || warmInFlight || autoTurnRunning || autoCompacting
      || poseLive?.working || poseLive?.compacting || !session?.sessionId
      || !session.cacheModel || !session.lastRequestEpoch || clock.anchor === null || clock.anchor <= compactedAt
      || !clock.contextTokens || autoStopped.has(session.sessionId)
      || !shouldAsk(clock, cache, now, lastAutoAsk?.sid === session.sessionId ? lastAutoAsk.anchor : null)) return
  lastAutoAsk = { sid: session.sessionId, anchor: clock.anchor }
  const gen = sessionGen
  const io = dataIo($)
  const root = await findTokenOptimizerRoot(io, await readHome(io))
  if (!root) return
  const args = decideArgs(session.sessionId, clock.contextTokens, (now / 1000) - session.lastRequestEpoch,
    session.cacheModel, cache.lifetime, liveMeters(await $.session.usage()))
  const auto = { sid: session.sessionId, anchor: clock.anchor, headless: false, args }
  if (!await autoStillIdle($, auto, gen) || warmInFlight) return
  const decision = await runMeasure(io, root, args, { timeoutMs: STATUS_TIMEOUT_MS })
  if (decision.exitCode !== 0) return
  auto.headless = allowsHeadlessFallback(decision.stdout)
  if (!auto.headless && parseDecision(decision.stdout) !== 'ping') return
  if (!await autoStillIdle($, auto, gen) || warmInFlight) return
  warmInFlight = true
  await feedClock($, { type: 'warm-start' }, await $.clock.now())
  await runWarm($, clock.contextTokens, gen, auto)
}

function stopAutoWarm($: EngineInterface, sid: string, gen: number, headless = false): void {
  if (autoStopped.has(sid)) return
  autoStopped.add(sid)
  if (gen === sessionGen) toast($, headless ? HEADLESS_STOP : PROOF_STOP)
}

async function runHeadlessWarm($: EngineInterface, auto: AutoWarm, gen: number,
                               contextTokens: number, snapshot: Record<string, unknown>): Promise<void> {
  const io = dataIo($)
  const root = await findTokenOptimizerRoot(io, await readHome(io))
  if (!root || !await autoStillIdle($, auto, gen, true)) return
  const [, ...args] = auto.args
  const result = await runMeasure(io, root, ['keepwarm-decide', 'headless', ...args],
    { timeoutMs: HEADLESS_TIMEOUT_MS, stdin: JSON.stringify(snapshot) })
  const { fired, proof, cacheRead } = parseHeadlessResult(result.stdout)
  if (!fired) return
  if (!proof) stopAutoWarm($, auto.sid, gen, true)
  else if (gen === sessionGen) {
    await feedClock($, { type: 'warm-done', at: await $.clock.now(), cacheReadTokens: cacheRead, contextTokens })
    $.clock.after(0, () => void attempt(() => refresh($, { savings: true }), undefined))
  }
}

async function runWarm($: EngineInterface, known: number | null, gen: number, auto?: AutoWarm): Promise<void> {
  try {
    const session = await attempt(() => read($, sessionAtom), null)
    const sid = cleanId(await attempt(() => $.session.id(), ''))
    const path = transcriptFor(sid)
    const stat = path ? await attempt(() => $.fs.stat(path), null) : null
    const at = await $.clock.now()
    const snapshot = { action_id: `${sid}-${at}-${Math.random().toString(36).slice(2)}`, session_id: sid,
      transcript_path: path, byte_size: stat?.size, transcript_mtime: stat ? stat.mtimeMs / 1000 : null }
    const contextTokens = known || session?.contextTokens || 0
    if (auto && !await autoStillIdle($, auto, gen, true)) return
    if (auto?.headless) return await runHeadlessWarm($, auto, gen, contextTokens, snapshot)
    const io = dataIo($)
    const flags = auto ? { auto: true } : {}
    const booking = async (action: string, fields: Record<string, unknown> = {}) =>
      bookModAction(io, { ...snapshot, ...flags, ...fields, action, timestamp: (await $.clock.now()) / 1000 })
    const prepare = auto ? { ...liveMeters(await $.session.usage()), context_tokens: contextTokens } : {}
    if (!await booking('warm-prepare', prepare)) {
      if (!auto) toast($, 'Keep warm did not run: its cost could not be reserved.')
      return
    }
    if (gen !== sessionGen || (auto && !await autoStillIdle($, auto, gen, true))) {
      await booking('warm-cancel')
      return
    }
    let receiptFailed = false
    const markUnproved = async () => {
      if (!auto) return
      stopAutoWarm($, sid, gen)
      await booking('warm-unproved', { proof: false })
    }
    const reply = await forkWithin($, BUSY_TIMEOUT_MS, async answer => {
      if (!answer.isAnswered && answer.reason === 'nothing-to-fork') {
        if (auto) stopAutoWarm($, sid, gen)
        await booking('warm-cancel', auto ? { proof: false } : {})
        return
      }
      if (!('usage' in answer) || !answer.usage || !Object.values(answer.usage).some(n => typeof n === 'number' && n > 0)) {
        await markUnproved()
        await logForkFailure(io, 'fork returned no usage')
        return
      }
      const proved = answer.isAnswered && proofOk(answer.usage, contextTokens)
      if (auto && !proved) stopAutoWarm($, sid, gen)
      const booked = await booking('warm', { answered: answer.isAnswered, usage: answer.usage,
        ...(auto ? { proof: proved } : {}) })
      receiptFailed = !booked
      if (!booked && !auto) toast($, 'Warm-up cost could not be recorded in Token Optimizer.')
      if (!booked && auto) await markUnproved()
    }).catch(() => null)
    if (reply === null) await markUnproved()
    if (gen !== sessionGen) return
    const finished = await $.clock.now()
    if (reply?.isAnswered && (!auto || (!autoStopped.has(sid) && !receiptFailed))) {
      await feedClock($, { type: 'warm-done', at: finished, cacheReadTokens: reply.usage.cache_read_input_tokens, contextTokens })
      if (auto) {
        $.clock.after(0, () => void attempt(() => refresh($, { savings: true }), undefined))
        return
      }
      const after = await attempt(() => read($, clockAtom), null)
      const line = warmToast({
        ok: true, lapsed: Boolean(after?.lapsed), contextTokens, cacheRead: reply.usage.cache_read_input_tokens,
        lifetimeS: view(after ?? initialClock(), finished, planDefault(session)).lifetime,
      }) + (receiptFailed ? ' Cost could not be recorded in Token Optimizer.' : '')
      toast($, line)
      await setUi($, u => withNote(u, line, finished))
    } else if (!auto) {
      toast($, warmToast({ ok: false, reason: reply && !reply.isAnswered ? reply.reason : 'the request failed' }))
    }
  } finally {
    if (gen === sessionGen) await feedClock($, { type: 'warm-failed' })
    warmInFlight = false
  }
}

/**
 * "Full dashboard": the dashboard command (it regenerates the page and opens it in the
 * browser, whatever the OS opener is) through the runner Start fresh uses. A click while
 * anything is busy is ignored; a failure is a note for NOTE_MS and nothing else.
 */
async function openDashboard($: EngineInterface): Promise<void> {
  const now = await $.clock.now()
  const ui = (await attempt(() => read($, uiAtom), null)) ?? initialUi()
  if (!canOpenDashboard(ui, now)) return
  await setUi($, u => withBusy(u, 'dashboard', now))
  armBusyTimeout($, 'dashboard', now)
  $.clock.after(0, () => void attempt(() => runDashboard($, now), undefined))
}

async function runDashboard($: EngineInterface, since: number): Promise<void> {
  const io = dataIo($)
  const root = await attempt(async () => findTokenOptimizerRoot(io, await readHome(io)), null)
  const result = root ? await attempt(() => runMeasure(io, root, DASHBOARD_ARGS, { timeoutMs: DASHBOARD_TIMEOUT_MS }), null) : null
  const ui = await attempt(() => read($, uiAtom), null)
  // Timed out meanwhile, or another step took over: the person was told already.
  if (!ui || ui.busy !== 'dashboard' || ui.busySince !== since) return
  const now = await $.clock.now()
  const failed = dashboardFailed(result)
  await setUi($, u => (failed ? withNote(withBusy(u, null, now), DASHBOARD_FAILED, now) : withBusy(u, null, now)))
}

/** Start fresh: first press arms, a second within 5 s runs it. */
async function startFresh($: EngineInterface): Promise<void> {
  const now = await $.clock.now()
  const ui = (await attempt(() => read($, uiAtom), null)) ?? initialUi()
  if (busyNow(ui, now) !== null) return
  if (!isArmed(ui, now)) {
    await setUi($, u => ({ ...u, freshArmedAt: now }))
    return
  }
  const running = await runningCall($)
  if (running) {
    await disarm($)
    toast($, stillRunning(running))
    return
  }
  if (await isTurnRunning($)) {
    await disarm($)
    toast($, 'Start fresh waits until the turn finishes.')
    return
  }
  const sid = cleanId(await attempt(() => $.session.id(), ''))
  if (!sid) {
    await disarm($)
    toast($, 'Start fresh stopped: no session to save. Nothing was cleared.')
    return
  }
  const gen = sessionGen
  await setUi($, u => withBusy(u, 'fresh-capture', now))
  armBusyTimeout($, 'fresh-capture', now)
  $.clock.after(0, () => void attempt(() => runFresh($, sid, now, gen), undefined))
}

/** Still the session Start fresh was pressed in: a typed /clear meanwhile means stand down. */
async function stillSession($: EngineInterface, sid: string, gen: number): Promise<boolean> {
  return gen === sessionGen && cleanId(await attempt(() => $.session.id(), '')) === sid
}

async function runFresh($: EngineInterface, sid: string, since: number, gen: number): Promise<void> {
  const io = dataIo($)
  const stop = async (reason: string): Promise<void> => {
    const ui = await attempt(() => read($, uiAtom), null)
    if (!ui || ui.busySince !== since) return
    const now = await $.clock.now()
    const line = `Start fresh stopped: ${reason}. Nothing was cleared.`
    await setUi($, u => withNote(withBusy(u, null, now), line, now))
    toast($, line)
  }

  const root = await findTokenOptimizerRoot(io, await readHome(io))
  if (!root) return stop('Token Optimizer was not found')
  const port: HandoffPort = {
    run: (args, stdin) =>
      runMeasure(io, root, args, {
        timeoutMs: args[0] === 'compact-capture' ? CAPTURE_TIMEOUT_MS : RESUME_TIMEOUT_MS,
        ...(stdin === undefined ? {} : { stdin }),
      }),
    read: path => io.read(path),
  }
  const cwd = await attempt(() => $.session.cwd(), '')
  // `now` is a placeholder: the hand-off is stamped once its save lands (below).
  const result = await prepareHandoff(port, { sessionId: sid, transcriptPath: transcriptFor(cleanId(sid)) ?? null, cwd, now: since })
  const standDown = async (): Promise<void> => {
    const now = await $.clock.now()
    await setUi($, u => (u.busySince === since ? withBusy(u, null, now) : u))
  }
  // The session changed under it (a typed /clear, a resume): clear nothing, and say so.
  const changed = async (): Promise<void> => {
    await standDown()
    toast($, 'Start fresh stopped: the session changed.')
  }
  if (!(await stillSession($, sid, gen))) return changed()
  const ui = await attempt(() => read($, uiAtom), null)
  // Timed out meanwhile: the person was told; clear nothing.
  if (!ui || ui.busy !== 'fresh-capture' || ui.busySince !== since) return
  if (!result.ok) return stop(result.reason)
  // Our compaction or clear still running: never overlap it.
  const running = await runningCall($)
  if (running) return stop(running === 'compact' ? 'the last clean-up is still running' : 'the last clear is still running')
  // A turn started during the capture: a queued clear would wipe it.
  if (await isTurnRunning($)) {
    await standDown()
    toast($, 'Start fresh waits until the turn finishes.')
    return
  }

  // Saved, then stamped from the clock right after the save lands: the
  // 10-minute window and "started since the save" both count from there.
  let handoff: Handoff
  try {
    // First kept with a stamp in the far future, so no session can count as "started
    // since the save" until the real stamp below lands.
    await $.store.set(HANDOFF_KEY, { ...result.handoff, createdAt: Number.MAX_SAFE_INTEGER, pendingSince: since })
  } catch {
    return stop('the hand-off could not be kept on disk')
  }
  try {
    handoff = { ...result.handoff, createdAt: await $.clock.now() }
    await $.store.set(HANDOFF_KEY, handoff)
  } catch {
    await dropHandoff($)
    return stop('the hand-off could not be kept on disk')
  }
  try {
    await update($, handoffAtom, () => handoff)
  } catch {
    // The new session would never see it.
    await dropHandoff($)
    return stop('the hand-off could not be kept on disk')
  }
  if (!(await stillSession($, sid, gen))) {
    await dropHandoff($)
    return changed()
  }
  const now = await $.clock.now()
  await setUi($, u => withBusy(u, 'fresh-clear', now))
  armBusyTimeout($, 'fresh-clear', now)
  $.clock.after(0, () => void attempt(() => runClear($, now, handoff, sid, gen), undefined))
}

async function runClear($: EngineInterface, since: number, handoff: Handoff, sid: string, gen: number): Promise<void> {
  const ours = (h: Handoff | null) => h !== null && h.fromSessionId === handoff.fromSessionId && h.createdAt === handoff.createdAt
  const fail = async (line: string): Promise<void> => {
    const held = await attempt(() => read($, handoffAtom), null)
    if (ours(held)) await dropHandoff($)
    const now = await $.clock.now()
    await setUi($, u => withNote(u.busy === 'fresh-clear' && u.busySince === since ? withBusy(u, null, now) : u, line, now))
    toast($, line)
  }
  // The session changed while the clear waited its turn: clear nothing.
  if (gen !== sessionGen || cleanId(await attempt(() => $.session.id(), '')) !== sid) {
    const held = await attempt(() => read($, handoffAtom), null)
    if (ours(held)) await dropHandoff($)
    const now = await $.clock.now()
    await setUi($, u => (u.busy === 'fresh-clear' && u.busySince === since ? withBusy(u, null, now) : u))
    toast($, 'Start fresh stopped: the session changed.')
    return
  }
  const running = await runningCall($)
  if (running || engineCall) return fail(`Start fresh stopped: ${(running ?? engineCall) === 'compact' ? 'the last clean-up is still running' : 'the last clear is still running'}. Nothing was cleared.`)
  // Claimed with no await between the check and the claim; recorded before the call.
  const mine = await claimCall($, 'clear')
  if (!mine) return fail('Start fresh could not clear. Your conversation is unchanged.')
  let cleared = false; const freshUsage = await attempt(() => $.session.usage(), null); const freshBefore = liveTokens(freshUsage) ?? (await attempt(() => read($, sessionAtom), null))?.contextTokens; const freshPath = transcriptFor(sid)
  try {
    await $.command.run({ command: 'clear' })
    cleared = true
  } catch {
    // Reported below, once the call has settled.
  } finally {
    await releaseCall($, mine)
  }
  if (!cleared) return fail('Start fresh could not clear. Your conversation is unchanged.'); const freshAt = await $.clock.now(); const saveFresh = async () => cleanId(await $.session.id()) !== sid && await bookModAction(dataIo($), { action: 'fresh', action_id: `${sid}-${handoff.createdAt}-fresh`, session_id: sid, timestamp: freshAt / 1000, transcript_path: freshPath, before_tokens: freshBefore, handoff_tokens_estimate: Math.ceil(handoff.text.length / 2) }); if (!await attempt(saveFresh, false)) $.clock.after(STATUS_AFTER_TURN_MS, () => void attempt(async () => { if (!await saveFresh()) toast($, 'Start fresh could not be confirmed or recorded for accounting.') }, undefined))
  // The hand-off now waits for the first prompt of a conversation started since the save.
  const now = await $.clock.now()
  await setUi($, u => (u.busy === 'fresh-clear' && u.busySince === since ? withBusy(u, null, now) : u))
}

/**
 * The user's band size: a stored click first, else the env default. The store
 * is read off the render path (a store read can be slow) and kept in module
 * state; a press made meanwhile always wins (it set sizeClicked itself).
 */
function bandSize(): BandSize {
  return sizeClicked ?? sizeEnvDefault
}

/** One store read for the size, fired off the render path; a found size redraws once when it differs. */
async function readSize($: EngineInterface): Promise<void> {
  if (sizeRead || sizeClicked !== null) return
  try {
    const stored = await $.store.get(SIZE_KEY)
    sizeRead = true
    const found = storedSize(stored)
    if (sizeClicked === null) {
      sizeClicked = found
      // A stored size that differs from what was drawn earns one redraw.
      if (found !== null && found !== sizeEnvDefault) await attempt(() => update($, frameAtom, n => (n ?? 0) + 1), undefined)
    }
  } catch {
    // Store unreadable for now: the env default serves; a later readSize call retries.
  }
}

/** The size button: flips the band and remembers the choice. A refused write just does not persist. */
async function toggleSize($: EngineInterface): Promise<void> {
  await disarm($)
  const next: BandSize = bandSize() === 'slim' ? 'full' : 'slim'
  sizeClicked = next
  await attempt(() => $.store.set(SIZE_KEY, next), undefined)
  await attempt(() => update($, frameAtom, n => (n ?? 0) + 1), undefined)
}

async function toggleDetails($: EngineInterface): Promise<void> {
  await disarm($)
  await flushTools($)
  const current = await attempt(() => read($, sessionAtom), null)
  if (!current) return
  const opening = !current.sheetOpen
  await update($, sessionAtom, cur => (cur ? { ...cur, sheetOpen: !cur.sheetOpen } : cur))
  // Savings read when the row opens.
  if (opening) $.clock.after(0, () => void attempt(() => refresh($, { savings: true }), undefined))
}

async function act($: EngineInterface, id: ActionId | 'dashboard'): Promise<void> {
  // One press at a time: the claim is taken before the first await, so a second press
  // arriving while this one reads the clock sees it (an unread time counts as now).
  const prior = claim
  if (prior !== null && prior.at === Number.POSITIVE_INFINITY) return
  const mine = { at: Number.POSITIVE_INFINITY }
  claim = mine
  const now = await attempt(() => $.clock.now(), 0)
  if (prior !== null && now - prior.at < BUSY_TIMEOUT_MS) {
    if (claim === mine) claim = prior
    return
  }
  mine.at = now
  try {
    if (id === 'clean' || id === 'clean-first') await cleanUp($, id)
    else if (id === 'dashboard') await openDashboard($)
    else if (id === 'fresh') await startFresh($)
    else await keepWarm($)
  } catch {
    toast($, 'That did not work. Try again in a moment.')
  } finally {
    if (claim === mine) claim = null
  }
}

const UNDERLAY_MS = 900
let clawdTop: ClawdLayer | null = null
let clawdUnder: (ClawdLayer & { until: number }) | null = null

/**
 * The desktop shows nothing while a changed picture loads, so a bare swap
 * blinks. Keep the old pose drawn, unchanged and under its own key, beneath
 * the new one until the new one has faded in, then drop it.
 */
function clawdLayers($: EngineInterface, layer: ClawdLayer, now: number): ClawdLayer[] {
  if (clawdTop !== null && clawdTop.key !== layer.key) {
    clawdUnder = { ...clawdTop, until: now + UNDERLAY_MS }
    $.clock.after(UNDERLAY_MS + 50, () => void attempt(() => update($, frameAtom, n => (n ?? 0) + 1), undefined))
  }
  clawdTop = layer
  if (clawdUnder !== null && (clawdUnder.until <= now || clawdUnder.key === layer.key)) clawdUnder = null
  return clawdUnder !== null ? [clawdUnder, layer] : [layer]
}

/** The band's side of a finished turn: Clawd, the clock, and the refreshes after it. */
async function endTurn($: EngineInterface, reason: Extract<PoseEvent, { type: 'turn-complete' }>['reason'], agentId: string | undefined): Promise<void> {
  try {
    if (agentId !== undefined) {
      await feedPose($, { type: 'turn-complete', reason, agentId })
      return
    }
    await closeAsks($)
    await flushTools($)
    await feedClock($, { type: 'working-changed', working: false })
    autoTurnRunning = false
    await feedPose($, { type: 'turn-complete', reason })
    await feedPose($, { type: 'working-changed', working: false })
    // Quality after each turn now; savings and the clock facts a little later.
    $.clock.after(0, () => void attempt(() => refresh($), undefined))
    statusTimer?.cancel()
    statusTimer = $.clock.after(STATUS_AFTER_TURN_MS, () => void attempt(() => refresh($, { savings: true }), undefined))
  } catch {
    // The band never breaks the turn it watched.
  }
}

/** Reads the TOKEN_OPTIMIZER_STATUS_BAR switches once per environment. */
async function readSwitches($: EngineInterface): Promise<void> {
  if (switchesRead) return
  switchesRead = true
  if (switchedOff(await attempt(() => $.env.get('TOKEN_OPTIMIZER_STATUS_BAR'), undefined))) enabled = false
  if (switchedOff(await attempt(() => $.env.get('TOKEN_OPTIMIZER_STATUS_BAR_ANIMATE'), undefined))) animate = false
  if (switchedOff(await attempt(() => $.env.get('TOKEN_OPTIMIZER_STATUS_BAR_CARDS'), undefined))) hoverCards = false
  sizeEnvDefault = envSize(await attempt(() => $.env.get('TOKEN_OPTIMIZER_STATUS_BAR_SIZE'), undefined))
  timeOpts = timeOptions(await attempt(() => $.env.get('TOKEN_OPTIMIZER_STATUS_BAR_TIME_FORMAT'), undefined))
}

/**
 * Reads the switches after the band is live (a wait before that would drop a
 * turn ending meanwhile) and, when switched off, stops it: no timers, no drawing.
 */
async function applySwitches($: EngineInterface): Promise<void> {
  await readSwitches($)
  if (enabled) return
  active = false
  for (const t of timers) t.cancel()
  timers = []
  statusTimer?.cancel()
  statusTimer = null
}

// ---- hooks ----

export const register: Register = on => {
  enabled = true
  animate = true
  hoverCards = true
  // The store is re-read (the durable choice survives); the env default stays as read.
  sizeClicked = null
  sizeRead = false

  on('session.start', async ($, e, next) => {
    const result = await next(e)
    active = enabled && e.isInteractive && e.surface !== 'terminal' && e.surface !== 'vscode'
    if (active) await start($)
    return result
  })

  // A clear: no session.start follows; this start carries the new id. It also
  // carries the session's transcript path, which compact-capture wants, each time
  // it fires. This is the only
  // classic.SessionStart handler on purpose: a policy plugin seated ahead of
  // ours (Claude Code's `cc-plugin-sec-default` on managed machines) skips every
  // classic.* event for a person's plugin and Claude Code tells the user once
  // per skipped event, so each extra classic handler costs one notice there.
  // Without a path the status and checkpoint commands look the session up by id.
  on('classic.SessionStart', async ($, e, next) => {
    const result = await next(e)
    noteTranscript(e.session_id, e.transcript_path)
    if (!active || e.source !== 'clear') return result
    // Work begun in the old session drops its result from here on.
    sessionGen += 1
    autoTurnRunning = false
    toolsPending = 0
    const sid = cleanId(e.session_id)
    // The new conversation's own start, never this event's arrival; unknown
    // means nothing is attached or announced until a prompt can read it.
    const startedAt = await sessionStartedAt($)
    await feedClock($, { type: 'clear' })
    lastCold = null
    await feedPose($, { type: 'session-start' })
    // Every step, note and Start fresh arm belonged to the old session.
    await setUi($, () => initialUi())
    const held = await heldHandoff($)
    const handoff = held ? await handoffThatFits($, held, { sessionId: sid, startedAt }) : null
    // The quick reads now; the status command after the start returns (it can take seconds).
    const clearedSid = e.session_id
    const clearedPath = e.transcript_path || undefined
    await attempt(() => refresh($, { sessionId: clearedSid, reset: true, transcript: clearedPath }), undefined)
    await later($, () => refresh($, { sessionId: clearedSid, savings: true, transcript: clearedPath }))
    // The held hand-off replaces Token Optimizer's cross-session pointer.
    if (handoff && result.additionalContext) {
      return { ...result, additionalContext: stripCrossSessionPointer(result.additionalContext) ?? [] }
    }
    return result
  })

  // The hand-off joins the first prompt the person sends, once.
  on('prompt.submit', async ($, e, next) => {
    if (!active || !attachesHandoff(e.origin?.kind) || takingHandoff) return next(e)
    // One prompt at a time takes it here, even if two are submitted at once.
    takingHandoff = true
    let handoff: Handoff | null = null
    try {
      const held = await heldHandoff($)
      handoff = held ? await handoffThatFits($, held) : null
      // Taken only once it is deleted: a failed delete attaches nothing.
      if (handoff && !(await dropHandoff($))) handoff = null
    } finally {
      takingHandoff = false
    }
    if (!handoff) return next(e)
    try {
      return await next({ ...e, context: [...(e.context ?? []), handoff.text] })
    } catch (error) {
      // Not accepted: the hand-off waits for the next prompt.
      const kept = await attempt(async () => {
        await $.store.set(HANDOFF_KEY, handoff)
        return true
      }, false)
      if (kept) await attempt(() => update($, handoffAtom, () => handoff), undefined)
      else toast($, `Start fresh's hand-off could not be kept. The checkpoint is still at ${handoff.checkpointPath}.`)
      throw error
    }
  })

  on('turn.start', async ($, e, next) => {
    if (active) {
      autoTurnRunning = true
      await feedClock($, { type: 'working-changed', working: true })
      await feedPose($, { type: 'turn-start' })
      await feedPose($, { type: 'working-changed', working: true })
    }
    return next(e)
  })

  // Per-request usage lives on each step's stop chunk; turn.complete's is summed.
  on('turn.step', async function* ($, e, next) {
    const stream = next(e)
    if (!active || e.agentId !== undefined) return yield* stream
    let last: string | null = null
    for (;;) {
      const step = await stream.next()
      if (step.done) return step.value
      const chunk = step.value
      try {
        if ((chunk.kind === 'thinking' || chunk.kind === 'text') && chunk.kind !== last) {
          last = chunk.kind
          await feedPose($, { type: chunk.kind === 'thinking' ? 'thinking' : 'text' })
        } else if (chunk.kind === 'tool') {
          last = 'tool'
        } else if (chunk.kind === 'stop' && chunk.usage) {
          const at = await $.clock.now()
          await feedClock($, { type: 'request-done', at, lifetime: lifetimeFromUsage(chunk.usage), contextTokens: requestContextTokens(chunk.usage) })
        }
      } catch {
        // Watching the stream never breaks it.
      }
      yield chunk
    }
  })

  on('tool.call', async ($, e, next) => {
    if (!active) return next(e)
    const tool = String(e.tool)
    const agentId = e.agentId
    await closeAsks($)
    await feedPose($, agentId === undefined ? { type: 'tool-call', tool } : { type: 'tool-call', tool, agentId })
    // The question dialog is open from the call until it answers.
    // Counted by the band itself, so the row has it before Token Optimizer's first quality file.
    // Counted in memory: a write per call would redraw (and restart Clawd) several times a second.
    if (agentId === undefined) toolsPending += 1
    const asking = tool === 'AskUserQuestion' && agentId === undefined
    if (asking) await feedPose($, { type: 'question-open' })
    try {
      return await next(e)
    } finally {
      if (asking) await feedPose($, { type: 'question-closed' })
      await closeAsks($)
      await feedPose($, agentId === undefined ? { type: 'tool-done' } : { type: 'tool-done', agentId })
    }
  })

  on('agent.spawn', async ($, e, next) => {
    const result = await next(e)
    if (active && result.agentId) await feedPose($, { type: 'agent-spawn', agentId: result.agentId, background: e.background })
    return result
  })

  on('classic.PermissionRequest', async ($, e, next) => {
    if (active) await feedPose($, { type: 'permission-open' })
    return next(e)
  })

  on('session.compact', async ($, e, next) => {
    if (!active || e.trigger === 'precompute' || e.agentId !== undefined) return next(e)
    autoCompacting = true
    sessionGen += 1
    try {
      compactedAt = await $.clock.now()
      await feedClock($, { type: 'warm-failed' })
      await feedPose($, { type: 'compact-start' })
      const before = await attempt(async () => {
        const cur = await read($, sessionAtom)
        return Math.max(cur?.compactionsSeen ?? 0, cur?.quality?.compactions ?? 0)
      }, 0)
      const result = await next(e)
      // The band watched this one land: count it now, before Claude Code writes its
      // marker to the transcript (it does so late) and before Token Optimizer re-reads.
      if (!('skip' in result && result.skip)) {
        compactionsLanded += 1; if (engineCall === 'compact' && e.trigger === 'manual' && result.messages !== undefined) { const evidence = result as { usage?: unknown; tokensBefore?: number; tokensAfter?: number }; modCompactEvidence = { usage: evidence.usage, before: evidence.tokensBefore, after: evidence.tokensAfter } }
        await attempt(
          () =>
            update($, sessionAtom, cur =>
              cur
                ? {
                    ...cur,
                    // Counted from what was known BEFORE it ran: Token Optimizer's own +1 may already be in.
                    compactionsSeen: Math.max(cur.compactionsSeen ?? 0, before + 1),
                  }
                : cur,
            ),
          undefined,
        )
      }
      return result
    } finally {
      await feedPose($, { type: 'compact-end' })
      autoCompacting = false
      // Any compaction (button, typed /compact, automatic): Token Optimizer saved a checkpoint
      // before it, so re-read everything now and again once its after-compact hooks have written.
      $.clock.after(0, () => void attempt(() => refresh($, { savings: true }), undefined))
      $.clock.after(STATUS_AFTER_TURN_MS, () => void attempt(() => refresh($, { savings: true }), undefined))
    }
  })

  on('turn.complete', async ($, e, next) => {
    // The turn is over even when a hook beneath rejects: the band says so either way.
    try {
      return await next(e)
    } finally {
      if (active) await endTurn($, e.reason, e.agentId)
    }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    // The terminal keeps its own status line; a survey holds the band.
    if (!enabled || e.surface !== 'desktop' || e.props.hasSurvey) return next(e)
    // The off switch is read here, off the start path (a wait there would drop a turn ending meanwhile).
    if (!switchesRead) {
      await applySwitches($)
      if (!enabled) return next(e)
    }
    if (!active) {
      // A desktop band without a session.start of ours (a late enable): start now, outside the drawing.
      active = true
      $.clock.after(0, () => void attempt(() => start($), undefined))
    }
    // The stored size is read off the render path; until it lands the env default draws.
    if (!sizeRead && sizeClicked === null) $.clock.after(0, () => void attempt(() => readSize($), undefined))

    // Every read fails soft: a hiccup in one value draws the band without it, never no band.
    await attempt(() => read($, frameAtom), 0)
    const now = await attempt(() => $.clock.now(), Date.now())
    const stored = await attempt(() => read($, sessionAtom), null)
    const clock = (await attempt(() => read($, clockAtom), null)) ?? initialClock()
    const ui = (await attempt(() => read($, uiAtom), null)) ?? initialUi()
    const pose = await attempt(() => read($, poseAtom), null)
    const handoff = await attempt(() => read($, handoffAtom), null)
    const liveSid = cleanId(await attempt(() => $.session.id(), ''))
    // A stored figure of another session never shows, nor its clock: a
    // resume with no session.start reads its own figures now.
    const otherSession = stored !== null && liveSid !== '' && stored.sessionId !== liveSid
    const s = otherSession ? null : stored
    if (otherSession && resyncFor !== liveSid) {
      resyncFor = liveSid
      $.clock.after(0, () => void attempt(() => refresh($, { savings: true }), undefined))
    }
    const working = e.props.isWorking
    // The rule the first prompt applies; dropping an expired one is left to that prompt.
    const ready = handoff !== null && handoffFate(handoff, { sessionId: liveSid, cwd: await attempt(() => $.session.cwd(), ''), startedAt: await sessionStartedAt($), now }) === 'attach'
    const shownClock = otherSession ? initialClock() : clock

    const snap: Snapshot = {
      now,
      working,
      quality: s?.quality ?? null,
      contextPercent: s?.contextPercent ?? (s?.contextWindowReduced ? null : s?.quality?.fillPct ?? null),
      contextTokens: s?.contextTokens ?? null,
      contextWindow: s?.contextWindow ?? null,
      contextModelWindow: s?.contextWindowReduced ? s?.contextModelWindow ?? null : null,
      fiveHour: s?.fiveHour ?? null,
      week: s?.week ?? null,
      cache: view({ ...shownClock, working }, now, planDefault(s)),
      branch: s?.branch ?? null,
      savings: s?.savings ?? null,
      savingsHeadline: s?.savingsHeadline ?? null,
      savingsLoading: s?.savingsState === 'loading',
      busy: busyNow(ui, now) ?? (shownClock.warming ? 'warming' : null),
      note: noteNow(ui, now),
      handoffPending: ready,
      freshArmed: isArmed(ui, now),
      earlierCheckpoint: s?.earlierCheckpoint ?? null,
      checkpointEpoch: s?.checkpointEpoch ?? null,
      startedAtMs: s?.startedAtMs ?? null,
      toolCallsSeen: s?.toolCallsSeen ?? 0,
      compactionsSeen: s?.compactionsSeen ?? 0,
    }
    // Light pictures: the marks and icons follow the app's dark mode themselves (DARK_STYLE);
    // Clawd keeps his light palette, which reads on both.
    const palette = LIGHT
    const poseNow = pose?.pose ?? 'idle'
    const mood = moodOf(snap)
    // A fade only for a pose change; a steady Clawd redraws with none, or every redraw would replay it.
    const changing = clawdTop !== null && clawdTop.key !== `clawd-${poseNow}-${mood}-${animate ? 'a' : 's'}`
    const clawdSource = clawdSvg(poseNow, mood, { animate, palette, fadeIn: changing || (clawdUnder !== null && clawdUnder.until > now) })
    const clawd = clawdLayers(
      $,
      { key: `clawd-${poseNow}-${mood}-${animate ? 'a' : 's'}`, source: clawdSource, alt: /aria-label="([^"]*)"/.exec(clawdSource)?.[1] ?? 'Clawd' },
      now,
    )

    const model: Model = {
      snap,
      tones: tonesFor(palette),
      sheetOpen: s?.sheetOpen ?? false,
      hoverCards,
      savingsReason: s?.savingsReason ?? null,
      keepwarmNote: s?.keepwarmNote ?? null,
      savingsComputedAt: s?.savingsComputedAt ?? null,
      savingsRefreshFailed: s?.savingsRefreshFailed ?? false,
      canWarm: canKeepWarm({ ...shownClock, working }, now),
      clawd,
      gazes:
        // Watching or napping: pointing at the band wakes him to look at it.
        (poseNow === 'idle' || poseNow === 'sleep') && animate
          ? (['right'] as const).map(gaze => {
              const source = clawdSvg('idle', mood, { animate, palette, gaze, fadeIn: false })
              return { key: `gaze-${gaze}-${mood}`, source, alt: 'Clawd: watching your pointer', gaze }
            })
          : [],
    }
    const handlers: Handlers = {
      act: id => void act($, id),
      dashboard: () => void act($, 'dashboard'),
      details: () => void toggleDetails($),
      size: () => void toggleSize($),
    }
    const D = $.ui.resolve(e)
    const band = bandSize() === 'slim' ? drawSlim(D, model, handlers, timeOpts) : drawBand(D, model, handlers, timeOpts)
    // The band shares AbovePrompt (#263): the bands of mods later in the chain go beneath
    // ours. Nothing beneath (the engine's own empty band) draws ours alone, as before; a
    // mod beneath that throws costs its own band, never ours.
    const beneath = await attempt(() => next(e), null)
    if (beneath === null || beneath.type === 'engine') return band
    return (
      <D.Box flexDirection="column">
        {band}
        {beneath}
      </D.Box>
    )
  })
}
