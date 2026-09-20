/**
 * 全局市场上下文 — A 股 / 港股 / 美股统一市场标识。
 *
 * 后端 regime / strength_ladder 等 API 已全面支持 market 查询参数 (cn/hk/us),
 * 前端需要一个统一的"当前市场"来源, 避免每个页面各写一份 useState + 各推导一遍路由。
 *
 * 设计:
 * - 初始值从路由推导 (访问 /hk* → hk, /us* → us, 其余 → cn), 复用 backtestMarket 的
 *   marketFromLocation 规则, 保证与回测页口径一致。
 * - 允许手动切换 (setMarket), 切换后不强制改写 URL —— 市场环境/看板这类"分析视图"
 *   允许用户在 A 股页面里临时切到港股看行情, 比跳路由更轻。
 * - 路由变化时会重新同步 (用户从 /hk 跳回 / 会重置为 cn)。
 *
 * 不做什么:
 * - 不接管个股详情页的市场判定 (那是 symbol 后缀 .HK/.US 的职责, 见 symbolMatchesAsset)。
 * - 不做持久化 (刷新回到路由默认值, 语义更可预期)。
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from 'react'
import { useLocation } from 'react-router-dom'
import { marketFromLocation } from '@/lib/backtestMarket'
// MarketCode 以 api.ts 的定义为准 (与后端 market 查询参数一一对应),
// 这里 re-export 避免两处定义漂移。
import type { MarketCode } from '@/lib/api'

export type { MarketCode }

export interface MarketMeta {
  code: MarketCode
  label: string
  /** 紧凑标签, 用于切换器按钮 (空间紧张时) */
  short: string
  currency: string
  benchmark: string
  /** 该地区主要时区, 用于"今天"判定与盘中/盘后提示 */
  timeZone: string
  /** 该市场是否有涨跌停制度 —— 决定连板梯队 / 封板率等 A 股专属指标是否展示 */
  hasLimitUp: boolean
}

export const MARKETS: Record<MarketCode, MarketMeta> = {
  cn: {
    code: 'cn',
    label: 'A 股',
    short: 'A股',
    currency: 'CNY',
    benchmark: '上证指数',
    timeZone: 'Asia/Shanghai',
    hasLimitUp: true,
  },
  hk: {
    code: 'hk',
    label: '港股',
    short: '港股',
    currency: 'HKD',
    benchmark: '恒生指数',
    timeZone: 'Asia/Hong_Kong',
    hasLimitUp: false,
  },
  us: {
    code: 'us',
    label: '美股',
    short: '美股',
    currency: 'USD',
    benchmark: '标普 500',
    timeZone: 'America/New_York',
    hasLimitUp: false,
  },
}

/** 切换器展示顺序 — A 股优先, 与产品主市场一致 */
export const MARKET_ORDER: MarketCode[] = ['cn', 'hk', 'us']

interface MarketContextValue {
  market: MarketCode
  meta: MarketMeta
  setMarket: (code: MarketCode) => void
  /** 该市场是否有涨跌停制度 (连板梯队等 A 股专属能力是否可用) */
  hasLimitUp: boolean
  /** 当前市场在当地时区的今天 (YYYY-MM-DD) */
  marketToday: string
}

const MarketContext = createContext<MarketContextValue | null>(null)

export function MarketProvider({ children }: { children: ReactNode }) {
  const location = useLocation()
  const routeMarket = useMemo(
    () => marketFromLocation(location.pathname, location.search),
    [location.pathname, location.search],
  )

  const [override, setOverride] = useState<MarketCode | null>(null)

  // 路由变化 → 丢弃手动覆盖, 回到路由推导值。
  // 否则用户从 /hk 跳到 / 后仍停在 hk, 与页面内容不一致。
  useEffect(() => {
    setOverride(null)
  }, [routeMarket])

  const market = override ?? routeMarket

  const setMarket = useCallback((code: MarketCode) => {
    setOverride(code)
  }, [])

  const value = useMemo<MarketContextValue>(() => {
    const meta = MARKETS[market]
    const today = new Intl.DateTimeFormat('en-CA', {
      timeZone: meta.timeZone,
      year: 'numeric', month: '2-digit', day: '2-digit',
    }).format(new Date())
    return {
      market,
      meta,
      setMarket,
      hasLimitUp: meta.hasLimitUp,
      marketToday: today,
    }
  }, [market, setMarket])

  return <MarketContext.Provider value={value}>{children}</MarketContext.Provider>
}

export function useMarket(): MarketContextValue {
  const ctx = useContext(MarketContext)
  if (!ctx) {
    throw new Error('useMarket 必须在 <MarketProvider> 内使用')
  }
  return ctx
}
