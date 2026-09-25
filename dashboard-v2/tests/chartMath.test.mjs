import assert from 'node:assert/strict'
import test from 'node:test'

import { completedIntradayBars, visibleStudyPoints } from '../src/lib/chartMath.ts'

test('EMA overlay excludes a developing candle but keeps the last completed one', () => {
  const start = Date.parse('2024-01-03T14:30:00Z')
  const bars = [0, 1, 2].map((i) => ({ t: new Date(start + i * 300_000).toISOString(), c: i + 1 }))
  assert.deepEqual(completedIntradayBars(bars, 5, start + 12 * 60_000), bars.slice(0, 2))
  assert.deepEqual(completedIntradayBars(bars, 5, start + 15 * 60_000), bars)
})

test('chart keeps provider-calculated values only for visible completed candles', () => {
  const start = Date.parse('2024-01-03T14:30:00Z')
  const bars = [0, 1, 2].map(i => ({ t: new Date(start + i * 300_000).toISOString() }))
  const points = bars.map((b, i) => ({ t: b.t, value: 101.25 + i * 0.5 }))
  const closed = completedIntradayBars(bars, 5, start + 12 * 60_000)
  assert.deepEqual(visibleStudyPoints(points, closed, Date.parse), [
    { time: start, value: 101.25 },
    { time: start + 300_000, value: 101.75 },
  ])
})
