import { useMemo, useState } from 'react'
import type { CheckEvent, CheckNow, CheckRow, CheckStatus, SetupCheckConfig } from '../../types'
import { api } from '../../lib/api'
import { usePoll } from '../../lib/usePoll'
import { fmtPrice } from '../../lib/format'
import { fmtTimeET } from '../../lib/time'
import { useLinkedSymbol, linkSymbol } from '../../stores/linkStore'
import { useScreens } from '../../stores/screensStore'
import { Empty, SymbolInput } from '../../components/primitives'

/** Setup check: type a stock, see for every setup whether its alert went out in
 *  the last few minutes, or exactly what stopped it. The answer to "why did I not
 *  get an alert on X?", which the feed cannot give because a blocked alert never
 *  reaches it. The scanner records as it runs (scanner/recent_activity.py). */

const STATUS: Record<CheckStatus, { label: string; cls: string; title: string }> = {
  // Outcome colors are diagnostic, never long/short green and red.
  sent: { label: 'Sent', cls: 'sc-sent', title: 'The alert went out to the feed' },
  blocked: { label: 'Blocked', cls: 'sc-blocked', title: 'The alert pattern happened, but a universe filter or parameter stopped it' },
  waiting: { label: 'Waiting', cls: 'sc-wait', title: 'Some triggers matched; the setup is waiting for the remaining required triggers before it can send an alert' },
  repeat: { label: 'Held', cls: 'muted', title: "Held back by a don't-repeat timer: it already alerted on this stock recently" },
  suppressed: { label: 'Suppressed', cls: 'muted', title: 'Replaced by a stronger setup on the same bar' },
  quiet: { label: 'No pattern', cls: 'muted', title: 'Its alert pattern did not happen in this window' },
}

// Event timestamps are the bar's START; the Scanner window shows the close.
const closeTime = (epoch: number) => fmtTimeET(new Date((epoch + 60) * 1000).toISOString())
const dirLabel = (d: string) => (d === 'long' ? 'long' : d === 'short' ? 'short' : '')
// Older live scanner processes may still send the previous wording until restart.
const reasonLabel = (reason: string) => reason.replace(/^(\d+ of \d+) alerts within (.+)$/, '$1 triggers matched within $2')

/** One line for a row with no events: per direction, pass or the first blocker. */
const nowSummary = (now: CheckNow[]) => now.map(n => {
  const d = dirLabel(n.direction)
  const what = n.ok === true ? 'filters pass' : (n.reasons[0] ?? '') + (n.reasons.length > 1 ? ` (+${n.reasons.length - 1} more)` : '')
  return d ? `${d}: ${what}` : what
}).join(' · ')

/** The first blocker, with a count of the rest: the full list is one click away. */
function FirstReason({ reasons }: { reasons: string[] }) {
  return <>{reasonLabel(reasons[0])}{reasons.length > 1 && <span className="sc-more">+{reasons.length - 1} more</span>}</>
}

function NowLine({ n, quietRow }: { n: CheckNow; quietRow: boolean }) {
  const d = dirLabel(n.direction)
  const text = n.ok === true
    ? (quietRow ? 'filters pass, waiting for the pattern' : 'filters pass now')
    : n.reasons.join('; ')
  return (
    <div className="sc-now">
      <span className={n.ok === true ? 'up' : n.ok === false ? 'down' : 'faint'}>{n.ok === true ? '✓' : n.ok === false ? '✗' : '·'}</span>
      {d && <span className="faint sc-dir">{d}</span>}
      <span className={n.ok === false ? 'dim' : 'faint'}>{text}</span>
    </div>
  )
}

function EventLine({ e }: { e: CheckEvent }) {
  const st = STATUS[e.outcome]
  return (
    <div className="sc-ev">
      <span className="mono faint">{closeTime(e.ts)}</span>
      <span className={`badge ${st.cls}`} title={st.title}>{st.label}</span>
      {e.direction && <span className="faint sc-dir">{dirLabel(e.direction)}</span>}
      <span className="dim">{e.reasons.length ? e.reasons.map(reasonLabel).join('; ') : (e.trigger || 'alert sent')}</span>
      {e.reasons.length > 0 && e.trigger && <span className="faint ellipsis" title={e.trigger}>{e.trigger}</span>}
    </div>
  )
}

