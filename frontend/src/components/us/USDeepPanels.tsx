import { AlertTriangle, Building2, Loader2, Target, UserSquare2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { request } from '@/lib/api'

/**
 * 美股深度面板 —— 分析师预期 / 机构持股 / 内部人交易 (OpenBB, 2026-10-12 新增)。
 *
 * 与港股 ``HKDeepPanels`` 同一套契约:
 * - 失败/不可用 → HTTP 200 + {available:false, reason}, 显示 reason, **不编造**;
 * - 空列表(无覆盖)由后端判 no_data, 这里照 reason 显示;
 * - 源站单位存疑的字段(market_value)后端只透 ``_raw``, 这里**不展示** ——
 *   宁可少一列, 也不拿口径不明的数字充数。
 *
 * 数据源: OpenBB(已登记 source_gate 白名单)。三个端点都依赖宿主常驻服务
 * (OpenBB 6900), 没起时后端 fail-closed, 这里显示"不可用: provider_error…"。
 */

interface Resp<T> {
  available: boolean
  source: string
  reason?: string
  symbol?: string
  data?: T
}

// ── 分析师一致预期 ──
interface EstimatesData {
  symbol: string
  currency: string | null
  current_price: number | null
  target_high: number | null
  target_low: number | null
  target_consensus: number | null
  target_median: number | null
  recommendation: string | null
  recommendation_mean: number | null
  number_of_analysts: number | null
}

// ── 机构持股 ──
interface InstRow {
  date: string | null
  owner: string | null
  shares_held: number | null
  shares_change: number | null
  shares_change_pct: number | null
  market_value_raw: number | null
  url: string | null
}

// ── 内部人交易 ──
interface InsiderRow {
  transaction_date: string | null
  owner: string | null
  title: string | null
  ownership_type: string | null
  transaction_type: string | null
  shares: number | null
  price: number | null
  shares_held_after: number | null
  url: string | null
}

// ───────────────────────────────────────────── 显示工具

function num(v: number | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

function money(v: number | null): string {
  return v == null || Number.isNaN(v) ? '—' : `$${v.toFixed(2)}`
}

/** 美股股本很大, 用 B/M/K 缩写; 变动为正负都要带符号。 */
function shares(v: number | null, signed = false): string {
  if (v == null || Number.isNaN(v)) return '—'
  const a = Math.abs(v)
  const sign = signed && v > 0 ? '+' : ''
  if (a >= 1e9) return `${sign}${(v / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `${sign}${(v / 1e6).toFixed(2)}M`
  if (a >= 1e3) return `${sign}${(v / 1e3).toFixed(1)}K`
  return `${sign}${v.toFixed(0)}`
}

function pctSigned(v: number | null, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

/** 美股惯例: **绿涨红跌**(与 A股相反), 所以增持用绿、减持用红。 */
function deltaClass(v: number | null): string {
  if (v == null || Number.isNaN(v) || v === 0) return 'text-fg-muted'
  return v > 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-500'
}

const RATING_STYLE: Record<string, { label: string; cls: string }> = {
  buy: { label: '买入', cls: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400' },
  overweight: { label: '增持', cls: 'bg-green-500/15 text-green-700 dark:text-green-400' },
  hold: { label: '持有', cls: 'bg-amber-500/15 text-amber-700 dark:text-amber-500' },
  sell: { label: '卖出', cls: 'bg-rose-500/15 text-rose-700 dark:text-rose-400' },
  underweight: { label: '减持', cls: 'bg-rose-500/15 text-rose-700 dark:text-rose-400' },
}

function RatingBadge({ rec }: { rec: string | null }) {
  if (!rec) return <span className="text-fg-muted text-xs">—</span>
  const key = rec.toLowerCase().trim()
  const s = RATING_STYLE[key]
  if (!s) return <span className="text-xs text-fg-secondary">{rec}</span>
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${s.cls}`}>{s.label}</span>
  )
}

function StateLine({ loading, warn, text }: { loading?: boolean; warn?: boolean; text: string }) {
  return (
    <div className={`flex items-center gap-2 py-3 text-xs ${warn ? 'text-amber-600 dark:text-amber-500' : 'text-fg-muted'}`}>
      {loading
        ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
        : warn
          ? <AlertTriangle className="h-3.5 w-3.5" />
          : null}
      <span className="min-w-0 break-words">{text}</span>
    </div>
  )
}

function PanelShell({ icon, title, subtitle, children }: {
  icon: React.ReactNode
  title: string
  subtitle: string
  children: React.ReactNode
}) {
  // 尺寸/留白与 USStockAnalysis 里既有的 Panel 对齐(bg-surface/60 + p-4)
  return (
    <section className="rounded-lg border border-border bg-surface/60 p-4">
      <div className="mb-3 flex items-center gap-2">
        {icon}
        <h3 className="text-sm font-semibold text-fg">{title}</h3>
        <span className="ml-auto text-xs text-fg-muted">{subtitle}</span>
      </div>
      {children}
    </section>
  )
}

