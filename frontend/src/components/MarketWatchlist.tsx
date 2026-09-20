import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Star, Trash2, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

interface MarketWatchlistProps {
  market: 'hk' | 'us'
}

const MARKET_META = {
  hk: { label: '港股', basePath: '/hk', hint: 'HKD · 无涨跌停' },
  us: { label: '美股', basePath: '/us', hint: 'USD · 延迟 15min' },
} as const

function marketForSymbol(symbol: string): 'cn' | 'hk' | 'us' {
  if (/\\.HK$/i.test(symbol)) return 'hk'
  if (/\\.US$/i.test(symbol)) return 'us'
  return 'cn'
}

/**
 * 港美股工作区「我的自选」区块 — P1 跨市场自选落地。
 *
 * 复用全局自选列表 (QK.watchlist), 按 P1 新增的 market 字段过滤出本市场标的;
 * 支持行内移除 + 点击跳详情页。数据源与 A 股自选同一份 watchlist.parquet,
 * 三市场共用, 只是按 market 分市场展示。
 */
export function MarketWatchlist({ market }: MarketWatchlistProps) {
  const meta = MARKET_META[market]
  const qc = useQueryClient()

  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    staleTime: 30_000,
  })

  const remove = useMutation({
    mutationFn: (entry: { symbol: string; market: 'hk' | 'us' }) =>
      api.watchlistRemove(entry.symbol, entry.market),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    },
  })

  const rows = (watchlist.data?.symbols ?? []).filter(
    (s) => (s.market ?? marketForSymbol(s.symbol)) === market,
  )

  return (
    <div className="rounded-lg border border-border bg-surface/60 overflow-hidden">
      {/* 标题区 */}
      <div className="flex items-center gap-2 border-b border-border/50 px-4 py-3">
        <Star className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
        <h3 className="text-sm font-medium text-fg">
          我的{meta.label}自选
          <span className="ml-2 text-xs font-normal text-fg-muted">
            {rows.length} 只 · {meta.hint}
          </span>
        </h3>
        <Link
          to={`${meta.basePath}/watchlist`}
          className="ml-auto text-xs text-accent hover:underline"
          title="打开完整自选列表 (含 A 股)"
        >
          查看全部自选 →
        </Link>
      </div>

      {/* 列表 */}
      <div className="px-2 py-2">
        {watchlist.isLoading && (
          <div className="flex items-center gap-2 px-2 py-3 text-xs text-muted">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            加载中…
          </div>
        )}

        {!watchlist.isLoading && rows.length === 0 && (
          <div className="px-2 py-4 text-xs text-fg-muted">
            还没有{meta.label}自选。
            <span className="text-fg-muted/80">{' '}可在看板榜单点击星标，或进入个股详情页添加。</span>
          </div>
        )}

        {rows.length > 0 && (
          <ul className="space-y-0.5">
            {rows.map((r) => (
              <li
                key={r.symbol}
                className="flex min-h-11 items-center gap-2 rounded px-2 transition-colors hover:bg-elevated/50"
              >
                <Link
                  to={`${meta.basePath}/${r.symbol}`}
                  className="flex min-w-0 flex-1 items-center gap-2 py-2"
                  title={`查看 ${r.symbol} 详情`}
                >
                  <span className="shrink-0 font-mono text-sm text-fg">{r.symbol}</span>
                  <span className="truncate text-xs text-fg-muted">
                    {r.name || '—'}
                  </span>
                </Link>
                <button
                  type="button"
                  onClick={() => remove.mutate({ symbol: r.symbol, market })}
                  disabled={remove.isPending}
                  className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-btn text-muted transition-colors hover:bg-danger/10 hover:text-danger disabled:opacity-50"
                  title={`从自选移除 ${r.symbol}`}
                  aria-label={`从自选移除 ${r.symbol}`}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}