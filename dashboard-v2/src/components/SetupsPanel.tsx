import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { CheckResult, CustomSetup, ParamDef, SettingsPayload, SetupTrigger, TriggerDef } from '../types'
import { api } from '../lib/api'
import { id as randomId } from '../lib/ids'
import { fmtTimeET } from '../lib/time'
import { useSetups, useSystemSetups, systemTone, TONE_COLOR } from '../stores/setupsStore'
import { useUniverse } from '../stores/universeStore'
import { useProfiles } from '../stores/profilesStore'
import { useToplists } from '../stores/toplistsStore'
import { SymbolInput } from './primitives'
import { TriggerPicker } from './TriggerPicker'
import { ALL_ID, ConditionList, UniverseEditor, UniverseSelect } from './UniverseEditor'

// Built-in (system) setups come from the backend (/api/v2/setups `system`), in
// the order it gives them, and may not exist at all. Nothing about them is known
// here: names, directions, parameters and gate stats are all backend data.

// What each toplist ranks on. The metric is fixed in scanner/toplists.py; the
// universe it ranks over is not, which is the whole point of the Toplists page.
const TOPLIST_RULE: Record<string, string> = {
  rvol: 'Time-of-day relative volume, highest first. 1.0 is a normal pace for this minute of the session.',
  gainers_close: 'Percent change from the prior close, highest first.',
  losers_close: 'Percent change from the prior close, lowest first.',
  gainers_open: 'Percent change from the session open, regular hours, highest first.',
  losers_open: 'Percent change from the session open, regular hours, lowest first.',
  movers_5m: 'Percent move over the last five 1-minute bars, ranked by size so both directions appear.',
  pm_gainers: 'Percent change from the prior close using 04:00-09:29 bars, highest first. Refreshes every 60 seconds.',
  pm_losers: 'Percent change from the prior close using 04:00-09:29 bars, lowest first. Refreshes every 60 seconds.',
  pm_volume: 'Total premarket volume, 04:00-09:29, highest first. Refreshes every 60 seconds.',
  hod_lod: 'Not a ranking. A live stream of every symbol printing a new high or low of the regular session, newest first.',
}
const SCOPE_NOTE: Record<string, string> = {
  static: 'This list has no current bar behind it, so only the session-fixed half of a filter applies (price, volume, dollar volume, ATR%, float). Conditions like RVOL or distance from VWAP are ignored here.',
  none: 'The filter you picked is made only of conditions that change every bar, and this list cannot evaluate those. Nothing is being filtered. Add a session-fixed condition (price, volume, ATR%) to scope it.',
}
const GROUP_ORDER = ['Gates', 'Time windows', 'Triggers', 'Stop & throttling']
const minutesToHHMM = (m: number) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
const hhmmToMinutes = (s: string) => { const [h, m] = s.split(':').map(Number); return isFinite(h) && isFinite(m) ? h * 60 + m : NaN }
const fmtVal = (p: ParamDef, v: number) => p.type === 'time' ? minutesToHHMM(v) : p.type === 'int' ? String(v) : String(Number(v.toFixed(4)))
const fmtNum = (v: number | null | undefined, d = 2) => (v == null || !isFinite(v) ? '·' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }))
const newId = (name: string) => `cs_${name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 24) || 'setup'}_${Math.random().toString(36).slice(2, 6)}`

type Tab = 'general' | 'alerts' | 'params' | 'summary' | 'check' | 'log'
/** 'none' = no setup picked yet; the panel picks the first one in the list. */
type Sel = { kind: 'none' } | { kind: 'system'; code: string } | { kind: 'custom'; id: string } | { kind: 'new' }
  | { kind: 'universe'; id: string } | { kind: 'universe-new' } | { kind: 'toplist'; name: string }

/** The three things Config holds. Setups and toplists both point at universe
 *  filters, so the filters get their own section rather than living inside one
 *  of them. */
type Section = 'setups' | 'toplists' | 'universe'
const SECTIONS: { key: Section; label: string }[] = [
  { key: 'setups', label: 'Setups' },
  { key: 'toplists', label: 'Rankings' },
  { key: 'universe', label: 'Universe' },
]
const sectionOf = (s: Sel): Section =>
  s.kind === 'toplist' ? 'toplists'
    : (s.kind === 'universe' || s.kind === 'universe-new') ? 'universe'
      : 'setups'

/** One row of the flat setups list.
 *
 *  System setups and custom setups are one alphabetical list with no source
 *  headings: from here they are all just setups. */
type NavItem = {
  key: string
  sel: Sel
  name: string
  sub: string
  tone: string
  fired?: number
  edited?: number
  setup?: CustomSetup
}

function ParamRow({ p, value, draft, onChange, gate }: {
  p: ParamDef; value: number; draft: number | undefined; onChange(v: number | undefined): void
  gate?: { pass: number; fail: number } | null
}) {
  const cur = draft ?? value
  const dirty = draft !== undefined && draft !== value
  const isDefault = cur === p.default
  const total = gate ? gate.pass + gate.fail : 0
  const passPct = total ? Math.round((gate!.pass / total) * 100) : null
  return (
    <div className={`cfg-row${dirty ? ' dirty' : ''}${!isDefault ? ' nondefault' : ''}`}>
      <div className="cfg-lbl">
        <div className="cfg-name">{p.label}{!isDefault && <span className="badge muted" title={`default ${fmtVal(p, p.default)}`}>edited</span>}</div>
        <div className="cfg-desc">{p.desc}</div>
        {gate && total > 0 && (
          <div className="cfg-stat" title={`${gate.pass} passed / ${gate.fail} failed today`}>
            <span className="cfg-bar"><span style={{ width: `${passPct}%` }} /></span>
            <span className="mono faint">{passPct}% pass · {total.toLocaleString()} evals</span>
          </div>
        )}
      </div>
      <div className="cfg-ctl">
        {p.type === 'time' ? (
          <input className="input mono" type="time" value={minutesToHHMM(cur)} onChange={e => { const m = hhmmToMinutes(e.target.value); if (!isNaN(m)) onChange(m) }} />
        ) : (
          <input className="input mono" type="number" value={cur} step={p.step ?? (p.type === 'int' ? 1 : 0.1)} min={p.min ?? undefined} max={p.max ?? undefined}
            onChange={e => { const v = e.target.value === '' ? NaN : Number(e.target.value); if (!isNaN(v)) onChange(v) }} />
        )}
        <span className="cfg-unit">{p.unit}</span>
        <button className="btn sm ghost" title={`Reset to default ${fmtVal(p, p.default)}`} disabled={isDefault} onClick={() => onChange(p.default)}>↺ {fmtVal(p, p.default)}</button>
      </div>
    </div>
  )
}

function Card({ title, extra, children }: { title: ReactNode; extra?: ReactNode; children: ReactNode }) {
  return <div className="cfg-card"><div className="cfg-card-head">{title}{extra && <span className="faint"> · {extra}</span>}</div>{children}</div>
}

function Tabs({ tab, setTab, tabs }: { tab: Tab; setTab(t: Tab): void; tabs: { key: Tab; label: string }[] }) {
  return (
    <div className="cfg-tabs">
      {tabs.map(t => <button key={t.key} className={`cfg-tab${tab === t.key ? ' on' : ''}`} onClick={() => setTab(t.key)}>{t.label}</button>)}
    </div>
  )
}

