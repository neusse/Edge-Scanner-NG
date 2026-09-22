import { useEffect, useMemo, useRef, useState } from 'react'
import type { SnapshotRow, WatchlistConfig } from '../../types'
import { api } from '../../lib/api'
import { usePoll } from '../../lib/usePoll'
import { fmtPct, fmtPrice, fmtX, DASH } from '../../lib/format'
import { useWatchlists } from '../../stores/watchlistsStore'
import { useScreens } from '../../stores/screensStore'
import { useUniverseSelection } from '../../stores/universeSelectionStore'
import { linkSymbol } from '../../stores/linkStore'
import { VirtualTable, type Column } from '../../components/VirtualTable'
import { ColumnPicker, Empty, Field, Select, SymbolInput, SymbolActions } from '../../components/primitives'
import { WATCHLIST_DEFAULT_COLUMNS } from '../defaults'

interface Row { symbol: string; snap: SnapshotRow | null }
const Pct = ({ v }: { v: number | null | undefined }) => v == null ? <>{DASH}</> : <span className={v > 0 ? 'up' : v < 0 ? 'down' : ''}>{fmtPct(v)}</span>

const ALL_COLUMNS: { id: string; label: string }[] = [
  { id: 'symbol', label: 'Symbol' }, { id: 'price', label: 'Price' }, { id: 'chg', label: '%Chg' }, { id: 'rth', label: '%Open' },
  { id: 'rvol', label: 'RVOL' }, { id: 'vwap', label: 'vs VWAP' }, { id: 'gap', label: 'Gap' }, { id: 'hod', label: 'HOD' }, { id: 'lod', label: 'LOD' },
]

