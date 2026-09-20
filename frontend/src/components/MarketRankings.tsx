import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { fetchMarketQuotes, quoteMapBySymbol } from '@/lib/marketQuotes'

type Market = 'hk' | 'us'
type Ranking = 'gainers' | 'losers' | 'active'
interface Quote { symbol: string; name: string; price: number | null; change_pct: number | null; volume: number | null; amount: number | null }

async function json<T>(url: string): Promise<T> { const res = await fetch(url); if (!res.ok) throw new Error(`${res.status} ${res.statusText}`); return res.json() as Promise<T> }
const tabs: { key: Ranking; label: string }[] = [{ key: 'gainers', label: '涨幅榜' }, { key: 'losers', label: '跌幅榜' }, { key: 'active', label: '成交/活跃榜' }]

export function MarketRankings({ market }: { market: Market }) {
  const [ranking, setRanking] = useState<Ranking>('gainers')
  const query = useQuery({ queryKey: [market, 'rankings'], queryFn: async () => {
    const pool = await json<{ results: { symbol: string; name: string }[] }>(`/api/${market}/stocks`)
    if (!pool.results.length) return []
    const quotes = await fetchMarketQuotes(market, pool.results.map((stock) => stock.symbol))
    const map = quoteMapBySymbol(quotes)
    return pool.results.map((stock): Quote => { const q = map.get(stock.symbol) ?? {}; return { symbol: stock.symbol, name: stock.name, price: typeof q.price === 'number' ? q.price : null, change_pct: typeof q.change_pct === 'number' ? q.change_pct : null, volume: typeof q.volume === 'number' ? q.volume : null, amount: typeof q.amount === 'number' ? q.amount : null } })
  }, refetchInterval: market === 'hk' ? 10_000 : 60_000 })
  const rows = useMemo(() => [...(query.data ?? [])].sort((a, b) => ranking === 'gainers' ? (b.change_pct ?? -Infinity) - (a.change_pct ?? -Infinity) : ranking === 'losers' ? (a.change_pct ?? Infinity) - (b.change_pct ?? Infinity) : (b.amount ?? b.volume ?? -Infinity) - (a.amount ?? a.volume ?? -Infinity)), [query.data, ranking])
  const compact = (value: number | null) => value == null ? '—' : new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 2 }).format(value)
  return <section className="border border-border bg-surface">
    <div className="flex flex-wrap items-center gap-1 border-b border-border/60 p-3"><h2 className="mr-3 text-sm font-semibold text-foreground">{market === 'hk' ? '港股' : '美股'}市场榜单</h2>{tabs.map((tab) => <button key={tab.key} onClick={() => setRanking(tab.key)} className={`px-3 py-1.5 text-xs ${ranking === tab.key ? 'bg-accent text-white' : 'text-secondary hover:bg-elevated'}`}>{tab.label}</button>)}</div>
    {query.isLoading && <p className="p-6 text-center text-xs text-muted">加载市场行情…</p>}{query.isError && <p className="p-6 text-center text-xs text-danger">行情源暂不可用，请稍后重试</p>}{!query.isLoading && !query.isError && rows.length === 0 && <p className="p-6 text-center text-xs text-muted">当前股票池暂无行情</p>}
    {rows.length > 0 && <div className="overflow-x-auto"><table className="w-full text-xs"><thead><tr className="border-b border-border/50 text-muted"><th className="px-4 py-2 text-left">标的</th><th className="px-3 py-2 text-right">最新价</th><th className="px-3 py-2 text-right">涨跌幅</th><th className="px-4 py-2 text-right">成交/活跃</th></tr></thead><tbody>{rows.map((row) => <tr key={row.symbol} className="border-b border-border/40 hover:bg-elevated/40"><td className="px-4 py-2"><Link className="font-medium text-foreground hover:text-accent" to={`/${market}/${row.symbol}`}>{row.name}</Link><div className="font-mono text-[10px] text-muted">{row.symbol}</div></td><td className="px-3 py-2 text-right font-mono">{row.price?.toFixed(2) ?? '—'}</td><td className={`px-3 py-2 text-right font-mono ${row.change_pct == null ? 'text-muted' : row.change_pct >= 0 ? 'text-bull' : 'text-bear'}`}>{row.change_pct == null ? '—' : `${row.change_pct >= 0 ? '+' : ''}${row.change_pct.toFixed(2)}%`}</td><td className="px-4 py-2 text-right font-mono text-secondary">{compact(row.amount ?? row.volume)}</td></tr>)}</tbody></table></div>}
    <p className="border-t border-border/50 px-4 py-2 text-[11px] text-muted">榜单仅基于当前内置市场池及可用行情，不代表全市场排名。</p>
  </section>
}