/** 四个状态里取一个渲染 —— 避免每个面板各写一遍 loading/error/unavailable。 */
function body(query: {
  isLoading: boolean
  error: Error | null
  data?: Resp<unknown> | null
}, render: (data: never) => React.ReactNode): React.ReactNode {
  if (query.isLoading) return <StateLine loading text="加载中…" />
  if (query.error) return <StateLine warn text={`请求失败: ${query.error.message}`} />
  const resp = query.data as Resp<unknown> | null | undefined
  if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
  if (!resp.data) return <StateLine warn text="不可用: 返回结构异常" />
  return render(resp.data as never)
}

// ───────────────────────────────────────────── 分析师一致预期

export function USEstimatesPanel({ symbol }: { symbol: string }) {
  const query = useQuery({
    queryKey: ['us', 'deep-estimates', symbol],
    queryFn: () => request<Resp<EstimatesData>>(
      `/api/us/deep/estimates/${encodeURIComponent(symbol)}`),
    staleTime: 600_000,
  })

  return (
    <PanelShell
      icon={<Target className="h-3.5 w-3.5 text-fg-muted" />}
      title="分析师一致预期"
      subtitle="OpenBB · yfinance 汇总"
    >
      {body(query, (d: EstimatesData) => {
        // 相对当前价的空间: 以中位数为准(中位比均值更抗极端目标价)
        const base = d.target_median ?? d.target_consensus
        const upside = base != null && d.current_price ? (base / d.current_price - 1) : null
        return (
          <div className="space-y-2.5">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Cell label="覆盖机构" value={d.number_of_analysts != null ? `${d.number_of_analysts} 家` : '—'} />
              <Cell label="一致评级" value={<RatingBadge rec={d.recommendation} />} />
              <Cell
                label="目标价中位"
                value={money(d.target_median ?? d.target_consensus)}
              />
              <Cell
                label="较现价空间"
                value={upside != null ? pctSigned(upside, 1) : '—'}
                valueClass={deltaClass(upside)}
              />
            </div>
            <TargetRangeBar
              low={d.target_low}
              high={d.target_high}
              consensus={d.target_median ?? d.target_consensus}
              current={d.current_price}
            />
            <p className="text-xs text-fg-muted">
              现价 {money(d.current_price)}
              {d.recommendation_mean != null && ` · 评级均值 ${num(d.recommendation_mean)}（1=强力买入 … 5=强力卖出）`}
              {d.currency && ` · ${d.currency}`}
            </p>
          </div>
        )
      })}
    </PanelShell>
  )
}

function Cell({ label, value, valueClass }: {
  label: string
  value: React.ReactNode
  valueClass?: string
}) {
  return (
    <div>
      <div className="text-xs text-fg-muted">{label}</div>
      <div className={`text-sm font-medium ${valueClass ?? 'text-fg'}`}>{value}</div>
    </div>
  )
}

/**
 * 目标价区间条 —— low ─ consensus ─ high, 另标现价位置。
 * 三个值缺任何一个就不画(半截的区间条比没有更误导)。
 */
function TargetRangeBar({ low, high, consensus, current }: {
  low: number | null
  high: number | null
  consensus: number | null
  current: number | null
}) {
  if (low == null || high == null || high <= low) return null
  const span = high - low
  const pos = (v: number) => Math.max(0, Math.min(100, ((v - low) / span) * 100))
  const marks: { key: string; v: number; cls: string; label: string }[] = []
  if (consensus != null) {
    marks.push({ key: 'cons', v: consensus, cls: 'bg-fg', label: '中位' })
  }
  if (current != null) {
    marks.push({ key: 'cur', v: current, cls: 'bg-amber-500', label: '现价' })
  }
  return (
    <div className="pt-1">
      <div className="relative h-6">
        <div className="absolute inset-x-0 top-2 h-1.5 rounded bg-border" />
        {marks.map(m => (
          <div
            key={m.key}
            className="absolute top-0 flex flex-col items-center"
            style={{ left: `${pos(m.v)}%`, transform: 'translateX(-50%)' }}
            title={`${m.label} ${money(m.v)}`}
          >
            <span className={`h-4 w-0.5 rounded ${m.cls}`} />
            <span className="mt-0.5 text-[10px] leading-none text-fg-muted">{m.label}</span>
          </div>
        ))}
      </div>
      <div className="flex justify-between text-[10px] text-fg-muted">
        <span>低 {money(low)}</span>
        <span>高 {money(high)}</span>
      </div>
    </div>
  )
}

// ───────────────────────────────────────────── 机构持股

