import { AlertTriangle, Gauge, Loader2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { request } from '@/lib/api'
import { priceColorClass } from '@/lib/format'

/**
 * 港美股「实时快照 + 五档盘口」—— 通达信扩展市场(MAC-EX 7727)。
 *
 * 为什么这块能信: 上游一次给齐昨收/均价/换手/内外盘, 且**美股给真五档**。
 * 九章同一页的盘口是 random(), 这里是真 CCD ——— 唯 一例外是港股: 上游对
 * 港股买卖档恒返回 0, payload 会带 depth_available=false + note, 这里就写
 * "上游不返回", 绝不画一排 0 元挂单。
 *
 * 契约: 失败/不可用 → HTTP 200 + {available:false, reason}, 显示 reason, 不编造。
 */

interface Snapshot {
  available?: boolean
  reason?: string
  source?: string
  name?: string | null
  prev_close?: number | null
  open?: number | null
  high?: number | null
  low?: number | null
  last?: number | null
  avg_price?: number | null
  volume?: number | null
  amount?: number | null
  turnover_pct?: number | null
  inside_volume?: number | null
  outside_volume?: number | null
  change?: number | null
  pct?: number | null
  volume_unit?: string | null
  server_time?: string | null
  derived?: string[] | null
  cached?: boolean
}

interface Level {
  price: number | null
  volume: number | null
}

interface Orderbook {
  available?: boolean
  reason?: string
  prev_close?: number | null
  last?: number | null
  bids?: Level[]
  asks?: Level[]
  bid_levels?: number
  ask_levels?: number
  depth_available?: boolean
  total_volume?: number | null
  inside_volume?: number | null
  outside_volume?: number | null
  note?: string | null
  server_time?: string | null
}

/** .HK / .US 后缀 → deep API 的市场段。A股不带后缀, 这里直接不渲染。 */
function deepMarket(symbol: string): 'hk' | 'us' | null {
  const s = symbol.trim().toUpperCase()
  if (s.endsWith('.HK')) return 'hk'
  if (s.endsWith('.US')) return 'us'
  return null
}

function num(v: number | null | undefined, d = 2): string {
  return v == null || Number.isNaN(v) ? '—' : v.toFixed(d)
}

function big(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (a >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return v.toFixed(0)
}

function signedPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function StateLine({ loading, warn, text }: { loading?: boolean; warn?: boolean; text: string }) {
  return (
    <div className={`flex items-start gap-2 py-2 text-[11px] leading-relaxed ${warn ? 'text-amber-600 dark:text-amber-500' : 'text-fg-muted'}`}>
      {loading
        ? <Loader2 className="mt-0.5 h-3 w-3 shrink-0 animate-spin" />
        : warn
          ? <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          : null}
      <span className="min-w-0 break-words">{text}</span>
    </div>
  )
}

function Cell({ label, value, valueClass }: {
  label: string
  value: string
  valueClass?: string
}) {
  return (
    <div className="min-w-0">
      <div className="truncate text-[10px] text-fg-muted">{label}</div>
      <div className={`truncate text-xs font-medium tabular-nums ${valueClass ?? 'text-fg'}`}>
        {value}
      </div>
    </div>
  )
}

export function HKUSQuoteBox({ symbol, className = '' }: {
  symbol: string
  className?: string
}) {
  const mkt = deepMarket(symbol)
  const enabled = mkt != null

  const snap = useQuery({
    queryKey: ['hkus', 'snapshot', symbol],
    queryFn: () => request<Snapshot>(
      `/api/${mkt}/deep/snapshot/${encodeURIComponent(symbol)}`),
    enabled,
    staleTime: 10_000,
    refetchInterval: 30_000,
  })

  const book = useQuery({
    queryKey: ['hkus', 'orderbook', symbol],
    queryFn: () => request<Orderbook>(
      `/api/${mkt}/deep/orderbook/${encodeURIComponent(symbol)}`),
    enabled,
    staleTime: 10_000,
    refetchInterval: 30_000,
  })

  if (!enabled) return null

  const s = snap.data?.available ? snap.data : null
  const b = book.data?.available ? book.data : null

  return (
    <div className={`rounded-lg border border-border bg-surface/60 px-3 py-2 ${className}`}>
      <div className="mb-1.5 flex items-center gap-1.5">
        <Gauge className="h-3.5 w-3.5 text-fg-muted" />
        <h3 className="text-xs font-semibold text-fg">实时行情 / 五档</h3>
        <span className="ml-auto text-[10px] text-fg-muted">MAC-EX · 通达信</span>
      </div>

      {snap.isLoading && <StateLine loading text="加载实时快照…" />}
      {!snap.isLoading && !s && (
        <StateLine warn text={`快照不可用: ${snap.data?.reason ?? snap.error?.message ?? '无返回'}`} />
      )}

      {s && (
        <>
          <div
            className="grid gap-x-3 gap-y-1.5"
            style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(4.5rem, 1fr))' }}
          >
            <Cell label="最新" value={num(s.last)}
                  valueClass={priceColorClass(s.pct ?? null)} />
            <Cell label="涨跌" value={signedPct(s.pct)}
                  valueClass={priceColorClass(s.pct ?? null)} />
            <Cell label="昨收" value={num(s.prev_close)} />
            <Cell label="今开" value={num(s.open)} />
            <Cell label="最高" value={num(s.high)} />
            <Cell label="最低" value={num(s.low)} />
            <Cell label="均价" value={num(s.avg_price)} />
            <Cell label="换手" value={s.turnover_pct != null ? `${num(s.turnover_pct, 4)}%` : '—'} />
            <Cell label="成交量(股)" value={big(s.volume)} />
            <Cell label="成交额" value={big(s.amount)} />
            <Cell label="内盘" value={big(s.inside_volume)} />
            <Cell label="外盘" value={big(s.outside_volume)} />
          </div>
          <p className="mt-1.5 text-[10px] leading-relaxed text-fg-muted">
            快照时间 {s.server_time ?? '—'}（北京时间）
            {s.derived?.includes('turnover_pct') && ' · 换手率由上游千分比换算'}
            {s.name ? ` · ${s.name}` : ''}
          </p>
        </>
      )}

      <div className="mt-2 border-t border-border pt-1.5">
        {book.isLoading && <StateLine loading text="加载五档盘口…" />}
        {!book.isLoading && !b && (
          <StateLine warn text={`盘口不可用: ${book.data?.reason ?? book.error?.message ?? '无返回'}`} />
        )}
        {b && !b.depth_available && (
          <StateLine warn text={b.note ?? '上游不返回买卖档'} />
        )}
        {b && b.depth_available && <DepthRows bids={b.bids ?? []} asks={b.asks ?? []} />}
        {b && (
          <p className="mt-1 text-[10px] text-fg-muted">
            总量 {big(b.total_volume)} 股 · 内盘 {big(b.inside_volume)} / 外盘 {big(b.outside_volume)}
          </p>
        )}
      </div>
    </div>
  )
}

/**
 * 盘口: 卖5→卖1 从上往下、买1→买5 从上往下(与桌面端同序)。
 * **档位为 0 的行不渲染** —— 上游未成交前后面几档就是 0, 画出来等于宣称"有人在 0 元挂单"。
 */
function DepthRows({ bids, asks }: { bids: Level[]; asks: Level[] }) {
  const live = (lv: Level) => (lv.price ?? 0) > 0 && (lv.volume ?? 0) > 0
  const asksLive = asks.filter(live)
  const bidsLive = bids.filter(live)
  return (
    <div className="space-y-0.5 text-[11px]">
      {[...asksLive].reverse().map((lv, i) => (
        <DepthRow key={`a${i}`} label={`卖${asksLive.length - i}`} lv={lv}
                  cls="text-rose-600 dark:text-rose-400" />
      ))}
      {bidsLive.map((lv, i) => (
        <DepthRow key={`b${i}`} label={`买${i + 1}`} lv={lv}
                  cls="text-emerald-600 dark:text-emerald-400" />
      ))}
    </div>
  )
}

function DepthRow({ label, lv, cls }: { label: string; lv: Level; cls: string }) {
  return (
    <div className="grid grid-cols-[2.2rem_1fr_1fr] items-baseline gap-2">
      <span className="text-fg-muted">{label}</span>
      <span className={`text-right font-mono tabular-nums ${cls}`}>{num(lv.price)}</span>
      <span className="text-right font-mono tabular-nums text-fg-secondary">{big(lv.volume)}</span>
    </div>
  )
}
