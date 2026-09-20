import { useQuery } from '@tanstack/react-query'
import { Star, Check } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useWatchlistBatchAdd } from '@/lib/useSharedMutations'
import { cn } from '@/lib/cn'

interface MarketWatchlistButtonProps {
  symbol: string
  market?: 'hk' | 'us'
  /** 尺寸: md=详情页带文案按钮 (h-9); sm=列表行内紧凑图标按钮 (h-8, 仍 >24px) */
  size?: 'md' | 'sm'
}

/**
 * 港美股「加自选」按钮 — P1 跨市场自选入口。
 *
 * 复用全局自选查询 (QK.watchlist) 判断已加/未加;
 * 触发 useWatchlistBatchAdd 走 /api/watchlist/batch, 新标的前置插入 + 缓存失效刷新。
 * 星形/对勾 + 文案(或 title) 双通道, 满足无障碍「信息非仅颜色」。
 */
export function MarketWatchlistButton({ symbol, market, size = 'md' }: MarketWatchlistButtonProps) {
  const watchlist = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    staleTime: 30_000,
  })
  const batchAdd = useWatchlistBatchAdd()
  const symbols = watchlist.data?.symbols ?? []
  const inList = symbols.some((s) =>
    s.symbol === symbol && (s.market ?? (symbol.endsWith('.HK') ? 'hk' : symbol.endsWith('.US') ? 'us' : 'cn')) === (market ?? 'cn'),
  )

  const handleAdd = () => {
    batchAdd.mutate({ symbols: [symbol], groupId: undefined, market })
  }

  const label = inList ? '已加自选' : batchAdd.isPending ? '添加中…' : '加自选'
  const tip = inList ? `${symbol} 已加入自选` : `把 ${symbol} 加入自选`

  if (size === 'sm') {
    return (
      <button
        type="button"
        onClick={handleAdd}
        disabled={inList || batchAdd.isPending}
        className={cn(
          'inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-btn border transition-colors',
          inList
            ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-500 cursor-default'
            : 'border-border bg-surface text-muted hover:border-accent/40 hover:text-accent disabled:opacity-50 cursor-pointer',
        )}
        title={tip}
        aria-label={tip}
      >
        {inList
          ? <Check className="h-3.5 w-3.5" aria-hidden="true" />
          : <Star className="h-3.5 w-3.5" aria-hidden="true" />}
      </button>
    )
  }

  return (
    <button
      type="button"
      onClick={handleAdd}
      disabled={inList || batchAdd.isPending}
      className={cn(
        'inline-flex h-9 items-center gap-1.5 rounded-btn border px-3 text-xs font-medium transition-colors',
        inList
          ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-500 cursor-default'
          : 'border-accent/40 bg-accent/10 text-accent hover:bg-accent/20 disabled:opacity-50 cursor-pointer',
      )}
      title={tip}
      aria-label={tip}
    >
      {inList
        ? <Check className="h-3.5 w-3.5" aria-hidden="true" />
        : <Star className="h-3.5 w-3.5" aria-hidden="true" />}
      {label}
    </button>
  )
}