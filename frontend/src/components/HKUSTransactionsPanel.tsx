import { Activity, AlertTriangle, Loader2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { request } from '@/lib/api'

/**
 * 港美股「逐笔成交」—— 通达信扩展市场(MAC-EX 7727)。
 *
 * ⚠️ 两个市场不是同一条协议, 后端已经分好叉, 这里只负责如实展示:
 *   - 港股: ex 协议 0x23FC(当日) / 0x2406(历史), 价格是上游整数 /1000 = 港元;
 *     上游没有买卖方向字段(nature 的映射未实测), 所以**不标颜色**。
 *   - 美股: MAC 协议 0x122F, 价格已是浮点, bs_flag 有协议注释口径(0买/1卖/2中性/5盘后)。
 *
 * 契约不变: 失败 / 不可用 → HTTP 200 + {available:false, reason}, 显示 reason, 不编造。
 */

interface TxRow {
  time: string
  price: number | null
  volume: number | null
  trade_count: number | null
  bs_flag?: number
  nature?: number
}

interface Resp {
  available: boolean
  source?: string
  reason?: string
  rows?: TxRow[]
  date?: string | null
  protocol?: string | null
  price_unit?: string | null
  volume_unit?: string | null
  note?: string | null
}

function deepMarket(symbol: string): 'hk' | 'us' | null {
  const s = symbol.trim().toUpperCase()
  return s.endsWith('.HK') ? 'hk' : s.endsWith('.US') ? 'us' : null
}

function num(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? '—' : v.toLocaleString('en-US')
}

function StateLine({ loading, warn, text }: { loading?: boolean; warn?: boolean; text: string }) {
  return (
    <div className={`flex items-start gap-2 py-3 text-xs leading-relaxed ${warn ? 'text-amber-600 dark:text-amber-500' : 'text-muted'}`}>
      {loading ? <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
        : warn ? <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> : null}
      <span className="min-w-0 break-words">{text}</span>
    </div>
  )
}

/** 仅美股有协议注释支撑的口径; 港股一律不标方向(后端明确说未实测)。 */
const US_SIDE: Record<number, { text: string; cls: string }> = {
  0: { text: '买', cls: 'text-red-600 dark:text-red-500' },
  1: { text: '卖', cls: 'text-green-600 dark:text-green-500' },
  2: { text: '中性', cls: 'text-muted' },
  5: { text: '盘后', cls: 'text-muted' },
}

export function HKUSTransactionsPanel({ symbol, limit = 60, className = '' }: {
  symbol: string
  limit?: number
  className?: string
}) {
  const mkt = deepMarket(symbol)
  const q = useQuery({
    queryKey: ['hkus', 'transactions', symbol, limit],
    queryFn: () => request<Resp>(
      `/api/${mkt}/deep/transactions/${encodeURIComponent(symbol)}?limit=${limit}`),
    enabled: mkt != null,
    staleTime: 30_000,
    refetchInterval: 30_000,
  })

  if (mkt == null) return null
  const rows = q.data?.available ? q.data.rows ?? [] : []
  const isUs = mkt === 'us'

  return (
    <section className={`rounded-lg border border-border bg-surface p-3 ${className}`}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Activity className="h-3.5 w-3.5 text-muted" />
        <h3 className="text-sm font-semibold text-foreground">逐笔成交</h3>
        <span className="text-xs text-muted">
          {isUs ? 'MAC-EX 0x122F' : 'MAC-EX 0x23FC / 0x2406'}
          {q.data?.date ? ` · ${q.data.date === 'today' ? '当日' : q.data.date}` : ''}
        </span>
        <span className="ml-auto text-[11px] text-muted">
          {q.data?.price_unit ?? ''}{q.data?.volume_unit ? ` · 量=${q.data.volume_unit}` : ''}
        </span>
      </div>

      {q.isLoading && <StateLine loading text="正在读取逐笔成交…" />}
      {q.isError && <StateLine warn text={`逐笔取不到: ${(q.error as Error)?.message ?? '请求失败'}`} />}
      {q.data && !q.data.available && (
        <StateLine warn text={`逐笔不可用 —— ${q.data.reason ?? '上游无返回'}。不用别的口径凑一份明细。`} />
      )}
      {q.data?.available && rows.length === 0 && <StateLine text="该标的当前没有返回逐笔明细" />}

      {rows.length > 0 && (
        <div className="max-h-72 overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-surface">
              <tr className="text-xs text-muted">
                <th className="py-1 pr-2 text-left font-normal">时间</th>
                <th className="py-1 pr-2 text-right font-normal">成交价</th>
                <th className="py-1 pr-2 text-right font-normal">成交量(股)</th>
                {isUs && <th className="py-1 pr-2 text-right font-normal">笔数</th>}
                {isUs && <th className="py-1 text-right font-normal">方向</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => {
                const side = isUs ? US_SIDE[r.bs_flag ?? 2] : null
                return (
                  <tr key={`${r.time}-${i}`} className="border-t border-border/60">
                    <td className="py-1 pr-2 font-mono text-xs">{r.time}</td>
                    <td className="py-1 pr-2 text-right font-mono text-xs">
                      {r.price == null ? '—' : r.price.toFixed(r.price >= 100 ? 2 : 3)}
                    </td>
                    <td className="py-1 pr-2 text-right font-mono text-xs">{num(r.volume)}</td>
                    {isUs && (
                      <td className="py-1 pr-2 text-right font-mono text-xs text-muted">
                        {r.trade_count == null ? '—' : num(r.trade_count)}
                      </td>
                    )}
                    {isUs && (
                      <td className={`py-1 text-right text-xs ${side?.cls ?? 'text-muted'}`}>
                        {side?.text ?? '—'}
                      </td>
                    )}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
        {isUs
          ? '方向为上游 bs_flag 的协议注释口径（0 买 / 1 卖 / 2 中性 / 5 盘后）。'
          : '港股逐笔走 ex 扩展行情协议；上游未返回买卖方向字段（nature 映射未实测），故此处不标方向。'}
        {q.data?.note ? ` ${q.data.note}` : ''}
      </p>
    </section>
  )
}
