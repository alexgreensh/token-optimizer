// Reading the TOKEN_OPTIMIZER_STATUS_BAR env switches. Pure.
import type { FormatOptions } from './format.ts'

/** Set to 0, false, off or no, a Token Optimizer switch turns its feature off. */
export function switchedOff(value: string | undefined): boolean {
  return /^(0|false|off|no)$/i.test((value ?? '').trim())
}

/** TOKEN_OPTIMIZER_STATUS_BAR_TIME_FORMAT: 24h or 12h force a clock; anything else (or unset) follows the locale. */
export function timeOptions(value: string | undefined): FormatOptions {
  const v = (value ?? '').trim().toLowerCase()
  if (v === '24h' || v === '24') return { hourCycle: 'h23' }
  if (v === '12h' || v === '12') return { hourCycle: 'h12' }
  return {}
}
