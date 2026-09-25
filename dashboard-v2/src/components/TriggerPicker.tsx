import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import type { SetupTrigger, TriggerDef } from '../types'

/** Two-pane picker: available triggers on the left (filter,
 *  grouped by category, (?) shows the description), selected on the right. */
export function TriggerPicker({ catalog, value, onChange, onClose }: {
  catalog: TriggerDef[]
  value: SetupTrigger[]
  onChange(v: SetupTrigger[]): void
  onClose(): void
}) {
  const [qa, setQa] = useState('')
  const [qs, setQs] = useState('')
  const [info, setInfo] = useState<TriggerDef | null>(null)
  const selectedIds = useMemo(() => new Set(value.map(t => t.id)), [value])
  // Closing the picker (Escape, the X, Done, the backdrop) returns focus to the
  // control that opened it, so keyboard use continues where it left off.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    return () => { if (opener && document.contains(opener)) opener.focus() }
  }, [])
  // The picker is the topmost modal, so Escape is its key: close the (?) panel
  // first, then the picker. Config ignores Escape while the picker is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      if (info) setInfo(null); else onClose()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [info, onClose])
  const byId = useMemo(() => Object.fromEntries(catalog.map(t => [t.id, t])), [catalog])

  const available = useMemo(() => {
    const q = qa.trim().toLowerCase()
    const rows = catalog.filter(t => !selectedIds.has(t.id)
      && (!q || `${t.name} ${t.category} ${t.desc}`.toLowerCase().includes(q)))
    const groups = new Map<string, TriggerDef[]>()
    for (const t of rows) groups.set(t.category, [...(groups.get(t.category) ?? []), t])
    return Array.from(groups.entries())
  }, [catalog, qa, selectedIds])

  const selected = useMemo(() => {
    const q = qs.trim().toLowerCase()
    return value.filter(t => !q || (byId[t.id]?.name ?? t.id).toLowerCase().includes(q))
  }, [value, qs, byId])

  const add = (t: TriggerDef) => {
    const params = Object.fromEntries(t.params.map(p => [p.key, p.default]))
    onChange([...value, { id: t.id, options: t.options.length ? [...t.default_options] : [], params, repeat_sec: 0 }])
  }
  const remove = (id: string) => onChange(value.filter(t => t.id !== id))
  const move = (id: string, dir: -1 | 1) => {
    const i = value.findIndex(t => t.id === id)
    const j = i + dir
    if (i < 0 || j < 0 || j >= value.length) return
    const next = [...value]
    ;[next[i], next[j]] = [next[j], next[i]]
    onChange(next)
  }

  const body = (
    <div className="modal-back picker-back" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal picker wf-nodrag" role="dialog" aria-modal="true">
        <div className="modal-head">
          <div className="modal-title">Select triggers</div>
          <span className="flex-spacer" />
          <button className="btn sm icon" title="Close (Esc)" onClick={onClose}>✕</button>
        </div>
        <div className="picker-body">
          <div className="picker-pane">
            <div className="picker-pane-head">Available triggers <span className="faint">{catalog.length - selectedIds.size}</span></div>
            <input className="input" placeholder="Filter" value={qa} onChange={e => setQa(e.target.value)} autoFocus />
            <div className="picker-list">
              {available.map(([cat, items]) => (
                <div key={cat}>
                  <div className="picker-cat">{cat}</div>
                  {items.map(t => (
                    <div key={t.id} className="picker-item" onClick={() => add(t)} title="Add">
                      <span className={`picker-dir ${t.direction}`} />
                      <span className="picker-name">{t.name}</span>
                      <button className="picker-info" title="What is this?" onMouseEnter={() => setInfo(t)} onMouseLeave={() => setInfo(null)}
                        onClick={e => { e.stopPropagation(); setInfo(i => (i?.id === t.id ? null : t)) }}>?</button>
                    </div>
                  ))}
                </div>
              ))}
              {available.length === 0 && <div className="faint" style={{ padding: 10 }}>Nothing matches.</div>}
            </div>
          </div>
          <div className="picker-arrows">
            <span className="faint" style={{ fontSize: 22 }}>→</span>
            <span className="faint" style={{ fontSize: 22 }}>←</span>
          </div>
          <div className="picker-pane">
            <div className="picker-pane-head">Selected triggers <span className="faint">{value.length}</span></div>
            <input className="input" placeholder="Filter" value={qs} onChange={e => setQs(e.target.value)} />
            <div className="picker-list">
              {selected.map(t => {
                const d = byId[t.id]
                return (
                  <div key={t.id} className="picker-item sel">
                    <span className={`picker-dir ${d?.direction ?? 'neutral'}`} />
                    <span className="picker-name">{d?.name ?? t.id}</span>
                    <span className="faint" style={{ fontSize: 10.5 }}>{t.options.length ? t.options.length + ' opt' : ''}</span>
                    <button className="wf-ctl" title="Move up" onClick={() => move(t.id, -1)}>↑</button>
                    <button className="wf-ctl" title="Move down" onClick={() => move(t.id, 1)}>↓</button>
                    <button className="wf-ctl close" title="Remove" onClick={() => remove(t.id)}>✕</button>
                  </div>
                )
              })}
              {value.length === 0 && <div className="faint" style={{ padding: 10 }}>Click a trigger on the left to add it.</div>}
            </div>
          </div>
          {info && (
            <div className="picker-popover">
              <div className="picker-pop-title">{info.name}</div>
              <div className="picker-pop-desc">{info.desc}</div>
              {info.options.length > 0 && (
                <>
                  <div className="picker-pop-h">{info.option_label}</div>
                  <div className="row wrap" style={{ gap: 4 }}>{info.options.map(o => <span key={o.key} className="chip static">{o.label}</span>)}</div>
                </>
              )}
              {info.params.length > 0 && (
                <>
                  <div className="picker-pop-h">Settings</div>
                  {info.params.map(p => <div key={p.key} className="dim" style={{ fontSize: 11.5 }}>{p.label}: default {p.default}{p.unit ? ' ' + p.unit : ''}{p.desc ? ` · ${p.desc}` : ''}</div>)}
                </>
              )}
              <div className="picker-pop-h">Calculated during</div>
              <div className="row" style={{ gap: 4 }}>{info.sessions.map(s => <span key={s} className="chip static">{s === 'pre' ? 'Pre-Market' : 'Market'}</span>)}</div>
              <div className="picker-pop-h">Source</div>
              <div className="dim" style={{ fontSize: 11.5 }}>
                {info.id.startsWith('setup:') ? 'Fires when the built-in setup fires.' : 'Computed by the scanner from live bars.'}
              </div>
            </div>
          )}
        </div>
        <div className="cfg-foot">
          <span className="dim" style={{ fontSize: 11 }}>Options and settings for each alert are edited on the Alerts tab after closing.</span>
          <span className="flex-spacer" />
          <button className="btn primary" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  )
  return createPortal(body, document.body)
}
