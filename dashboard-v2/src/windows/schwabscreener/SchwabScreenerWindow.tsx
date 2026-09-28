import { useMemo, useState, type ReactNode } from 'react'
import type { SchwabScreenerConfig, SchwabScreenerRow } from '../../types'
import { api } from '../../lib/api'
import { usePoll } from '../../lib/usePoll'
import { fmtPct, fmtPrice, fmtVol, DASH } from '../../lib/format'
import { fmtTimeET } from '../../lib/time'
import { linkSymbol } from '../../stores/linkStore'
import { useScreens } from '../../stores/screensStore'
import { Field, Select, SymbolActions } from '../../components/primitives'
import { VirtualTable, type Column } from '../../components/VirtualTable'

function ageLabel(ms: number | null): string {
  if (ms == null) return 'no snapshot yet'
  if (ms < 1000) return 'received now'
  if (ms < 60_000) return `received ${Math.floor(ms / 1000)}s ago`
  return `received ${Math.floor(ms / 60_000)}m ago`
}

function shortKey(key: string): string {
  return key.replace('EQUITY_ALL_', '').replace('AVERAGE_PERCENT_VOLUME', 'AVG % VOL')
    .replace('PERCENT_CHANGE_UP', '% UP').replace('PERCENT_CHANGE_DOWN', '% DOWN')
}

function promotionTitle(row: SchwabScreenerRow): string {
  const promotion = row.promotion
  if (!promotion) return ''
  const readiness = promotion.readiness
  const lines = [`Chart: ${promotion.chart}; Level One: ${promotion.level_one}; setups: ${promotion.setup_evaluation}`]
  if (promotion.protections?.length) lines.push(`Protected: ${promotion.protections.map(item => item.label).join(', ')}`)
  if (promotion.reason) lines.push(promotion.reason)
  for (const setup of readiness?.setups ?? []) {
    if (setup.status !== 'ready') lines.push(`${setup.name}: ${setup.status} — ${setup.reasons.join('; ')}`)
  }
  for (const event of (promotion.audit ?? []).slice(-5)) lines.push(`${fmtTimeET(event.at)} ${event.action.replaceAll('_', ' ')}`)
  return lines.join('\n')
}

const STATUS_LABEL = {
  live: 'Live', waiting: 'Waiting', disconnected: 'Disconnected', error: 'Error', unavailable: 'Unavailable',
} as const

