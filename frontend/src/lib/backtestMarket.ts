import type { StrategyBacktestAsset, StrategyBacktestResult } from './api'

export const BACKTEST_MARKETS = {
  stock: { label: 'A 股股票', currency: 'CNY', market: 'cn', benchmark: '上证指数' },
  etf: { label: 'A 股 ETF', currency: 'CNY', market: 'cn', benchmark: '上证指数' },
  hk: { label: '港股', currency: 'HKD', market: 'hk', benchmark: '恒生指数' },
  us: { label: '美股', currency: 'USD', market: 'us', benchmark: '标普 500 指数' },
} as const

export function isInternationalAsset(asset: StrategyBacktestAsset): asset is 'hk' | 'us' {
  return asset === 'hk' || asset === 'us'
}

export function backtestAssetFromQuery(query: URLSearchParams): StrategyBacktestAsset {
  const value = query.get('market') ?? query.get('asset_type')
  return value === 'hk' || value === 'us' || value === 'etf' ? value : 'stock'
}

export function marketFromLocation(pathname: string, search: string): 'cn' | 'hk' | 'us' {
  if (pathname === '/hk' || pathname.startsWith('/hk/')) return 'hk'
  if (pathname === '/us' || pathname.startsWith('/us/')) return 'us'
  if (pathname === '/backtest') {
    return BACKTEST_MARKETS[backtestAssetFromQuery(new URLSearchParams(search))].market
  }
  return 'cn'
}

export function symbolMatchesAsset(symbol: string, asset: StrategyBacktestAsset): boolean {
  const upper = symbol.trim().toUpperCase()
  if (asset === 'hk') return upper.endsWith('.HK')
  if (asset === 'us') return upper.endsWith('.US')
  return !upper.endsWith('.HK') && !upper.endsWith('.US')
}

export function marketToday(asset: StrategyBacktestAsset): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: asset === 'us' ? 'America/New_York' : 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date())
}

export function resultMarket(result: StrategyBacktestResult) {
  const asset = result.config.asset_type as StrategyBacktestAsset
  return BACKTEST_MARKETS[asset] ?? BACKTEST_MARKETS.stock
}

export function resultBenchmarkName(result: StrategyBacktestResult): string {
  return result.benchmark_curve?.find((row) => row.name)?.name
    || result.config.benchmark_symbol
    || resultMarket(result).benchmark
}
