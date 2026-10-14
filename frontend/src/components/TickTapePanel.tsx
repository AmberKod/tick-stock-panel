/**
 * 成交明细(**真逐笔**)面板 —— 通达信标准市 `GetHistoryTransactionDataCmd` 0x0fb5。
 *
 * 与它替掉的旧版(腾讯 3 秒快照)的区别:
 *   旧版一行 = 某个 3 秒窗口内所有成交的**合计**; 这里**一行就是一笔成交**。
 *   不需要 Level-2 —— 面板上不再写"逐笔需付费"。
 *
 * 必须原样摆给用户的四条口径(都由后端随包下发, 前端照抄不另编):
 *   1. 量单位是**手**(1 手 = 100 股), 与日K同口径;
 *   2. 方向是行情商按成交价与盘口比较判定的, **不是交易所披露字段**;
 *   3. 竞价/盘后不再混在"连续竞价"里 —— 09:25 开盘、15:00 收盘撮合标"竞价",
 *      15:00 之后标"盘后"(不计入当日成交量);
 *   4. 上游夹的 vol=0 行情标记行已被后端剔除并计数 —— 剔除数要显示出来,
 *      否则用户会以为"剔除的数据去哪了"。
 *
 * 交叉校验徽章: 末笔盘中成交价必须等于日K收盘价(实测 0.000000% 偏差)。
 * 校验不过就要红字说, 不许绿着灯让人以为一切正常。
 */
import { useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import type { AstockTickCheck, AstockTickSummary, AstockTickTrade } from '@/lib/api'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'

interface Props {
  trades: AstockTickTrade[]
  summary: AstockTickSummary
  crossCheck: AstockTickCheck
  /** 这批成交属于哪个交易日 */
  date: string
  /** 可选交易日列表(升序); 给了才显示日期下拉 */
  dates?: string[]
  name?: string
  tail: number
  loading?: boolean
  onRefresh?: () => void
  onTailChange?: (tail: number) => void
  onDateChange?: (date: string) => void
  unitNote?: string
  directionNote?: string
  afterHoursNote?: string
  className?: string
}

type SideFilter = 'all' | 'buy' | 'sell' | 'auction' | 'big'
/** 大单阈值默认 100 万元 —— A股里单笔百万通常已在当日成交前列 */
const BIG_AMOUNT_DEFAULT = 1_000_000
const MAX_RENDER = 400

const SIDES: Record<AstockTickTrade['side'], { label: string; cls: string }> = {
  buy: { label: '买', cls: 'text-bull' },
  sell: { label: '卖', cls: 'text-bear' },
  auction: { label: '竞价', cls: 'text-amber-600 dark:text-amber-400' },
  unknown: { label: '—', cls: 'text-fg-muted' },
}

const SESSION_MARKS: Record<string, { label: string; cls: string; hint: string }> = {
  open_auction: {
    label: '开', cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
    hint: '09:25 开盘集合竞价撮合',
  },
  close_auction: {
    label: '收', cls: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
    hint: '15:00 收盘集合竞价撮合',
  },
  after_hours: {
    label: '盘后', cls: 'bg-fg/10 text-fg-muted',
    hint: '15:00 之后的成交, 恒等于收盘价(盘后固定价格/大宗), 不计入当日成交量',
  },
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
  trades, summary, crossCheck, date, dates, name, tail,
  loading, onRefresh, onTailChange, onDateChange,
  unitNote, directionNote, afterHoursNote, className,
}: Props) {
  const [side, setSide] = useState<SideFilter>('all')
  const bigThreshold = BIG_AMOUNT_DEFAULT

  const amountOf = (t: AstockTickTrade) => t.vol * 100 * t.price

  const filtered = useMemo(() => {
    return trades.filter(t => {
      if (side === 'buy') return t.side === 'buy'
      if (side === 'sell') return t.side === 'sell'
      if (side === 'auction') return t.session.endsWith('auction')
      if (side === 'big') return amountOf(t) >= bigThreshold
      return true
    })
  }, [trades, side]) // eslint-disable-line react-hooks/exhaustive-deps

  const shown = filtered.slice(0, MAX_RENDER)

  const stats = useMemo(() => {
    let buyVol = 0, sellVol = 0, auctionVol = 0
    let maxVol = 0, maxTime: string | null = null, maxPrice: number | null = null
    for (const t of trades) {
      if (t.side === 'buy') buyVol += t.vol
      else if (t.side === 'sell') sellVol += t.vol
      else if (t.side === 'auction') auctionVol += t.vol
      if (t.vol > maxVol) { maxVol = t.vol; maxTime = t.time; maxPrice = t.price }
    }
    return { buyVol, sellVol, auctionVol, maxVol, maxTime, maxPrice }
  }, [trades])

  const netCls = (stats.buyVol - stats.sellVol) > 0 ? 'text-bull'
    : (stats.buyVol - stats.sellVol) < 0 ? 'text-bear' : 'text-fg-secondary'

  const checkVerified = crossCheck.available && crossCheck.verified

  return (
    <div className={cn('rounded-md border border-border bg-surface p-2', className)}>
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[12px] font-medium text-fg">成交明细</span>
        {name && <span className="text-[11px] text-fg-muted">{name}</span>}
        {/* 粒度徽章: 绿 —— 真逐笔, 一行一笔 */}
        <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-[10px] text-emerald-600 dark:text-emerald-400"
              title="通达信标准市 GetHistoryTransactionDataCmd(0x0fb5) —— 一行就是一笔成交, 不是时间窗合计">
          真逐笔 · 一行一笔
        </span>
        {/* 交叉校验徽章 —— 校验不过就红字, 不假装正常 */}
        {!crossCheck.available ? (
          <span className="rounded bg-fg/10 px-1.5 py-0.5 text-[10px] text-fg-muted"
                title={crossCheck.reason ?? ''}>
            未交叉校验
          </span>
        ) : crossCheck.verified ? (
          <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-[10px] text-emerald-600 dark:text-emerald-400"
                title={`末笔 ${crossCheck.last_trade_time} 成交价 ${crossCheck.last_trade_price} = 腾讯日K收盘价 ${crossCheck.daily_close}`}>
            收盘价咬合 {(crossCheck.close_diff_pct ?? 0).toFixed(4)}%
          </span>
        ) : (
          <span className="rounded bg-red-500/10 px-1.5 py-0.5 text-[10px] text-red-600 dark:text-red-400"
                title={`末笔 ${crossCheck.last_trade_time} @ ${crossCheck.last_trade_price} vs 日K收盘 ${crossCheck.daily_close}`}>
            校验未通过 {(crossCheck.close_diff_pct ?? 0).toFixed(4)}%
          </span>
        )}
        <span className="text-[10px] text-fg-muted">{date}</span>
        <span className="text-[10px] text-fg-muted">
          {trades[0]?.time} ~ {trades[trades.length - 1]?.time}
        </span>

        <span className="ml-auto inline-flex items-center gap-1">
          {dates && dates.length > 0 && onDateChange && (
            <select
              value={date}
              onChange={e => onDateChange(e.target.value)}
              className="rounded border border-border bg-elevated px-1 py-0.5 text-[10px] text-fg-secondary"
              title="交易日。列表来自腾讯日K日历 —— 非交易日不在里面, 选了也拿不到数据"
            >
              {dates.map(d => <option key={d} value={d}>{d}</option>)}
            </select>
          )}
          {onTailChange && (
            <select
              value={tail}
              onChange={e => onTailChange(Number(e.target.value))}
              className="rounded border border-border bg-elevated px-1 py-0.5 text-[10px] text-fg-secondary"
              title="取最近多少**笔**成交。已剔除上游的 vol=0 标记行, 所以笔数是真实成交数"
            >
              {[50, 100, 200, 400, 800].map(n => (
                <option key={n} value={n}>最近 {n} 笔</option>
              ))}
            </select>
          )}
          {onRefresh && (
            <button
              type="button"
              onClick={onRefresh}
              title="重新拉取(后端有 60 秒缓存)"
              className="rounded border border-border p-0.5 text-fg-muted transition-colors hover:text-fg"
            >
              <RefreshCw className={cn('h-3 w-3', loading && 'animate-spin')} />
            </button>
          )}
        </span>
      </div>

      {/* 筛选 —— 只改变"看哪些行", 汇总仍是对取回的整个窗口 */}
      <div className="mb-1.5 flex flex-wrap items-center gap-1">
        {([
          ['all', '全部'],
          ['buy', '仅主动买'],
          ['sell', '仅主动卖'],
          ['auction', '仅竞价'],
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
          {side === 'all' ? `共 ${trades.length} 笔` : `筛出 ${filtered.length} 笔`}
          {shown.length < filtered.length && ` · 只渲染前 ${shown.length} 笔`}
        </span>
      </div>

      {/* 汇总 —— 明确标出是"最近 N 笔窗口", 不是全日 */}
      <div className="mb-1.5 grid grid-cols-2 gap-1 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="成交笔数" value={String(trades.length)}
              hint="已剔除上游夹带的 vol=0 行情标记行, 笔数即真实成交数" />
        <Stat label="成交量(手)" value={fmtBigNum(summary.volume_regular)}
              hint={`盘中(≤15:00)合计 ${summary.volume_regular} 手; 盘后 ${summary.volume_after_hours ?? 0} 手另计`} />
        <Stat label="主动买(手)" value={fmtBigNum(stats.buyVol)} cls="text-bull"
              hint={directionNote} />
        <Stat label="主动卖(手)" value={fmtBigNum(stats.sellVol)} cls="text-bear" />
        <Stat label="净额(买−卖)" value={fmtBigNum(stats.buyVol - stats.sellVol)} cls={netCls}
              hint="竞价撮合不计入买也不计入卖 —— 它不是主动单" />
        <Stat label="最大单" value={`${fmtBigNum(stats.maxVol)} 手`}
              hint={stats.maxTime ? `${stats.maxTime} @ ${stats.maxPrice}` : undefined} />
      </div>

      <div className="max-h-[320px] overflow-auto rounded border border-border">
        <table className="w-full text-[11px]">
          <thead className="sticky top-0 bg-elevated text-fg-muted">
            <tr className="text-left">
              <th className="px-2 py-1 font-normal">时间</th>
              <th className="px-2 py-1 text-right font-normal">成交价</th>
              <th className="px-2 py-1 text-right font-normal">手数</th>
              <th className="px-2 py-1 text-right font-normal">成交额</th>
              <th className="px-2 py-1 text-center font-normal">方向</th>
            </tr>
          </thead>
          <tbody>
            {shown.map(t => {
              const mark = SESSION_MARKS[t.session]
              return (
                <tr key={t.seq} className="border-t border-border/50">
                  <td className="px-2 py-0.5 font-mono text-fg-secondary">
                    {t.time}
                    {mark && (
                      <span className={cn('ml-1 rounded px-1 text-[9.5px]', mark.cls)} title={mark.hint}>
                        {mark.label}
                      </span>
                    )}
                  </td>
                  <td className="px-2 py-0.5 text-right font-mono text-fg">{t.price.toFixed(2)}</td>
                  <td className="px-2 py-0.5 text-right font-mono text-fg-secondary">{t.vol}</td>
                  <td className="px-2 py-0.5 text-right font-mono text-fg-secondary">
                    {fmtBigNum(amountOf(t))}
                  </td>
                  <td className={cn('px-2 py-0.5 text-center font-mono', SIDES[t.side].cls)}>
                    {SIDES[t.side].label}
                  </td>
                </tr>
              )
            })}
            {shown.length === 0 && (
              <tr>
                <td colSpan={5} className="px-2 py-3 text-center text-fg-muted">
                  当前筛选下没有成交
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* 口径说明 —— 这是**真数据**但有判定边界, 与筹码那种"估算"警示是两种性质 */}
      <div className="mt-1.5 rounded border border-border bg-elevated/50 px-2 py-1.5 text-[10.5px] leading-relaxed text-fg-muted">
        <div>
          <span className="text-fg-secondary">来源</span>：通达信标准市 TCP 7709（source: tdx-tick），
          一行 = <strong className="text-fg-secondary">一笔成交</strong>；时间精度只到分钟，同一分钟内沿用上游顺序。
          {unitNote && <span className="ml-0.5">{unitNote}</span>}
        </div>
        <div className="mt-0.5">
          <span className="text-fg-secondary">方向</span>：{directionNote ?? '买卖方向由行情商判定, 不是交易所披露字段。'}
        </div>
        <div className="mt-0.5">
          <span className="text-fg-secondary">盘后</span>：{afterHoursNote ?? '15:00 之后的成交不计入当日成交量。'}
        </div>
        <div className="mt-0.5">
          <span className="text-fg-secondary">清洗</span>：上游夹了 {summary.marker_rows_dropped} 行
          vol=0 的行情标记行（非成交），已剔除；本次取回的 {summary.upstream_rows} 行里有
          {' '}{trades.length} 笔真实成交
          {summary.after_hours_count > 0 && `（含盘后 ${summary.after_hours_count} 笔）`}。
        </div>
        <div className="mt-0.5">
          <span className="text-fg-secondary">口径</span>：上方汇总只对
          <strong className="text-fg-secondary">最近 {tail} 笔这一窗口</strong>负责，不是全日数据；
          {checkVerified && ` 每天全量逐笔的量会与日K成交量对账（两样本 0.000000%）。`}
        </div>
      </div>
    </div>
  )
}