// ── stock check ──────────────────────────────────────────────────────────────
function StockCheck({ setupId, universeOnly }: { setupId: string; universeOnly?: boolean }) {
  const [sym, setSym] = useState('')
  const [res, setRes] = useState<CheckResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const inUniverse = useUniverse(s => s.symbols)
  const run = async (s: string) => {
    setSym(s); setBusy(true); setErr(null)
    try { setRes(await api.setups.check(setupId, s)) } catch (e) { setErr(e instanceof Error ? e.message : String(e)); setRes(null) }
    finally { setBusy(false) }
  }
  return (
    <div className="cfg-cards">
      <Card title="Stock check" extra="does this symbol currently pass?">
        <div style={{ padding: 10 }} className="row">
          <SymbolInput value={sym} onCommit={run} placeholder="Symbol" autoFocus />
          <button className="btn sm primary" disabled={!sym || busy} onClick={() => run(sym)}>{busy ? 'Checking…' : 'Check'}</button>
          {sym && <span className="faint" style={{ fontSize: 11 }}>{inUniverse.includes(sym.toUpperCase()) ? 'in the universe' : 'not in the universe'}</span>}
        </div>
        <div className="faint" style={{ padding: '0 10px 10px', fontSize: 11 }}>
          Universe filter first (data/universe.csv), then {universeOnly ? 'every gate of the setup' : 'each alert of the setup'} against the live state. Read-only, nothing is emitted.
        </div>
      </Card>
      {err && <div className="wf-error">{err}</div>}
      {res && !res.in_universe && <Card title={res.symbol}><div style={{ padding: 10 }} className="down">{res.message ?? 'Not in the universe.'}</div></Card>}
      {res && res.in_universe && res.gates && (
        <Card title={`${res.symbol} · ${res.fired ? 'WOULD FIRE' : 'does not fire'}`} extra={res.as_of ? `as of ${fmtTimeET(res.as_of)} ET · price ${fmtNum(res.price)}` : undefined}>
          {res.message && <div className="dim" style={{ padding: 10 }}>{res.message}</div>}
          <div className="chk-grid">
            {res.gates.map(g => (
              <div key={g.name} className={`chk-row ${g.passed ? 'ok' : 'bad'}`}>
                <span className={`chk-dot ${g.passed ? 'ok' : 'bad'}`} />
                <span className="chk-name">{g.name.replace(/_/g, ' ')}{g.mandatory && <span className="badge muted" style={{ marginLeft: 6 }}>mandatory</span>}</span>
                <span className="mono chk-val">{fmtNum(g.value, 3)}</span>
                <span className="dim chk-reason">{g.reason}</span>
              </div>
            ))}
          </div>
          {res.notes && res.notes.length > 0 && <div className="dim" style={{ padding: '6px 10px 10px', fontSize: 11.5 }}>{res.notes.join(' · ')}</div>}
          {res.context && (
            <div className="row wrap" style={{ padding: '0 10px 10px', gap: 6 }}>
              {Object.entries(res.context).map(([k, v]) => <span key={k} className="chip static"><span className="faint">{k.replace(/_/g, ' ')}</span>&nbsp;<span className="mono">{typeof v === 'number' ? fmtNum(v, 2) : String(v)}</span></span>)}
            </div>
          )}
        </Card>
      )}
      {res && res.in_universe && res.triggers && (
        <Card title={`${res.symbol} · alerts`} extra={`price ${fmtNum(res.price)} · VWAP ${fmtNum(res.vwap)} · RVOL ${fmtNum(res.rvol, 1)} · ${res.bars_1m ?? 0} bars today`}>
          {res.message && <div className="dim" style={{ padding: 10 }}>{res.message}</div>}
          <div className="chk-grid">
            {res.triggers.map(t => (
              <div key={t.key} className={`chk-row ${t.fired_last_bar ? 'ok' : ''}`}>
                <span className={`chk-dot ${t.fired_last_bar ? 'ok' : 'idle'}`} />
                <span className="chk-name">{t.label}</span>
                <span className="mono chk-val" title="level the alert compares against">{t.level != null ? fmtNum(t.level, 2) : ''}</span>
                <span className="dim chk-reason">{t.fired_last_bar ? `fired on the last bar: ${t.note}` : t.fires_today ? `${t.fires_today} today` : 'not firing'}{t.last_eval ? ` · ${fmtTimeET(t.last_eval)}` : ''}</span>
              </div>
            ))}
            {res.triggers.length === 0 && <div className="faint" style={{ padding: 10 }}>No alerts configured.</div>}
          </div>
        </Card>
      )}
    </div>
  )
}

// ── custom setup editor ──────────────────────────────────────────────────────
function TriggerCard({ t, def, onChange, onRemove }: { t: SetupTrigger; def: TriggerDef; onChange(p: Partial<SetupTrigger>): void; onRemove(): void }) {
  const toggle = (k: string) => onChange({ options: t.options.includes(k) ? t.options.filter(x => x !== k) : [...t.options, k] })
  return (
    <div className="trig-card">
      <div className="trig-head">
        <span className={`picker-dir ${def.direction}`} />
        <span className="trig-name">{def.name}</span>
        <span className="faint" style={{ fontSize: 10.5 }} title={def.desc}>{def.category}</span>
        <span className="flex-spacer" />
        <button className="wf-ctl close" title="Remove alert" onClick={onRemove}>🗑</button>
      </div>
      <div className="trig-desc">{def.desc}</div>
      {def.options.length > 0 && (
        <div className="trig-opts">
          <span className="faint" style={{ fontSize: 10.5, minWidth: 64 }}>{def.option_label}</span>
          {def.options.map(o => (
            <label key={o.key} className={`trig-opt${t.options.includes(o.key) ? ' on' : ''}`}>
              <input type="checkbox" checked={t.options.includes(o.key)} onChange={() => toggle(o.key)} />{o.label}
            </label>
          ))}
          {t.options.length === 0 && <span className="down" style={{ fontSize: 11 }}>pick at least one</span>}
        </div>
      )}
      {(
        <div className="trig-params">
          {def.params.map(p => (
            <label key={p.key} className="trig-param" title={p.desc}>
              <span className="faint">{p.label}</span>
              {p.choices && p.choices.length ? (
                // A fixed set (candle sizes): a dropdown shows what exists and cannot
                // produce a candle the scanner does not build.
                <select className="input sm mono" value={t.params[p.key] ?? p.default}
                  onChange={e => onChange({ params: { ...t.params, [p.key]: Number(e.target.value) } })}>
                  {p.choices.map((ch, i) => <option key={ch} value={ch}>{p.choice_labels?.[i] ?? ch}</option>)}
                </select>
              ) : (
                <input className="input sm mono" type="number" value={t.params[p.key] ?? p.default} step={p.step ?? 0.1} min={p.min ?? undefined} max={p.max ?? undefined}
                  onChange={e => { const v = Number(e.target.value); if (!isNaN(v)) onChange({ params: { ...t.params, [p.key]: v } }) }} />
              )}
              <span className="faint">{p.unit}</span>
            </label>
          ))}
          <label className="trig-param" title="Do not repeat this alert for the same symbol within N seconds (0 = feed default, 5 min per alert)">
            <span className="faint">Do not repeat same symbol for</span>
            <input className="input sm mono" type="number" value={t.repeat_sec ?? 0} min={0} step={30} onChange={e => onChange({ repeat_sec: Math.max(0, Number(e.target.value) || 0) })} />
            <span className="faint">seconds</span>
          </label>
        </div>
      )}
    </div>
  )
}

