import { AlertTriangle, Layers, Loader2 } from 'lucide-react'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { request } from '@/lib/api'
import { priceColorClass } from '@/lib/format'

/**
 * 港美股「所属板块 + 同业联动」—— 通达信扩展市场(MAC-EX 7727)。
 *
 * 这块是九章没有的维度: 一只票归到哪些行业/概念板块(00700 有 23 条、AAPL 有 17 条),
 * 点进去直接看**同板块成员**的实时报价(涨跌/量比/市值/PE/股息率)。
 *
 * 单位已在后端核对并换算, 这里只负责展示:
 * - total_shares 由上游**万股**换算成股; market_cap 是绝对金额; dividend_yield 是百分数。
 * 契约不变: 失败/不可用 → HTTP 200 + {available:false, reason}, 显示 reason, 不编造。
 */

interface BoardRow {
  board_code: string | null
  board_name: string | null
  close: number | null
  pre_close: number | null
  pct: number | null
}

interface MemberRow {
  code: string | null
  name: string | null
  close: number | null
  pre_close: number | null
  pct: number | null
  volume: number | null
  amount: number | null
  vol_ratio: number | null
  market_cap: number | null
  total_shares: number | null
  eps: number | null
  pe_dynamic: number | null
  pe_ttm: number | null
  dividend_yield: number | null
  net_assets: number | null
}

interface Resp<T> {
  available: boolean
  source?: string
  reason?: string
  boards?: T
  members?: T
  note?: string | null
}

function deepMarket(symbol: string): 'hk' | 'us' | null {
  const s = symbol.trim().toUpperCase()
  return s.endsWith('.HK') ? 'hk' : s.endsWith('.US') ? 'us' : null
}

function num(v: number | null | undefined, d = 2): string {
  return v == null || Number.isNaN(v) ? '—' : v.toFixed(d)
}

