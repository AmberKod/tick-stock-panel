import { AlertTriangle, Layers3, Loader2 } from 'lucide-react'
import { Fragment, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { request } from '@/lib/api'
import { priceColorClass } from '@/lib/format'

/**
 * 港美股「板块 / 概念榜」—— 通达信扩展市场(MAC-EX 7727)。
 *
 * 数据形态(实测): 每行是一个板块指数(US0212 美股-大麻), 自带 close/pre_close,
 * 并附**板块内领涨股** —— 所以榜单天然能回答"今天哪些概念在动、谁在领涨"。
 * 点一行 → 拉该板块成分股(同一真源), 不是另接一个榜单凑数。
 *
 * kind: US_ALL/US_HY/US_GN/HK_ALL/HK_HY/HK_GN(后端 BOARD_KINDS, 传错会 fail-closed)。
 */

interface BoardRow {
  board_code: string | null
  board_name: string | null
  close: number | null
  pre_close: number | null
  pct: number | null
  rise_speed: number | null
  leader_code: string | null
  leader_name: string | null
  leader_pct: number | null
}

interface Resp {
  available: boolean
  source?: string
  reason?: string
  boards?: BoardRow[]
  kind?: string
}

interface MemberResp {
  available: boolean
  reason?: string
  members?: {
    code: string | null
    name: string | null
    close: number | null
    pct: number | null
    amount: number | null
    market_cap: number | null
    dividend_yield: number | null
  }[]
}

const KINDS = [
  { key: 'US_ALL', label: '美股·全部' },
  { key: 'US_HY', label: '美股·行业' },
  { key: 'US_GN', label: '美股·概念' },
  { key: 'HK_ALL', label: '港股·全部' },
  { key: 'HK_HY', label: '港股·行业' },
  { key: 'HK_GN', label: '港股·概念' },
] as const

function pctText(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function num(v: number | null | undefined, d = 2): string {
  return v == null || Number.isNaN(v) ? '—' : v.toFixed(d)
}

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
    <div className={`flex items-start gap-2 py-4 text-xs leading-relaxed ${warn ? 'text-amber-600 dark:text-amber-500' : 'text-muted'}`}>
      {loading ? <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
        : warn ? <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : null}
      <span className="min-w-0 break-words">{text}</span>
    </div>
  )
}

