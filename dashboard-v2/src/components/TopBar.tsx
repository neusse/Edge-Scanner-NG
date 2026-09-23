import { useEffect, useState } from 'react'
import { useScreens, SCREEN_TEMPLATES, type ScreenTemplate } from '../stores/screensStore'
import { useSettings } from '../stores/settingsStore'
import { useFeeds, visibleSources, SOURCE_LABEL, SOURCE_SHORT } from '../stores/feedsStore'
import { useCapabilities } from '../stores/capabilitiesStore'
import type { WindowType } from '../types'
import { THEMES } from '../lib/theme'
import { nowET } from '../lib/time'
import { audioReady, unlockAudio, playTone } from '../lib/audio'
import { usePoll } from '../lib/usePoll'
import { api } from '../lib/api'
import { Menu, MenuHead, MenuItem, MenuSep } from './Menu'
import { SetupsPanel } from './SetupsPanel'
import { WINDOW_ICONS, WINDOW_TITLES } from '../windows/defaults'

const WINDOW_ORDER: WindowType[] = ['chart', 'quotes', 'scanner', 'toplist', 'screener', 'news', 'stockinfo', 'setupcheck', 'watchlist', 'clock']

function FeedDots() {
  const status = useFeeds(s => s.status)
  const counts = useFeeds(s => s.counts)
  const hasSystem = useCapabilities(s => s.system_setups)
  const cls = status === 'connected' ? 'ok' : status === 'reconnecting' ? 'warn' : 'bad'
  return (
    <div className="row" style={{ gap: 8 }} title={`Unified alert feed (/ws/alerts): ${status}`}>
      <span className="row" style={{ gap: 4, fontSize: 10.5 }}><span className={`dot ${cls}`} /><span className="faint">FEED</span></span>
      {visibleSources(hasSystem).map(f => (
        <span key={f} className="faint mono" style={{ fontSize: 10.5 }} title={`${SOURCE_LABEL[f]}: ${counts[f]} today`}>{SOURCE_SHORT[f]} {counts[f]}</span>
      ))}
    </div>
  )
}

