import { useCallback, useEffect, useState } from 'react'
import type { ScreenerConfig, ScreenerFilters, YahooScreenerRow } from '../../types'
import { api } from '../../lib/api'
import { usePoll } from '../../lib/usePoll'
import { fmtPct, fmtPrice, fmtVol, DASH } from '../../lib/format'
import { linkSymbol } from '../../stores/linkStore'
import { useScreens } from '../../stores/screensStore'
import { useWatchlists } from '../../stores/watchlistsStore'
import { useUniverseSelection } from '../../stores/universeSelectionStore'
import { ColumnPicker, Empty, Field, Select, Toggle, SymbolActions } from '../../components/primitives'
import { VirtualTable, type Column } from '../../components/VirtualTable'

const DEFAULT_PRESETS = [
  { id: 'day_gainers', label: 'Day gainers' }, { id: 'day_losers', label: 'Day losers' },
  { id: 'most_actives', label: 'Most active' }, { id: 'small_cap_gainers', label: 'Small-cap gainers' },
  { id: 'most_shorted_stocks', label: 'Most shorted' }, { id: 'aggressive_small_caps', label: 'Aggressive small caps' },
]

const ALL_COLUMNS = [
  { id: 'select', label: 'Select' }, { id: 'symbol', label: 'Symbol' }, { id: 'price', label: 'Price' },
  { id: 'change', label: '% change' }, { id: 'volume', label: 'Volume' }, { id: 'avgVolume', label: 'Avg volume' },
  { id: 'marketCap', label: 'Market cap' }, { id: 'exchange', label: 'Exchange' }, { id: 'range52', label: '52-week range' },
]

const sourceLabel = (win: ScreenerConfig, presets: { id: string; label: string }[]) =>
  win.mode === 'custom' ? 'Yahoo custom screen' : `Yahoo ${presets.find(p => p.id === win.preset)?.label ?? win.preset}`

