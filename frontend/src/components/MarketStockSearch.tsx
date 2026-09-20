import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Search } from 'lucide-react'
import { MarketWatchlistButton } from './MarketWatchlistButton'

type Market = 'hk' | 'us'
interface StockRow { symbol: string; name: string; code?: string }

async function getStocks(market: Market): Promise<StockRow[]> {
  const res = await fetch(`/api/${market}/stocks`)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  const data = await res.json() as { results: StockRow[] }
  return data.results
}

export function MarketStockSearch({ market }: { market: Market }) {
  const [keyword, setKeyword] = useState('')
  const query = useQuery({ queryKey: [market, 'stock-search'], queryFn: () => getStocks(market), staleTime: 60_000 })
  const rows = useMemo(() => {
    const key = keyword.trim().toLowerCase()
    if (!key) return query.data ?? []
    return (query.data ?? []).filter((row) => row.symbol.toLowerCase().includes(key) || row.name.toLowerCase().includes(key) || row.code?.toLowerCase().includes(key))
  }, [keyword, query.data])
  const label = market === 'hk' ? '港股' : '美股'

  return <section className="border border-border bg-surface">
    <div className="flex flex-wrap items-center gap-3 border-b border-border/60 p-3">
      <div><h2 className="text-sm font-semibold text-foreground">{label}个股分析</h2><p className="mt-0.5 text-[11px] text-muted">从当前市场池选择标的，进入行情与日 K 详情</p></div>
      <label className="relative ml-auto w-full sm:w-72"><Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" /><input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索代码或名称" className="h-10 w-full border border-border bg-base pl-9 pr-3 text-xs text-foreground outline-none focus:border-accent" /></label>
    </div>
    {query.isLoading && <p className="p-6 text-center text-xs text-muted">加载标的列表…</p>}
    {query.isError && <p className="p-6 text-center text-xs text-danger">标的列表暂不可用</p>}
    {!query.isLoading && !query.isError && rows.length === 0 && <p className="p-6 text-center text-xs text-muted">没有匹配的标的</p>}
    <div className="divide-y divide-border/50">{rows.map((row) => <div key={row.symbol} className="flex min-h-12 items-center gap-3 px-4 py-2 hover:bg-elevated/40"><Link to={`/${market}/${row.symbol}`} className="min-w-0 flex-1"><div className="truncate text-sm font-medium text-foreground hover:text-accent">{row.name}</div><div className="font-mono text-[11px] text-muted">{row.symbol}</div></Link><MarketWatchlistButton symbol={row.symbol} size="sm" /><Link to={`/${market}/${row.symbol}`} className="border border-border px-3 py-2 text-xs text-secondary hover:border-accent/50 hover:text-accent">分析</Link></div>)}</div>
    <p className="border-t border-border/50 px-4 py-2 text-[11px] text-muted">当前共 {query.data?.length ?? 0} 只内置标的；后续数据源扩展后可覆盖更大市场池。</p>
  </section>
}
