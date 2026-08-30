import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

interface USIndexQuote {
  symbol: string
  name: string
  price: number | null
  change_pct: string | null
}

interface USLeaderRow {
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

/** 美股实时迷你看板: 3 指数 + 15 龙头涨跌榜。
 *
 *  yfinance 免费延迟 15min, 轮询间隔设 60s (比港股 10s 慢, 避免限流)。
 */
export function USMarketWidget() {
  const indices = useQuery({
    queryKey: ['us', 'widget', 'indices'],
    queryFn: async () => {
      const r = await fetchJson<{ results: { symbol: string; name: string }[] }>('/api/us/indices')
      const q = await fetchJson<{ results: Record<string, unknown>[] }>(
        `/api/us/realtime/batch?symbols=${r.results.map((i) => i.symbol).join(',')}`,
      )
      const map = new Map<string, Record<string, unknown>>()
      for (const row of q.results) map.set(row.symbol as string, row)
      return r.results.map((idx): USIndexQuote => {
        const row = map.get(idx.symbol)
        const pct = (row?.change_pct as number | undefined) ?? null
        return {
          symbol: idx.symbol,
          name: idx.name,
          price: (row?.price as number) ?? null,
          change_pct: pct != null ? `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%` : null,
        }
      })
    },
    refetchInterval: 60_000,
  })

  const leaders = useQuery({
    queryKey: ['us', 'widget', 'leaders'],
    queryFn: async () => {
      const r = await fetchJson<{ results: { symbol: string; name: string }[] }>('/api/us/stocks')
      const q = await fetchJson<{ results: Record<string, unknown>[] }>(
        `/api/us/realtime/batch?symbols=${r.results.map((s) => s.symbol).join(',')}`,
      )
      const map = new Map<string, Record<string, unknown>>()
      for (const row of q.results) map.set(row.symbol as string, row)
      const out: USLeaderRow[] = r.results.map((s) => {
        const row = map.get(s.symbol)
        return {
          symbol: s.symbol,
          name: s.name,
          price: (row?.price as number) ?? null,
          change_pct: (row?.change_pct as number) ?? null,
        }
      })
      out.sort((a, b) => (b.change_pct ?? -999) - (a.change_pct ?? -999))
      return out
    },
    refetchInterval: 60_000,
  })

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div className="rounded-lg border border-border bg-surface/60 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-medium text-fg-muted">美股指数</h2>
          <span className="text-xs text-fg-muted">60s 刷新 (延迟 15min)</span>
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
                  <div className="div">{idx.name}</div>
                  <div className="text-xs text-fg-muted font-mono">{idx.symbol}</div>
                </div>
                <div className="text-right">
                  <div className="text-base font-semibold text-fg">
                    {idx.price != null ? idx.price.toFixed(2) : '--'}
                  </div>
                  <div className="text-xs font-medium text-fg-muted">{idx.change_pct ?? '--'}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="rounded-lg border border-border bg-surface/60 p-4">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-medium text-fg-muted">美股龙头涨跌榜</h2>
          <span className="text-xs text-fg-muted">{leaders.data?.length ?? 0} 只</span>
        </div>
        {leaders.isLoading && <div className="text-xs text-fg-muted py-2">加载中...</div>}
        {leaders.data?.slice(0, 10).map((s) => (
          <Link
            to={`/us/${s.symbol}`}
            key={s.symbol}
            className="flex items-center justify-between border-b border-border/40 last:border-0 py-1.5 hover:bg-elevated/30 -mx-2 px-2 rounded"
          >
            <div className="min-w-0 flex-1">
              <div className="text-sm text-fg truncate">{s.name}</div>
              <div className="text-xs text-fg-muted font-mono">{s.symbol}</div>
            </div>
            <div className="text-right flex-shrink-0 ml-2">
              <div className="text-sm text-fg">{s.price != null ? s.price.toFixed(2) : '--'}</div>
              <div className="text-xs font-medium text-fg-muted">{s.change_pct ?? '--'}</div>
            </div>
          </Link>
        ))}
      </div>
    </div>
  )
}