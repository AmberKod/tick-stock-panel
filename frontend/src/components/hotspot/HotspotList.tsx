import { Flame } from 'lucide-react'
import type { HotspotSummaryRow } from '@/lib/api'
import { fmtPct, priceColorClass } from '@/lib/format'
import { qualityOf, stageClass, stateLabel } from './labels'

interface HotspotListProps {
  items: HotspotSummaryRow[]
  loading?: boolean
  selectedTopic?: string | null
  onSelect: (topic: string) => void
}

/** 0-100 分数条; null 显示 '—' 不画条 */
function ScoreBar({ value, tone }: { value: number | null | undefined; tone: 'accent' | 'bull' | 'danger' }) {
  if (value == null) return <span className="text-muted">—</span>
  const pct = Math.max(0, Math.min(100, Number(value)))
  const barClass = tone === 'bull' ? 'bg-bull' : tone === 'danger' ? 'bg-danger' : 'bg-accent'
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-1 w-10 overflow-hidden rounded-full bg-elevated">
        <span className={`block h-full ${barClass}`} style={{ width: `${pct}%` }} />
      </span>
      <span className="w-8 text-right font-mono text-[11px] text-secondary">{pct.toFixed(0)}</span>
    </span>
  )
}

/**
 * 热点主题列表 — 一行一个 topic。
 *
 * 展示口径与后端 HotspotSummary 对齐: 阶段/热度三维度/领涨股/数据质量徽标;
 * 缺失字段走 quality 徽标 + title 明细, 不在列表里用占位数字冒充真实值。
 */
export function HotspotList({ items, loading, selectedTopic, onSelect }: HotspotListProps) {
  if (loading && items.length === 0) {
    return <div className="py-10 text-center text-sm text-muted">热点加载中…</div>
  }
  if (items.length === 0) {
    return (
      <div className="rounded-card bg-elevated p-6 text-center text-sm text-muted">
        暂无热点主题。可点击右上角「同步」重新拉取(A 股东财概念/行业板块,港美本地行业聚合)。
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-card border border-border bg-surface">
      <div className="grid grid-cols-[2.5rem_minmax(9rem,1.6fr)_5rem_5.5rem_6rem_6rem_6rem_minmax(10rem,1.4fr)_6.5rem] items-center gap-2 border-b border-border bg-elevated/60 px-3 py-2 text-[11px] font-medium text-muted">
        <span>#</span>
        <span>主题</span>
        <span>阶段</span>
        <span className="text-right">涨跌幅</span>
        <span>热度</span>
        <span>趋势</span>
        <span>持续</span>
        <span>领涨股</span>
        <span className="text-right">数据质量</span>
      </div>
      <div className="max-h-[calc(100vh-20rem)] divide-y divide-border/60 overflow-auto">
        {items.map((item, idx) => {
          const quality = qualityOf(item.quality_status)
          const state = stateLabel(item.state)
          const active = item.topic === selectedTopic
          return (
            <button
              key={`${item.topic}-${idx}`}
              type="button"
              onClick={() => onSelect(item.topic)}
              aria-current={active ? 'true' : undefined}
              className={`grid w-full grid-cols-[2.5rem_minmax(9rem,1.6fr)_5rem_5.5rem_6rem_6rem_6rem_minmax(10rem,1.4fr)_6.5rem] items-center gap-2 px-3 py-2 text-left text-xs transition-colors ${
                active ? 'bg-accent/10' : 'hover:bg-elevated'
              }`}
            >
              <span className="font-mono text-[11px] text-muted">{item.rank ?? idx + 1}</span>
              <span className="min-w-0">
                <span className="flex items-center gap-1">
                  <Flame className="h-3 w-3 shrink-0 text-accent" />
                  <span className="truncate font-medium text-foreground">{item.name || item.topic}</span>
                </span>
                <span className="mt-0.5 block truncate text-[10px] text-muted">
                  {[item.topic_date ? `快照 ${item.topic_date}` : null, state, `样本 ${item.sample_stock_count}`]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
              <span>
                <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] ${stageClass(item.stage)}`}>
                  {item.stage || '—'}
                </span>
              </span>
              <span className={`text-right font-mono ${priceColorClass(item.change_pct)}`}>
                {fmtPct(item.change_pct)}
              </span>
              <ScoreBar value={item.heat_score} tone="accent" />
              <ScoreBar value={item.trend_score} tone="bull" />
              <ScoreBar value={item.persistence_score} tone="accent" />
              <span className="min-w-0 truncate text-secondary">
                {item.leaders?.length
                  ? item.leaders.slice(0, 3).join(' / ')
                  : item.leader_stocks?.length
                    ? item.leader_stocks.slice(0, 3).map(s => s.name || s.code).join(' / ')
                    : '—'}
              </span>
              <span className="text-right">
                <span
                  className={`inline-block rounded px-1.5 py-0.5 text-[10px] ${quality.className}`}
                  title={[
                    item.missing_fields?.length ? `缺失字段: ${item.missing_fields.join(', ')}` : null,
                    item.source_errors?.length ? `源错误: ${item.source_errors.join('; ')}` : null,
                    item.stale ? `缓存 ${item.stale_age_hours ?? '?'} 小时前` : null,
                  ]
                    .filter(Boolean)
                    .join('\n') || undefined}
                >
                  {quality.text}
                </span>
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
