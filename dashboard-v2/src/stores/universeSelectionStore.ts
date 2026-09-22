import { create } from 'zustand'
import type { UniverseSelectionPayload } from '../types'
import { api } from '../lib/api'

interface UniverseSelectionState {
  data: UniverseSelectionPayload | null
  loaded: boolean
  busy: boolean
  error: string | null
  load(force?: boolean): Promise<void>
  select(watchlistId: string | null): Promise<boolean>
}

export const useUniverseSelection = create<UniverseSelectionState>()((set, get) => ({
  data: null, loaded: false, busy: false, error: null,
  async load(force = false) {
    if (get().loaded && !force) return
    try {
      set({ busy: true, error: null })
      set({ data: await api.universeSelection.get(), loaded: true })
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e), loaded: true })
    } finally { set({ busy: false }) }
  },
  async select(watchlistId) {
    try {
      set({ busy: true, error: null })
      set({ data: await api.universeSelection.set(watchlistId), loaded: true })
      return true
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e) })
      return false
    } finally { set({ busy: false }) }
  },
}))