export function ScreenerWindow({ win }: { win: ScreenerConfig }) {
  const updateWindow = useScreens(s => s.updateWindow)
  const lists = useWatchlists(s => s.lists)
  const order = useWatchlists(s => s.order)
  const loadedLists = useWatchlists(s => s.loaded)
  const { create, update, setSymbols, mergeSymbols } = useWatchlists.getState()
  const universe = useUniverseSelection(s => s.data)
  const loadUniverse = useUniverseSelection(s => s.load)
  const catalog = usePoll(() => api.screener.catalog(), 3_600_000, true, [])
  const presets = catalog.data?.presets ?? DEFAULT_PRESETS
  const [rows, setRows] = useState<YahooScreenerRow[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState('')
  const [saveOpen, setSaveOpen] = useState(false)
  const [saveMode, setSaveMode] = useState<'create' | 'merge' | 'replace'>('create')
  const [target, setTarget] = useState('')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  useEffect(() => { useWatchlists.getState().load(); void loadUniverse() }, [loadUniverse])

  const refresh = useCallback(async () => {
    setLoading(true); setError(null); setStatus('')
    try {
      const result = await api.screener.run(win)
      setRows(result.rows); setSelected(new Set())
      setStatus(`${result.count} shown${result.total > result.count ? ` of ${result.total}` : ''}${result.cached ? ' · cached' : ''}`)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setLoading(false) }
  }, [win])

  const symbolsToSave = selected.size ? rows.filter(r => selected.has(r.symbol)).map(r => r.symbol) : rows.map(r => r.symbol)
  const openSave = () => {
    const label = sourceLabel(win, presets)
    setName(label.replace(/^Yahoo /, ''))
    setDescription(`Captured from ${label}.`)
    setSaveMode('create'); setTarget(order[0] ?? ''); setSaveOpen(true); setError(null)
  }
  const save = () => {
    if (!symbolsToSave.length) return
    const now = new Date().toISOString()
    const meta = { description, source: 'yahoo_screener', sourceLabel: sourceLabel(win, presets), capturedAt: now }
    if (saveMode === 'create') {
      create(name, symbolsToSave, meta)
    } else {
      const wl = lists[target]
      if (!wl) { setError('Choose a watchlist.'); return }
      const next = saveMode === 'merge' ? Array.from(new Set([...wl.symbols, ...symbolsToSave])) : symbolsToSave
      if (universe?.watchlist_id === wl.id && next.length > universe.safe_watchlist_cap) {
        setError(`That is the selected scanner universe. It cannot exceed ${universe.safe_watchlist_cap} stocks.`); return
      }
      update(wl.id, meta)
      if (saveMode === 'merge') mergeSymbols(wl.id, symbolsToSave); else setSymbols(wl.id, symbolsToSave)
    }
    setSaveOpen(false); setStatus(`${symbolsToSave.length} symbols saved to watchlist`)
  }

  const toggle = (symbol: string) => setSelected(prev => {
    const next = new Set(prev); if (next.has(symbol)) next.delete(symbol); else next.add(symbol); return next
  })
  const defs: Record<string, Column<YahooScreenerRow>> = {
    select: { id: 'select', label: '', width: '30px', cell: r => <input type="checkbox" checked={selected.has(r.symbol)} onChange={() => toggle(r.symbol)} onClick={e => e.stopPropagation()} aria-label={`Select ${r.symbol}`} /> },
    symbol: { id: 'symbol', label: 'Symbol', width: 'minmax(100px,1fr)', cell: r => <span className="row" style={{ gap: 4 }}><span className="sym">{r.symbol}</span><SymbolActions symbol={r.symbol} /><span className="faint ellipsis">{r.name}</span></span>, sortValue: r => r.symbol },
    price: { id: 'price', label: 'Price', width: '68px', num: true, cell: r => fmtPrice(r.price), sortValue: r => r.price },
    change: { id: 'change', label: '%Chg', width: '64px', num: true, cell: r => <span className={(r.change_pct ?? 0) > 0 ? 'up' : (r.change_pct ?? 0) < 0 ? 'down' : ''}>{fmtPct(r.change_pct)}</span>, sortValue: r => r.change_pct },
    volume: { id: 'volume', label: 'Volume', width: '72px', num: true, cell: r => fmtVol(r.volume), sortValue: r => r.volume },
    avgVolume: { id: 'avgVolume', label: 'Avg Vol', width: '72px', num: true, cell: r => fmtVol(r.avg_volume), sortValue: r => r.avg_volume },
    marketCap: { id: 'marketCap', label: 'Mkt Cap', width: '76px', num: true, cell: r => fmtVol(r.market_cap), sortValue: r => r.market_cap },
    exchange: { id: 'exchange', label: 'Exchange', width: '72px', cell: r => r.exchange || DASH, sortValue: r => r.exchange },
    range52: { id: 'range52', label: '52W range', width: '118px', cell: r => r.fifty_two_week_low == null || r.fifty_two_week_high == null ? DASH : `${fmtPrice(r.fifty_two_week_low)}–${fmtPrice(r.fifty_two_week_high)}` },
  }
  const columns = (win.columns.length ? win.columns : ALL_COLUMNS.map(c => c.id)).map(c => defs[c]).filter(Boolean)

  return <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
    <div className="wf-toolbar wf-nodrag">
      <Select value={win.mode} onChange={mode => updateWindow(win.id, { mode })} options={[{ value: 'preset', label: 'Yahoo preset' }, { value: 'custom', label: 'Custom' }]} small />
      {win.mode === 'preset' && <Select value={win.preset} onChange={preset => updateWindow(win.id, { preset })} options={presets.map(p => ({ value: p.id, label: p.label }))} small />}
      <button className="btn sm" onClick={() => void refresh()} disabled={loading}>{loading ? 'Loading…' : 'Refresh'}</button>
      <button className="btn sm" onClick={() => setSelected(selected.size === rows.length ? new Set() : new Set(rows.map(r => r.symbol)))} disabled={!rows.length}>{selected.size === rows.length && rows.length ? 'Clear' : 'Select all'}</button>
      <span className="flex-spacer" />
      <span className="faint" style={{ fontSize: 10.5 }}>{selected.size ? `${selected.size} selected · ` : ''}{status}</span>
      <button className="btn sm primary" onClick={openSave} disabled={!rows.length}>Save to watchlist</button>
    </div>
    {error && <div className="down" style={{ padding: '5px 9px', borderBottom: '1px solid var(--border-soft)', fontSize: 11 }}>{error}</div>}
    <div style={{ flex: 1, minHeight: 0 }}>
      {rows.length ? <VirtualTable rows={rows} columns={columns} rowKey={r => r.symbol} onRowClick={r => linkSymbol(win, r.symbol, null)}
        colWidths={win.colWidths} onColWidths={colWidths => updateWindow(win.id, { colWidths })} emptyText="No candidates" />
        : !loading && !error ? <Empty title="No candidates">Adjust the screen and refresh.</Empty> : null}
    </div>
    {saveOpen && <div className="wf-nodrag" style={{ padding: 8, borderTop: '1px solid var(--border)', background: 'var(--panel-2)' }}>
      <div className="row" style={{ alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <Field label="Save"><Select value={saveMode} onChange={v => setSaveMode(v as 'create' | 'merge' | 'replace')} options={[{ value: 'create', label: 'Create new' }, { value: 'merge', label: 'Merge into' }, { value: 'replace', label: 'Replace' }]} small /></Field>
        {saveMode === 'create' ? <Field label="Name"><input className="input" value={name} onChange={e => setName(e.target.value)} /></Field>
          : <Field label="Watchlist"><Select value={target} onChange={setTarget} options={order.map(id => ({ value: id, label: lists[id]?.name ?? id }))} small /></Field>}
        <Field label="Description"><input className="input" value={description} onChange={e => setDescription(e.target.value)} style={{ minWidth: 230 }} /></Field>
        <span className="flex-spacer" /><button className="btn sm" onClick={() => setSaveOpen(false)}>Cancel</button>
        <button className="btn sm primary" onClick={save} disabled={!loadedLists || (saveMode === 'create' && !name.trim())}>Save {symbolsToSave.length}</button>
      </div>
    </div>}
  </div>
}

const numberValue = (value: string): number | null => value.trim() === '' ? null : Number(value)

export function ScreenerSettings({ win, onChange }: { win: ScreenerConfig; onChange(p: Partial<ScreenerConfig>): void }) {
  const patchFilter = (key: keyof ScreenerFilters, value: number | string | null) => onChange({ filters: { ...win.filters, [key]: value } })
  const num = (key: keyof ScreenerFilters, label: string, placeholder: string) => <Field label={label}><input className="input" type="number" value={(win.filters[key] as number | null | undefined) ?? ''} placeholder={placeholder} onChange={e => patchFilter(key, numberValue(e.target.value))} /></Field>
  return <>
    <div className="grid2">
      <Field label="Source"><Select value={win.mode} onChange={mode => onChange({ mode })} options={[{ value: 'preset', label: 'Yahoo preset' }, { value: 'custom', label: 'Custom equity screen' }]} /></Field>
      <Field label="Maximum rows"><Select value={String(win.limit)} onChange={v => onChange({ limit: Number(v) })} options={['25', '50', '100', '250'].map(v => ({ value: v, label: v }))} /></Field>
      <Field label="Sort"><Select value={win.sortField} onChange={sortField => onChange({ sortField })} options={[{ value: 'percentchange', label: '% change' }, { value: 'dayvolume', label: 'Volume' }, { value: 'avgdailyvol3m', label: 'Average volume' }, { value: 'intradaymarketcap', label: 'Market cap' }, { value: 'intradayprice', label: 'Price' }]} /></Field>
      <Field label="Order"><Select value={win.sortAsc ? 'asc' : 'desc'} onChange={v => onChange({ sortAsc: v === 'asc' })} options={[{ value: 'desc', label: 'Highest first' }, { value: 'asc', label: 'Lowest first' }]} /></Field>
    </div>
    {win.mode === 'custom' && <div className="grid2">
      {num('min_price', 'Minimum price', '$')}{num('max_price', 'Maximum price', '$')}
      {num('min_change_pct', 'Minimum % change', '%')}{num('max_change_pct', 'Maximum % change', '%')}
      {num('min_volume', 'Minimum volume', 'shares')}{num('min_avg_volume', 'Minimum avg volume', 'shares')}
      {num('min_market_cap', 'Minimum market cap', '$')}{num('max_market_cap', 'Maximum market cap', '$')}
    </div>}
    <Toggle checked={win.includeOtc} onChange={includeOtc => onChange({ includeOtc })} label="Include OTC equities" />
    <Field label="Columns"><ColumnPicker value={win.columns} onChange={columns => onChange({ columns })} all={ALL_COLUMNS} /></Field>
    <div className="faint" style={{ fontSize: 11 }}>Yahoo finds candidates. Saving creates a snapshot; Schwab supplies live data only after a watchlist is explicitly selected as the next scanner universe.</div>
  </>
}
