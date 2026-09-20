import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { fetchMarketQuotes, quoteMapBySymbol } from '@/lib/marketQuotes'

type Market = 'hk' | 'us'
interface IndexRow { symbol: string; name: string; price: number | null; change_pct: number | null; source?: string }

const META: Record<Market, { label: string; interval: number; delay: string }> = {
  hk: { label: '港股', interval: 10_000, delay: '腾讯/新浪行情，约 10 秒刷新' },
  us: { label: '美股', interval: 60_000, delay: 'yfinance，通常延迟约 15 分钟' },
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

export function MarketIndices({ market }: { market: Market }) {
  const meta = META[market]
  const query = useQuery({
    queryKey: [market, 'indices', 'realtime'],
    queryFn: async (): Promise<IndexRow[]> => {
      const base = await getJson<{ results: { symbol: string; name: string }[] }>(`/api/${market}/indices`)
      if (base.results.length === 0) return []
      const quote = await fetchMarketQuotes(market, base.results.map((item) => item.symbol))
      const map = quoteMapBySymbol(quote)
      return base.results.map((idx) => {
        const row = map.get(idx.symbol) ?? {}
        return { symbol: idx.symbol, name: idx.name, price: typeof row.price === 'number' ? row.price : null, change_pct: typeof row.change_pct === 'number' ? row.change_pct : null, source: typeof row.source === 'string' ? row.source : undefined }
      })
    },
    refetchInterval: meta.interval,
  })
  return <section className="border border-border bg-surface p-4">
    <header className="flex items-center gap-2 border-b border-border/60 pb-3"><h2 className="text-sm font-semibold text-foreground">{meta.label}主要指数</h2><span className="text-[11px] text-muted">{meta.delay}</span><button className="ml-auto p-1 text-muted hover:text-accent" onClick={() => void query.refetch()} title="刷新指数"><RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? 'animate-spin' : ''}`} /></button></header>
    {query.isLoading && <p className="py-6 text-center text-xs text-muted">加载指数行情…</p>}
    {query.isError && <div className="py-6 text-center text-xs text-danger">指数行情暂不可用，请稍后重试</div>}
    {!query.isLoading && !query.isError && (query.data?.length ?? 0) === 0 && <p className="py-6 text-center text-xs text-muted">暂无指数数据</p>}
    <div className="grid grid-cols-1 gap-2 pt-3 sm:grid-cols-3">{query.data?.map((row) => <div key={row.symbol} className="border border-border/60 bg-elevated/30 p-3"><div className="truncate text-xs text-secondary">{row.name}</div><div className="mt-2 font-mono text-lg text-foreground">{row.price == null ? '—' : row.price.toFixed(2)}</div><div className={`mt-1 font-mono text-xs ${row.change_pct == null ? 'text-muted' : row.change_pct >= 0 ? 'text-bull' : 'text-bear'}`}>{row.change_pct == null ? '—' : `${row.change_pct >= 0 ? '+' : ''}${row.change_pct.toFixed(2)}%`}</div></div>)}</div>
  </section>
}
