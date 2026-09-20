/**
 * 强度梯队面板 — 港美版"连板梯队"。
 *
 * 港美无涨跌停/连板制度, A 股的连板层级(首板/2板/3板…)在此不可用。
 * 本组件用 **20 日动量档位** 替代:
 *   m25 (>=25%) / m15 (15~25%) / m8 (8~15%) / m3 (3~8%), 动量 <3% 不入档。
 *
 * 数据来源: GET /api/strength_ladder?market=hk&date=...&bands=...
 * 后端 market=cn 返 400 (A 股走连板梯队), 所以本组件只在 hk/us 下渲染。
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Flame, TrendingUp, Loader2, AlertCircle } from 'lucide-react'
import {
  api, STRENGTH_BANDS, STRENGTH_BAND_META,
  type MarketCode, type StrengthBand, type StrengthLadderRow,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { MARKETS } from '@/lib/marketContext'
import { fmtPct, priceColorClass } from '@/lib/format'
import { cn } from '@/lib/cn'

/** 每档最多展示条数 — m3 档可能上百只, 全量渲染会拖垮页面 */
const TOP_N = 20

interface Props {
  market: MarketCode
  /** 交易日 (YYYY-MM-DD); 不传则由后端取该市场最新一日 */
  date?: string | null
}

export function StrengthLadderPanel({ market, date }: Props) {
  const [band, setBand] = useState<StrengthBand>('m25')

  const query = useQuery({
    queryKey: QK.strengthLadder(market, date ?? undefined),
    queryFn: () => api.strengthLadder(market, date ?? undefined),
    staleTime: 5 * 60 * 1000,
  })

  const rows = useMemo(
    () => query.data?.bands?.[band] ?? [],
    [query.data, band],
  )

  // 各档位计数 — 即使当前档为空也要显示其它档有多少只, 便于用户切换
  const counts = useMemo(() => {
    const out: Record<StrengthBand, number> = { m25: 0, m15: 0, m8: 0, m3: 0 }
    for (const b of STRENGTH_BANDS) {
      out[b] = query.data?.bands?.[b]?.length ?? 0
    }
    return out
  }, [query.data])

  const shown = rows.slice(0, TOP_N)
  const meta = MARKETS[market]

  return (
    <div className="rounded-card border border-border bg-surface/80 p-4 backdrop-blur-sm">
      {/* ── 标题行 ── */}
      <div className="mb-3 flex items-center gap-2">
        <Flame className="h-4 w-4 text-accent" />
        <h3 className="text-sm font-semibold text-foreground">强度梯队</h3>
        <span className="text-[11px] text-muted">
          20 日动量档位 · 港美无涨停连板, 以动量层级替代
        </span>
        {query.isFetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
        <span className="ml-auto text-[11px] text-muted">
          {query.data?.date ? `交易日 ${query.data.date}` : meta.label}
          {' · 共 '}
          {query.data?.total_count ?? 0} 只
        </span>
      </div>

      {/* ── 档位切换 ── */}
      <div className="mb-3 flex flex-wrap items-center gap-1.5">
        {STRENGTH_BANDS.map(b => {
          const m = STRENGTH_BAND_META[b]
          const active = band === b
          const n = counts[b]
          return (
            <button
              key={b}
              onClick={() => setBand(b)}
              title={m.desc}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-btn border px-2.5 py-1 text-xs font-medium transition-colors',
                active
                  ? 'border-transparent text-white'
                  : 'border-border bg-base/60 text-secondary hover:text-foreground',
                n === 0 && !active && 'opacity-50',
              )}
              style={active ? { backgroundColor: m.color } : undefined}
            >
              <span
                className="h-1.5 w-1.5 rounded-full"
                style={{ backgroundColor: active ? '#fff' : m.color }}
              />
              {m.label}
              <span className={cn('tabular-nums', active ? 'text-white/80' : 'text-muted')}>
                {n}
              </span>
            </button>
          )
        })}
      </div>

      {/* ── 明细表 ── */}
      {query.isLoading ? (
        <div className="flex items-center justify-center gap-2 py-8 text-xs text-muted">
          <Loader2 className="h-4 w-4 animate-spin" />
          加载中…
        </div>
      ) : query.isError ? (
        <div className="flex items-center gap-2 py-6 text-xs text-muted">
          <AlertCircle className="h-4 w-4 text-warn" />
          梯队数据加载失败 · {String((query.error as Error)?.message || query.error)}
        </div>
      ) : shown.length === 0 ? (
        <div className="py-8 text-center text-xs text-muted">
          {STRENGTH_BAND_META[band].desc} · 该交易日无标的入档
        </div>
      ) : (
        <div className="overflow-hidden rounded-btn border border-border">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-base/60 text-[11px] text-muted">
                <th className="px-3 py-1.5 text-left font-medium">#</th>
                <th className="px-3 py-1.5 text-left font-medium">代码</th>
                <th className="px-3 py-1.5 text-right font-medium">20 日动量</th>
                <th className="px-3 py-1.5 text-right font-medium">最新价</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r, i) => (
                <LadderRow key={r.symbol} row={r} rank={i + 1} />
              ))}
            </tbody>
          </table>
          {rows.length > TOP_N && (
            <div className="border-t border-border bg-base/40 px-3 py-1.5 text-center text-[11px] text-muted">
              仅显示前 {TOP_N} 只 · 该档共 {rows.length} 只
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function LadderRow({ row, rank }: { row: StrengthLadderRow; rank: number }) {
  const momentum = row.momentum_20d
  return (
    <tr className="border-t border-border/60 transition-colors hover:bg-base/40">
      <td className="px-3 py-1.5 tabular-nums text-muted">{rank}</td>
      <td className="px-3 py-1.5">
        <span className="font-medium text-foreground">{row.symbol}</span>
      </td>
      <td className="px-3 py-1.5 text-right">
        <span className={cn('inline-flex items-center gap-1 tabular-nums', priceColorClass(momentum))}>
          <TrendingUp className="h-3 w-3" />
          {fmtPct(momentum)}
        </span>
      </td>
      <td className="px-3 py-1.5 text-right tabular-nums text-secondary">
        {row.last_close != null ? row.last_close.toFixed(2) : '—'}
      </td>
    </tr>
  )
}
