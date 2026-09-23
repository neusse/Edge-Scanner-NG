import { useEffect, useMemo, useRef, useState } from 'react'
import { ColorType, createChart, LineSeries, type IChartApi, type ISeriesApi, type UTCTimestamp } from 'lightweight-charts'
import type { QuotesConfig } from '../../types'
import { api, type QuoteObservation, type QuoteSample } from '../../lib/api'
import { useLinkedSymbol, linkSymbol } from '../../stores/linkStore'
import { Empty, SymbolInput } from '../../components/primitives'
import { readChartPalette } from '../../lib/theme'
import { toSecET } from '../../lib/time'

type Line = ISeriesApi<'Line'>
type Refs = { chart: IChartApi; bid: Line; ask: Line; spread: Line }
type Point = { time: UTCTimestamp; value?: number }
const EMPTY_HISTORY: QuoteSample[] = []
const money = (v: number | null | undefined) => v == null ? '—' : `$${v.toFixed(2)}`
const bps = (v: number | null | undefined) => v == null ? '—' : `${v.toFixed(1)} bps`
const chartSecond = (ms: number) => toSecET(new Date(ms).toISOString())

function chartData(samples: QuoteSample[]) {
  const bid: Point[] = [], ask: Point[] = [], spread: Point[] = []
  const byTime = new Map<number, QuoteSample>()
  let last = 0
  for (const row of samples) {
    const time = chartSecond(row.time_ms)
    if (time <= last) continue
    if (last && time - last > 3) {
      const gap = { time: (last + 1) as UTCTimestamp }
      bid.push(gap); ask.push(gap); spread.push(gap)
    }
    const point = (value: number | null) =>
      (row.quality === 'valid' || row.quality === 'locked') && value != null
        ? { time: time as UTCTimestamp, value } : { time: time as UTCTimestamp }
    bid.push(point(row.bid)); ask.push(point(row.ask)); spread.push(point(row.spread_bps))
    byTime.set(time, row)
    last = time
  }
  return { bid, ask, spread, byTime }
}

export function QuotesWindow({ win }: { win: QuotesConfig }) {
  const symbol = useLinkedSymbol(win, win.symbol)
  const host = useRef<HTMLDivElement>(null)
  const refs = useRef<Refs | null>(null)
  const fitted = useRef<string | null>(null)
  const [response, setResponse] = useState<{ symbol: string; current: QuoteObservation | null;
    history: QuoteSample[]; error: string | null } | null>(null)
  const [hoverState, setHover] = useState<{ symbol: string; row: QuoteSample | null } | null>(null)
  const active = response?.symbol === symbol ? response : null
  const current = active?.current ?? null
  const history = active?.history ?? EMPTY_HISTORY
  const plotted = useMemo(() => chartData(history), [history])
  const error = active?.error ?? null
  const hover = hoverState?.symbol === symbol ? hoverState.row : null

  useEffect(() => {
    if (!host.current) return
    const pal = readChartPalette()
    const chart = createChart(host.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: pal.bg }, textColor: pal.text, fontSize: 10 },
      grid: { vertLines: { color: pal.grid }, horzLines: { color: pal.grid } },
      rightPriceScale: { borderColor: pal.border },
      timeScale: { borderColor: pal.border, timeVisible: true, secondsVisible: true },
    })
    const bid = chart.addSeries(LineSeries, { color: '#54c88e', lineWidth: 2, priceLineVisible: false })
    const ask = chart.addSeries(LineSeries, { color: '#ed7779', lineWidth: 2, priceLineVisible: false })
    const spread = chart.addSeries(LineSeries, { color: '#e7b35b', lineWidth: 2,
      priceLineVisible: false, priceFormat: { type: 'custom', formatter: (n: number) => `${n.toFixed(1)} bps` } }, 1)
    chart.panes()[1]?.setHeight(95)
    refs.current = { chart, bid, ask, spread }
    return () => { refs.current = null; chart.remove() }
  }, [])

  useEffect(() => {
    const r = refs.current
    if (!r) return
    r.bid.setData(plotted.bid)
    r.ask.setData(plotted.ask)
    r.spread.setData(plotted.spread)
    if (symbol && history.length && fitted.current !== symbol) {
      r.chart.timeScale().fitContent()
      fitted.current = symbol
    }
  }, [history, plotted, symbol])

  useEffect(() => {
    const chart = refs.current?.chart
    if (!chart || !symbol) return
    const onMove = (param: { time?: unknown }) => {
      const sec = param.time === undefined ? null : Number(param.time)
      const row = sec === null ? null : plotted.byTime.get(sec) ?? null
      setHover({ symbol, row })
    }
    chart.subscribeCrosshairMove(onMove)
    return () => chart.unsubscribeCrosshairMove(onMove)
  }, [plotted, symbol])

  useEffect(() => {
    if (!symbol) return
    let alive = true
    const load = async () => {
      try {
        const [now, past] = await Promise.all([api.quote(symbol), api.quoteHistory(symbol, 1800)])
        if (!alive) return
        setResponse({ symbol, current: now, history: past.samples, error: null })
      } catch (e) {
        if (alive) setResponse({ symbol, current: null, history: [],
          error: e instanceof Error ? e.message : String(e) })
      }
    }
    void load()
    const timer = window.setInterval(() => { void load() }, 2000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [symbol])

  const shown = hover ?? current
  return <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
    <div className="wf-toolbar wf-nodrag" style={{ gap: 10 }}>
      <SymbolInput value={symbol} small onCommit={s => linkSymbol(win, s)} />
      <span style={{ color: '#54c88e' }}>Bid {money(shown?.bid)}</span>
      <span style={{ color: '#ed7779' }}>Ask {money(shown?.ask)}</span>
      <span>Spread {bps(shown?.spread_bps)}</span>
      <span className="flex-spacer" />
      <span title="Each side has its own market timestamp; never use this display status as an order eligibility decision">
        {current?.quality ?? 'waiting'} · {current?.bid_age_ms == null || current?.ask_age_ms == null
          ? 'age unknown' : `${Math.max(current.bid_age_ms, current.ask_age_ms) / 1000 | 0}s old`}
      </span>
    </div>
    <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
      <div ref={host} className="chart-host" />
      {!symbol && <div style={{ position: 'absolute', inset: 0 }}><Empty title="No symbol">Pick a link color or type a symbol.</Empty></div>}
      {symbol && !history.length && <div className="chart-loading">{error ?? 'Waiting for live bid / ask observations…'}</div>}
    </div>
    <div className="wf-toolbar wf-nodrag" style={{ fontSize: 10, color: 'var(--text-faint)' }}>
      Green bid · red ask · gold spread (bps). Gaps mean no valid fresh observation. One sample per receipt second; memory only.
      {current?.delayed && <strong className="down">Delayed feed</strong>}
    </div>
  </div>
}
