import { Fragment, useState, type ReactNode } from 'react'
import type { StockInfo, StockInfoConfig } from '../../types'
import { api } from '../../lib/api'
import { usePoll } from '../../lib/usePoll'
import { daysUntil, fmtDate, fmtMoney, fmtNum, fmtPct, fmtPrice, fmtVol, fmtX, DASH } from '../../lib/format'
import { useLinkedSymbol, linkSymbol } from '../../stores/linkStore'
import { CompanyLogo, Empty, SymbolInput, SymbolActions } from '../../components/primitives'
import { useFundamentals } from '../../lib/useFundamentals'

const Pct = ({ v, d = 2 }: { v: number | null | undefined; d?: number }) =>
  v == null ? <>{DASH}</> : <span className={v > 0 ? 'up' : v < 0 ? 'down' : ''}>{fmtPct(v, d)}</span>

type SchwabField = [key: string, label: string, format?: 'money' | 'volume' | 'percent' | 'price']
const SCHWAB_GROUPS: [string, SchwabField[]][] = [
  ['Schwab overview', [
    ['marketCap', 'Market cap', 'money'], ['marketCapFloat', 'Float shares', 'volume'],
    ['sharesOutstanding', 'Shares outstanding', 'volume'], ['beta', 'Beta'],
    ['high52', '52-week high', 'price'], ['low52', '52-week low', 'price'],
  ]],
  ['Valuation', [
    ['peRatio', 'P/E'], ['pegRatio', 'PEG'], ['pbRatio', 'Price / book'],
    ['prRatio', 'Price / revenue'], ['pcfRatio', 'Price / cash flow'],
    ['bookValuePerShare', 'Book value / share', 'price'], ['eps', 'EPS', 'price'],
    ['epsTTM', 'EPS TTM', 'price'],
  ]],
  ['Profitability', [
    ['grossMarginTTM', 'Gross margin TTM', 'percent'], ['grossMarginMRQ', 'Gross margin MRQ', 'percent'],
    ['netProfitMarginTTM', 'Net margin TTM', 'percent'], ['netProfitMarginMRQ', 'Net margin MRQ', 'percent'],
    ['operatingMarginTTM', 'Operating margin TTM', 'percent'], ['operatingMarginMRQ', 'Operating margin MRQ', 'percent'],
    ['returnOnEquity', 'Return on equity', 'percent'], ['returnOnAssets', 'Return on assets', 'percent'],
    ['returnOnInvestment', 'Return on investment', 'percent'],
  ]],
  ['Growth', [
    ['epsChangePercentTTM', 'EPS change TTM', 'percent'], ['epsChangeYear', 'EPS change year', 'percent'],
    ['epsChange', 'EPS change', 'percent'], ['revChangeYear', 'Revenue change year', 'percent'],
    ['revChangeTTM', 'Revenue change TTM', 'percent'], ['revChangeIn', 'Revenue change', 'percent'],
  ]],
  ['Financial health', [
    ['currentRatio', 'Current ratio'], ['quickRatio', 'Quick ratio'], ['interestCoverage', 'Interest coverage'],
    ['totalDebtToCapital', 'Debt / capital', 'percent'], ['ltDebtToEquity', 'LT debt / equity', 'percent'],
    ['totalDebtToEquity', 'Total debt / equity', 'percent'], ['fundLeverageFactor', 'Leverage factor'],
  ]],
  ['Dividends', [
    ['dividendAmount', 'Dividend amount', 'price'], ['dividendYield', 'Dividend yield', 'percent'],
    ['dividendFreq', 'Dividend frequency'], ['dividendPayAmount', 'Dividend pay amount', 'price'],
    ['divGrowthRate3Year', 'Dividend growth 3y', 'percent'],
  ]],
  ['Trading statistics', [
    ['avg1DayVolume', 'Average volume 1d', 'volume'], ['avg10DaysVolume', 'Average volume 10d', 'volume'],
    ['avg3MonthVolume', 'Average volume 3m', 'volume'], ['dtnVolume', 'DTN volume', 'volume'],
    ['vol1DayAvg', 'Volume average 1d', 'volume'], ['vol10DayAvg', 'Volume average 10d', 'volume'],
    ['vol3MonthAvg', 'Volume average 3m', 'volume'], ['shortIntToFloat', 'Short interest / float', 'percent'],
    ['shortIntDayToCover', 'Short days to cover'],
  ]],
]

