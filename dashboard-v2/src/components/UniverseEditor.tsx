import { useEffect, useMemo, useState } from 'react'
import type { ConditionDef, MembersResult, ProfileCondition, UniverseProfile } from '../types'
import { api } from '../lib/api'
import { useProfiles } from '../stores/profilesStore'

/** Editor for one universe profile, and the selector that attaches one to a setup.
 *
 *  A profile is a named AND-list of conditions, defined once here and reused by
 *  any number of setups. The screen it expresses is checked when a setup would
 *  fire, before the alert is emitted, so a profile can only ever REMOVE alerts.
 */

export const ALL_ID = 'up_all'

const fmt = (v: number, unit: string): string => {
  const a = Math.abs(v)
  const n = a >= 1_000_000 ? `${+(v / 1_000_000).toFixed(2)}M`
    : a >= 10_000 ? `${+(v / 1_000).toFixed(1)}K`
      : `${+v.toFixed(2)}`
  if (unit === '$') return `$${n}`
  if (unit === '%' || unit === 'x') return `${n}${unit}`
  return unit ? `${n} ${unit}` : n
}

const OP_LABEL: Record<string, string> = { gte: '≥', lte: '≤', gt: '>', lt: '<' }

function describeCondition(c: ProfileCondition, def?: ConditionDef): string {
  if (!def) return c.id
  const opt = def.options.length && c.option
    ? ` (${def.options.find(o => o.key === c.option)?.label ?? c.option})` : ''
  return `${def.name}${opt} ${OP_LABEL[c.op] ?? c.op} ${fmt(c.value, def.unit)}`
}

// ── one condition row ────────────────────────────────────────────────────────

