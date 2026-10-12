/**
 * 周期切换 + 数据能力说明 —— 个股页 K 线区共用。
 *
 * 为什么抽出来: StockDetail(A股) / HKStockAnalysis / USStockAnalysis 三处都要
 * 同一组周期 tab 与同一套"哪些能力不可用、为什么"的说明。抄三份必然漂移,
 * 与 KlineChartTools 抽共用层是同一个道理。
 */
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'

import { cn } from '@/lib/cn'
import { PERIOD_DEFAULT_BARS, type KPeriod } from '@/lib/klinePeriod'
import { MK_PERIODS, type MklinePeriod } from '@/components/StockDailyKChart'

export const K_PERIODS: { label: string; value: KPeriod }[] = [
  { label: '日K', value: 'day' },
  { label: '周K', value: 'week' },
  { label: '月K', value: 'month' },
  { label: '年K', value: 'year' },
]

interface TabsProps {
  value: KPeriod
  onChange: (period: KPeriod, defaultBars: number) => void
  periods?: { label: string; value: KPeriod }[]
  className?: string
}

/** 周期分段控件。周/月/年由日K本地聚合(确定性派生), 不是另打一份数据。 */
export function KPeriodTabs({ value, onChange, periods = K_PERIODS, className }: TabsProps) {
  return (
    <div className={cn('inline-flex items-center rounded border border-border bg-elevated p-0.5', className)}>
      {periods.map(p => (
        <button
          key={p.value}
          type="button"
          onClick={() => onChange(p.value, PERIOD_DEFAULT_BARS[p.value])}
          className={cn('rounded px-2 py-0.5 text-xs transition-colors',
            value === p.value ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
        >
          {p.label}
        </button>
      ))}
    </div>
  )
}

/**
 * 分钟档切换 —— 港美股专用。
 *
 * 为什么单独一组而不是并进 K_PERIODS: 分钟K 是**另一份数据**(通达信扩展市场
 * MAC-EX 7727), 不是日K聚合出来的, 与日/周/月/年不是一个坐标系。混在一组 tabs
 * 里会让人以为"切到 30 分"和"切到周K"是同一种操作。
 *
 * 「关」回到日K链路 —— 分钟K 上限 700 根, 看长周期必须切回去。
 */
export function MinutePeriodTabs({ value, onChange, className }: {
  value: MklinePeriod | null
  onChange: (period: MklinePeriod | null) => void
  className?: string
}) {
  return (
    <div className={cn('inline-flex items-center rounded border border-border bg-elevated p-0.5', className)}>
      <button
        type="button"
        onClick={() => onChange(null)}
        className={cn('rounded px-2 py-0.5 text-xs transition-colors',
          value == null ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
      >
        关
      </button>
      {MK_PERIODS.filter(p => p.value !== 'm1').map(p => (
        <button
          key={p.value}
          type="button"
          onClick={() => onChange(p.value)}
          className={cn('rounded px-2 py-0.5 text-xs transition-colors',
            value === p.value ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
        >
          {p.label}
        </button>
      ))}
    </div>
  )
}

/**
 * 分钟K 的**深度**切换 —— 港美股专用。
 *
 * 两个档位走的是两个不同的上游端点, 画出来的东西也不一样:
 *   - `1 日`: /minute(0x122E) 交易所原生蜡烛, 但上游单次上限 700 根;
 *   - `3/5/10 日`: /minuterange(0x240C 按日循环) 突破 700 根, 代价是开/高/低
 *     由 1 分钟价格聚合而来(收盘价与上游一致, 开高低平均偏差 <0.05%)。
 *
 * 所以这里不是"多取几天"这么简单 —— 选 3 日以上时图上会写明是聚合蜡烛。
 */
export const MINUTE_DEPTHS: { label: string; value: number }[] = [
  { label: '1 日', value: 1 },
  { label: '3 日', value: 3 },
  { label: '5 日', value: 5 },
  { label: '10 日', value: 10 },
]

export function MinuteDepthTabs({ value, onChange, className }: {
  value: number
  onChange: (days: number) => void
  className?: string
}) {
  return (
    <div className={cn('inline-flex items-center rounded border border-border bg-elevated p-0.5', className)}>
      {MINUTE_DEPTHS.map(p => (
        <button
          key={p.value}
          type="button"
          onClick={() => onChange(p.value)}
          className={cn('rounded px-2 py-0.5 text-xs transition-colors',
            value === p.value ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
        >
          {p.label}
        </button>
      ))}
    </div>
  )
}

interface Caps {
  market: string
  items: Record<string, {
    available: boolean
    source?: string | null
    reason?: string
    derivation?: string
    note?: string
  }>
}

/**
 * 能力说明: 把后端判定为「无真源」的能力连同原因显示出来。
 *
 * 不是装饰 —— 复权这项在港美股确实没有真源(fqkline 的 qfq/hfq 与未复权价
 * 逐日相同)。与其让前端留空让用户猜, 不如把"为什么没有"写出来。
 *
 * 分钟K 曾长期归在这一类(依据是腾讯 mkline 对港美股返回 param error), 但
 * 2026-10-12 实测发现那条结论只对腾讯源成立 —— 现在走通达信扩展市场
 * (MAC-EX 7727), 见 MinutePeriodTabs。
 */
export function DeepCapabilityNote({ market }: { market: 'HK' | 'US' }) {
  const prefix = market === 'HK' ? '/api/hk' : '/api/us'
  const { data } = useQuery<Caps>({
    queryKey: ['deep-capabilities', market],
    queryFn: () => fetch(`${prefix}/deep/capabilities`).then(r => r.json()),
    staleTime: 10 * 60 * 1000,
  })

  const unavailable = Object.entries(data?.items ?? {}).filter(([, v]) => !v.available)
  const derived = Object.entries(data?.items ?? {}).filter(
    ([, v]) => v.available && v.derivation === 'aggregated_from_daily')

  if (!data) return null
  if (!unavailable.length && !derived.length) return null

  return (
    <div className="space-y-1 text-xs text-fg-muted">
      {derived.length > 0 && (
        <div className="flex items-start gap-1.5">
          <span className="mt-[1px] text-fg-subtle">ⓘ</span>
          <span>
            {derived.map(([k]) => PERIOD_LABEL[k] ?? k).join(' / ')}
            {' '}由真实日K本地聚合而来, 非交易所原生周期K
          </span>
        </div>
      )}
      {unavailable.length > 0 && (
        <details className="group">
          <summary className="flex items-center gap-1.5 cursor-pointer hover:text-fg-secondary">
            <AlertTriangle className="h-3.5 w-3.5 text-warn" />
            <span>
              {unavailable.map(([k]) => PERIOD_LABEL[k] ?? k).join(' / ')} 不可用
            </span>
            <span className="text-fg-subtle group-open:hidden">（看原因）</span>
          </summary>
          <ul className="mt-1 ml-5 space-y-1 list-disc">
            {unavailable.map(([k, v]) => (
              <li key={k}>
                <span className="text-fg-secondary">{PERIOD_LABEL[k] ?? k}</span>: {v.reason ?? '无真源'}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}

const PERIOD_LABEL: Record<string, string> = {
  day: '日K', week: '周K', month: '月K', quarter: '季K', year: '年K',
  minute: '分钟K', adjratio: '复权系数', index: '指数',
}