function fmtSchwab(value: string | number | null | undefined, format?: SchwabField[2]) {
  if (value == null || value === '') return DASH
  if (typeof value !== 'number') return String(value)
  if (format === 'money') return fmtMoney(value)
  if (format === 'volume') return fmtVol(value)
  if (format === 'percent') return fmtPct(value, 2, false)
  if (format === 'price') return fmtPrice(value)
  return fmtNum(value, 2)
}

export function StockInfoWindow({ win }: { win: StockInfoConfig }) {
  const symbol = useLinkedSymbol(win, win.symbol)
  const { data, error } = usePoll(() => api.state(symbol!), 5000, !!symbol, [symbol])
  const f = useFundamentals(symbol)
  const [more, setMore] = useState(false)

  if (!symbol) return <Empty title="No symbol">Pick a link color or type a symbol.</Empty>
  const s: StockInfo | null = data
  const row = (k: string, v: ReactNode) => <><div className="k">{k}</div><div className="v">{v}</div></>
  const earn = daysUntil(f?.next_earnings)

  return (
    <div style={{ height: '100%', overflow: 'auto' }}>
      <div className="row" style={{ padding: '6px 10px', gap: 8, borderBottom: '1px solid var(--border-soft)', position: 'sticky', top: 0, background: 'var(--panel)', zIndex: 1 }}>
        <CompanyLogo symbol={symbol} website={f?.website} large />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row" style={{ gap: 6 }}>
            <SymbolInput value={symbol} small onCommit={sym => linkSymbol(win, sym)} />
            <SymbolActions symbol={symbol} />
          </div>
          <div className="ellipsis" style={{ fontSize: 12, fontWeight: 600, marginTop: 2 }}>{f?.name ?? ''}</div>
          <div className="ellipsis faint" style={{ fontSize: 10.5 }}>{[f?.sector, f?.industry].filter(Boolean).join(' · ')}{f?.website ? ` · ${f.website.replace(/^https?:\/\//, '').replace(/\/$/, '')}` : ''}</div>
        </div>
      </div>
      {f?.summary && (
        <div className={`si-desc${more ? '' : ' clamp'}`} title={more ? 'Click to collapse' : 'Click to expand'} onClick={() => setMore(m => !m)} style={{ cursor: 'pointer' }}>{f.summary}</div>
      )}
      {error && !s && <div className="wf-error">{error}</div>}
      {s && !s.found && <Empty title={`${symbol} is not in the universe`}>Stock Info only covers scanned symbols.</Empty>}
      {s?.found && (
        <>
          <div className="kv-sec">Fundamentals</div>
          <div className="kv">
            {f?.pending && !f.ok ? row('Status', <span className="pulse dim">fetching…</span>) : null}
            {row('Sector', f?.sector ?? DASH)}
            {row('Industry', <span className="ellipsis" style={{ maxWidth: 150, display: 'inline-block' }}>{f?.industry ?? DASH}</span>)}
            {row('Market cap', fmtMoney(f?.market_cap))}
            {row('Shares out', fmtVol(f?.shares_outstanding))}
            {row('Float', fmtVol(f?.float_shares))}
            {row('Short % float', f?.short_pct_float == null ? DASH : fmtPct(f.short_pct_float * 100, 1, false))}
            {row('Short ratio', fmtNum(f?.short_ratio, 1))}
            {row('Next earnings', f?.next_earnings ? <span className={earn != null && earn <= 5 ? 'down' : ''} style={{ fontWeight: earn != null && earn <= 5 ? 700 : 400 }}>{fmtDate(f.next_earnings)}{earn != null ? ` (${earn}d)` : ''}</span> : DASH)}
            {row('Source', f?.provider === 'schwab' ? 'Schwab Instruments + Yahoo profile' : 'Yahoo Finance')}
            {f && !f.ok && !f.pending ? row('Note', <span className="faint" style={{ fontSize: 10 }}>{f.error ?? 'unavailable'}</span>) : null}
          </div>
          {f?.schwab_fundamentals && SCHWAB_GROUPS.map(([title, fields]) => {
            const visible = fields.filter(([key]) => Object.prototype.hasOwnProperty.call(f.schwab_fundamentals, key))
            if (!visible.length) return null
            return <div key={title}>
              <div className="kv-sec">{title}</div>
              <div className="kv">
                {visible.map(([key, label, format]) =>
                  <Fragment key={key}>{row(label, fmtSchwab(f.schwab_fundamentals?.[key], format))}</Fragment>
                )}
              </div>
            </div>
          })}
          <div className="kv-sec">Price</div>
          <div className="kv">
            {row('Last', <b>{fmtPrice(s.price)}</b>)}
            {row('vs close', <Pct v={s.chg_pct} />)}
            {row('vs open', <Pct v={s.rth_chg_pct} />)}
            {row('Gap', <Pct v={s.gap_pct} />)}
            {row('Day range pos', s.day_range_pos == null ? DASH : `${Math.round(s.day_range_pos * 100)}%`)}
            {row('HOD / LOD', `${fmtPrice(s.hod)} / ${fmtPrice(s.lod)}`)}
            {row('PM high / low', `${fmtPrice(s.pm_high)} / ${fmtPrice(s.pm_low)}`)}
            {row('PM volume', fmtVol(s.pm_vol))}
            {row('Session open', fmtPrice(s.session_open))}
            {row('Prior H / L / C', `${fmtPrice(s.prior_high)} / ${fmtPrice(s.prior_low)} / ${fmtPrice(s.prior_close)}`)}
            {row('Prior day', <Pct v={s.prior_day_chg_pct} />)}
          </div>
          <div className="kv-sec">Flow</div>
          <div className="kv">
            {row('RVOL', <b>{fmtX(s.rvol, 2)}</b>)}
            {row('ADV20', fmtVol(s.adv20))}
            {row('Avg $ vol 20d', fmtMoney(s.universe?.avg_dollar_vol_20d))}
            {row('vs VWAP', <Pct v={s.dist_vwap_pct} />)}
            {row('VWAP', fmtPrice(s.vwap))}
            {row('15m momentum', <Pct v={s.mom_15m_pct} />)}
            {row('VWAP crosses 30m', s.vwap_crosses_30m ?? DASH)}
          </div>
          <div className="kv-sec">Trend</div>
          <div className="kv">
            {row('EMA9 / EMA21 (5m)', `${fmtPrice(s.ema9)} / ${fmtPrice(s.ema21)}`)}
            {row('vs EMA9', <Pct v={s.dist_ema9_pct} />)}
            {row('SMA 50 / 100 / 200', `${fmtPrice(s.sma50)} / ${fmtPrice(s.sma100)} / ${fmtPrice(s.sma200)}`)}
            {row('ATR (daily)', `${fmtNum(s.atr_d1)} (${fmtNum(s.universe?.atr_pct, 1)}%)`)}
            {row('RRS d1 / sector', `${fmtNum(s.rrs_d1, 1)} / ${fmtNum(s.rrs_sector_d1, 1)}`)}
            {row('Sector ETF', s.sector_etf ?? DASH)}
            {row('Chart quality', fmtNum(s.chart_quality, 1))}
          </div>
        </>
      )}
    </div>
  )
}
