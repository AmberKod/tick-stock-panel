/**
 * 成交明细(成交快照流水)面板。
 *
 * ⚠️ **这里显示的是每 3 秒一行的成交快照, 不是逐笔成交**。
 * 免费公开源(腾讯 stock.gtimg.cn)只到这个粒度 —— 真正的逐笔要 Level-2 付费行情。
 * 面板上必须写清这一点: 标着"逐笔"卖 3 秒快照比不做这个功能更糟, 因为它看起来像 AI 想要的那种数据。
 *
 * 同样必须写清的两条口径:
 *   1. 买卖方向(B/S/M)是行情商按成交价与当时盘口的比较判定的, **不是交易所披露字段**;
 *      M 表示判不出来, 汇总时单独一列, 不塞进买或卖充数。
 *   2. 底部汇总只对**当前取回来的这一窗口**负责 —— 不是全日口径。
 *      想看全日就把 tail 调大, 但那会带来更多次上游请求(接口只能从开盘往后翻页)。
 */
import { useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import type { TickRow } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'

interface Summary {
  count: number
  amount: number
  shares: number
  buy_amount: number
  buy_shares: number
  sell_amount: number
  sell_shares: number
  neutral_amount: number
  neutral_shares: number
  net_amount: number
  max_amount: number
  max_time: string | null
  max_price: number | null
  from: string
  to: string
}

interface Props {
  rows: TickRow[]
  summary: Summary
  /** 这批成交属于哪个交易日(非交易日时是最近一个交易日) */
  date: string | null
  name?: string
  /** 已请求的行数上限 —— 汇总按这个窗口算, 要如实告诉用户 */
  tail: number
  loading?: boolean
  onRefresh?: () => void
  onTailChange?: (tail: number) => void
  className?: string
}

type SideFilter = 'all' | 'buy' | 'sell' | 'big'
/** 大单阈值默认 100 万元 —— A股里"单笔百万"通常已在当日成交的前列 */
const BIG_AMOUNT_DEFAULT = 1_000_000

const SIDES: Record<TickRow['side'], { label: string; cls: string }> = {
  B: { label: '买', cls: 'text-bull' },
  S: { label: '卖', cls: 'text-bear' },
  M: { label: '中', cls: 'text-fg-muted' },
}

const SESSIONS: Record<string, string> = {
  open: '开盘竞价',
  close: '收盘竞价',
  after: '盘后',
}

function Stat({ label, value, hint, cls }: { label: string; value: string; hint?: string; cls?: string }) {
  return (
    <div className="rounded border border-border bg-elevated px-2 py-1" title={hint}>
      <div className="text-[10px] leading-tight text-fg-muted">{label}</div>
      <div className={cn('font-mono text-[12px] leading-tight', cls ?? 'text-fg')}>{value}</div>
    </div>
  )
}

export function TickTapePanel({
  rows, summary, date, name, tail, loading, onRefresh, onTailChange, className,
}: Props) {
  const [side, setSide] = useState<SideFilter>('all')
  const [bigThreshold] = useState(BIG_AMOUNT_DEFAULT)

  const filtered = useMemo(() => {
    return rows.filter(r => {
      if (side === 'buy') return r.side === 'B'
      if (side === 'sell') return r.side === 'S'
      if (side === 'big') return r.amount >= bigThreshold
      return true
    })
  }, [rows, side, bigThreshold])

  const shown = filtered.slice(0, 200)   // 一次渲染 200 行够翻了, 更多交给筛选

  const netCls = summary.net_amount > 0 ? 'text-bull'
    : summary.net_amount < 0 ? 'text-bear' : 'text-fg-secondary'

  return (
    <div className={cn('rounded-md border border-border bg-surface p-2', className)}>
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[12px] font-medium text-fg">成交明细</span>
        {name && <span className="text-[11px] text-fg-muted">{name}</span>}
        {/* 粒度徽章 —— 与"估算"那种橙色徽章区分: 这是真实数据, 但粒度有上限 */}
        <span className="rounded bg-sky-400/10 px-1.5 py-0.5 text-[10px] text-sky-500"
              title="腾讯公开接口每 3 秒发布一行成交合计, 这一行是该时间窗内所有成交的合计。真正的逐笔(Level-2)是付费数据, 这里拿不到, 也不假装拿到了">
          3 秒快照 · 非逐笔
        </span>
        {date && <span className="text-[10px] text-fg-muted">{date}</span>}
        <span className="text-[10px] text-fg-muted">
          {summary.from} ~ {summary.to}
        </span>

        <span className="ml-auto inline-flex items-center gap-1">
          {onTailChange && (
            <select
              value={tail}
              onChange={e => onTailChange(Number(e.target.value))}
              className="rounded border border-border bg-elevated px-1 py-0.5 text-[10px] text-fg-secondary"
              title="取最近多少行。行数越多, 后端要翻的页越多 —— 汇总是对这个窗口算的"
            >
              {[100, 200, 500, 1000, 3000].map(n => (
                <option key={n} value={n}>最近 {n} 行</option>
              ))}
            </select>
          )}
          {onRefresh && (
            <button
              type="button"
              onClick={onRefresh}
              title="重新拉取(后端有 20 秒缓存)"
              className="rounded border border-border p-0.5 text-fg-muted transition-colors hover:text-fg"
            >
              <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} />
            </button>
          )}
        </span>
      </div>

      {/* 筛选 —— 只改变"看哪些行", 底部汇总仍是对取回的整个窗口(另有说明) */}
      <div className="mb-1.5 flex flex-wrap items-center gap-1">
        {([
          ['all', '全部'],
          ['buy', '仅主动买'],
          ['sell', '仅主动卖'],
          ['big', `大单 ≥${bigThreshold / 1e4}万`],
        ] as const).map(([v, label]) => (
          <button
            key={v}
            type="button"
            onClick={() => setSide(v)}
            className={cn('rounded border px-1.5 py-0.5 text-[10.5px] transition-colors',
              side === v
                ? 'border-sky-400/40 bg-sky-400/10 text-sky-500'
                : 'border-border text-fg-muted hover:text-fg-secondary')}
          >
            {label}
          </button>
        ))}
        <span className="text-[10px] text-fg-muted">
          {side === 'all' ? `共 ${rows.length} 行` : `筛出 ${filtered.length} 行`}
          {shown.length < filtered.length && ` · 只渲染前 ${shown.length} 行`}
        </span>
      </div>

      {/* 汇总 —— 明确标出是"这 ${tail} 行窗口", 不是全日 */}
      <div className="mb-1.5 grid grid-cols-2 gap-1 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="成交笔数(行)" value={String(summary.count)} hint="每 3 秒一行, 行数 ≠ 成交笔数" />
        <Stat label="成交额" value={fmtBigNum(summary.amount)} />
        <Stat label="主动买额" value={fmtBigNum(summary.buy_amount)} cls="text-bull"
              hint="行情商判定为主动买的行(B)。方向不是交易所披露字段" />
        <Stat label="主动卖额" value={fmtBigNum(summary.sell_amount)} cls="text-bear" />
        <Stat label="净额(买−卖)" value={fmtBigNum(summary.net_amount)} cls={netCls}
              hint="中性行(M, 判不出方向)不计入买也不计入卖" />
        <Stat label="最大单" value={fmtBigNum(summary.max_amount)}
              hint={summary.max_time ? `${summary.max_time} @ ${summary.max_price}` : undefined} />
      </div>

      <div className="max-h-[320px] overflow-auto rounded border border-border">
        <table className="w-full text-[11px]">
          <thead className="sticky top-0 bg-elevated text-fg-muted">
            <tr className="text-left">
              <th className="px-2 py-1 font-normal">时间</th>
              <th className="px-2 py-1 text-right font-normal">成交价</th>
              <th className="px-2 py-1 text-right font-normal">涨跌</th>
              <th className="px-2 py-1 text-right font-normal">手数</th>
              <th className="px-2 py-1 text-right font-normal">成交额</th>
              <th className="px-2 py-1 text-center font-normal">方向</th>
            </tr>
          </thead>
          <tbody>
            {shown.map(r => (
              <tr key={r.seq} className="border-t border-border/50">
                <td className="px-2 py-0.5 font-mono text-fg-secondary">
                  {r.time}
                  {r.auction && (
                    <span className="ml-1 rounded bg-fg/10 px-1 text-[9.5px] text-fg-muted"
                          title={SESSIONS[r.auction] ?? ''}>
                      {SESSIONS[r.auction] ?? r.auction}
                    </span>
                  )}
                </td>
                <td className="px-2 py-0.5 text-right font-mono text-fg">{r.price.toFixed(2)}</td>
                <td className={cn('px-2 py-0.5 text-right font-mono',
                  (r.change ?? 0) > 0 ? 'text-bull' : (r.change ?? 0) < 0 ? 'text-bear' : 'text-fg-muted')}>
                  {r.change == null ? '—' : `${r.change > 0 ? '+' : ''}${r.change.toFixed(2)}`}
                </td>
                <td className="px-2 py-0.5 text-right font-mono text-fg-secondary">
                  {r.lots.toFixed(0)}
                </td>
                <td className="px-2 py-0.5 text-right font-mono text-fg-secondary">
                  {fmtBigNum(r.amount)}
                </td>
                <td className={cn('px-2 py-0.5 text-center font-mono', SIDES[r.side].cls)}>
                  {SIDES[r.side].label}
                </td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr>
                <td colSpan={6} className="px-2 py-3 text-center text-fg-muted">
                  当前筛选下没有成交行
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* 口径说明 —— 与筹码那种"估算"警示不同, 这里是**真数据但有粒度/判定边界** */}
      <div className="mt-1.5 rounded border border-border bg-elevated/50 px-2 py-1.5 text-[10.5px] leading-relaxed text-fg-muted">
        <div>
          <span className="text-fg-secondary">来源</span>：腾讯财经公开接口（source: live-tencent），
          每 3 秒一行 —— 一行是该时间窗内所有成交的<strong className="text-fg-secondary">合计</strong>，不是单笔成交；
          <strong className="text-fg-secondary">逐笔</strong>（Level-2）是付费数据，这里拿不到也不假装拿到。
        </div>
        <div className="mt-0.5">
          <span className="text-fg-secondary">方向</span>：B/S/M 由行情商按成交价与当时盘口比较判定，
          <strong className="text-fg-secondary">不是交易所披露字段</strong>；M = 判不出方向，汇总时既不算买也不算卖。
        </div>
        <div className="mt-0.5">
          <span className="text-fg-secondary">口径</span>：手数由「成交额 ÷ 成交价」算出（原始成交量列的单位随标的时而手、时而股，
          直接用会差 100 倍）；上方汇总只对
          <strong className="text-fg-secondary">最近 {tail} 行这一窗口</strong>负责，不是全日数据。
        </div>
      </div>
    </div>
  )
}