export function HKUSBoardsPage() {
  const [kind, setKind] = useState<string>('US_HY')
  const [limit, setLimit] = useState(50)
  const [open, setOpen] = useState<string | null>(null)
  const mkt = kind.startsWith('HK') ? 'hk' : 'us'
  const suffix = mkt === 'hk' ? '.HK' : '.US'

  const query = useQuery({
    queryKey: ['hkus', 'boardlist', kind, limit],
    queryFn: () => request<Resp>(
      `/api/${mkt}/deep/boardlist?kind=${encodeURIComponent(kind)}&limit=${limit}`),
    staleTime: 60_000,
  })

  const rows = query.data?.available ? query.data.boards ?? [] : []

  return (
    <div className="mx-auto max-w-[1400px] space-y-3 px-3 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <Layers3 className="h-4 w-4 text-muted" />
        <h1 className="text-lg font-semibold text-foreground">港美股板块 / 概念榜</h1>
        <span className="text-xs text-muted">MAC-EX · 通达信扩展市场</span>
        <div className="ml-auto inline-flex flex-wrap gap-1">
          {KINDS.map(k => (
            <button
              key={k.key}
              type="button"
              onClick={() => { setKind(k.key); setOpen(null) }}
              className={`rounded px-2 py-0.5 text-xs transition-colors ${
                kind === k.key ? 'bg-elevated text-foreground shadow-sm' : 'text-muted hover:text-secondary'
              }`}
            >
              {k.label}
            </button>
          ))}
        </div>
      </div>

      <div className="rounded-lg border border-border bg-surface p-3">
        <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-muted">
          <span>按板块涨跌降序 · 前 {limit} 个板块</span>
          <div className="ml-auto inline-flex items-center gap-1">
            {[30, 50, 100].map(n => (
              <button
                key={n}
                type="button"
                onClick={() => setLimit(n)}
                className={`rounded px-1.5 py-0.5 text-[11px] ${
                  limit === n ? 'bg-elevated text-foreground' : 'text-muted hover:text-secondary'
                }`}
              >{n}</button>
            ))}
          </div>
        </div>

        {query.isLoading && <StateLine loading text="加载板块榜…" />}
        {!query.isLoading && !query.data?.available && (
          <StateLine warn text={`不可用: ${query.data?.reason ?? query.error?.message ?? '无返回'}`} />
        )}

        {rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted">
                  <th className="py-1 pr-2 text-left font-normal">板块</th>
                  <th className="py-1 pr-2 text-right font-normal">指数</th>
                  <th className="py-1 pr-2 text-right font-normal">涨跌%</th>
                  <th className="py-1 pr-2 text-left font-normal">领涨股</th>
                  <th className="py-1 text-right font-normal">领涨%</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((b, i) => {
                  const key = b.board_code ?? `row-${i}`
                  const active = open === key
                  return (
                    <Fragment key={key}>
                      <tr
                        className="cursor-pointer border-t border-border/60 hover:bg-elevated/50"
                        onClick={() => setOpen(active ? null : key)}
                      >
                        <td className="py-1 pr-2">
                          <span className="text-foreground">{b.board_name ?? key}</span>
                          <span className="ml-1 font-mono text-[10px] text-muted">{key}</span>
                        </td>
                        <td className="py-1 pr-2 text-right font-mono text-xs">{num(b.close)}</td>
                        <td className={`py-1 pr-2 text-right font-mono text-xs ${priceColorClass(b.pct)}`}>
                          {pctText(b.pct)}
                        </td>
                        <td className="py-1 pr-2 text-xs">
                          {b.leader_code
                            ? <Link
                                to={`/stock/${encodeURIComponent(`${b.leader_code}${suffix}`)}`}
                                className="text-foreground hover:underline"
                                onClick={e => e.stopPropagation()}
                              >{b.leader_name ?? b.leader_code}</Link>
                            : <span className="text-muted">—</span>}
                        </td>
                        <td className={`py-1 text-right font-mono text-xs ${priceColorClass(b.leader_pct)}`}>
                          {pctText(b.leader_pct)}
                        </td>
                      </tr>
                      {active && (
                        <tr className="border-t border-border/40">
                          <td colSpan={5} className="bg-elevated/30 px-2 py-2">
                            <BoardMembers boardCode={key} mkt={mkt} suffix={suffix} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
            <p className="mt-2 text-[11px] leading-relaxed text-muted">
              板块指数是通达信自己编的扩展板块指数（非官方指数），涨跌为该指数当日值换算；
              领涨股取板块内当日涨幅最高者。点行展开看成分股。
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

/** 展开某一个板块的成分股 —— 与榜单同源于 BoardMembersQuotes。 */
function BoardMembers({ boardCode, mkt, suffix }: {
  boardCode: string
  mkt: 'hk' | 'us'
  suffix: string
}) {
  const q = useQuery({
    queryKey: ['hkus', 'board-members', boardCode],
    queryFn: () => request<MemberResp>(
      `/api/${mkt}/deep/board/${encodeURIComponent(boardCode)}/members?limit=50`),
    staleTime: 60_000,
  })

  if (q.isLoading) return <StateLine loading text="加载成分股…" />
  if (!q.data?.available) {
    return <StateLine warn text={`成分股不可用: ${q.data?.reason ?? q.error?.message ?? '无返回'}`} />
  }
  const rows = q.data.members ?? []
  if (!rows.length) return <StateLine text="该板块没有返回成分股" />

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-muted">
            <th className="py-1 pr-2 text-left font-normal">名称</th>
            <th className="py-1 pr-2 text-right font-normal">最新</th>
            <th className="py-1 pr-2 text-right font-normal">涨跌%</th>
            <th className="py-1 pr-2 text-right font-normal">成交额</th>
            <th className="py-1 text-right font-normal">市值</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.code ?? 'm'}-${i}`} className="border-t border-border/40">
              <td className="py-1 pr-2">
                {r.code
                  ? <Link
                      to={`/stock/${encodeURIComponent(`${r.code}${suffix}`)}`}
                      className="text-foreground hover:underline"
                    >{r.name ?? r.code}</Link>
                  : <span>{r.name ?? '—'}</span>}
                <span className="ml-1 font-mono text-[10px] text-muted">{r.code}</span>
              </td>
              <td className="py-1 pr-2 text-right font-mono">{num(r.close)}</td>
              <td className={`py-1 pr-2 text-right font-mono ${priceColorClass(r.pct)}`}>
                {pctText(r.pct)}
              </td>
              <td className="py-1 pr-2 text-right font-mono">{big(r.amount)}</td>
              <td className="py-1 text-right font-mono">{big(r.market_cap)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