export function SchwabScreenerWindow({ win }: { win: SchwabScreenerConfig }) {
  const viewKey = win.view === 'list' ? win.listKey : 'combined'
  const { data, error, refresh } = usePoll(() => api.schwabScreener(viewKey), 2000, true, [viewKey])
  const sessions = usePoll(api.schwabDiscoverySessions, 30_000)
  const watchlists = usePoll(api.watchlists.list, 30_000)
  const [sessionDate, setSessionDate] = useState('live')
  const retained = usePoll(() => api.schwabDiscoverySession(sessionDate), 60_000, sessionDate !== 'live', [sessionDate])
  const rows = sessionDate === 'live' ? data?.rows ?? [] : retained.data?.candidates ?? []
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [saveOpen, setSaveOpen] = useState(false)
  const [saveMode, setSaveMode] = useState<'append' | 'replace'>('append')
  const [target, setTarget] = useState('new')
  const [newId, setNewId] = useState('')
  const [name, setName] = useState('Discovery candidates')
  const [description, setDescription] = useState('Candidates captured from Schwab discovery for later review.')
  const [saveStatus, setSaveStatus] = useState<string | null>(null)
  const [admitStatus, setAdmitStatus] = useState<string | null>(null)
  const setView = (value: string) => useScreens.getState().updateWindow(win.id,
    value === 'combined' ? { view: 'combined' } : { view: 'list', listKey: value })
  const toggle = (symbol: string) => setSelected(prev => {
    const next = new Set(prev); if (next.has(symbol)) next.delete(symbol); else next.add(symbol); return next
  })
  const admit = async (symbol: string) => {
    try {
      const result = await api.admitSchwabCandidate(symbol)
      setAdmitStatus(`${symbol}: ${result.state}. Chart and Level One acknowledgement are required before warmup.`)
      await refresh()
    } catch (e) { setAdmitStatus(e instanceof Error ? e.message : String(e)) }
  }
  const protect = async (symbol: string, reason: 'manual' | 'operator_hold', enabled: boolean) => {
    try {
      const result = await api.protectSchwabCandidate(symbol, reason, enabled)
      setAdmitStatus(`${symbol}: ${enabled ? 'added' : 'removed'} ${reason === 'manual' ? 'manual pin' : 'operator hold'}.`)
      if (result.protected && !enabled) setAdmitStatus(`${symbol}: that protection was removed; other protections remain.`)
      await refresh()
    } catch (e) { setAdmitStatus(e instanceof Error ? e.message : String(e)) }
  }
  const release = async (symbol: string) => {
    try {
      const result = await api.releaseSchwabCandidate(symbol)
      setAdmitStatus(`${symbol}: ${result.state}. Setup evaluation is disabled while Chart and Level One removal are acknowledged.`)
      await refresh()
    } catch (e) { setAdmitStatus(e instanceof Error ? e.message : String(e)) }
  }
  const cols = useMemo<Column<SchwabScreenerRow>[]>(() => [
    { id: 'select', label: '', width: '30px', cell: r => <input type="checkbox" checked={selected.has(r.symbol)} onChange={() => toggle(r.symbol)} onClick={e => e.stopPropagation()} aria-label={`Select ${r.symbol}`} /> },
    { id: 'rank', label: '#', width: '34px', num: true, cell: r => <span className="faint">{r.current_rank ?? r.rank ?? DASH}</span>, sortValue: r => r.current_rank ?? r.rank },
    { id: 'symbol', label: 'Symbol', width: 'minmax(82px,1fr)', cell: r => <span className="row" style={{ gap: 4 }}><span className="sym">{r.symbol}</span><SymbolActions symbol={r.symbol} /></span>, sortValue: r => r.symbol },
    { id: 'price', label: 'Last', width: '72px', num: true, cell: r => fmtPrice(r.price), sortValue: r => r.price },
    { id: 'change', label: 'Schwab %', width: '76px', num: true, cell: r => <span className={r.percent_change == null ? '' : r.percent_change >= 0 ? 'up' : 'down'}>{fmtPct(r.percent_change)}</span>, sortValue: r => r.percent_change },
    { id: 'volume', label: 'Schwab Vol', width: '84px', num: true, cell: r => fmtVol(r.volume ?? r.total_volume), sortValue: r => r.volume ?? r.total_volume },
    { id: 'trades', label: 'Trades', width: '68px', num: true, cell: r => r.trades == null ? DASH : r.trades.toLocaleString(), sortValue: r => r.trades },
    { id: 'lists', label: 'Lists', width: '54px', num: true, cell: r => <span title={r.contributing_lists?.map(x => x.list_key).join('\n')}>{r.contributing_lists?.length ?? 1}</span>, sortValue: r => r.contributing_lists?.length ?? 1 },
    { id: 'seen', label: 'Seen', width: '52px', num: true, cell: r => r.recurrence ?? 1, sortValue: r => r.recurrence },
    { id: 'first', label: 'First', width: '54px', cell: r => r.first_seen ? <span className="mono faint">{fmtTimeET(r.first_seen)}</span> : DASH, sortValue: r => r.first_seen },
    { id: 'last', label: 'Last', width: '54px', cell: r => r.last_seen ? <span className="mono faint">{fmtTimeET(r.last_seen)}</span> : DASH, sortValue: r => r.last_seen },
    { id: 'scope', label: 'Scope', width: '86px', cell: r => <span className={r.in_live_universe ? 'up' : 'faint'}>{r.in_live_universe ? 'Live universe' : 'Candidate'}</span>, sortValue: r => r.in_live_universe ? 1 : 0 },
    { id: 'admission', label: 'Admission', width: '260px', cell: r => sessionDate !== 'live' ? <span className="faint">Final</span>
      : r.promotion?.state && r.promotion.state !== 'candidate' ? <span className="row" style={{ gap: 4 }}><span className={`badge ${r.promotion.state === 'ready' ? 'hod' : ['failed', 'release_failed'].includes(r.promotion.state) ? 'short' : 'muted'}`} title={promotionTitle(r)}>{r.promotion.state}{r.promotion.state === 'ready' && r.promotion.readiness ? ` · ${r.promotion.readiness.ready_setup_ids.length}/${r.promotion.readiness.setups.length}` : ''}</span>{['ready', 'release_failed'].includes(r.promotion.state) && <><button className={`btn sm ${r.promotion.manual_pin ? 'primary' : ''}`} onClick={e => { e.stopPropagation(); void protect(r.symbol, 'manual', !r.promotion?.manual_pin) }}>{r.promotion.manual_pin ? 'Unpin' : 'Pin'}</button><button className={`btn sm ${r.promotion.operator_hold ? 'primary' : ''}`} onClick={e => { e.stopPropagation(); void protect(r.symbol, 'operator_hold', !r.promotion?.operator_hold) }}>{r.promotion.operator_hold ? 'Unhold' : 'Hold'}</button><button className="btn sm" title={r.promotion.protections?.length ? `Protected: ${r.promotion.protections.map(item => item.label).join(', ')}` : 'Release this dynamic lease'} disabled={Boolean(r.promotion.protections?.length)} onClick={e => { e.stopPropagation(); void release(r.symbol) }}>{r.promotion.state === 'release_failed' ? 'Retry release' : 'Release'}</button></>}{r.promotion.state === 'released' && <button className="btn sm" onClick={e => { e.stopPropagation(); void admit(r.symbol) }}>Re-admit</button>}</span>
      : r.in_live_universe ? <span className="faint">Initial</span>
      : <button className="btn sm" onClick={e => { e.stopPropagation(); void admit(r.symbol) }}>Admit</button>, sortValue: r => r.promotion?.state ?? 'candidate' },
  ], [selected, sessionDate])
  const selectedHealth = data?.lists.find(item => item.list_key === data.list_key)
  const failed = data?.lists.filter(item => item.status === 'error') ?? []
  const empty: ReactNode = error || data?.error || selectedHealth?.error
    ? <span className="down">{error ?? data?.error ?? selectedHealth?.error}</span>
    : data?.status === 'waiting' ? 'Connected; waiting for the first Schwab screener snapshot'
    : 'No Schwab screener candidates received'
  const viewOptions = [{ value: 'combined', label: `Combined (${data?.requested_keys.length ?? 0} lists)` },
    ...(data?.requested_keys ?? []).map(key => ({ value: key, label: shortKey(key) }))]
  const activeSessionDate = sessionDate === 'live' ? data?.session?.session_date : retained.data?.session_date
  const save = async () => {
    const id = target === 'new' ? newId.trim() : target
    if (!id || !activeSessionDate || !selected.size) return
    try {
      const result = await api.saveSchwabCandidates(id, {
        session_date: activeSessionDate, symbols: [...selected], mode: saveMode,
        ...(target === 'new' ? { name, description } : {}),
      })
      setSaveOpen(false); setSelected(new Set()); setSaveStatus(`${result.watchlist.symbols.length} symbols now in ${result.watchlist.name}. Scanner universe unchanged.`)
      await watchlists.refresh()
    } catch (e) { setSaveStatus(e instanceof Error ? e.message : String(e)) }
  }
  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '4px 8px', borderBottom: '1px solid var(--border-soft)', fontSize: 11 }}>
        <span className={`badge ${sessionDate === 'live' && data?.status === 'live' ? 'hod' : ''}`}>{sessionDate === 'live' ? (data ? STATUS_LABEL[data.status] : 'Loading') : 'Final'}</span>
        <select aria-label="Discovery session" className="input sm" value={sessionDate} onChange={e => {
          setSessionDate(e.target.value); setSelected(new Set()); setSaveStatus(null)
        }} style={{ maxWidth: 170 }}>
          <option value="live">Current session</option>
          {(sessions.data ?? []).map(item => <option key={item.session_date ?? ''} value={item.session_date ?? ''}>{item.session_date} · {item.candidate_count ?? 0}</option>)}
        </select>
        <select aria-label="Discovery view" className="input sm" value={sessionDate === 'live' ? viewKey : 'combined'} disabled={sessionDate !== 'live'} onChange={e => setView(e.target.value)} style={{ maxWidth: 230 }}>
          {viewOptions.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        <span className="faint">{sessionDate === 'live' ? ageLabel(data?.receipt_age_ms ?? null) : `closed ${retained.data?.closed_at ? fmtTimeET(retained.data.closed_at) : ''}`}</span>
        {failed.length > 0 && <span className="badge short" title={failed.map(item => `${item.list_key}: ${item.error}`).join('\n')}>{failed.length} list error{failed.length === 1 ? '' : 's'}</span>}
        {data?.capacity && <span className={data.capacity.admissions_blocked ? 'down' : 'faint'} title={`Connection epoch ${data.capacity.connection_epoch ?? 0}. Manual admission preserves configured Chart headroom.${data.capacity.chart.error ? ` Chart: ${data.capacity.chart.error}` : ''}${data.capacity.level_one.error ? ` Level One: ${data.capacity.level_one.error}` : ''}`}>Chart {data.capacity.chart.status ?? 'unknown'} {data.capacity.chart.used}/{data.capacity.chart.cap}{data.capacity.chart.acknowledged != null ? ` · ${data.capacity.chart.acknowledged} ack` : ''}{data.capacity.chart.rejected ? ` · ${data.capacity.chart.rejected} rejected` : ''}{data.capacity.chart.deficit ? ` · deficit ${data.capacity.chart.deficit}` : ` · ${data.capacity.chart.available} open`} · L1 {data.capacity.level_one.status ?? 'unknown'} {data.capacity.level_one.used}/{data.capacity.level_one.cap}{data.capacity.level_one.acknowledged != null ? ` · ${data.capacity.level_one.acknowledged} ack` : ''}{data.capacity.level_one.rejected ? ` · ${data.capacity.level_one.rejected} rejected` : ''}{data.capacity.level_one.deficit ? ` · deficit ${data.capacity.level_one.deficit}` : ''}</span>}
        <span style={{ marginLeft: 'auto' }} className="faint">Discovery · {selected.size ? `${selected.size} selected · ` : ''}{rows.length} rows</span>
        <button className="btn sm" onClick={() => setSelected(selected.size === rows.length ? new Set() : new Set(rows.map(r => r.symbol)))} disabled={!rows.length}>{selected.size === rows.length && rows.length ? 'Clear' : 'Select all'}</button>
        <button className="btn sm primary" onClick={() => setSaveOpen(true)} disabled={!selected.size || !activeSessionDate}>Save selected</button>
      </div>
      {(admitStatus || saveStatus || retained.error || sessions.error) && <div className={retained.error || sessions.error ? 'down' : 'faint'} style={{ padding: '4px 8px', fontSize: 11, borderBottom: '1px solid var(--border-soft)' }}>{retained.error ?? sessions.error ?? admitStatus ?? saveStatus}</div>}
      <div style={{ flex: 1, minHeight: 0 }}>
        <VirtualTable rows={rows} columns={cols} rowKey={r => `${sessionDate}:${data?.list_key}:${r.symbol}`}
          onRowClick={r => linkSymbol(win, r.symbol, null)} emptyText={empty}
          colWidths={win.colWidths} onColWidths={colWidths => useScreens.getState().updateWindow(win.id, { colWidths })} />
      </div>
      {saveOpen && <div className="wf-nodrag" style={{ padding: 8, borderTop: '1px solid var(--border)', background: 'var(--panel-2)' }}>
        <div className="row" style={{ alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <Field label="Watchlist"><select className="input sm" value={target} onChange={e => setTarget(e.target.value)}><option value="new">Create new</option>{(watchlists.data ?? []).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
          {target === 'new' && <><Field label="ID"><input className="input" value={newId} onChange={e => setNewId(e.target.value.replace(/[^A-Za-z0-9_-]/g, '-'))} placeholder="morning-movers" /></Field><Field label="Name"><input className="input" value={name} onChange={e => setName(e.target.value)} /></Field></>}
          {target !== 'new' && <Field label="Save mode"><Select value={saveMode} onChange={value => setSaveMode(value as 'append' | 'replace')} options={[{ value: 'append', label: 'Add to list' }, { value: 'replace', label: 'Replace list' }]} small /></Field>}
          <Field label="Description"><input className="input" value={description} disabled={target !== 'new'} onChange={e => setDescription(e.target.value)} style={{ minWidth: 260 }} /></Field>
          <span style={{ flex: 1 }} /><button className="btn sm" onClick={() => setSaveOpen(false)}>Cancel</button><button className="btn sm primary" onClick={() => void save()} disabled={target === 'new' && (!newId.trim() || !name.trim())}>Save {selected.size}</button>
        </div>
        <div className="faint" style={{ marginTop: 5, fontSize: 10.5 }}>This stores a review watchlist with session provenance. It does not subscribe symbols or select a scanner universe.</div>
      </div>}
    </div>
  )
}

export function SchwabScreenerSettings({ win, onChange }: {
  win: SchwabScreenerConfig; onChange(patch: Partial<SchwabScreenerConfig>): void
}) {
  const catalog = usePoll(api.schwabScreenerCatalog, 3_600_000)
  const state = usePoll(() => api.schwabScreener('combined'), 2000)
  const [market, setMarket] = useState('EQUITY_ALL')
  const [measure, setMeasure] = useState('PERCENT_CHANGE_UP')
  const [period, setPeriod] = useState('5')
  const [problem, setProblem] = useState<string | null>(null)
  const requested = state.data?.requested_keys ?? []
  const apply = async (keys: string[]) => {
    try {
      await api.setSchwabScreenerSubscriptions(keys)
      setProblem(null)
      await state.refresh()
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e))
    }
  }
  const key = `${market}_${measure}_${period}`
  return (
    <>
      <div className="grid2">
        <Field label="Market"><Select value={market} onChange={setMarket} options={catalog.data?.markets ?? []} /></Field>
        <Field label="Measure"><Select value={measure} onChange={setMeasure} options={catalog.data?.measures ?? []} /></Field>
        <Field label="Period"><Select value={period} onChange={setPeriod} options={(catalog.data?.periods ?? []).map(p => ({ value: String(p.value), label: p.label }))} /></Field>
        <Field label="Generated key"><span className="mono faint" style={{ fontSize: 10, overflowWrap: 'anywhere' }}>{key}</span></Field>
      </div>
      <button className="btn primary" disabled={requested.includes(key)} onClick={() => void apply([...requested, key])}>+ Add discovery list</button>
      <div style={{ marginTop: 12, marginBottom: 4, fontSize: 11, fontWeight: 700 }}>Active lists</div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {requested.map(active => {
          const health = state.data?.lists.find(item => item.list_key === active)
          return <div key={active} className="row" style={{ gap: 6 }}>
            <span className={`badge ${health?.status === 'error' ? 'short' : health?.status === 'live' ? 'hod' : 'muted'}`}>{health?.status ?? 'waiting'}</span>
            <span className="mono" style={{ fontSize: 10, flex: 1, overflowWrap: 'anywhere' }} title={health?.error ?? undefined}>{active}</span>
            <button className="btn sm" disabled={requested.length <= 1} onClick={() => {
              void apply(requested.filter(item => item !== active))
              if (win.listKey === active) onChange({ view: 'combined' })
            }}>Remove</button>
          </div>
        })}
      </div>
      <button className="btn sm" style={{ marginTop: 10 }} onClick={() => catalog.data && void apply(catalog.data.defaults)}>Restore long defaults</button>
      {problem && <div className="down" style={{ marginTop: 8, fontSize: 11 }}>{problem}</div>}
      <div className="faint" style={{ marginTop: 10, fontSize: 11 }}>This session-wide setting changes only Schwab screener discovery lists. It never changes Chart or Level One membership.</div>
    </>
  )
}