export function USInstitutionalPanel({ symbol, limit = 20 }: {
  symbol: string
  limit?: number
}) {
  const query = useQuery({
    queryKey: ['us', 'deep-institutional', symbol, limit],
    queryFn: () => request<Resp<InstRow[]>>(
      `/api/us/deep/institutional/${encodeURIComponent(symbol)}?limit=${limit}`),
    staleTime: 600_000,
  })

  return (
    <PanelShell
      icon={<Building2 className="h-3.5 w-3.5 text-fg-muted" />}
      title="机构持股"
      subtitle="OpenBB · Nasdaq"
    >
      {body(query, (rows: InstRow[]) => {
        if (rows.length === 0) return <StateLine text="无机构持股披露" />
        return (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-fg-muted">
                  <th className="py-1 pr-2 text-left font-normal">机构</th>
                  <th className="py-1 pr-2 text-left font-normal">报告期</th>
                  <th className="py-1 pr-2 text-right font-normal">持股</th>
                  <th className="py-1 pr-2 text-right font-normal">变动</th>
                  <th className="py-1 text-right font-normal">变动%</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={`${r.owner ?? 'inst'}-${r.date ?? i}`}
                      className="border-t border-border/60">
                    <td className="py-1 pr-2">
                      {r.url
                        ? <a href={r.url} target="_blank" rel="noreferrer"
                             className="text-fg hover:underline">{r.owner ?? '—'}</a>
                        : <span>{r.owner ?? '—'}</span>}
                    </td>
                    <td className="py-1 pr-2 text-xs text-fg-muted">{r.date ?? '—'}</td>
                    <td className="py-1 pr-2 text-right font-mono text-xs">{shares(r.shares_held)}</td>
                    <td className={`py-1 pr-2 text-right font-mono text-xs ${deltaClass(r.shares_change)}`}>
                      {shares(r.shares_change, true)}
                    </td>
                    <td className={`py-1 text-right font-mono text-xs ${deltaClass(r.shares_change_pct)}`}>
                      {pctSigned(r.shares_change_pct)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-1.5 text-[11px] text-fg-muted">
              源站市值字段单位存疑（疑似千美元口径），故不展示；判断机构动向看持股数与变动%。
            </p>
          </div>
        )
      })}
    </PanelShell>
  )
}

// ───────────────────────────────────────────── 内部人交易

export function USInsiderPanel({ symbol, limit = 20 }: {
  symbol: string
  limit?: number
}) {
  const query = useQuery({
    queryKey: ['us', 'deep-insider', symbol, limit],
    queryFn: () => request<Resp<InsiderRow[]>>(
      `/api/us/deep/insider/${encodeURIComponent(symbol)}?limit=${limit}`),
    staleTime: 600_000,
  })

  return (
    <PanelShell
      icon={<UserSquare2 className="h-3.5 w-3.5 text-fg-muted" />}
      title="内部人交易"
      subtitle="OpenBB · Nasdaq (Form 4)"
    >
      {body(query, (rows: InsiderRow[]) => {
        if (rows.length === 0) return <StateLine text="无内部人交易记录" />
        return (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-fg-muted">
                  <th className="py-1 pr-2 text-left font-normal">日期</th>
                  <th className="py-1 pr-2 text-left font-normal">姓名 / 身份</th>
                  <th className="py-1 pr-2 text-left font-normal">类型</th>
                  <th className="py-1 pr-2 text-right font-normal">股数</th>
                  <th className="py-1 pr-2 text-right font-normal">价格</th>
                  <th className="py-1 text-right font-normal">交易后持股</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={`${r.owner ?? 'ins'}-${r.transaction_date ?? i}-${i}`}
                      className="border-t border-border/60">
                    <td className="py-1 pr-2 text-xs text-fg-muted">{r.transaction_date ?? '—'}</td>
                    <td className="py-1 pr-2">
                      <span className="block truncate">{r.owner ?? '—'}</span>
                      {r.title && <span className="block text-[11px] text-fg-muted">{r.title}</span>}
                    </td>
                    <td className="py-1 pr-2 text-xs">
                      <span className={`rounded px-1.5 py-0.5 ${txClass(r.transaction_type)}`}>
                        {r.transaction_type ?? '—'}
                      </span>
                    </td>
                    <td className="py-1 pr-2 text-right font-mono text-xs">{shares(r.shares)}</td>
                    <td className="py-1 pr-2 text-right font-mono text-xs">{money(r.price)}</td>
                    <td className="py-1 text-right font-mono text-xs">{shares(r.shares_held_after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-1.5 text-[11px] text-fg-muted">
              Automatic Sell 多为薪酬行权后的自动卖出（非主动减持判断）；Award / Gift 类不计入买卖意向。
            </p>
          </div>
        )
      })}
    </PanelShell>
  )
}

/** 交易类型配色: 卖出偏红、买入/获授偏绿、其余中性。 */
function txClass(t: string | null): string {
  const s = (t ?? '').toLowerCase()
  if (s.includes('sell')) return 'bg-rose-500/15 text-rose-700 dark:text-rose-400'
  if (s.includes('buy') || s.includes('award')) {
    return 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400'
  }
  return 'bg-elevated text-fg-secondary'
}