export function WatchlistWindow({ win }: { win: WatchlistConfig }) {
  const lists = useWatchlists(s => s.lists)
  const order = useWatchlists(s => s.order)
  const loaded = useWatchlists(s => s.loaded)
  const { create, setSymbols } = useWatchlists.getState()
  const updateWindow = useScreens(s => s.updateWindow)
  const universe = useUniverseSelection(s => s.data)
  const loadUniverse = useUniverseSelection(s => s.load)
  const [drag, setDrag] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  // bind to the first list (or create one) when the window has none
  useEffect(() => {
    void loadUniverse()
    if (!loaded) return
    if (win.watchlistId && lists[win.watchlistId]) return
    const id = order[0] ?? create('Watchlist')
    updateWindow(win.id, { watchlistId: id })
  }, [loaded, win.watchlistId, win.id, lists, order, create, updateWindow, loadUniverse])

  const wl = win.watchlistId ? lists[win.watchlistId] : undefined
  const symbols = useMemo(() => wl?.symbols ?? [], [wl])
  const { data } = usePoll(() => api.snapshot(symbols), 3000, symbols.length > 0, [symbols.join(',')])
  const rows = useMemo<Row[]>(() => symbols.map(s => ({ symbol: s, snap: data?.rows[s] ?? null })), [symbols, data])

  const remove = (sym: string) => wl && setSymbols(wl.id, wl.symbols.filter(s => s !== sym))
  const add = (sym: string) => {
    if (!wl) return
    if (universe?.watchlist_id === wl.id && !wl.symbols.includes(sym) && wl.symbols.length >= universe.safe_watchlist_cap) {
      setMessage(`Selected universe is limited to ${universe.safe_watchlist_cap} stocks.`); return
    }
    setSymbols(wl.id, [...wl.symbols, sym]); setMessage('')
  }
  const parseImport = (text: string) => {
    const lines = text.split(/\r?\n/).map(x => x.trim()).filter(Boolean)
    const body = lines[0]?.toLowerCase().split(',')[0] === 'symbol' ? lines.slice(1) : lines
    const cells = body.flatMap(line => line.includes(',') ? [line.split(',')[0]]
      : line.includes(';') ? [line.split(';')[0]] : line.split(/\s+/))
    return Array.from(new Set(cells.map(s => s.trim().toUpperCase()).filter(Boolean)))
  }
  const importFile = async (file: File) => {
    if (!wl) return
    const symbols = parseImport(await file.text())
    if (!symbols.length) { setMessage('No symbols found in that file.'); return }
    const answer = prompt(
      `Import ${symbols.length} symbols into "${wl.name}". Type MERGE to keep existing symbols, REPLACE to replace the list, or CANCEL to stop.`,
      'MERGE',
    )
    if (answer == null || answer.trim().toUpperCase() === 'CANCEL') return
    const mode = answer.trim().toUpperCase()
    if (mode !== 'MERGE' && mode !== 'REPLACE') {
      setMessage('Import cancelled: choose MERGE or REPLACE.'); return
    }
    const merge = mode === 'MERGE'
    const next = merge ? Array.from(new Set([...wl.symbols, ...symbols])) : symbols
    if (universe?.watchlist_id === wl.id && next.length > (universe.safe_watchlist_cap ?? 288)) {
      setMessage(`Import would exceed the selected-universe limit of ${universe.safe_watchlist_cap} stocks.`); return
    }
    setSymbols(wl.id, next); setMessage(`${next.length} symbols ${merge ? 'merged' : 'imported'}.`)
  }
  const exportFile = () => {
    if (!wl) return
    const blob = new Blob([`symbol\n${wl.symbols.join('\n')}\n`], { type: 'text/csv' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
    a.download = `${wl.name.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-|-$/g, '') || 'watchlist'}.csv`
    a.click(); URL.revokeObjectURL(a.href)
  }
  const move = (from: string, to: string) => {
    if (!wl || from === to) return
    const arr = wl.symbols.slice()
    const i = arr.indexOf(from), j = arr.indexOf(to)
    if (i < 0 || j < 0) return
    arr.splice(i, 1); arr.splice(j, 0, from)
    setSymbols(wl.id, arr)
  }

  const defs: Record<string, Column<Row>> = {
    symbol: { id: 'symbol', label: 'Symbol', width: 'minmax(84px,1fr)', cell: r => (
      <span className="row" style={{ gap: 4 }} draggable onDragStart={() => setDrag(r.symbol)} onDragOver={e => e.preventDefault()} onDrop={() => { if (drag) move(drag, r.symbol); setDrag(null) }}>
        <span className="faint" style={{ cursor: 'grab' }} title="Drag to reorder">⋮⋮</span><span className="sym">{r.symbol}</span><SymbolActions symbol={r.symbol} />
        <button className="wf-ctl close" style={{ width: 16, height: 16, fontSize: 10 }} title="Remove" onClick={e => { e.stopPropagation(); remove(r.symbol) }}>✕</button>
      </span>) },
    price: { id: 'price', label: 'Price', width: '64px', num: true, cell: r => fmtPrice(r.snap?.price), sortValue: r => r.snap?.price ?? null },
    chg: { id: 'chg', label: '%Chg', width: '62px', num: true, cell: r => <Pct v={r.snap?.chg_pct} />, sortValue: r => r.snap?.chg_pct ?? null },
    rth: { id: 'rth', label: '%Open', width: '62px', num: true, cell: r => <Pct v={r.snap?.rth_chg_pct} />, sortValue: r => r.snap?.rth_chg_pct ?? null },
    rvol: { id: 'rvol', label: 'RVOL', width: '54px', num: true, cell: r => fmtX(r.snap?.rvol), sortValue: r => r.snap?.rvol ?? null },
    vwap: { id: 'vwap', label: 'vs VWAP', width: '64px', num: true, cell: r => <Pct v={r.snap?.dist_vwap_pct} />, sortValue: r => r.snap?.dist_vwap_pct ?? null },
    gap: { id: 'gap', label: 'Gap', width: '58px', num: true, cell: r => <Pct v={r.snap?.gap_pct} />, sortValue: r => r.snap?.gap_pct ?? null },
    hod: { id: 'hod', label: 'HOD', width: '64px', num: true, cell: r => fmtPrice(r.snap?.hod), sortValue: r => r.snap?.hod ?? null },
    lod: { id: 'lod', label: 'LOD', width: '64px', num: true, cell: r => fmtPrice(r.snap?.lod), sortValue: r => r.snap?.lod ?? null },
  }
  const columns = (win.columns.length ? win.columns : WATCHLIST_DEFAULT_COLUMNS).map(c => defs[c]).filter(Boolean)

  if (!loaded) return <Empty title="Loading watchlists…" />
  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div className="wf-toolbar wf-nodrag">
        <Select value={wl?.id ?? ''} onChange={id => updateWindow(win.id, { watchlistId: id })} options={order.map(id => ({ value: id, label: lists[id]?.name ?? id }))} small />
        <SymbolInput small placeholder="+ add" clearOnCommit onCommit={add} />
        <input ref={fileRef} type="file" accept=".txt,.csv,text/plain,text/csv" style={{ display: 'none' }} onChange={e => { const f = e.target.files?.[0]; if (f) void importFile(f); e.currentTarget.value = '' }} />
        <button className="btn sm" onClick={() => fileRef.current?.click()} disabled={!wl}>Import</button>
        <button className="btn sm" onClick={exportFile} disabled={!wl || !symbols.length}>Export</button>
        <span className="flex-spacer" />
        <span className={message ? 'down' : 'faint'} style={{ fontSize: 10.5 }}>{message || `${symbols.length} symbols`}</span>
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>
        <VirtualTable<Row> rows={rows} columns={columns} rowKey={r => r.symbol} onRowClick={r => linkSymbol(win, r.symbol, null)}
          emptyText={<span>Empty list. Type a symbol above and press Enter.</span>}
          colWidths={win.colWidths} onColWidths={colWidths => updateWindow(win.id, { colWidths })} />
      </div>
    </div>
  )
}

export function WatchlistSettings({ win, onChange }: { win: WatchlistConfig; onChange(p: Partial<WatchlistConfig>): void }) {
  const lists = useWatchlists(s => s.lists)
  const order = useWatchlists(s => s.order)
  const { create, update, remove } = useWatchlists.getState()
  const universe = useUniverseSelection(s => s.data)
  const busy = useUniverseSelection(s => s.busy)
  const universeError = useUniverseSelection(s => s.error)
  const loadUniverse = useUniverseSelection(s => s.load)
  const selectUniverse = useUniverseSelection(s => s.select)
  const wl = win.watchlistId ? lists[win.watchlistId] : undefined
  const [name, setName] = useState(wl?.name ?? '')
  const [description, setDescription] = useState(wl?.description ?? '')
  const [prevName, setPrevName] = useState(wl?.name)
  const [prevDescription, setPrevDescription] = useState(wl?.description)
  useEffect(() => { void loadUniverse() }, [loadUniverse])
  if (wl?.name !== prevName || wl?.description !== prevDescription) {
    setPrevName(wl?.name); setPrevDescription(wl?.description)
    setName(wl?.name ?? ''); setDescription(wl?.description ?? '')
  }
  const active = !!wl && universe?.watchlist_id === wl.id
  const saveDetails = () => wl && update(wl.id, { name, description })
  return (
    <>
      <div className="grid2">
        <Field label="Watchlist"><Select value={wl?.id ?? ''} onChange={id => onChange({ watchlistId: id })} options={order.map(id => ({ value: id, label: lists[id]?.name ?? id }))} /></Field>
        <Field label="Name">
          <input className="input" value={name} onChange={e => setName(e.target.value)} />
        </Field>
      </div>
      <Field label="Description"><textarea className="input" value={description} onChange={e => setDescription(e.target.value)} rows={2} style={{ width: '100%', resize: 'vertical' }} /></Field>
      <button className="btn sm" disabled={!wl} onClick={saveDetails}>Save details</button>
      <div className="row">
        <button className="btn sm" onClick={() => { const n = prompt('New watchlist name', 'Watchlist'); if (n != null) onChange({ watchlistId: create(n) }) }}>+ New list</button>
        <button className="btn sm danger" disabled={!wl} onClick={() => { if (wl && confirm(`Delete "${wl.name}"?`)) { remove(wl.id); onChange({ watchlistId: null }) } }}>Delete list</button>
      </div>
      <Field label="Columns"><ColumnPicker value={win.columns.length ? win.columns : WATCHLIST_DEFAULT_COLUMNS} onChange={columns => onChange({ columns })} all={ALL_COLUMNS} /></Field>
      <div style={{ borderTop: '1px solid var(--border-soft)', paddingTop: 10, marginTop: 8 }}>
        <div className="row" style={{ marginBottom: 6 }}>
          <strong>Scanner universe</strong><span className="flex-spacer" />
          <span className="faint">{universe ? `${universe.current_total}/${universe.cap} live streams` : 'loading…'}</span>
        </div>
        {active ? <>
          <div className="up" style={{ fontSize: 11, marginBottom: 6 }}>Selected for the next scanner start{universe?.applied ? ' · currently applied' : ''}.</div>
          <button className="btn sm" disabled={busy} onClick={() => void selectUniverse(null)}>Clear selection</button>
        </> : <button className="btn sm primary" disabled={!wl || !wl.symbols.length || busy || !!(universe && wl.symbols.length > universe.safe_watchlist_cap)} onClick={() => wl && void selectUniverse(wl.id)}>Use this watchlist on next restart</button>}
        {wl && universe && wl.symbols.length > universe.safe_watchlist_cap && <div className="down" style={{ fontSize: 11, marginTop: 6 }}>{wl.symbols.length} stocks exceeds the safe maximum of {universe.safe_watchlist_cap}.</div>}
        {universeError && <div className="down" style={{ fontSize: 11, marginTop: 6 }}>{universeError}</div>}
        <div className="faint" style={{ fontSize: 11, marginTop: 6 }}>Only one watchlist is selected. The running scanner does not change until restart.</div>
      </div>
    </>
  )
}
