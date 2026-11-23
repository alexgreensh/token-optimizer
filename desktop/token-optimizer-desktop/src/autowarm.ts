import { canKeepWarm, type ClockState } from './clock.ts'
import type { CacheView } from './contracts.ts'
import { parseUsage } from './parse.ts'

export const HEADLESS_TIMEOUT_MS = 190_000
export const PROOF_STOP = 'Automatic keep-warm stopped for this session: the warm-up did not read the cache.'
export const HEADLESS_STOP = 'Headless keep-warm stopped for this installation: a warm-up failed the cache proof.'

export function shouldAsk(clock: ClockState, view: CacheView, now: number, lastAskedAnchor: number | null): boolean {
  return view.state === 'warning' && view.measured && clock.lifetime !== null
    && clock.anchor !== lastAskedAnchor && canKeepWarm(clock, now)
}

export function liveMeters(usage: unknown) {
  const { fiveHour, week } = parseUsage(usage)
  return { five_hour_pct: fiveHour?.percentUsed, week_pct: week?.percentUsed }
}

export function decideArgs(sid: string, tokens: number, idle: number, model: string, lifetime: number,
                           meters: ReturnType<typeof liveMeters>): string[] {
  const args = ['--session', sid, '--context-tokens', String(tokens), '--idle-seconds', String(idle),
    '--model', model, '--lifetime', String(lifetime), '--json']
  if (meters.five_hour_pct !== undefined) args.push('--five-hour-pct', String(meters.five_hour_pct))
  if (meters.week_pct !== undefined) args.push('--week-pct', String(meters.week_pct))
  return ['keepwarm-decide', ...args]
}

function readObject(stdout: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(stdout)
    return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown> : null
  } catch {
    return null
  }
}

export function parseDecision(stdout: string): 'ping' | 'stop' {
  return readObject(stdout)?.action === 'ping' ? 'ping' : 'stop'
}

export function allowsHeadlessFallback(stdout: string): boolean {
  const decision = readObject(stdout)
  return decision?.action === 'stop' && decision.reason === 'off'
}

export function parseHeadlessResult(stdout: string): { fired: boolean; proof: boolean; cacheRead: number } {
  const result = readObject(stdout)
  const read = result?.cache_read
  return {
    fired: result?.fired === true,
    proof: result?.proof === true && typeof read === 'number' && Number.isSafeInteger(read) && read > 0,
    cacheRead: typeof read === 'number' ? read : 0,
  }
}

export function proofOk(usage: unknown, tokens: number): boolean {
  if (usage === null || typeof usage !== 'object' || !Number.isSafeInteger(tokens) || tokens <= 0) return false
  const { cache_read_input_tokens: read, cache_creation_input_tokens: write } = usage as Record<string, unknown>
  return typeof read === 'number' && typeof write === 'number' && Number.isSafeInteger(read)
    && Number.isSafeInteger(write) && write >= 0 && read * 100 >= tokens * 90 && write * 100 <= tokens * 5
}