function Row({ r, open, onToggle, lastBar }: { r: CheckRow; open: boolean; onToggle(): void; lastBar: string }) {
  const st = STATUS[r.status]
  const last = r.events[0]
  const quiet = r.status === 'quiet'
  return (
    <div className={`sc-row${open ? ' open' : ''}`}>
      <button className="sc-head" onClick={onToggle} title={quiet ? 'Show what would block it now' : 'Show every event and the state now'}>
        <span className={`badge ${st.cls}`} title={st.title}>{st.label}</span>
        <span className="badge muted sc-src">{r.source === 'CS' || r.source === 'custom' ? 'CS' : 'SYS'}</span>
        <span className="sc-name ellipsis">{r.name}</span>
        {last ? (
          <span className="faint mono sc-when">{closeTime(last.ts)}{last.direction ? ` ${dirLabel(last.direction)}` : ''}{r.events.length > 1 ? ` ×${r.events.length}` : ''}</span>
        ) : (
          <span className={`sc-when ${r.now.some(n => n.ok === true) ? 'dim' : r.now.some(n => n.ok === false) ? 'warn' : 'faint'}`}>
            {r.now.some(n => n.ok === true) ? 'filters pass, waiting for pattern' : r.now.some(n => n.ok === false) ? 'blocked now' : 'waiting'}
          </span>
        )}
      </button>
      {!open && last && last.reasons.length > 0 && <div className="sc-why dim ellipsis" title={last.reasons.map(reasonLabel).join('\n')}><FirstReason reasons={last.reasons} /></div>}
      {!open && !last && r.now.length > 0 && <div className="sc-why faint ellipsis" title={nowSummary(r.now)}>{nowSummary(r.now)}</div>}
      {open && (
        <div className="sc-body">
          {r.events.map((e, i) => <EventLine key={i} e={e} />)}
          {r.now.length > 0 && <div className="sc-sub faint">Latest bar filters{lastBar ? ` (bar ${lastBar})` : ''}: would it pass if the pattern fired now</div>}
          {r.now.map((n, i) => <NowLine key={i} n={n} quietRow={quiet} />)}
        </div>
      )}
    </div>
  )
}

export function SetupCheckWindow({ win }: { win: SetupCheckConfig }) {
  const symbol = useLinkedSymbol(win, win.symbol)
  const minutes = win.minutes || 5
  const { data, error, loading, refresh } = usePoll(() => api.setupCheck(symbol!, minutes), 15000, !!symbol, [symbol, minutes])
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [showQuiet, setShowQuiet] = useState(false)
  const toggle = (id: string) => setOpen(o => ({ ...o, [id]: !o[id] }))

  // A response for another symbol is never shown under this one (it can still be
  // in flight or retained after the symbol changes).
  const fresh = data?.symbol === symbol ? data : null
  const rows = useMemo(() => fresh?.setups ?? [], [fresh])
  const lastBar = fresh?.last_bar ? closeTime(fresh.last_bar) : ''
  const active = rows.filter(r => r.status !== 'quiet')
  const quiet = rows.filter(r => r.status === 'quiet')
  const counts = active.reduce<Record<string, number>>((m, r) => ({ ...m, [r.status]: (m[r.status] ?? 0) + 1 }), {})

  const header = (
    <div className="sc-top">
      <SymbolInput value={symbol} small onCommit={sym => linkSymbol(win, sym)} />
      <select className="input sm" value={minutes} title="How far back to look"
        onChange={e => useScreens.getState().updateWindow(win.id, { minutes: Number(e.target.value) })}>
        {[5, 10, 15].map(m => <option key={m} value={m}>last {m} min</option>)}
      </select>
      {fresh?.found && <span className="faint mono" style={{ fontSize: 11 }}>{fmtPrice(fresh.price)}{lastBar ? ` · bar ${lastBar}` : ''}</span>}
      <span className="flex-spacer" />
      <button className="btn sm" onClick={() => void refresh()} disabled={loading} title="Check again now (refreshes every 15 s)">{loading ? '…' : 'Refresh'}</button>
    </div>
  )

  if (!symbol) return <div className="sc-wrap">{header}<Empty title="No symbol">Type a stock, or pick a link color and click a symbol in another window.</Empty></div>
  if (error && !fresh) return <div className="sc-wrap">{header}<div className="wf-error">{error}</div></div>
  if (fresh && !fresh.found) return <div className="sc-wrap">{header}<Empty title={`${symbol} is not scanned`}>{fresh.message}</Empty></div>
  if (!fresh) return <div className="sc-wrap">{header}<Empty title={`Checking ${symbol}…`}>Asking the scanner what every setup did on it.</Empty></div>

  return (
    <div className="sc-wrap">
      {header}
      {error && <div className="sc-stale">Refresh failed ({error}). Showing the check from bar {lastBar || 'unknown'}.</div>}
      <div className="sc-scroll">
        <div className="sc-sec">Observed in the last {minutes} min</div>
        <div className="sc-sum">
          {(['sent', 'blocked', 'waiting', 'repeat', 'suppressed'] as CheckStatus[]).filter(s => counts[s]).map(s => (
            <span key={s} className={`badge ${STATUS[s].cls}`} title={STATUS[s].title}>{counts[s]} {STATUS[s].label.toLowerCase()}</span>
          ))}
          {!active.length && <span className="faint">No setup's alert pattern happened on {symbol} in the last {minutes} min.</span>}
        </div>
        {active.map(r => <Row key={r.id} r={r} open={!!open[r.id]} onToggle={() => toggle(r.id)} lastBar={lastBar} />)}
        {quiet.length > 0 && (
          <>
            <div className="sc-sec">Latest bar filters <span className="mono">{lastBar ? `bar ${lastBar} ET` : ''}</span></div>
            <button className="sc-quiet-toggle faint" onClick={() => setShowQuiet(v => !v)}>
              {showQuiet ? '▾' : '▸'} {quiet.length} setup{quiet.length > 1 ? 's' : ''} with no pattern yet: would each pass its filters now
            </button>
          </>
        )}
        {showQuiet && quiet.map(r => <Row key={r.id} r={r} open={!!open[r.id]} onToggle={() => toggle(r.id)} lastBar={lastBar} />)}
      </div>
    </div>
  )
}