function blankSetup(): CustomSetup {
  return { id: '', name: '', color: '#3b82f6', enabled: true, mode: 'or', direction: 'all', sessions: ['rth'], repeat_sec: 0,
    alert_direction: '', and_window_min: 5, min_triggers: 2, size_hint: 'half', triggers: [], parameters: [], notes: '', pending_filters: [], source: 'user' }
}

// ── panel ────────────────────────────────────────────────────────────────────
export function SetupsPanel({ onClose }: { onClose(): void }) {
  const setups = useSetups()
  const [settings, setSettings] = useState<SettingsPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const systemSetups = useSystemSetups()
  const [sel, setSel] = useState<Sel>({ kind: 'none' })
  // Universe filter being created; its id is minted when the user starts one.
  const [newUniId, setNewUniId] = useState('')
  const profiles = useProfiles()
  const toplists = useToplists()
  useEffect(() => { void profiles.load(); void toplists.load() }, [])
  const [section, setSection] = useState<Section>('setups')
  const [tab, setTab] = useState<Tab>('general')
  const [busy, setBusy] = useState(false)
  const [paramDraft, setParamDraft] = useState<Record<string, number>>({})
  const [draft, setDraft] = useState<CustomSetup | null>(null)      // custom setup being edited
  const [nameDraft, setNameDraft] = useState<string | null>(null)   // system display name being edited
  const [picker, setPicker] = useState(false)
  const [presetName, setPresetName] = useState('')
  // The universe / parameter-set editor reports unsaved edits here. A ref, not state:
  // a save that then selects the saved filter must not ask about the edit it just saved.
  const uniDirtyRef = useRef(false)
  const setUniDirty = useCallback((d: boolean) => { uniDirtyRef.current = d }, [])

  const loadSettings = useCallback(async () => {
    try { setSettings(await api.settings.get()); setError(null) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }, [])
  useEffect(() => {
    let alive = true
    api.settings.get().then(d => { if (alive) { setSettings(d); setError(null) } }).catch(e => { if (alive) setError(e instanceof Error ? e.message : String(e)) })
    void useSetups.getState().load()
    return () => { alive = false }
  }, [])
  useEffect(() => {
    const t = setInterval(() => {
      api.settings.stats().then(st => setSettings(d => (d ? { ...d, stats: st } : d))).catch(() => { /* ignore */ })
      void useSetups.getState().load()
    }, 5000)
    return () => clearInterval(t)
  }, [])

  // derived: the custom setup currently shown (draft wins)
  const current = useMemo<CustomSetup | null>(() => {
    if (sel.kind === 'custom') return draft?.id === sel.id ? draft : (setups.customById[sel.id] ?? null)
    if (sel.kind === 'new') return draft
    return null
  }, [sel, draft, setups.customById])
  const dirtyCustom = !!draft && (sel.kind === 'new' || JSON.stringify(draft) !== JSON.stringify(setups.customById[draft.id]))
  const dirtyKeys = useMemo(() => Object.keys(paramDraft).filter(k => settings && paramDraft[k] !== settings.values[k]), [paramDraft, settings])
  const dirtyName = sel.kind === 'system' && nameDraft != null && nameDraft !== (setups.systemNames[sel.code] ?? sel.code)
  /** One question for every way out of an edit: switching setup or section, Escape,
   *  the close button and a click on the backdrop. Parameter edits count too. */
  const confirmDiscard = () => !(dirtyCustom || dirtyName || uniDirtyRef.current || dirtyKeys.length > 0)
    || confirm('You have unsaved changes. Discard them?')
  const requestClose = () => { if (confirmDiscard()) onClose() }

  const select = (s: Sel) => {
    if (!confirmDiscard()) return
    setDraft(null); setNameDraft(null); setParamDraft({}); setUniDirty(false); setSel(s); setSection(sectionOf(s))
    if (s.kind === 'new') { setDraft(blankSetup()); setTab('general') }
    else if (tab === 'params' && (s.kind === 'universe' || s.kind === 'universe-new'
             || s.kind === 'toplist')) setTab('general')
    else if (tab === 'log' && s.kind !== 'system') setTab('general')
    if (s.kind === 'universe' || s.kind === 'universe-new') setTab('general')
    if (s.kind === 'universe-new') setNewUniId(randomId('up'))
  }
  const goSection = (next: Section) => {
    if (next === section) return
    if (!confirmDiscard()) return
    setDraft(null); setNameDraft(null); setParamDraft({}); setUniDirty(false); setSection(next); setTab('general')
    if (next === 'setups') setSel({ kind: 'none' })
    else if (next === 'toplists') setSel({ kind: 'toplist', name: toplists.toplists[0]?.name ?? 'rvol' })
    else setSel({ kind: 'universe', id: ALL_ID })
  }
  const edit = (patch: Partial<CustomSetup>) => setDraft(d => ({ ...(d ?? current ?? blankSetup()), ...patch }))
  const editTrigger = (i: number, patch: Partial<SetupTrigger>) => {
    const base = draft ?? current
    if (!base) return
    const triggers = base.triggers.map((t, j) => (j === i ? { ...t, ...patch } : t))
    edit({ triggers })
  }

  const saveCustom = async () => {
    const d = draft
    if (!d) return
    const name = d.name.trim()
    if (!name) { setError('Name is required'); return }
    if (!d.triggers.length) { setError('Add at least one alert'); return }
    if (d.triggers.some(t => (setups.catalogById[t.id]?.options.length ?? 0) > 0 && t.options.length === 0)) { setError('Every alert needs at least one option selected'); return }
    const id = d.id || newId(name)
    setBusy(true)
    try {
      const saved = await setups.save({ ...d, id, name })
      setDraft(null); setSel({ kind: 'custom', id: saved.id }); setError(null)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }
  const deleteCustom = async () => {
    if (sel.kind !== 'custom') return
    if (!confirm(`Delete setup "${setups.customById[sel.id]?.name ?? sel.id}"?`)) return
    setBusy(true)
    try { await setups.remove(sel.id); setDraft(null); setSel({ kind: 'none' }) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }
  const duplicate = () => {
    if (!confirmDiscard()) return
    let base: CustomSetup
    if (sel.kind === 'system') {
      const code = sel.code
      const dir = setups.systemByCode[code]?.direction
      base = { ...blankSetup(), name: `${setups.systemNames[code] ?? code} (copy)`, color: '#3b82f6',
        direction: dir === 'long' ? 'long' : dir === 'short' ? 'short' : 'all',
        triggers: [{ id: `setup:${code}`, options: [], params: {}, repeat_sec: 0 }],
        notes: `Duplicated from built-in setup ${code}. The "${setups.systemNames[code] ?? code}" alert fires exactly when the engine emits ${code}; add more alerts to extend it.` }
    } else if (current) {
      base = { ...current, id: '', name: `${current.name} (copy)`, source: 'user', createdAt: undefined, updatedAt: undefined, summary: undefined }
    } else return
    setSel({ kind: 'new' }); setDraft(base); setTab('general')
  }
  const toggleEnabled = async (s: CustomSetup) => {
    try { await setups.save({ ...s, enabled: !s.enabled }) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  const commitSystemName = async () => {
    if (sel.kind !== 'system' || nameDraft == null) return
    try { await setups.renameSystem(sel.code, nameDraft); setNameDraft(null) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  // system params
  const byKey = useMemo(() => Object.fromEntries((settings?.schema ?? []).map(p => [p.key, p])), [settings])
  // Escape closes the trigger picker first (it handles its own key), then Config, guarded.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || picker) return
      if (!(dirtyCustom || dirtyName || uniDirtyRef.current || dirtyKeys.length > 0) || confirm('You have unsaved changes. Discard them?')) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [picker, dirtyCustom, dirtyName, dirtyKeys, onClose])
  // Parameters of one system setup: its own, plus the shared ('*') ones. Which
  // setup owns each parameter is the backend's call, never decided here.
  const paramsFor = (code: string) => (settings?.schema ?? []).filter(p => (
    p.setups.includes(code) || p.setups.includes('*')
  ))
  const modifiedCount = (code: string) => paramsFor(code).filter(p => settings && settings.values[p.key] !== p.default).length
  const runSettings = async (fn: () => Promise<SettingsPayload | { ok: boolean }>) => {
    setBusy(true)
    try { const r = await fn(); if ('schema' in r) setSettings(r); else await loadSettings(); setParamDraft({}); setError(null) }
    catch (e) { setError(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }
  const saveParams = () => {
    if (!dirtyKeys.length) return
    const note = prompt('Note for the change log (optional)', '') ?? ''
    void runSettings(() => api.settings.save(Object.fromEntries(dirtyKeys.map(k => [k, paramDraft[k]])), note))
  }

  const nModified = Object.keys(settings?.modified ?? {}).length
  const sysCode = sel.kind === 'system' ? sel.code : null
  const uniSel = sel.kind === 'universe' ? profiles.byId[sel.id] : null
  const uniNew = sel.kind === 'universe-new'
  const sysStats = sysCode ? settings?.stats.setups?.[sysCode] : undefined
  const customStats = current ? setups.stats.setups[current.id] : undefined
  const toplistSel = sel.kind === 'toplist' ? (toplists.byName[sel.name] ?? null) : null

  // Every setup, system and custom, in one alphabetical list. The source only
  // survives as the dot colour.
  const setupItems = useMemo<NavItem[]>(() => {
    const out: NavItem[] = []
    for (const s of systemSetups) out.push({
      key: `system:${s.code}`, sel: { kind: 'system', code: s.code },
      name: s.name || s.code, sub: s.code, tone: TONE_COLOR[systemTone(s.direction)],
      fired: settings?.stats.setups?.[s.code]?.fired,
      edited: settings ? modifiedCount(s.code) : 0,
    })
    for (const c of setups.custom) out.push({
      key: `custom:${c.id}`, sel: { kind: 'custom', id: c.id },
      name: c.name, sub: c.id, tone: c.color, setup: c,
      fired: Object.values(setups.stats.setups[c.id] ?? {}).reduce((a, b) => a + b, 0),
    })
    return out.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }))
  }, [systemSetups, setups.custom, setups.stats, settings])

  const selEq = (a: Sel, b: Sel) => JSON.stringify(a) === JSON.stringify(b)
  // Nothing picked yet (first open, after a delete): show the first setup in the list.
  if (sel.kind === 'none' && section === 'setups' && setupItems.length) setSel(setupItems[0].sel)

  const tabs: { key: Tab; label: string }[] = section === 'toplists'
    ? [{ key: 'general', label: 'List' }]
    : (uniSel || uniNew)
    ? [{ key: 'general', label: 'Filter' }]
    : sysCode
    ? [{ key: 'general', label: 'General' }, { key: 'alerts', label: 'Alerts' }, { key: 'params', label: 'Parameters' }, { key: 'summary', label: 'Summary' }, { key: 'check', label: 'Stock check' }, { key: 'log', label: 'Change log' }]
    : [{ key: 'general', label: 'General' }, { key: 'alerts', label: 'Alerts' }, { key: 'params', label: 'Parameters' }, { key: 'summary', label: 'Summary' }, { key: 'check', label: 'Stock check' }]

  const header = (
    <div className="cfg-head">
      {sysCode ? (
        <>
          <span className="cfg-dot lg" style={{ background: TONE_COLOR[systemTone(setups.systemByCode[sysCode]?.direction)] }} />
          <input className="input cfg-name-input" value={nameDraft ?? setups.systemNames[sysCode] ?? sysCode} onChange={e => setNameDraft(e.target.value)}
            onBlur={commitSystemName} onKeyDown={e => { if (e.key === 'Enter') void commitSystemName(); if (e.key === 'Escape') setNameDraft(null) }} title="Display name (click to rename). The wire code never changes." />
          <span className="badge muted mono" title="Setup code on the feed">{sysCode}</span>
          <span className="flex-spacer" />
          {sysStats && <span className="faint mono" style={{ fontSize: 11 }} title="fired / evaluated today">{sysStats.fired} fired / {sysStats.evals.toLocaleString()} evals</span>}
          <button className="btn sm" onClick={duplicate} title="Create a custom setup that fires when this one fires, then add alerts to it">⧉ Duplicate as custom</button>
          <button className="btn sm danger" disabled={!settings || busy} onClick={() => { if (confirm(`Reset every parameter of ${sysCode} to the code defaults?`)) void runSettings(() => api.settings.reset({ setup: sysCode })) }}>Reset parameters</button>
        </>
      ) : toplistSel ? (
        <>
          <span className="cfg-dot lg" style={{ background: 'var(--link-teal, var(--accent))' }} />
          <span className="cfg-name-input" style={{ display: 'inline-flex', alignItems: 'center' }}>{toplistSel.label}</span>
          <span className="badge muted mono" title="ranking key used by the window and the API">{toplistSel.name}</span>
          <span className="flex-spacer" />
        </>
      ) : current ? (
        <>
          <input type="color" className="cfg-color" value={current.color} onChange={e => edit({ color: e.target.value })} title="Badge color" />
          <input className="input cfg-name-input" placeholder="Setup name" value={current.name} onChange={e => edit({ name: e.target.value })} autoFocus={sel.kind === 'new'} />
          <label className={`chip${current.enabled ? ' on' : ''}`} style={{ cursor: 'pointer' }} title="Disabled setups are kept but never evaluated">
            <input type="checkbox" checked={current.enabled} onChange={e => edit({ enabled: e.target.checked })} style={{ marginRight: 4 }} />{current.enabled ? 'Enabled' : 'Disabled'}
          </label>
          <span className="flex-spacer" />
          {customStats && <span className="faint mono" style={{ fontSize: 11 }} title="alerts today per trigger">{Object.values(customStats).reduce((a, b) => a + b, 0)} fired today</span>}
          {sel.kind === 'custom' && <button className="btn sm" onClick={duplicate}>⧉ Duplicate</button>}
          {sel.kind === 'custom' && <button className="btn sm danger" onClick={deleteCustom} disabled={busy}>Delete</button>}
        </>
      ) : null}
    </div>
  )

  const body = (
    <div className="modal-back" onMouseDown={e => { if (e.target === e.currentTarget) requestClose() }}>
      <div className="modal cfg wf-nodrag" role="dialog" aria-modal="true">
        <div className="modal-head" style={{ alignItems: 'center' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="modal-title">Config</div>
            <div className="faint" style={{ fontSize: 11 }}>
              {section === 'setups'
                ? 'Every setup that can fire an alert, in one list. Rename it, tune it, choose the universe it runs on.'
                : section === 'toplists'
                  ? 'The six ranked lists. The metric is fixed in the engine; what you set here is the universe each one ranks over and how many rows a new window opens with.'
                  : 'Named symbol screens. A setup or a ranking points at one, and it can only ever remove rows, never add them.'}
              {' Saves apply on the next bar, no restart.'}
              {settings && <> Config hash <span className="mono">{settings.hash}</span>.</>}
              {!setups.live && setups.loaded && <span className="down"> Custom evaluator is not attached to this scanner process (restart it).</span>}
            </div>
          </div>
          {settings && sysCode && (
            <div className="row" style={{ gap: 6 }}>
              <select className="input sm" value="" onChange={e => { const n = e.target.value; if (n && confirm(`Apply preset "${n}"? This replaces all current parameter values.`)) void runSettings(() => api.settings.applyPreset(n)) }}>
                <option value="">Presets ({settings.presets.length})…</option>
                {settings.presets.map(p => <option key={p.name} value={p.name}>{p.name} · {p.n_modified} edited</option>)}
              </select>
              <input className="input sm" placeholder="save as preset…" value={presetName} onChange={e => setPresetName(e.target.value)} style={{ width: 130 }}
                onKeyDown={e => { if (e.key === 'Enter' && presetName.trim()) { void runSettings(() => api.settings.savePreset(presetName.trim())); setPresetName('') } }} />
              <button className="btn sm" disabled={!presetName.trim() || busy} onClick={() => { void runSettings(() => api.settings.savePreset(presetName.trim())); setPresetName('') }}>Save preset</button>
            </div>
          )}
          <button className="btn sm icon" title="Close (Esc)" onClick={requestClose}>✕</button>
        </div>

        {nModified > 0 && sysCode && (
          <div className="cfg-warn">
            <b>{nModified} parameter{nModified > 1 ? 's' : ''} differ from the defaults.</b> Every alert carries <span className="mono">config_hash {settings?.hash}</span> and <span className="mono">modified: true</span>, so alerts fired under edited settings can be told apart from the default definition.
            <button className="btn sm" style={{ marginLeft: 10 }} onClick={() => { if (confirm('Reset ALL setups to the code defaults?')) void runSettings(() => api.settings.reset({})) }}>Reset all</button>
          </div>
        )}
        {error && <div className="wf-error">{error}</div>}

        <div className="cfg-body">
          <nav className="cfg-nav">
            <div className="cfg-sections">
              {SECTIONS.map(sc => (
                <button key={sc.key} className={`cfg-section${section === sc.key ? ' on' : ''}`}
                  onClick={() => goSection(sc.key)}>{sc.label}</button>
              ))}
            </div>

            {section === 'setups' && (<>
              <button className="btn primary" style={{ width: '100%', justifyContent: 'center', margin: '6px 0' }} onClick={() => select({ kind: 'new' })}>+ Add setup</button>
              {setupItems.map(it => (
                <button key={it.key} className={`cfg-nav-item${selEq(sel, it.sel) ? ' on' : ''}${it.setup && !it.setup.enabled ? ' off' : ''}`} onClick={() => select(it.sel)}>
                  <span className="cfg-dot" style={{ background: it.tone }} />
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <div className="cfg-nav-title">{it.name}</div>
                  </span>
                  {!!it.edited && <span className="badge muted" title="edited parameters">{it.edited}</span>}
                  {!!it.fired && <span className="faint mono" style={{ fontSize: 10 }} title="fired today">{it.fired}</span>}
                  {it.setup && (
                    <span className={`cfg-switch${it.setup.enabled ? ' on' : ''}`} title={it.setup.enabled ? 'Enabled (click to disable)' : 'Disabled (click to enable)'}
                      onClick={e => { e.stopPropagation(); void toggleEnabled(it.setup!) }}><span /></span>
                  )}
                </button>
              ))}
              {sel.kind === 'new' && (
                <button className="cfg-nav-item on"><span className="cfg-dot" style={{ background: draft?.color }} /><span style={{ flex: 1 }}><div className="cfg-nav-title">{draft?.name || 'New setup'}</div><div className="cfg-nav-sub">unsaved</div></span></button>
              )}
            </>)}

            {section === 'toplists' && (<>
              {toplists.toplists.map(t => (
                <button key={t.name} className={`cfg-nav-item${sel.kind === 'toplist' && sel.name === t.name ? ' on' : ''}`} onClick={() => select({ kind: 'toplist', name: t.name })}>
                  <span className="cfg-dot" style={{ background: 'var(--link-teal, var(--accent))' }} />
                  <span style={{ flex: 1, minWidth: 0 }}>
                    <div className="cfg-nav-title">{t.label}</div>
                    <div className="cfg-nav-sub">{profiles.label(t.universe === ALL_ID ? null : t.universe)}{t.scope === 'static' && t.universe !== ALL_ID ? ' (static only)' : t.scope === 'none' ? ' (not applied)' : ''} · {t.rows} rows</div>
                  </span>
                </button>
              ))}
              {!toplists.toplists.length && <div className="faint" style={{ padding: '4px 9px', fontSize: 11 }}>{toplists.error ?? 'Loading…'}</div>}
            </>)}

            {section === 'universe' && (<>
              <div className="cfg-nav-sec">Universe filters <span className="faint">what kind of stock</span></div>
              <button className="btn primary" style={{ width: '100%', justifyContent: 'center', margin: '2px 0 6px' }} onClick={() => select({ kind: 'universe-new' })}>+ Add universe filter</button>
              {profiles.profiles.map(pr => {
                const n = profiles.members[pr.id]
                const users = Object.values(profiles.assignments).filter(v => v === pr.id).length
                  + setups.custom.filter(c => c.universe_profile === pr.id).length
                return (
                  <button key={pr.id} className={`cfg-nav-item${sel.kind === 'universe' && sel.id === pr.id ? ' on' : ''}`} onClick={() => select({ kind: 'universe', id: pr.id })}>
                    <span className="cfg-dot" style={{ background: pr.color }} />
                    <span style={{ flex: 1, minWidth: 0 }}>
                      <div className="cfg-nav-title">{pr.id === ALL_ID ? 'All symbols' : pr.name}</div>
                      <div className="cfg-nav-sub">{pr.conditions.length} condition{pr.conditions.length === 1 ? '' : 's'}{users ? ` · used by ${users}` : ''}</div>
                    </span>
                    {n != null && <span className="faint mono" style={{ fontSize: 10 }} title="symbols passing the session-fixed conditions">{n}</span>}
                  </button>
                )
              })}

            </>)}
          </nav>

          <div className="cfg-main">
            {header}
            {sel.kind !== 'none' && <Tabs tab={tab} setTab={setTab} tabs={tabs} />}
            {sel.kind === 'none' && section === 'setups' && (
              <div className="wf-empty" style={{ height: 200 }}>
                <b>{setups.loaded ? 'No setups yet' : 'Loading…'}</b>
                <span>{setups.loaded ? 'Click + Add setup to compose one from the alert catalog.' : 'Fetching the setups.'}</span>
              </div>
            )}
            {/* ── system setup tabs ── */}
            {(uniSel || uniNew) && (
              <UniverseEditor
                profile={uniSel ?? {
                  id: newUniId, name: 'New filter', desc: '',
                  color: '#8b5cf6', conditions: [], source: 'user',
                }}
                onSaved={pr => { setUniDirty(false); select({ kind: 'universe', id: pr.id }) }}
                onDeleted={() => { setUniDirty(false); select({ kind: 'none' }) }}
                onDirtyChange={setUniDirty}
              />
            )}
            {section === 'toplists' && !toplistSel && (
              <div className="cfg-cards">
                <div className="wf-empty" style={{ height: 200 }}>
                  <b>{toplists.error ? 'Rankings unavailable' : 'Loading…'}</b>
                  <span>{toplists.error
                    ? 'The scanner process was started before this page existed. Restart it after the close and the six lists appear here.'
                    : 'Fetching the six ranked lists.'}</span>
                </div>
              </div>
            )}
            {toplistSel && (
              <div className="cfg-cards">
                <Card title="Universe">
                  <UniverseSelect value={toplistSel.universe}
                    onChange={id => { void toplists.save(toplistSel.name, { universe: id === ALL_ID ? null : id }).then(() => profiles.load()).catch(() => { /* store holds the error */ }) }} />
                </Card>
                <Card title="Display">
                  <div className="cfg-row">
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="cfg-row-label">Rows</div>
                      <div className="cfg-row-desc faint">How many rows a new window of this list opens with. Windows already on a screen keep whatever you set on them.</div>
                    </div>
                    <input className="input mono" type="number" min={1} max={200} style={{ width: 90 }}
                      value={toplistSel.rows}
                      onChange={e => { const rows = Math.max(1, Math.min(200, Number(e.target.value) || 1)); void toplists.save(toplistSel.name, { rows }).catch(() => { /* store holds the error */ }) }} />
                  </div>
                </Card>
                <Card title="Ranking" extra="fixed by the engine">
                  <div className="dim" style={{ padding: 10, fontSize: 12.5 }}>
                    <p style={{ marginBottom: 6 }}>{TOPLIST_RULE[toplistSel.name] ?? toplistSel.label}</p>
                    <p>{toplistSel.scope === 'full'
                      ? 'Rows refresh every 2 seconds. The universe filter is applied before the ranking, so a narrow filter makes the list cheaper rather than dearer.'
                      : 'The universe filter is applied before the list is built, so a narrow filter makes it cheaper rather than dearer.'}</p>
                    {SCOPE_NOTE[toplistSel.scope] && (
                      <p style={{ marginTop: 6 }} className={toplistSel.scope === 'none' ? 'down' : undefined}>{SCOPE_NOTE[toplistSel.scope]}</p>
                    )}
                  </div>
                </Card>
                {toplists.error && <div className="wf-error">{toplists.error}</div>}
              </div>
            )}
            {sysCode && tab === 'general' && (
              <div className="cfg-cards">
                <Card title="About this setup">
                  <div style={{ padding: 10 }} className="dim">
                    <p style={{ marginBottom: 6 }}><b style={{ color: 'var(--text)' }}>{setups.systemNames[sysCode] ?? sysCode}</b> is a built-in setup provided by an engine plugin. You can rename it and tune its thresholds on the Parameters tab, or duplicate it as a custom setup to add alerts on top.</p>
                    <p>Alerts publish on the unified feed with <span className="mono">setup: "{sysCode}"</span>. Renaming changes only the display name; the code never changes.</p>
                  </div>
                </Card>
                {sysStats && (
                  <Card title="Today" extra={`since ${fmtTimeET(settings!.stats.since)} ET`}>
                    <div className="row" style={{ padding: 10, gap: 18 }}>
                      <span><span className="faint">evaluations</span> <b className="mono">{sysStats.evals.toLocaleString()}</b></span>
                      <span><span className="faint">fired</span> <b className="mono">{sysStats.fired}</b></span>
                      <span><span className="faint">edited parameters</span> <b className="mono">{modifiedCount(sysCode)}</b></span>
                    </div>
                  </Card>
                )}
              </div>
            )}
            {sysCode && tab === 'general' && (
              <div className="cfg-cards" style={{ marginTop: -8 }}>
                <Card title="Universe">
                  <UniverseSelect value={profiles.assignments[sysCode]}
                    onChange={id => { void profiles.assign({ [sysCode]: id === ALL_ID ? null : id }) }} />
                </Card>
              </div>
            )}
            {sysCode && tab === 'alerts' && (
              <div className="cfg-cards">
                <Card title="What makes it fire" extra="fixed by the engine">
                  <ul className="cfg-list"><li>{setups.catalogById[`setup:${sysCode}`]?.desc || "The rules of this setup are fixed by the engine plugin that provides it. Its thresholds, with today's gate pass rates, are on the Parameters tab."}</li></ul>
                  <div className="dim" style={{ padding: '0 10px 10px', fontSize: 11.5 }}>To combine this setup with other alerts (for example, fire {setups.systemNames[sysCode]} OR a bullish engulfing 5-min candle), use <b>Duplicate as custom</b>: the copy gets a "{setups.systemNames[sysCode]}" alert that fires exactly when the engine emits {sysCode}.</div>
                </Card>
              </div>
            )}
            {sysCode && tab === 'params' && settings && (
              <div className="cfg-cards">
                {GROUP_ORDER.map(g => {
                  const ps = paramsFor(sysCode).filter(p => p.group === g)
                  if (!ps.length) return null
                  const shared = ps.every(p => p.setups.includes('*'))
                  return (
                    <Card key={g} title={g} extra={shared ? 'shared with other built-in setups' : undefined}>
                      {ps.map(p => (
                        <ParamRow key={p.key} p={p} value={settings.values[p.key]} draft={paramDraft[p.key]} gate={sysStats && p.gate ? sysStats.gates[p.gate] ?? null : null}
                          onChange={v => setParamDraft(d => { const n = { ...d }; if (v === undefined || v === settings.values[p.key]) delete n[p.key]; else n[p.key] = v; return n })} />
                      ))}
                    </Card>
                  )
                })}
                {sysStats && (
                  <Card title="Gate pass rates today" extra={`${sysStats.evals.toLocaleString()} evaluations, ${sysStats.fired} fired`}>
                    <div className="cfg-gates">
                      {Object.entries(sysStats.gates).map(([g, c]) => {
                        const t = c.pass + c.fail; const pct = t ? Math.round((c.pass / t) * 100) : 0
                        return (
                          <div key={g} className="cfg-gate">
                            <span className="cfg-gate-name">{g.replace(/_/g, ' ')}</span>
                            <span className="cfg-bar"><span style={{ width: `${pct}%` }} /></span>
                            <span className="mono faint">{pct}%</span>
                            <span className="mono faint" style={{ minWidth: 92, textAlign: 'right' }}>{c.pass.toLocaleString()} / {c.fail.toLocaleString()}</span>
                          </div>
                        )
                      })}
                    </div>
                    <div className="faint" style={{ padding: '4px 10px 8px', fontSize: 10.5 }}>A gate near 0% is the one doing the blocking.</div>
                  </Card>
                )}
              </div>
            )}
            {sysCode && tab === 'summary' && settings && (
              <div className="cfg-cards">
                <Card title="Setup summary">
                  <div className="sum-sec">Universe</div>
                  <ul className="cfg-list"><li>Only symbols in the scanner universe (data/universe.csv, liquidity screen) are evaluated.</li></ul>
                  <div className="sum-sec">Alerts</div>
                  <ul className="cfg-list"><li>{setups.catalogById[`setup:${sysCode}`]?.desc || 'Fixed by the engine plugin that provides this setup.'}</li></ul>
                  <div className="sum-sec">Gates and thresholds in force</div>
                  <ul className="cfg-list">
                    {paramsFor(sysCode).map(p => (
                      <li key={p.key}>The value of <span className="chip static">{p.label}</span> is <b className="mono">{fmtVal(p, settings.values[p.key])}{p.unit ? ' ' + p.unit : ''}</b>{settings.values[p.key] !== p.default && <span className="faint"> (default {fmtVal(p, p.default)})</span>}</li>
                    ))}
                  </ul>
                </Card>
              </div>
            )}
            {sysCode && tab === 'check' && <StockCheck setupId={sysCode} universeOnly />}
            {sysCode && tab === 'log' && settings && (
              <div className="cfg-cards">
                <Card title="Change log" extra="newest first · data/settings/history.jsonl">
                  {settings.history.length === 0 && <div className="faint" style={{ padding: 10 }}>No changes yet.</div>}
                  {settings.history.map((h, i) => (
                    <div key={i} className="cfg-hist">
                      <div className="row" style={{ gap: 8 }}>
                        <span className="mono dim">{h.ts.slice(0, 10)} {fmtTimeET(h.ts)}</span>
                        <span className="badge muted">{h.source}</span>
                        <span className="mono faint">{h.hash}</span>
                        {h.note && <span className="dim">{h.note}</span>}
                      </div>
                      <div className="cfg-hist-ch">
                        {Object.entries(h.changes).map(([k, [a, b]]) => {
                          const p = byKey[k]
                          return <span key={k} className="chip static"><span className="dim">{p?.label ?? k}</span>&nbsp;<span className="mono">{p ? fmtVal(p, a) : a} → <b>{p ? fmtVal(p, b) : b}</b></span></span>
                        })}
                      </div>
                    </div>
                  ))}
                </Card>
              </div>
            )}

            {/* ── custom setup tabs ── */}
            {!sysCode && current && tab === 'general' && (
              <div className="cfg-cards">
                <Card title="General">
                  <div className="cfg-form">
                    <label className="field"><span>Alert mode</span>
                      <select className="input" value={current.mode} onChange={e => edit({ mode: e.target.value as CustomSetup['mode'] })}>
                        <option value="or">OR · any selected alert fires the setup</option>
                        <option value="and">AND · every selected alert must fire within a window</option>
                        <option value="atleast">AT LEAST · N of the selected alerts, within a window</option>
                      </select>
                    </label>
                    {current.mode === 'atleast' && (
                      <label className="field"><span>How many must fire</span>
                        <span className="row">
                          <input className="input mono" type="number" min={1} max={Math.max(1, current.triggers.length)}
                            value={current.min_triggers ?? 2}
                            onChange={e => edit({ min_triggers: Math.max(1, Number(e.target.value) || 1) })} style={{ width: 90 }} />
                          <span className="faint">of {current.triggers.length} selected</span>
                        </span>
                      </label>
                    )}
                    {(current.mode === 'and' || current.mode === 'atleast') && (
                      <label className="field"><span>Window</span>
                        <span className="row"><input className="input mono" type="number" min={1} max={120} value={current.and_window_min} onChange={e => edit({ and_window_min: Math.max(1, Number(e.target.value) || 1) })} style={{ width: 90 }} /><span className="faint">minutes</span></span>
                      </label>
                    )}
                    <label className="field"><span>Signals to detect</span>
                      <select className="input" value={current.direction} onChange={e => edit({ direction: e.target.value as CustomSetup['direction'] })}>
                        <option value="all">Long and short</option><option value="long">Long only</option><option value="short">Short only</option>
                      </select>
                    </label>
                    <label className="field"><span>Report alert as</span>
                      <select className="input" value={current.alert_direction ?? ''}
                        onChange={e => edit({ alert_direction: e.target.value as CustomSetup['alert_direction'] })}>
                        <option value="">Detected signal direction</option>
                        <option value="long">Long</option><option value="short">Short</option><option value="neutral">Neutral</option>
                      </select>
                      <span className="faint">For management alerts, a bearish exit can be reported as Long so long-only Scanner windows show it.</span>
                    </label>
                    <label className="field"><span>Calculated during</span>
                      <span className="row" style={{ gap: 10 }}>
                        <label className="row" style={{ gap: 4 }}><input type="checkbox" checked disabled /> Market (09:30-16:00)</label>
                        <label className="row" style={{ gap: 4 }}><input type="checkbox" checked={current.sessions.includes('pre')} onChange={e => edit({ sessions: e.target.checked ? ['pre', 'rth'] : ['rth'] })} /> Pre-market (04:00-09:29)</label>
                      </span>
                    </label>
                    <label className="field"><span>Do not repeat same symbol for</span>
                      <span className="row"><input className="input mono" type="number" min={0} step={30} value={current.repeat_sec} onChange={e => edit({ repeat_sec: Math.max(0, Number(e.target.value) || 0) })} style={{ width: 90 }} /><span className="faint">seconds (0 = per-alert 5-min feed cooldown only)</span></span>
                    </label>
                    <label className="field"><span>Size hint</span>
                      <select className="input" value={current.size_hint} onChange={e => edit({ size_hint: e.target.value as CustomSetup['size_hint'] })}>
                        <option value="full">Full</option><option value="three_quarter">Three quarter</option><option value="half">Half</option>
                      </select>
                    </label>
                    <label className="field" style={{ gridColumn: '1 / -1' }}><span>Notes</span>
                      <textarea className="input" rows={3} value={current.notes} onChange={e => edit({ notes: e.target.value })} style={{ height: 'auto', padding: 6 }} />
                    </label>
                  </div>
                </Card>
                <Card title="Universe">
                  <UniverseSelect value={current.universe_profile}
                    onChange={id => edit({ universe_profile: id === ALL_ID ? '' : id })} />
                </Card>
                {current.pending_filters.length > 0 && (
                  <Card title="Conditions the scanner cannot compute" extra="not enforced">
                    <ul className="cfg-list">{current.pending_filters.map((f, i) => <li key={i} className="dim">{f}</li>)}</ul>
                    <div className="faint" style={{ padding: '0 10px 10px', fontSize: 11 }}>These filters need data the bar feed does not carry (for example halt status, which is tick-level), so they are recorded but not applied.</div>
                  </Card>
                )}
              </div>
            )}
            {!sysCode && current && tab === 'alerts' && (
              <div className="cfg-cards">
                <div className="row" style={{ gap: 8 }}>
                  <span className="faint" style={{ fontSize: 11 }}>Alert mode</span>
                  <select className="input sm" value={current.mode} onChange={e => edit({ mode: e.target.value as CustomSetup['mode'] })} style={{ width: 92 }}><option value="or">or</option><option value="and">and</option><option value="atleast">at least</option></select>
                  {current.mode === 'atleast' && (
                    <input className="input sm mono" type="number" min={1} max={Math.max(1, current.triggers.length)}
                      value={current.min_triggers ?? 2} title="How many of the selected alerts must fire"
                      onChange={e => edit({ min_triggers: Math.max(1, Number(e.target.value) || 1) })} style={{ width: 48 }} />
                  )}
                  <button className="btn sm" onClick={() => setPicker(true)}>☰ Select alerts</button>
                  <span className="faint" style={{ fontSize: 11 }}>{current.triggers.length} selected · {setups.catalog.length} available</span>
                </div>
                {current.triggers.map((t, i) => {
                  const def = setups.catalogById[t.id]
                  if (!def) return <div key={t.id} className="wf-error">Unknown alert {t.id}</div>
                  return <TriggerCard key={t.id} t={t} def={def} onChange={p => editTrigger(i, p)} onRemove={() => edit({ triggers: current.triggers.filter((_, j) => j !== i) })} />
                })}
                {current.triggers.length === 0 && <div className="wf-empty" style={{ height: 160 }}><b>No alerts yet</b><span>Click Select alerts to choose from the catalog.</span></div>}
                {picker && <TriggerPicker catalog={setups.catalog} value={current.triggers} onChange={triggers => edit({ triggers })} onClose={() => setPicker(false)} />}
              </div>
            )}
            {!sysCode && current && tab === 'params' && (
              <div className="cfg-cards">
                <Card title="Parameters">
                  <div className="faint" style={{ padding: '0 10px 8px', fontSize: 11 }}>
                    What the stock has to be doing <b>right now</b> for this setup to fire: relative
                    volume, distance from VWAP, percent change, candle streaks. ANDed with each other
                    and with the universe filter. These belong to this setup alone, so changing one
                    never touches another setup.
                  </div>
                  <ConditionList conditions={current.parameters ?? []} kind="dynamic"
                    addLabel="+ Add parameter"
                    onChange={parameters => edit({ parameters })} />
                </Card>
                <Card title="Universe" extra="what kind of stock, fixed for the session">
                  <UniverseSelect value={current.universe_profile}
                    onChange={id => edit({ universe_profile: id === ALL_ID ? '' : id })} />
                </Card>
              </div>
            )}
            {!sysCode && current && tab === 'summary' && (
              <div className="cfg-cards">
                <Card title="Setup summary">
                  {(() => {
                    const s = current.summary ?? setups.customById[current.id]?.summary
                    const alerts = current.triggers.flatMap(t => {
                      const d = setups.catalogById[t.id]
                      const opts = t.options.length ? t.options : ['']
                      return opts.map(o => {
                        const ol = d?.options.find(x => x.key === o)?.label
                        const ps = d?.params.map(p => `${p.label} ${t.params[p.key] ?? p.default}${p.unit ? ' ' + p.unit : ''}`).join(', ')
                        return `${d?.name ?? t.id}${ol ? ' · ' + ol : ''}${ps ? ' (' + ps + ')' : ''}`
                      })
                    })
                    return (
                      <>
                        <div className="sum-sec">Important conditions</div>
                        <ul className="cfg-list">
                          <li>{s?.universe ?? 'Only symbols in the scanner universe (data/universe.csv) are scanned.'}</li>
                          <li>{current.mode === 'or' ? 'Any one of the alerts below fires the setup.' : `All alerts below must fire within ${current.and_window_min} minutes.`}</li>
                          <li>{{ all: 'Long and short alerts.', long: 'Long alerts only.', short: 'Short alerts only.' }[current.direction]} {current.sessions.includes('pre') ? 'Premarket and regular session.' : 'Regular session only.'}</li>
                          <li>{current.repeat_sec ? `The same symbol does not repeat for ${current.repeat_sec} seconds.` : 'The same symbol + alert does not repeat within the feed\'s 5-minute cooldown.'} {!current.enabled && <b className="down">Setup is disabled.</b>}</li>
                        </ul>
                        <div className="sum-sec">Alerts</div>
                        <ul className="cfg-list">{alerts.map((a, i) => <li key={i}>[ {a} ]</li>)}{alerts.length === 0 && <li className="faint">none</li>}</ul>
                        {current.pending_filters.length > 0 && (<><div className="sum-sec">Filters (not enforced yet)</div><ul className="cfg-list">{current.pending_filters.map((f, i) => <li key={i} className="dim">{f}</li>)}</ul></>)}
                        {customStats && (<><div className="sum-sec">Fired today</div><ul className="cfg-list">{Object.entries(customStats).map(([k, n]) => <li key={k}><span className="mono">{k}</span> · {n}</li>)}</ul></>)}
                      </>
                    )
                  })()}
                </Card>
              </div>
            )}
            {!sysCode && current && tab === 'check' && (current.id ? <StockCheck setupId={current.id} /> : <div className="wf-empty" style={{ height: 160 }}><b>Save the setup first</b><span>Stock check runs against the saved definition.</span></div>)}
          </div>
        </div>

        <div className="cfg-foot">
          {section === 'toplists' ? (
            <>
              <span className="faint" style={{ fontSize: 11 }}>Ranking changes save as you make them. The ranking metric is fixed in the engine; the universe and row count are not.</span>
              <span className="flex-spacer" />
            </>
          ) : section === 'universe' ? (
            <>
              <span className="faint" style={{ fontSize: 11 }}>A filter is checked when a setup would fire or a ranking is computed. It can only remove rows, never add them.</span>
              <span className="flex-spacer" />
            </>
          ) : sysCode ? (
            <>
              <span className="flex-spacer" />
              {dirtyKeys.length > 0 && <span className="dim" style={{ fontSize: 11 }}>{dirtyKeys.length} unsaved parameter change{dirtyKeys.length > 1 ? 's' : ''}</span>}
              <button className="btn sm" disabled={!dirtyKeys.length || busy} onClick={() => setParamDraft({})}>Discard</button>
              <button className="btn primary" disabled={!dirtyKeys.length || busy} onClick={saveParams}>{busy ? 'Saving…' : 'Save & apply'}</button>
            </>
          ) : (
            <>
              <span className="faint" style={{ fontSize: 11 }}>Custom alerts publish on the unified feed with <span className="mono">custom: true</span> and <span className="mono">setup: {current?.id || 'cs_…'}</span>.</span>
              <span className="flex-spacer" />
              {dirtyCustom && <span className="dim" style={{ fontSize: 11 }}>unsaved changes</span>}
              <button className="btn sm" disabled={!dirtyCustom || busy} onClick={() => { setDraft(null); if (sel.kind === 'new') setSel({ kind: 'none' }) }}>Discard</button>
              <button className="btn primary" disabled={!dirtyCustom || busy} onClick={saveCustom}>{busy ? 'Saving…' : 'Save & apply'}</button>
            </>
          )}
        </div>
      </div>
    </div>
  )
  return createPortal(body, document.body)
}
