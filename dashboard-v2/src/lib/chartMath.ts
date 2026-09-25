import type { Bar } from '../types'
import { isPremarket } from './time.ts'

export interface Pt { time: number; value: number }

/** Bar timestamps mark the opening; overlays only consume closed candles. */
export function completedIntradayBars<T extends { t: string }>(bars: T[], minutes: number, now = Date.now()): T[] {
  return bars.filter(b => Date.parse(b.t) + minutes * 60_000 <= now)
}

/** Display only server-calculated study values for candles already closed on this chart. */
export function visibleStudyPoints(
  points: { t: string; value: number }[], closedBars: { t: string }[], toTime: (t: string) => number,
): Pt[] {
  const closedTimes = new Set(closedBars.map(b => toTime(b.t)))
  return points.map(p => ({ time: toTime(p.t), value: p.value })).filter(p => closedTimes.has(p.time))
}

/** Premarket high/low from today's intraday bars (04:00-09:29 ET). */
export function pmHighLow(bars: Bar[]): { high: number; low: number } | null {
  let hi = -Infinity, lo = Infinity
  for (const b of bars) if (isPremarket(b.t)) { if (b.h > hi) hi = b.h; if (b.l < lo) lo = b.l }
  return isFinite(hi) && isFinite(lo) ? { high: hi, low: lo } : null
}

/** Prior completed session's high/low/close from daily bars. */
export function priorDayHL(daily: Bar[], todayIso: string): { high: number; low: number; close: number } | null {
  const prior = daily.filter(b => b.t.slice(0, 10) < todayIso)
  const b = prior[prior.length - 1]
  return b ? { high: b.h, low: b.l, close: b.c } : null
}