function ConditionRow({ cond, def, onChange, onRemove }: {
  cond: ProfileCondition
  def: ConditionDef
  onChange(c: ProfileCondition): void
  onRemove(): void
}) {
  return (
    <div className="cfg-row" style={{ alignItems: 'flex-start', gap: 8 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="cfg-row-label">
          {def.name}
          <span className={`badge ${def.kind === 'static' ? 'muted' : ''}`}
            style={{ marginLeft: 6, fontSize: 9 }}
            title={def.kind === 'static'
              ? 'Fixed for the session. Resolved once into a member list, so checking it is free.'
              : 'Changes every bar. Evaluated when the setup would fire.'}>
            {def.kind}
          </span>
          {def.availability === 'pass' && (
            <span className="badge muted" style={{ marginLeft: 4, fontSize: 9 }}
              title="Missing values PASS. This field comes from yfinance and is often absent.">
              pass if unknown
            </span>
          )}
        </div>
        <div className="cfg-row-desc faint">{def.desc}</div>
      </div>

      <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
        {def.unit === 'bool' ? (
          // A yes/no condition has no threshold to pick, so an operator select
          // and a number box would just be two controls that must not be touched.
          <label className="row" style={{ gap: 5, cursor: 'pointer' }}>
            <input type="checkbox" checked={cond.value >= 1}
              onChange={e => onChange({ ...cond, value: e.target.checked ? 1 : 0 })} />
            <span className="faint" style={{ fontSize: 11 }}>required</span>
          </label>
        ) : (<>
        {def.options.length > 0 && (
          <select className="input" style={{ width: 104 }} value={cond.option}
            onChange={e => onChange({ ...cond, option: e.target.value })}
            title={def.option_label}>
            {def.options.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}
          </select>
        )}
        <select className="input" style={{ width: 56 }} value={cond.op}
          onChange={e => onChange({ ...cond, op: e.target.value })}>
          {def.ops.map(o => <option key={o} value={o}>{OP_LABEL[o] ?? o}</option>)}
        </select>
        <input className="input mono" type="number" style={{ width: 108 }}
          value={cond.value} step={def.step ?? 1}
          min={def.min ?? undefined} max={def.max ?? undefined}
          onChange={e => onChange({ ...cond, value: Number(e.target.value) })} />
        <span className="faint mono" style={{ width: 44, fontSize: 11 }}>{def.unit}</span>
        </>)}
        {def.params.map(p => (
          <span key={p.key} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span className="faint" style={{ fontSize: 11 }} title={p.desc}>{p.label}</span>
            {p.choices && p.choices.length ? (
              // A fixed set (candle sizes): a dropdown shows what is allowed and
              // cannot produce a timeframe the scanner does not build.
              <select className="input mono" style={{ width: 80 }}
                value={cond.params[p.key] ?? p.default}
                onChange={e => onChange({
                  ...cond, params: { ...cond.params, [p.key]: Number(e.target.value) },
                })}>
                {p.choices.map(c => <option key={c} value={c}>{c}{p.unit ? ` ${p.unit}` : ''}</option>)}
              </select>
            ) : (
              <input className="input mono" type="number" style={{ width: 64 }}
                value={cond.params[p.key] ?? p.default} step={p.step ?? 1}
                min={p.min ?? undefined} max={p.max ?? undefined}
                onChange={e => onChange({
                  ...cond, params: { ...cond.params, [p.key]: Number(e.target.value) },
                })} />
            )}
          </span>
        ))}
        <button className="btn ghost" title="Remove this condition" onClick={onRemove}>×</button>
      </div>
    </div>
  )
}

// ── a kind-filtered list of conditions, shared by both editors ───────────────

/** The condition rows plus their "+ Add" picker.
 *
 *  `kind` decides which half of the catalog is offered, and that split is the
 *  whole model: STATIC conditions describe what kind of stock this is and are
 *  fixed for the session, so they live in a named universe filter that resolves
 *  once into a member set. DYNAMIC conditions describe what it is doing right
 *  now, so they live on one setup as its parameters and are evaluated when it
 *  would fire. Offering both in both places is what produced nine "universe
 *  filters" with no universe in them.
 */
export function ConditionList({ conditions, kind, onChange, addLabel }: {
  conditions: ProfileCondition[]
  kind: 'static' | 'dynamic'
  onChange(next: ProfileCondition[]): void
  addLabel?: string
}) {
  const profiles = useProfiles()
  const [adding, setAdding] = useState(false)

  const byCategory = useMemo(() => {
    const out: Record<string, ConditionDef[]> = {}
    for (const d of profiles.catalog) if (d.kind === kind) (out[d.category] ??= []).push(d)
    return out
  }, [profiles.catalog, kind])

  const has = (id: string, option: string) =>
    conditions.some(c => c.id === id && (c.option || '') === (option || ''))

  const add = (def: ConditionDef) => {
    onChange([...conditions, {
      id: def.id, op: def.default_op, value: def.default_value,
      option: def.default_option,
      params: Object.fromEntries(def.params.map(p => [p.key, p.default])),
    }])
    setAdding(false)
  }

  return (
    <>
      <div className="cfg-card-head">
        <span>{kind === 'static' ? 'Conditions' : 'Checks'} <span className="faint">{conditions.length}</span></span>
        <button className="btn" onClick={() => setAdding(v => !v)}>{addLabel ?? '+ Add condition'}</button>
      </div>

      {adding && (
        <div style={{ padding: '4px 10px 10px' }}>
          {Object.entries(byCategory).map(([cat, defs]) => (
            <div key={cat} style={{ marginBottom: 6 }}>
              <div className="sum-sec">{cat}</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                {defs.map(d => (
                  <button key={d.id} className="btn ghost" title={d.desc}
                    disabled={!d.options.length && has(d.id, '')}
                    onClick={() => add(d)}>
                    {d.name}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {conditions.length === 0 && !adding && (
        <div className="faint" style={{ padding: '4px 10px 10px', fontSize: 11 }}>
          {kind === 'static'
            ? 'No conditions. Every symbol passes.'
            : 'No checks. Nothing about the current bar is required.'}
        </div>
      )}

      {conditions.map((c, i) => {
        const def = profiles.catalogById[c.id]
        if (!def) return (
          <div key={i} className="cfg-row"><div className="cfg-row-label dim">unknown condition {c.id}</div></div>
        )
        return (
          <ConditionRow key={`${c.id}:${c.option}:${i}`} cond={c} def={def}
            onChange={next => onChange(conditions.map((x, j) => (j === i ? next : x)))}
            onRemove={() => onChange(conditions.filter((_, j) => j !== i))} />
        )
      })}
    </>
  )
}

// ── the profile editor ───────────────────────────────────────────────────────

export function UniverseEditor({ profile, kind = 'universe', usedBy = [], onSaved, onDeleted, onDirtyChange }: {
  profile: UniverseProfile
  /** Which half of the catalog this list holds. A universe filter is static and
   *  resolves into a member set; a parameter set is dynamic and is evaluated
   *  when a setup would fire, so it has no membership to preview. */
  kind?: 'universe' | 'parameters'
  /** Setup ids pointing at this one (parameter sets only; universe filters read
   *  their users from the assignment map). */
  usedBy?: string[]
  onSaved?(p: UniverseProfile): void
  onDeleted?(): void
  /** Lets the Config panel guard its close and navigation against unsaved filter edits. */
  onDirtyChange?(dirty: boolean): void
}) {
  const profiles = useProfiles()
  const [draft, setDraft] = useState<UniverseProfile>(profile)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [members, setMembers] = useState<MembersResult | null>(null)

  // Reset the editor when a different profile (or a newer save of it) comes in.
  // Derived during render rather than in an effect, so there is no extra pass.
  const profileKey = `${profile.id}|${profile.updatedAt ?? ''}`
  const [prevKey, setPrevKey] = useState(profileKey)
  if (profileKey !== prevKey) { setPrevKey(profileKey); setDraft(profile); setErr(null); setMembers(null) }

  const dirty = useMemo(
    () => JSON.stringify(draft.conditions) !== JSON.stringify(profile.conditions)
      || draft.name !== profile.name || draft.desc !== profile.desc,
    [draft, profile])
  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange])

  const isParams = kind === 'parameters'
  const isAll = draft.id === ALL_ID && !isParams
  const used = useMemo(() => (isParams ? usedBy : Object.entries(profiles.assignments)
    .filter(([, v]) => v === draft.id).map(([k]) => k)), [profiles.assignments, draft.id, isParams, usedBy])

  const save = async () => {
    setBusy(true); setErr(null)
    try {
      if (isParams) {
        await profiles.saveSet(draft)
        onSaved?.(draft)
      } else {
        const saved = await profiles.save(draft)
        onSaved?.(saved)
        setMembers(await api.profiles.members(saved.id).catch(() => null as never))
      }
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  const remove = async () => {
    setBusy(true); setErr(null)
    try { await (isParams ? profiles.removeSet(draft.id) : profiles.remove(draft.id)); onDeleted?.() }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  const preview = async () => {
    setBusy(true); setErr(null)
    try { setMembers(await api.profiles.members(draft.id)) }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false) }
  }

  return (
    <div className="cfg-cards">
      {err && <div className="wf-error">{err}</div>}

      <div className="cfg-card">
        <div className="cfg-card-head">
          <span>{isParams ? 'Shared checks' : 'Filter'}</span>
          <span className="faint mono" style={{ fontSize: 10 }}>{draft.id}</span>
        </div>
        <div className="cfg-row">
          <div className="cfg-row-label">Name</div>
          <input className="input" style={{ width: 260 }} value={draft.name} disabled={isAll}
            onChange={e => setDraft(d => ({ ...d, name: e.target.value }))} />
        </div>
        <div className="cfg-row">
          <div className="cfg-row-label">Note</div>
          <input className="input" style={{ width: 380 }} value={draft.desc}
            placeholder="what this filter is for" disabled={isAll}
            onChange={e => setDraft(d => ({ ...d, desc: e.target.value }))} />
        </div>
        <div className="faint" style={{ padding: '0 10px 10px', fontSize: 11 }}>
          {isAll
            ? 'The default filter. It has no conditions, so every symbol in the universe passes. It cannot be edited or deleted.'
            : used.length
              ? `Used by ${used.join(', ')}. Conditions are ANDed: all must pass for an alert to be emitted.`
              : 'Conditions are ANDed: all must pass for an alert to be emitted. Attach it to a setup from that setup’s page.'}
          {!isAll && (isParams
            ? ' Shared checks hold only what changes bar to bar: relative volume, distance from VWAP, percent change, EMA stack. They are ANDed with each setup’s own checks, so a setup can tighten one of these values but never loosen it.'
            : ' A universe holds only what is fixed for the session: price, volume, dollar volume, ATR%, market cap, float, short interest. Anything that changes bar to bar is a setup check.')}
        </div>
      </div>

      <div className="cfg-card">
        {isAll
          ? <div className="cfg-card-head"><span>Conditions <span className="faint">0</span></span></div>
          : <ConditionList conditions={draft.conditions} kind={isParams ? 'dynamic' : 'static'}
              addLabel={isParams ? '+ Add check' : '+ Add condition'}
              onChange={conditions => setDraft(d => ({ ...d, conditions }))} />}
        {isAll && (
          <div className="faint" style={{ padding: '4px 10px 10px', fontSize: 11 }}>
            No conditions. Every symbol passes.
          </div>
        )}
      </div>

      {members && (
        <div className="cfg-card">
          <div className="cfg-card-head"><span>What this admits</span></div>
          <div style={{ padding: '0 10px 10px' }}>
            <div style={{ fontSize: 20, fontWeight: 600 }}>
              {members.count}<span className="faint" style={{ fontSize: 13, fontWeight: 400 }}> / {members.universe_size} symbols</span>
            </div>
            <div className="faint" style={{ fontSize: 11, marginTop: 2 }}>
              From the {members.static_conditions} session-fixed condition{members.static_conditions === 1 ? '' : 's'}.
              {members.dynamic_conditions > 0 && ` ${members.dynamic_conditions} more change every bar and are checked when a setup fires, so the live count is lower.`}
            </div>
            {members.symbols.length > 0 && (
              <div className="mono faint" style={{ fontSize: 11, marginTop: 6, wordBreak: 'break-all' }}>
                {members.symbols.slice(0, 60).join(' ')}{members.truncated || members.symbols.length > 60 ? ' …' : ''}
              </div>
            )}
          </div>
        </div>
      )}

      <div className="cfg-actions">
        {/* A parameter set has no membership to preview: its conditions are a
            function of the current bar, not of the symbol. */}
        {!isParams && <button className="btn" onClick={preview} disabled={busy}>Preview matches</button>}
        <div style={{ flex: 1 }} />
        {!isAll && (
          <button className="btn danger" onClick={remove}
            disabled={busy || used.length > 0}
            title={used.length ? `Still used by ${used.join(', ')}` : 'Delete this filter'}>
            Delete
          </button>
        )}
        <button className="btn primary" onClick={save} disabled={busy || isAll || !dirty}>
          {dirty ? 'Save' : 'Saved'}
        </button>
      </div>
    </div>
  )
}

// ── the selector that attaches a profile to a setup ──────────────────────────

export function UniverseSelect({ value, onChange, disabled }: {
  value: string | undefined | null
  onChange(id: string): void
  disabled?: boolean
}) {
  const profiles = useProfiles()
  const current = value || ALL_ID
  const p = profiles.byId[current]
  const count = profiles.members[current]

  return (
    <div className="cfg-row">
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="cfg-row-label">Universe filter</div>
        <div className="cfg-row-desc faint">
          Checked when this setup would fire, before the alert is emitted. It can only
          remove alerts, never add one. Define filters under Universe filters.
        </div>
        {p && p.conditions.length > 0 && (
          <ul className="cfg-list" style={{ marginTop: 4 }}>
            {(p.summary ?? p.conditions.map(c => describeCondition(c, profiles.catalogById[c.id])))
              .map((line, i) => <li key={i} className="dim">{line}</li>)}
          </ul>
        )}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
        {count != null && (
          <span className="faint mono" style={{ fontSize: 11 }}
            title="symbols passing the session-fixed conditions">
            {count}/{profiles.universeSize}
          </span>
        )}
        <select className="input" style={{ width: 220 }} value={current} disabled={disabled}
          onChange={e => onChange(e.target.value)}>
          {profiles.profiles.map(x => (
            <option key={x.id} value={x.id}>
              {x.id === ALL_ID ? 'All symbols (no filter)' : x.name}
            </option>
          ))}
        </select>
      </div>
    </div>
  )
}
