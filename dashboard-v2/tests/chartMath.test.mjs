import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { calcEMA, completedIntradayBars } from '../src/lib/chartMath.ts'

test('EMA overlay excludes a developing candle but keeps the last completed one', () => {
  const start = Date.parse('2024-01-03T14:30:00Z')
  const bars = [0, 1, 2].map((i) => ({ t: new Date(start + i * 300_000).toISOString(), c: i + 1 }))
  assert.deepEqual(completedIntradayBars(bars, 5, start + 12 * 60_000), bars.slice(0, 2))
  assert.deepEqual(completedIntradayBars(bars, 5, start + 15 * 60_000), bars)
})

test('chart EMA uses the completed-candle SMA seed', () => {
  const bars = [1, 2, 3, 4].map((c, i) => ({ t: String(i), o: c, h: c, l: c, c, v: 1 }))
  assert.deepEqual(calcEMA(bars, 3, Number), [
    { time: 2, value: 2 },
    { time: 3, value: 3 },
  ])
})

test('chart EMA waits for a full new window after an invalid close', () => {
  const bars = [1, 2, NaN, 4, 5, 6].map((c, i) => ({ t: String(i), o: c, h: c, l: c, c, v: 1 }))
  assert.deepEqual(calcEMA(bars, 3, Number), [{ time: 5, value: 5 }])
})

test('EMA9 and EMA21 chart points match the shared alert fixture', () => {
  const fixture = JSON.parse(readFileSync(new URL('../../tests/fixtures/ema_contract.json', import.meta.url), 'utf8'))
  const bars = fixture.closes.map((c, i) => ({ t: String(i), o: c, h: c, l: c, c, v: 1 }))
  for (const period of [9, 21]) {
    const points = calcEMA(bars, period, Number)
    const expected = fixture[`ema${period}`]
    assert.equal(points[0].time, expected.first_index)
    assert.equal(points[0].value, expected.first_value)
    assert.equal(points.at(-1).value, expected.last_value)
  }
})
