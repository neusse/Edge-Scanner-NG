import { create } from 'zustand'
import type { Watchlist } from '../types'
import { api } from '../lib/api'
import { id as newId } from '../lib/ids'

interface WatchlistsState {
  lists: Record<string, Watchlist>
  order: string[]
  loaded: boolean
  load(): Promise<void>
  create(name: string, symbols?: string[], extra?: Partial<Watchlist>): string
  rename(id: string, name: string): void
  update(id: string, patch: Partial<Watchlist>): void
  remove(id: string): void
  setSymbols(id: string, symbols: string[]): void
  mergeSymbols(id: string, symbols: string[]): void
}

const norm = (syms: string[]) => Array.from(new Set(syms.map(s => s.trim().toUpperCase()).filter(Boolean)))

function persist(w: Watchlist) {
  api.watchlists.save(w).catch(() => { /* surfaced by the window's status line */ })
}

export const useWatchlists = create<WatchlistsState>()((set, get) => ({
  lists: {},
  order: [],
  loaded: false,

  async load() {
    if (get().loaded) return
    try {
      const ls = await api.watchlists.list()
      const lists: Record<string, Watchlist> = {}
      for (const w of ls) lists[w.id] = w
      set({ lists, order: ls.map(w => w.id), loaded: true })
    } catch { set({ loaded: true }) }
  },

  create(name, symbols = [], extra = {}) {
    const now = new Date().toISOString()
    const w: Watchlist = { ...extra, id: newId('wl'), name: name.trim() || 'Watchlist',
      description: extra.description?.trim() ?? '', symbols: norm(symbols), createdAt: now, updatedAt: now }
    set(s => ({ lists: { ...s.lists, [w.id]: w }, order: [...s.order, w.id] }))
    persist(w)
    return w.id
  },

  rename(id, name) {
    const w = get().lists[id]; if (!w) return
    const n = { ...w, name: name.trim() || w.name, updatedAt: new Date().toISOString() }
    set(s => ({ lists: { ...s.lists, [id]: n } })); persist(n)
  },

  update(id, patch) {
    const w = get().lists[id]; if (!w) return
    const n: Watchlist = { ...w, ...patch, id: w.id,
      name: (patch.name ?? w.name).trim() || w.name,
      description: (patch.description ?? w.description ?? '').trim(),
      symbols: norm(patch.symbols ?? w.symbols), updatedAt: new Date().toISOString() }
    set(s => ({ lists: { ...s.lists, [id]: n } })); persist(n)
  },

  remove(id) {
    set(s => {
      const lists = { ...s.lists }; delete lists[id]
      return { lists, order: s.order.filter(x => x !== id) }
    })
    api.watchlists.delete(id).catch(() => { /* ignore */ })
  },

  setSymbols(id, symbols) {
    const w = get().lists[id]; if (!w) return
    const n = { ...w, symbols: norm(symbols), updatedAt: new Date().toISOString() }
    set(s => ({ lists: { ...s.lists, [id]: n } })); persist(n)
  },

  mergeSymbols(id, symbols) {
    const w = get().lists[id]; if (!w) return
    get().setSymbols(id, [...w.symbols, ...symbols])
  },
}))