function ScreenSelector() {
  const screens = useScreens(s => s.screens)
  const order = useScreens(s => s.order)
  const activeId = useScreens(s => s.activeId)
  const { setActive, createScreen, createFromTemplate, renameScreen, duplicateScreen, deleteScreen } = useScreens.getState()
  const [open, setOpen] = useState(false)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [name, setName] = useState('')
  const active = activeId ? screens[activeId] : null

  const startRename = (id: string) => { setRenaming(id); setName(screens[id]?.name ?? '') }
  const commitRename = () => { if (renaming) renameScreen(renaming, name); setRenaming(null) }

  return (
    <div style={{ position: 'relative' }}>
      <button className="btn" onClick={() => setOpen(o => !o)} title="Screens">
        <span className="faint">Screen</span> <b>{active?.name ?? '—'}</b> <span className="faint">▾</span>
      </button>
      <Menu open={open} onClose={() => { setOpen(false); setRenaming(null) }} style={{ minWidth: 240 }}>
        <MenuHead>Screens</MenuHead>
        {order.map(id => {
          const s = screens[id]
          if (!s) return null
          if (renaming === id) {
            return (
              <div key={id} className="row" style={{ padding: '4px 6px' }}>
                <input className="input sm" autoFocus value={name} onChange={e => setName(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') commitRename(); if (e.key === 'Escape') setRenaming(null) }} style={{ flex: 1 }} />
                <button className="btn sm primary" onClick={commitRename}>OK</button>
              </div>
            )
          }
          return (
            <div key={id} className="row" style={{ gap: 2 }}>
              <button className={`menu-item${id === activeId ? ' on' : ''}`} style={{ flex: 1 }} onClick={() => { setActive(id); setOpen(false) }}>
                {s.name}{s.locked && <span className="k">🔒</span>}
              </button>
              <button className="wf-ctl" title="Rename" onClick={() => startRename(id)}>✎</button>
              <button className="wf-ctl" title="Duplicate" onClick={() => { duplicateScreen(id); setOpen(false) }}>⧉</button>
              <button className="wf-ctl close" title="Delete" onClick={() => { if (confirm(`Delete screen "${s.name}"?`)) deleteScreen(id) }}>✕</button>
            </div>
          )
        })}
        <MenuSep />
        <MenuItem icon="+" onClick={() => { const n = prompt('New screen name', 'New screen'); if (n != null) { createScreen(n); setOpen(false) } }}>New screen</MenuItem>
        <MenuHead>Starter layouts (added as a new screen)</MenuHead>
        {(Object.keys(SCREEN_TEMPLATES) as ScreenTemplate[]).map(k => (
          <MenuItem key={k} icon="▦" onClick={() => { createFromTemplate(k); setOpen(false) }}>
            <span title={SCREEN_TEMPLATES[k].desc}>{SCREEN_TEMPLATES[k].label}</span>
          </MenuItem>
        ))}
      </Menu>
    </div>
  )
}

export function AddWindowMenu({ open, onClose }: { open: boolean; onClose(): void }) {
  const addWindow = useScreens(s => s.addWindow)
  return (
    <Menu open={open} onClose={onClose} style={{ minWidth: 200 }}>
      <MenuHead>Add window</MenuHead>
      {WINDOW_ORDER.map(t => (
        <MenuItem key={t} icon={WINDOW_ICONS[t]} onClick={() => { addWindow(t); onClose() }}>{WINDOW_TITLES[t]}</MenuItem>
      ))}
    </Menu>
  )
}

export function TopBar() {
  const [clock, setClock] = useState(nowET)
  const [addOpen, setAddOpen] = useState(false)
  const [cfgOpen, setCfgOpen] = useState(false)
  const [audioOn, setAudioOn] = useState(audioReady())
  const activeId = useScreens(s => s.activeId)
  const locked = useScreens(s => (s.activeId ? s.screens[s.activeId]?.locked : false) ?? false)
  const saveState = useScreens(s => s.saveState)
  const setLocked = useScreens(s => s.setLocked)
  const theme = useSettings(s => s.theme)
  const setTheme = useSettings(s => s.setTheme)
  const menuHidden = useSettings(s => s.menuHidden)
  const setMenuHidden = useSettings(s => s.setMenuHidden)
  const globalMute = useSettings(s => s.globalMute)
  const setGlobalMute = useSettings(s => s.setGlobalMute)
  const { data: clockInfo } = usePoll(api.clock, 5000)

  useEffect(() => { const t = setInterval(() => setClock(nowET()), 1000); return () => clearInterval(t) }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setAddOpen(o => !o) }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'l') { e.preventDefault(); setLocked(!locked) }
      if ((e.ctrlKey || e.metaKey) && e.key === ',') { e.preventDefault(); setCfgOpen(o => !o) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [locked, setLocked])

  if (menuHidden) {
    return <div className="topbar hidden" title="Show menu" onClick={() => setMenuHidden(false)} />
  }

  const regime = (clockInfo?.regime ?? 'neutral').toLowerCase()
  const session = clockInfo?.session

  return (
    <header className="topbar">
      <div className="brand"><span className="brand-mark" /><span className="brand-name">EDGE SCANNER</span></div>
      <ScreenSelector />
      <div style={{ position: 'relative' }}>
        <button className="btn primary" onClick={() => setAddOpen(o => !o)} title="Add window (Ctrl+K)">+ Add Window</button>
        <AddWindowMenu open={addOpen} onClose={() => setAddOpen(false)} />
      </div>
      <button className={`btn icon${locked ? ' on' : ''}`} title={locked ? 'Unlock layout (Ctrl+L)' : 'Lock layout (Ctrl+L)'} onClick={() => setLocked(!locked)} disabled={!activeId}>
        {locked ? '🔒' : '🔓'}
      </button>
      <button className="btn" title="Config: setups, rankings and universe filters (Ctrl+,)" onClick={() => setCfgOpen(true)}>⚙ Config</button>
      {cfgOpen && <SetupsPanel onClose={() => setCfgOpen(false)} />}
      <button className="btn" title="Save the current layout as a new named screen" disabled={!activeId}
        onClick={() => { if (!activeId) return; const n = prompt('Save layout as', ''); if (n && n.trim()) useScreens.getState().duplicateScreen(activeId, n) }}>
        💾 Save as…
      </button>
      <span className={saveState === 'error' ? 'down' : 'faint'} style={{ fontSize: 10.5, minWidth: 54 }}
        title={saveState === 'error' ? 'Layout could not be saved to the scanner' : 'Layout changes save automatically'}>
        {saveState === 'saving' ? 'saving…' : saveState === 'error' ? 'save failed' : 'auto-saved'}
      </span>
      <span className="flex-spacer" />
      <FeedDots />
      <span className="sep" />
      {clockInfo?.replay && <span className="chip static" style={{ color: 'var(--link-purple)', borderColor: 'var(--link-purple)' }} title="Replaying a past session">REPLAY {clockInfo.replay.date}</span>}
      {session && <span className={`chip static ${session === 'rth' ? 'up' : ''}`} title="Session">{session.toUpperCase()}</span>}
      <span className={`regime ${regime}`} title="SPY regime"><span className="dot" style={{ background: 'currentColor' }} />{regime}</span>
      <span className="clock">{clock}<span className="clock-tz">ET</span></span>
      <span className="sep" />
      {!audioOn && (
        <button className="btn sm" title="Browsers require a click before sound/voice can play" onClick={async () => { setAudioOn(await unlockAudio()); playTone('ping') }}>🔈 enable sound</button>
      )}
      <button className={`btn icon${globalMute ? ' on' : ''}`} title={globalMute ? 'Unmute all' : 'Mute all'} onClick={() => setGlobalMute(!globalMute)}>{globalMute ? '🔇' : '🔔'}</button>
      <div className="row" style={{ gap: 2 }} title="Theme">
        {THEMES.map(t => (
          <button key={t.key} className={`btn sm${theme === t.key ? ' on' : ''}`} style={{ width: 26, padding: 0, justifyContent: 'center' }} title={t.label} onClick={() => setTheme(t.key)}>{t.short}</button>
        ))}
      </div>
      <button className="btn icon" title="Hide menu" onClick={() => setMenuHidden(true)}>▴</button>
      <button className="btn icon" title="Fullscreen" onClick={() => { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen?.() }}>⛶</button>
    </header>
  )
}
