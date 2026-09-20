import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { fetchMarketQuotes, quoteMapBySymbol } from '@/lib/marketQuotes'

interface HKIndexQuote {
  symbol: string
  name: string
  price: number | null
  change_pct: number | null
}

interface HKLeaderRow {
  symbol: string
  name: string
  price: number | null
  change_pct: number | null
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status}`)
  return res.json() as Promise<T>
}

/** 港股实时迷你看板: 3 指数 + 10 龙头涨幅榜。
 *
 *  用在 HKStocks 列表页顶部, 让用户进入港股页就能看到"现在怎么样"。
 *  10s 轮询 (M1 quickquote 限流, 后续 M2 改 3s)。
 */
export function HKMarketWidget() {
  const indices = useQuery({
    queryKey: ['hk', 'widget', 'indices'],
    queryFn: async () => {
      const r = await fetchJson<{ results: { symbol: string; name: string }[] }>('/api/hk/indices')
      const quotes = await fetchMarketQuotes('hk', r.results.map((item) => item.symbol))
      const map = quoteMapBySymbol(quotes)
      return r.results.map((idx) => {
        const row = map.get(idx.symbol) ?? {}
        return {
          symbol: idx.symbol,
          name: idx.name,
          price: (row.price as number) ?? null,
          change_pct: (row.change_pct as number) ?? null,
        } as HKIndexQuote
      })
    },
    refetchInterval: 10_000,
  })

  const leaders = useQuery({
    queryKey: ['hk', 'widget', 'leaders'],
    queryFn: async () => {
      const stocks = await fetchJson<{ results: { symbol: string; name: string }[] }>(
        '/api/hk/stocks',
      )
      const quotes = await fetchMarketQuotes('hk', stocks.results.map((stock) => stock.symbol))
      const map = quoteMapBySymbol(quotes)
      const out: HKLeaderRow[] = stocks.results.map((s) => {
        const row = map.get(s.symbol) ?? {}
        return {
          symbol: s.symbol,
          name: s.name,
          price: (row.price as number) ?? null,
          change_pct: (row.change_pct as number) ?? null,
        }
      })
      // 按涨跌幅降序
      out.sort((a, b) => (b.change_pct ?? -999) - (a.change_pct ?? -999))
      return out
    },
    refetchInterval: 10_000,
  })

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {/* 指数条 */}
      <div className="rounded-lg border border-border bg-surface/60 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-medium text-fg-muted">港股指数</h2>
          <span className="text-xs text-fg-muted">10s 自动刷新</span>
        </div>
        {indices.isLoading && <div className="text-xs text-fg-muted py-2">加载中...</div>}
        {indices.data && (
          <div className="space-y-2">
            {indices.data.map((idx) => (
              <div
                key={idx.symbol}
                className="flex items-center justify-between border-b border-border/40 last:border-0 pb-1.5 last:pb-0"
              >
                <div>
                  <div className="text-sm text-fg">{idx.name}</div>
                  <div className="text-xs text-fg-muted font-mono">{idx.symbol}</div>
                </div>
                <div className="text-right">
                  <div className="text-base font-semibold text-fg">
                    {idx.price != null ? idx.price.toFixed(2) : '--'}
                  </div>
                  <div
                    className={`text-xs font-medium ${
                      idx.change_pct == null
                        ? 'text-fg-muted'
                        : idx.change_pct >= 0
                          ? 'text-rose-500'
                          : 'text-emerald-500'
                    }`}
                  >
                    {idx.change_pct != null
                      ? `${idx.change_pct >= 0 ? '+' : ''}${idx.change_pct.toFixed(2)}%`
                      : '--'}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 龙头涨幅榜 */}
      <div className="rounded-lg border border-border bg-surface/60 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-medium text-fg-muted">港股龙头涨幅榜</h2>
          <span className="text-xs text-fg-muted">{leaders.data?.length ?? 0} 只</span>
        </div>
        {leaders.isLoading && <div className="text-xs text-fg-muted py-2">加载中...</div>}
        {leaders.data && (
          <div className="space-y-1">
            {leaders.data.slice(0, 10).map((s) => (
              <Link
                to={`/hk/${s.symbol}`}
                key={s.symbol}
                className="flex items-center justify-between border-b border-border/40 last:border-0 py-1.5 hover:bg-elevated/30 -mx-2 px-2 rounded"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-fg truncate">{s.name}</div>
                  <div className="text-xs text-fg-muted font-mono">{s.symbol}</div>
                </div>
                <div className="text-right flex-shrink-0 ml-2">
                  <div className="text-sm text-fg">
                    {s.price != null ? s.price.toFixed(2) : '--'}
                  </div>
                  <div
                    className={`text-xs font-medium ${
                      s.change_pct == null
                        ? 'text-fg-muted'
                        : s.change_pct >= 0
                          ? 'text-rose-500'
                          : 'text-emerald-500'
                    }`}
                  >
                    {s.change_pct != null
                      ? `${s.change_pct >= 0 ? '+' : ''}${s.change_pct.toFixed(2)}%`
                      : '--'}
                  </div>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