function pctText(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

/** 金额用亿/万亿缩写 —— 成员市值动辄几百亿到万亿, 原样打印读不出来。 */
function big(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e12) return `${(v / 1e12).toFixed(2)}万亿`
  if (a >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (a >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return v.toFixed(0)
}

function StateLine({ loading, warn, text }: { loading?: boolean; warn?: boolean; text: string }) {
  return (
    <div className={`flex items-start gap-2 py-3 text-xs leading-relaxed ${warn ? 'text-amber-600 dark:text-amber-500' : 'text-fg-muted'}`}>
      {loading ? <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
        : warn ? <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : null}
      <span className="min-w-0 break-words">{text}</span>
    </div>
  )
}

export function HKUSBoardsPanel({ symbol, className = '' }: {
  symbol: string
  className?: string
}) {
  const mkt = deepMarket(symbol)
  const [active, setActive] = useState<string | null>(null)

  const boards = useQuery({
    queryKey: ['hkus', 'boards', symbol],
    queryFn: () => request<Resp<BoardRow[]>>(
      `/api/${mkt}/deep/boards/${encodeURIComponent(symbol)}`),
    enabled: mkt != null,
    staleTime: 120_000,
  })

  const rows = boards.data?.available ? boards.data.boards ?? [] : []
  const selected = active ?? rows[0]?.board_code ?? null

  const members = useQuery({
    queryKey: ['hkus', 'board-members', selected],
    queryFn: () => request<Resp<MemberRow[]>>(
      `/api/${mkt}/deep/board/${encodeURIComponent(String(selected))}/members?limit=50`),
    enabled: mkt != null && !!selected,
    staleTime: 60_000,
  })

  if (mkt == null) return null

  return (
    <section className={`rounded-lg border border-border bg-surface p-3 ${className}`}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Layers className="h-3.5 w-3.5 text-fg-muted" />
        <h3 className="text-sm font-semibold text-fg">所属板块 / 同业联动</h3>
        <span className="text-xs text-fg-muted">MAC-EX · 通达信扩展市场</span>
        <span className="ml-auto text-[11px] text-fg-muted">
          {rows.length ? `${rows.length} 个板块` : ''}
        </span>
      </div>

      {boards.isLoading && <StateLine loading text="加载所属板块…" />}
      {!boards.isLoading && !boards.data?.available && (
        <StateLine warn text={`不可用: ${boards.data?.reason ?? boards.error?.message ?? '无返回'}`} />
      )}

      {rows.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {rows.map(b => (
            <button
              key={b.board_code ?? Math.random()}
              type="button"
              onClick={() => setActive(b.board_code)}
              className={`rounded border px-2 py-1 text-[11px] transition-colors ${
                b.board_code === selected
                  ? 'border-fg-muted bg-elevated text-fg'
                  : 'border-border text-fg-muted hover:text-fg-secondary'
              }`}
              title={b.board_name ?? undefined}
            >
              <span className="mr-1">{b.board_name ?? b.board_code ?? '—'}</span>
              <span className={`font-mono tabular-nums ${priceColorClass(b.pct)}`}>
                {pctText(b.pct)}
              </span>
            </button>
          ))}
        </div>
      )}

      {selected && (
        members.isLoading
          ? <StateLine loading text="加载同业成分股…" />
          : !members.data?.available
            ? <StateLine warn text={`成分股不可用: ${members.data?.reason ?? members.error?.message ?? '无返回'}`} />
            : (members.data.members ?? []).length === 0
              ? <StateLine text="该板块当前没有返回成分股" />
              : <MembersTable rows={members.data.members ?? []} market={mkt} />
      )}
    </section>
  )
}

function MembersTable({ rows, market }: { rows: MemberRow[]; market: 'hk' | 'us' }) {
  const suffix = market === 'hk' ? '.HK' : '.US'
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-fg-muted">
            <th className="py-1 pr-2 text-left font-normal">名称</th>
            <th className="py-1 pr-2 text-right font-normal">最新</th>
            <th className="py-1 pr-2 text-right font-normal">涨跌%</th>
            <th className="py-1 pr-2 text-right font-normal">量比</th>
            <th className="py-1 pr-2 text-right font-normal">成交额</th>
            <th className="py-1 pr-2 text-right font-normal">市值</th>
            <th className="py-1 pr-2 text-right font-normal">PE(动)</th>
            <th className="py-1 pr-2 text-right font-normal">股息率%</th>
            <th className="py-1 text-right font-normal">每股净资产</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.code ?? 'm'}-${i}`} className="border-t border-border/60">
              <td className="py-1 pr-2">
                {r.code
                  ? <Link
                      to={`/stock/${encodeURIComponent(`${r.code}${suffix}`)}`}
                      className="text-fg hover:underline"
                    >{r.name ?? r.code}</Link>
                  : <span>{r.name ?? '—'}</span>}
                <span className="ml-1 font-mono text-[10px] text-fg-muted">{r.code}</span>
              </td>
              <td className="py-1 pr-2 text-right font-mono text-xs">{num(r.close)}</td>
              <td className={`py-1 pr-2 text-right font-mono text-xs ${priceColorClass(r.pct)}`}>
                {pctText(r.pct)}
              </td>
              <td className="py-1 pr-2 text-right font-mono text-xs">{num(r.vol_ratio)}</td>
              <td className="py-1 pr-2 text-right font-mono text-xs">{big(r.amount)}</td>
              <td className="py-1 pr-2 text-right font-mono text-xs">{big(r.market_cap)}</td>
              <td className="py-1 pr-2 text-right font-mono text-xs">{num(r.pe_dynamic, 1)}</td>
              <td className="py-1 pr-2 text-right font-mono text-xs">{num(r.dividend_yield)}</td>
              <td className="py-1 text-right font-mono text-xs">{num(r.net_assets)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1.5 text-[11px] text-fg-muted">
        总股本由上游「万股」换算成股；市值/股息率为绝对金额与百分数口径（已交叉核对）。
        PE 为上游动态口径，与 close/EPS 不等属正常。
      </p>
    </div>
  )
}
