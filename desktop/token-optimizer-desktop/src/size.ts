// The band's two sizes, and what the slim line's right end earns. Pure.
import type { IconName } from './icons.ts'
import type { Action, Run, Sentence, Tone } from './ladder.ts'

export type BandSize = 'slim' | 'full'

/** The env default: 'slim' slims, anything else (or unset) is the full band. */
export function envSize(value: string | undefined): BandSize {
  return (value ?? '').trim().toLowerCase() === 'slim' ? 'slim' : 'full'
}

/** A stored choice: only the two real sizes count; anything else is nothing stored. */
export function storedSize(value: unknown): BandSize | null {
  return value === 'slim' || value === 'full' ? value : null
}

/** The slim line's right end: the sentence's button, its text when it warns, or nothing. */
export type SlimSide =
  | { kind: 'action'; icon: IconName; tone: Tone; action: Action }
  | { kind: 'warn'; icon: IconName; tone: Tone; runs: Run[] }
  | { kind: 'none' }

export function slimSide(say: Sentence): SlimSide {
  // The sentence's own button first: it is the one thing worth a press.
  if (say.action !== null) return { kind: 'action', icon: say.icon, tone: say.tone, action: say.action }
  // No button but not calm: the sentence itself. Only a good tone is calm here.
  if (say.tone !== 'good') return { kind: 'warn', icon: say.icon, tone: say.tone, runs: say.runs }
  return { kind: 'none' }
}
