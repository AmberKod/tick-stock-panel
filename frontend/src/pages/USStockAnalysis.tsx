import { useQuery } from '@tanstack/react-query'
import { useParams, Link } from 'react-router-dom'
import { MarketWatchlistButton } from '@/components/MarketWatchlistButton'
import { StockDailyKChart } from '@/components/StockDailyKChart'

interface USRealtime {
  symbol: string
  name?: string
  code?: string
  price: number | null
  pre_close: number | null
  open: number | null
  high: number | null
  low: number | null
  volume: number | null
  amount: number | null
  change_pct: number | null
  source: string
  market?: string
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

/**
 * 美股个股页 — 实时行情卡片 + A股同款日K蜡烛图 (MA/MACD/KDJ/RSI/BOLL)。
 *
 * K线走 /api/kline/daily (repository.get_daily 对 .US symbol 分流读
 * kline_hk_us_enriched 本地全量 enriched), 与 A股 StockDailyKChart 完全同源。
 * 美股无涨跌停, 关闭 limitMarkers。
 */
export function USStockAnalysisPage() {
  const { symbol: rawSymbol = 'AAPL.US' } = useParams<{ symbol: string }>()
  const symbol = rawSymbol.toUpperCase()

  const realtime = useQuery({
    queryKey: ['us', 'realtime', symbol],
    queryFn: () => fetchJson<USRealtime>(`/api/us/realtime/${encodeURIComponent(symbol)}`),
    refetchInterval: 60_000,
  })

  const r = realtime.data
  const changeColor =
    r?.change_pct == null
      ? 'text-fg-muted'
      : r.change_pct >= 0
        ? 'text-emerald-500'
        : 'text-rose-500'

  // 美股惯例: 涨绿跌红 (与中国相反)
  return (
    <div className="p-6 space-y-6">
      <div className="flex items-baseline gap-3">
        <Link to="/us" className="text-sm text-fg-muted hover:text-fg">← 美股热门池</Link>
        <span className="text-fg-muted">/</span>
        <h1 className="text-2xl font-semibold text-fg">
          {r?.name || symbol} <span className="text-sm text-fg-muted font-mono">{symbol}</span>
        </h1>
        <span className="inline-block px-1.5 py-0.5 text-xs rounded bg-blue-500/10 text-blue-500">
          US · USD
        </span>
        <span className="ml-auto">
          <MarketWatchlistButton symbol={symbol} market="us" />
        </span>
        {r && (
          <span className="text-xs text-fg-muted">
            数据源: {r.source} {r.source === 'yfinance' ? '(延迟 15min)' : ''}
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card label="现价">
          {r?.price != null ? (
            <span className={`text-2xl font-bold ${changeColor}`}>
              ${r.price.toFixed(2)}
            </span>
          ) : (
            <span className="text-fg-muted">--</span>
          )}
        </Card>
        <Card label="涨跌幅">
          {r?.change_pct != null ? (
            <span className={`text-2xl font-bold ${changeColor}`}>
              {r.change_pct >= 0 ? '+' : ''}
              {r.change_pct.toFixed(2)}%
            </span>
          ) : (
            <span className="text-fg-muted">--</span>
          )}
        </Card>
        <Card label="今开">{r?.open != null ? `$${r.open.toFixed(2)}` : '--'}</Card>
        <Card label="昨收">{r?.pre_close != null ? `$${r.pre_close.toFixed(2)}` : '--'}</Card>
        <Card label="最高">{r?.high != null ? `$${r.high.toFixed(2)}` : '--'}</Card>
        <Card label="最低">{r?.low != null ? `$${r.low.toFixed(2)}` : '--'}</Card>
        <Card label="成交量">
          {r?.volume != null ? formatVolume(r.volume) : '--'}
        </Card>
        <Card label="成交额">
          {r?.amount != null ? formatAmount(r.amount) : '--'}
        </Card>
      </div>

      <div className="text-xs text-fg-muted p-3 rounded border border-border bg-surface/40">
        美股惯例: 绿涨红跌 · T+0 交收 · 无涨跌停 · K线为本地 enriched 全量日K (新浪源优先, yfinance 兜底)
      </div>

      {/* 日K蜡烛图 (A股同款组件, 本地 enriched 全指标) */}
      <StockDailyKChart symbol={symbol} height={560} showLimitMarkers={false} />
    </div>
  )
}

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface/60 p-3">
      <div className="text-xs text-fg-muted mb-1">{label}</div>
      <div className="text-lg font-semibold text-fg">{children}</div>
    </div>
  )
}

function formatVolume(v: number): string {
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (v >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return v.toFixed(0)
}

function formatAmount(a: number): string {
  if (a >= 1e8) return `${(a / 1e8).toFixed(2)}亿`
  if (a >= 1e4) return `${(a / 1e4).toFixed(2)}万`
  return a.toFixed(0)
}
