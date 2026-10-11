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
 * 不是装饰 —— 分钟K / 复权这两项在港美股确实没有真源(腾讯 mkline 返回
 * param error; fqkline 的 qfq/hfq 与未复权价逐日相同)。与其让前端留空让用户
 * 猜, 不如把"为什么没有"写出来。
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
