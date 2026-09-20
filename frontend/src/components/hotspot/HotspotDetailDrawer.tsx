import { useQuery } from '@tanstack/react-query'
import { Loader2, X } from 'lucide-react'
import { api, type HotspotStockRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtBigNum, fmtPct, priceColorClass } from '@/lib/format'
import { Modal } from '@/components/Modal'
import { qualityOf, roleClass, stageClass, stateLabel } from './labels'

interface HotspotDetailDrawerProps {
  topic: string | null
  market: 'cn' | 'hk' | 'us'
  onClose: () => void
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-btn bg-elevated px-2 py-1.5">
      <div className="text-[10px] text-muted">{label}</div>
      <div className="font-mono text-xs text-foreground">{value}</div>
    </div>
  )
}

function fmtScore(v: number | null | undefined) {
  return v == null ? '—' : Number(v).toFixed(1)
}

/**
 * 热点详情抽屉 — 主题评分 + 成分股表。
 *
 * 字段缺失显示 '—' (后端对不可得字段显式返回 null, 前端不做任何推断)。
 * 涨停只认后端 is_limit_up, 不从涨幅推断。
 */
export function HotspotDetailDrawer({ topic, market, onClose }: HotspotDetailDrawerProps) {
  const query = useQuery({
    queryKey: QK.hotspotDetail(market, topic ?? ''),
    queryFn: () => api.hotspotDetail(topic as string, market),
    enabled: !!topic,
  })

  const detail = query.data
  const summary = detail?.summary
  const stocks: HotspotStockRow[] = detail?.stocks ?? []
  const quality = qualityOf(detail?.quality_status ?? summary?.quality_status)

  return (
    <Modal
      onClose={onClose}
      ariaLabel={topic ? `热点详情 ${topic}` : '热点详情'}
      overlayClassName="fixed inset-0 z-50 flex justify-end bg-black/50 backdrop-blur-sm"
      panelClassName="flex h-full w-[min(46rem,96vw)] flex-col border-l border-border bg-surface shadow-xl"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-foreground">
            {detail?.name || topic || '热点详情'}
          </h2>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
            {summary?.stage && (
              <span className={`rounded px-1.5 py-0.5 ${stageClass(summary.stage)}`}>{summary.stage}</span>
            )}
            {stateLabel(summary?.state) && <span>{stateLabel(summary?.state)}</span>}
            <span>样本 {summary?.sample_stock_count ?? 0}</span>
            <span>· 成分 {detail?.stock_count ?? stocks.length}</span>
            <span>· 来源 {detail?.provider_used || detail?.provider || '—'}</span>
            {summary?.stale && <span>· 缓存 {summary.stale_age_hours ?? '?'}h</span>}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭详情"
          className="rounded-btn p-1.5 text-muted transition-colors hover:bg-elevated hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 overflow-auto px-4 py-3">
        {query.isLoading && (
          <div className="flex items-center justify-center gap-2 py-10 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> 详情加载中…
          </div>
        )}
        {query.isError && <div className="py-6 text-sm text-danger">详情加载失败,请稍后重试。</div>}

        {detail && (
          <>
            <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Metric label="热度" value={fmtScore(summary?.heat_score)} />
              <Metric label="趋势" value={fmtScore(summary?.trend_score)} />
              <Metric label="持续性" value={fmtScore(summary?.persistence_score)} />
              <Metric label="降温" value={fmtScore(summary?.cooling_score)} />
            </div>

            <div className="mb-3 flex flex-wrap items-center gap-2 text-[11px]">
              <span className={`rounded px-1.5 py-0.5 ${quality.className}`}>{quality.text}</span>
              {!!detail.missing_fields?.length && (
                <span className="text-muted">缺失字段: {detail.missing_fields.join(' / ')}</span>
              )}
              {!!detail.source_errors?.length && (
                <span className="text-danger">源错误: {detail.source_errors.join('; ')}</span>
              )}
            </div>

            {!!detail.route?.length && (
              <div className="mb-3 rounded-card bg-elevated p-2 text-[11px] text-secondary">
                <div className="mb-1 text-muted">关联链路</div>
                <div className="flex flex-wrap gap-1">
                  {detail.route.map((node, i) => (
                    <span key={i} className="rounded bg-surface px-1.5 py-0.5">
                      {String((node as any)?.name ?? (node as any)?.topic ?? JSON.stringify(node))}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div className="overflow-hidden rounded-card border border-border">
              <div className="grid grid-cols-[minmax(6rem,1.3fr)_4rem_4.5rem_4.5rem_4.5rem_5rem_5rem] items-center gap-2 border-b border-border bg-elevated/60 px-3 py-2 text-[11px] font-medium text-muted">
                <span>成分股</span>
                <span className="text-right">涨跌幅</span>
                <span className="text-right">成交额</span>
                <span className="text-right">换手</span>
                <span className="text-right">量比</span>
                <span className="text-right">评分</span>
                <span className="text-right">角色</span>
              </div>
              <div className="divide-y divide-border/60">
                {stocks.map((s, i) => (
                  <div
                    key={`${s.code}-${i}`}
                    className="grid grid-cols-[minmax(6rem,1.3fr)_4rem_4.5rem_4.5rem_4.5rem_5rem_5rem] items-center gap-2 px-3 py-1.5 text-xs"
                  >
                    <span className="min-w-0">
                      <span className="flex items-center gap-1">
                        <span className="truncate text-foreground">{s.name || s.code}</span>
                        {s.is_limit_up && <span className="rounded bg-bull/15 px-1 text-[10px] text-bull">涨停</span>}
                      </span>
                      <span className="block truncate font-mono text-[10px] text-muted">{s.code}</span>
                    </span>
                    <span className={`text-right font-mono ${priceColorClass(s.change_pct)}`}>{fmtPct(s.change_pct)}</span>
                    <span className="text-right font-mono text-secondary">{fmtBigNum(s.amount)}</span>
                    <span className="text-right font-mono text-secondary">
                      {s.turnover_rate == null ? '—' : `${(Number(s.turnover_rate) * 100).toFixed(2)}%`}
                    </span>
                    <span className="text-right font-mono text-secondary">
                      {s.volume_ratio == null ? '—' : Number(s.volume_ratio).toFixed(2)}
                    </span>
                    <span className="text-right font-mono text-secondary">{fmtScore(s.hot_stock_score)}</span>
                    <span className="text-right">
                      <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] ${roleClass(s.role)}`}>
                        {s.role || '—'}
                      </span>
                    </span>
                  </div>
                ))}
                {stocks.length === 0 && (
                  <div className="px-3 py-6 text-center text-xs text-muted">暂无成分股数据。</div>
                )}
              </div>
            </div>

            {!!detail.timeline?.length && (
              <div className="mt-3 rounded-card bg-elevated p-2">
                <div className="mb-1 text-[11px] text-muted">热度时间线</div>
                <div className="space-y-1">
                  {detail.timeline.map((row, i) => (
                    <div key={i} className="flex items-center gap-2 text-[11px] text-secondary">
                      <span className="w-20 font-mono text-muted">
                        {String((row as any)?.date ?? (row as any)?.topic_date ?? '')}
                      </span>
                      <span className="font-mono">{fmtScore((row as any)?.heat_score)}</span>
                      {!!(row as any)?.stage && (
                        <span className={`rounded px-1.5 py-0.5 text-[10px] ${stageClass((row as any)?.stage)}`}>
                          {String((row as any)?.stage)}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}
