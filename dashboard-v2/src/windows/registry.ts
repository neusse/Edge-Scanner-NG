import type { ComponentType } from 'react'
import type { FeedId, WindowConfig, WindowType } from '../types'
import { ScannerWindow, ScannerSettings } from './scanner/ScannerWindow'
import { ChartWindow, ChartSettings } from './chart/ChartWindow'
import { ToplistWindow, ToplistSettings } from './toplist/ToplistWindow'
import { ScreenerWindow, ScreenerSettings } from './screener/ScreenerWindow'
import { TOPLIST_LABEL } from './defaults'
import { NewsWindow, NewsSettings } from './news/NewsWindow'
import { StockInfoWindow } from './stockinfo/StockInfoWindow'
import { WatchlistWindow, WatchlistSettings } from './watchlist/WatchlistWindow'
import { ClockWindow, ClockSettings } from './clock/ClockWindow'
import { SetupCheckWindow } from './setupcheck/SetupCheckWindow'
import { SOURCE_SHORT } from '../stores/feedsStore'

export interface Subtitle { text: string; strong?: boolean }

export interface WindowDef {
  component: ComponentType<{ win: never }>
  settings?: ComponentType<{ win: never; onChange(patch: Record<string, unknown>): void }>
  subtitle?(win: WindowConfig, linkedSymbol: string | null): Subtitle | null
}

const symSub = (win: WindowConfig, linked: string | null): Subtitle | null => {
  const own = 'symbol' in win ? (win as { symbol: string | null }).symbol : null
  const s = win.link === 'none' ? own : (linked ?? own)
  return s ? { text: s, strong: true } : { text: win.link === 'none' ? 'no symbol' : 'waiting for link' }
}

// Component props are typed per window; the frame passes `win as never`.
export const WINDOW_REGISTRY: Record<WindowType, WindowDef> = {
  scanner: {
    component: ScannerWindow as unknown as WindowDef['component'],
    settings: ScannerSettings as unknown as WindowDef['settings'],
    subtitle: w => { const s = (w as { sources?: FeedId[] }).sources ?? []; return { text: s.length ? s.map(x => SOURCE_SHORT[x]).join(' + ') : 'all sources' } },
  },
  chart: {
    component: ChartWindow as unknown as WindowDef['component'],
    settings: ChartSettings as unknown as WindowDef['settings'],
    subtitle: symSub,
  },
  toplist: {
    component: ToplistWindow as unknown as WindowDef['component'],
    settings: ToplistSettings as unknown as WindowDef['settings'],
    subtitle: w => ({ text: TOPLIST_LABEL[(w as { list: keyof typeof TOPLIST_LABEL }).list] }),
  },
  screener: {
    component: ScreenerWindow as unknown as WindowDef['component'],
    settings: ScreenerSettings as unknown as WindowDef['settings'],
    subtitle: w => ({ text: (w as { mode: string }).mode === 'custom' ? 'custom' : 'Yahoo' }),
  },
  news: {
    component: NewsWindow as unknown as WindowDef['component'],
    settings: NewsSettings as unknown as WindowDef['settings'],
    subtitle: (w, linked) => ((w as { mode: string }).mode === 'market' ? { text: 'market' } : symSub(w, linked)),
  },
  stockinfo: {
    component: StockInfoWindow as unknown as WindowDef['component'],
    subtitle: symSub,
  },
  watchlist: {
    component: WatchlistWindow as unknown as WindowDef['component'],
    settings: WatchlistSettings as unknown as WindowDef['settings'],
  },
  clock: {
    component: ClockWindow as unknown as WindowDef['component'],
    settings: ClockSettings as unknown as WindowDef['settings'],
  },
  setupcheck: {
    component: SetupCheckWindow as unknown as WindowDef['component'],
    subtitle: symSub,
  },
}
