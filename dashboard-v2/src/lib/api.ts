import type {
  Bar, CheckResult, ClockInfo, CustomSetup, SetupsPayload, EventsPayload, Fundamentals, NewsPayload, PremarketPayload, Screen,
  SettingsPayload, SettingsStats, SnapshotPayload, StockInfo, ToplistPayload, UniverseMetaPayload, Watchlist,
  MembersResult, ProfilesPayload, UniverseProfile, ToplistsPayload, SetupCheckPayload,
  YahooScreenerCatalog, YahooScreenerPayload, ScreenerConfig, UniverseSelectionPayload,
} from '../types'

export interface QuoteObservation {
  symbol: string; stream_id: string; bid: number | null; ask: number | null; last: number | null
  spread: number | null; spread_bps: number | null; midpoint: number | null
  bid_market_ms: number | null; ask_market_ms: number | null; last_market_ms: number | null
  bid_age_ms: number | null; ask_age_ms: number | null; last_age_ms: number | null
  bid_size: number | null; ask_size: number | null; receipt_ms: number | null
  quality: string; coverage: string; delayed: boolean | null; source: string; tier: string; session: string
}
export interface QuoteSample {
  time_ms: number; market_ms: number | null; bid: number | null; ask: number | null
  last: number | null; spread: number | null; spread_bps: number | null; quality: string; seq: number
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    // A 404 on /api/v2 means the running scanner predates V2: it has to be restarted
    // to pick up scanner/api_v2.py. Say so instead of a bare "Not Found".
    if (res.status === 404 && url.startsWith('/api/v2/')) throw new Error('scanner needs a restart for V2 endpoints')
    let msg = `HTTP ${res.status}`
    try {
      const b = await res.json()
      if (b?.error) msg = String(b.error)
      else if (b?.detail) msg = String(b.detail)
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

function put<T>(url: string, body: unknown): Promise<T> {
  return json<T>(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}
function del<T>(url: string): Promise<T> {
  return json<T>(url, { method: 'DELETE' })
}
function post<T>(url: string, body: unknown): Promise<T> {
  return json<T>(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}

export const api = {
  // existing scanner endpoints (:7777)
  bars: (symbol: string, tf: string) =>
    json<{ bars: Bar[]; error?: string }>(`/api/bars/${encodeURIComponent(symbol)}/${tf}`).then(d => d.bars ?? []),
  chartBars: (symbol: string, tf: string, extended: boolean) =>
    json<{ bars: Bar[]; indicators: Record<string, { t: string; value: number }[]>; indicator_version: string; asof: string | null }>(
      `/api/bars/${encodeURIComponent(symbol)}/${tf}?with_indicators=true&extended=${extended}`),
  premarket: () => json<PremarketPayload>('/api/premarket'),
  regime: () => json<{ regime: string }>('/api/regime'),
  // /api/v2
  capabilities: () => json<Record<string, boolean>>('/api/v2/capabilities'),
  clock: () => json<ClockInfo>('/api/v2/clock'),
  replayStatus: () => json<{ position: number; total: number; paused: boolean; complete: boolean; speed: number }>('/api/replay/status'),
  replayControl: (action: string, speed?: number) => post<{ position: number; total: number; paused: boolean; complete: boolean; speed: number }>('/api/replay/control', { action, speed }),
  toplist: (name: string, limit: number) => json<ToplistPayload>(`/api/v2/toplists/${name}?limit=${limit}`),
  events: (since: number, limit = 200) => json<EventsPayload>(`/api/v2/events?since=${since}&limit=${limit}`),
  snapshot: (symbols: string[]) =>
    json<SnapshotPayload>(`/api/v2/snapshot?symbols=${encodeURIComponent(symbols.join(','))}`),
  quote: (symbol: string) => json<QuoteObservation>(`/api/v2/quotes/${encodeURIComponent(symbol)}`),
  quoteHistory: (symbol: string, limit = 600) =>
    json<{ symbol: string; resolution: string; samples: QuoteSample[] }>(`/api/v2/quotes/${encodeURIComponent(symbol)}/history?limit=${limit}`),
  state: (symbol: string) => json<StockInfo>(`/api/v2/state/${encodeURIComponent(symbol)}`),
  setupCheck: (symbol: string, minutes: number) =>
    json<SetupCheckPayload>(`/api/v2/check/${encodeURIComponent(symbol)}?minutes=${minutes}`),
  fundamentals: (symbol: string) => json<Fundamentals>(`/api/v2/fundamentals/${encodeURIComponent(symbol)}`),
  news: (symbols: string[] | null, limit: number, hours: number) => {
    const q = new URLSearchParams({ limit: String(limit), hours: String(hours) })
    if (symbols?.length) q.set('symbols', symbols.join(','))
    return json<NewsPayload>(`/api/v2/news?${q}`)
  },
  universeMeta: () => json<UniverseMetaPayload>('/api/v2/universe/meta'),
  screener: {
    catalog: () => json<YahooScreenerCatalog>('/api/v2/screener/yahoo/catalog'),
    run: (config: ScreenerConfig) => post<YahooScreenerPayload>('/api/v2/screener/yahoo', {
      mode: config.mode, preset: config.preset, filters: config.filters,
      sort_field: config.sortField, sort_asc: config.sortAsc,
      limit: config.limit, include_otc: config.includeOtc,
    }),
  },
  universeSelection: {
    get: () => json<UniverseSelectionPayload>('/api/v2/universe/selection'),
    set: (watchlistId: string | null) =>
      put<UniverseSelectionPayload>('/api/v2/universe/selection', { watchlist_id: watchlistId }),
  },
  layouts: {
    list: () => json<{ screens: Screen[] }>('/api/v2/layouts').then(d => d.screens ?? []),
    save: (s: Screen) => put<{ ok: boolean }>(`/api/v2/layouts/${encodeURIComponent(s.id)}`, s),
    delete: (id: string) => del<{ ok: boolean }>(`/api/v2/layouts/${encodeURIComponent(id)}`),
  },
  setups: {
    get: () => json<SetupsPayload>('/api/v2/setups'),
    save: (s: CustomSetup) => put<{ ok: boolean; setup: CustomSetup }>(`/api/v2/setups/${encodeURIComponent(s.id)}`, s),
    delete: (id: string) => del<{ ok: boolean }>(`/api/v2/setups/${encodeURIComponent(id)}`),
    names: (names: Record<string, string>) => put<{ ok: boolean; names: Record<string, string> }>('/api/v2/setups/names', names),
    check: (id: string, symbol: string) => json<CheckResult>(`/api/v2/setups/${encodeURIComponent(id)}/check?symbol=${encodeURIComponent(symbol)}`),
  },
  toplists: {
    get: () => json<ToplistsPayload>('/api/v2/toplists'),
    save: (name: string, patch: { rows?: number; universe?: string | null }) =>
      put<ToplistsPayload>(`/api/v2/toplists/${encodeURIComponent(name)}`, patch),
  },
  profiles: {
    get: () => json<ProfilesPayload>('/api/v2/universe/profiles'),
    save: (p: UniverseProfile) =>
      put<{ ok: boolean; profile: UniverseProfile } & ProfilesPayload>(`/api/v2/universe/profiles/${encodeURIComponent(p.id)}`, p),
    delete: (id: string) => del<{ ok: boolean } & ProfilesPayload>(`/api/v2/universe/profiles/${encodeURIComponent(id)}`),
    assign: (m: Record<string, string | null>) =>
      put<{ ok: boolean } & ProfilesPayload>('/api/v2/universe/assignments', m),
    members: (id: string, limit = 200) =>
      json<MembersResult>(`/api/v2/universe/profiles/${encodeURIComponent(id)}/members?limit=${limit}`),
    saveSet: (p: UniverseProfile) =>
      put<{ ok: boolean } & ProfilesPayload>(`/api/v2/universe/paramsets/${encodeURIComponent(p.id)}`, p),
    deleteSet: (id: string) =>
      del<{ ok: boolean } & ProfilesPayload>(`/api/v2/universe/paramsets/${encodeURIComponent(id)}`),
  },
  settings: {
    get: () => json<SettingsPayload>('/api/v2/settings'),
    save: (values: Record<string, number>, note = '') => put<SettingsPayload>('/api/v2/settings', { values, note }),
    reset: (opts: { setup?: string; keys?: string[]; note?: string }) => post<SettingsPayload>('/api/v2/settings/reset', opts),
    stats: () => json<SettingsStats>('/api/v2/settings/stats'),
    savePreset: (name: string) => put<{ ok: boolean }>(`/api/v2/settings/presets/${encodeURIComponent(name)}`, {}),
    applyPreset: (name: string) => post<SettingsPayload>(`/api/v2/settings/presets/${encodeURIComponent(name)}/apply`, {}),
    deletePreset: (name: string) => del<{ ok: boolean }>(`/api/v2/settings/presets/${encodeURIComponent(name)}`),
  },
  watchlists: {
    list: () => json<{ watchlists: Watchlist[] }>('/api/v2/watchlists').then(d => d.watchlists ?? []),
    save: (w: Watchlist) => put<{ ok: boolean }>(`/api/v2/watchlists/${encodeURIComponent(w.id)}`, w),
    delete: (id: string) => del<{ ok: boolean }>(`/api/v2/watchlists/${encodeURIComponent(id)}`),
  },
}
