import { useState } from 'react'
import { AlertTriangle, Building2, FileText, Loader2, Table2, Target, UserSquare2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { api, US_DEEP_NO_DATA_TEXT } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import type {
  UsDeepEnvelope as Resp,
  UsDeepEstimates as EstimatesData,
  UsDeepInstitutionalRow as InstRow,
  UsDeepInsiderRow as InsiderRow,
  UsDeepStatementRow,
  UsDeepSummary,
} from '@/lib/api'

/**
 * 美股深度面板 —— 分析师预期 / 机构持股 / 内部人交易 / 三大报表 / 中文摘要
 * (OpenBB, 2026-10-12 新增; 三表与摘要 2026-10-16 补)。
 *
 * 与港股 ``HKDeepPanels`` 同一套契约:
 * - 失败/不可用 → HTTP 200 + {available:false, reason}, 显示 reason, **不编造**;
 * - 空列表(无覆盖)由后端判 no_data, 这里照 reason 显示;
 * - 源站单位存疑的字段(market_value)后端只透 ``_raw``, 这里**不展示** ——
 *   宁可少一列, 也不拿口径不明的数字充数。
 *
 * 数据源: OpenBB(已登记 source_gate 白名单)。五个端点都依赖宿主常驻服务
 * (OpenBB 6900), 没起时后端 fail-closed, 这里显示"不可用: provider_error…"。
 *
 * 请求一律走 ``api.usDeep*`` (frontend/src/lib/api.ts) + ``QK.usDeep*``
 * (frontend/src/lib/queryKeys.ts), 不在组件里手拼 URL。
 */

// ───────────────────────────────────────────── 显示工具

/**
 * ⚠️ 所有取值函数都收 ``T | null | undefined``: 上游是**原样透传**,
 * 字段可能整个缺失(不只是 null), 类型上是 optional。缺值一律 "—"。
 */
function num(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

function money(v: number | null | undefined): string {
  return v == null || Number.isNaN(v) ? '—' : `$${v.toFixed(2)}`
}

/** 美股股本很大, 用 B/M/K 缩写; 变动为正负都要带符号。 */
function shares(v: number | null | undefined, signed = false): string {
  if (v == null || Number.isNaN(v)) return '—'
  const a = Math.abs(v)
  const sign = signed && v > 0 ? '+' : ''
  if (a >= 1e9) return `${sign}${(v / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `${sign}${(v / 1e6).toFixed(2)}M`
  if (a >= 1e3) return `${sign}${(v / 1e3).toFixed(1)}K`
  return `${sign}${v.toFixed(0)}`
}

function pctSigned(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

/** 美股惯例: **绿涨红跌**(与 A股相反), 所以增持用绿、减持用红。 */
function deltaClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return 'text-muted'
  return v > 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-500'
}

const RATING_STYLE: Record<string, { label: string; cls: string }> = {
  // yfinance 实测最常见的是 strong_buy(如 NVDA), 不映射就会直接把英文原值
  // 显示成徽章外的裸文本 —— 补上这两个档位。
  strong_buy: { label: '强力买入', cls: 'bg-emerald-500/20 text-emerald-700 dark:text-emerald-400' },
  strong_sell: { label: '强力卖出', cls: 'bg-rose-500/20 text-rose-700 dark:text-rose-400' },
  buy: { label: '买入', cls: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400' },
  overweight: { label: '增持', cls: 'bg-green-500/15 text-green-700 dark:text-green-400' },
  hold: { label: '持有', cls: 'bg-amber-500/15 text-amber-700 dark:text-amber-500' },
  sell: { label: '卖出', cls: 'bg-rose-500/15 text-rose-700 dark:text-rose-400' },
  underweight: { label: '减持', cls: 'bg-rose-500/15 text-rose-700 dark:text-rose-400' },
}

function RatingBadge({ rec }: { rec: string | null | undefined }) {
  if (!rec) return <span className="text-muted text-xs">—</span>
  const key = rec.toLowerCase().trim()
  const s = RATING_STYLE[key]
  if (!s) return <span className="text-xs text-secondary">{rec}</span>
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${s.cls}`}>{s.label}</span>
  )
}

function StateLine({ loading, warn, text }: { loading?: boolean; warn?: boolean; text: string }) {
  return (
    <div className={`flex items-center gap-2 py-3 text-xs ${warn ? 'text-amber-600 dark:text-amber-500' : 'text-muted'}`}>
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
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        <span className="ml-auto text-xs text-muted">{subtitle}</span>
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
    queryKey: QK.usDeepEstimates(symbol),
    queryFn: () => api.usDeepEstimates(symbol),
    staleTime: 600_000,
  })

  return (
    <PanelShell
      icon={<Target className="h-3.5 w-3.5 text-muted" />}
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
            <p className="text-xs text-muted">
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
      <div className="text-xs text-muted">{label}</div>
      <div className={`text-sm font-medium ${valueClass ?? 'text-foreground'}`}>{value}</div>
    </div>
  )
}

/**
 * 目标价区间条 —— low ─ consensus ─ high, 另标现价位置。
 * 三个值缺任何一个就不画(半截的区间条比没有更误导)。
 */
function TargetRangeBar({ low, high, consensus, current }: {
  low: number | null | undefined
  high: number | null | undefined
  consensus: number | null | undefined
  current: number | null | undefined
}) {
  if (low == null || high == null || high <= low) return null
  const span = high - low
  const pos = (v: number) => Math.max(0, Math.min(100, ((v - low) / span) * 100))
  const marks: { key: string; v: number; cls: string; label: string }[] = []
  if (consensus != null) {
    marks.push({ key: 'cons', v: consensus, cls: 'bg-foreground', label: '中位' })
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
            <span className="mt-0.5 text-[10px] leading-none text-muted">{m.label}</span>
          </div>
        ))}
      </div>
      <div className="flex justify-between text-[10px] text-muted">
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
    queryKey: QK.usDeepInstitutional(symbol, limit),
    queryFn: () => api.usDeepInstitutional(symbol, limit),
    staleTime: 600_000,
  })

  return (
    <PanelShell
      icon={<Building2 className="h-3.5 w-3.5 text-muted" />}
      title="机构持股"
      subtitle="OpenBB · Nasdaq"
    >
      {body(query, (rows: InstRow[]) => {
        if (rows.length === 0) return <StateLine text="无机构持股披露" />
        return (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted">
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
                             className="text-foreground hover:underline">{r.owner ?? '—'}</a>
                        : <span>{r.owner ?? '—'}</span>}
                    </td>
                    <td className="py-1 pr-2 text-xs text-muted">{r.date ?? '—'}</td>
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
            <p className="mt-1.5 text-[11px] text-muted">
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
    queryKey: QK.usDeepInsider(symbol, limit),
    queryFn: () => api.usDeepInsider(symbol, limit),
    staleTime: 600_000,
  })

  return (
    <PanelShell
      icon={<UserSquare2 className="h-3.5 w-3.5 text-muted" />}
      title="内部人交易"
      subtitle="OpenBB · Nasdaq (Form 4)"
    >
      {body(query, (rows: InsiderRow[]) => {
        if (rows.length === 0) return <StateLine text="无内部人交易记录" />
        return (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted">
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
                    <td className="py-1 pr-2 text-xs text-muted">{r.transaction_date ?? '—'}</td>
                    <td className="py-1 pr-2">
                      <span className="block truncate">{r.owner ?? '—'}</span>
                      {r.title && <span className="block text-[11px] text-muted">{r.title}</span>}
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
            <p className="mt-1.5 text-[11px] text-muted">
              Automatic Sell 多为薪酬行权后的自动卖出（非主动减持判断）；Award / Gift 类不计入买卖意向。
            </p>
          </div>
        )
      })}
    </PanelShell>
  )
}

/** 交易类型配色: 卖出偏红、买入/获授偏绿、其余中性。 */
function txClass(t: string | null | undefined): string {
  const s = (t ?? '').toLowerCase()
  if (s.includes('sell')) return 'bg-rose-500/15 text-rose-700 dark:text-rose-400'
  if (s.includes('buy') || s.includes('award')) {
    return 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400'
  }
  return 'bg-elevated text-secondary'
}

// ───────────────────────────────────────────── 财报三表

type StatementKind = 'income' | 'balance' | 'cash'
type StatementPeriod = 'annual' | 'quarter'

const STATEMENT_KINDS: { key: StatementKind; label: string }[] = [
  { key: 'income', label: '利润表' },
  { key: 'balance', label: '资产负债表' },
  { key: 'cash', label: '现金流量表' },
]

const PERIODS: { key: StatementPeriod; label: string }[] = [
  { key: 'annual', label: '年度' },
  { key: 'quarter', label: '季度' },
]

/**
 * 核心科目 —— 上游一期实测有 40(利润表)/77(资产负债表)/53(现金流量表)个字段,
 * 全铺在一屏里没法看, 这里只挑判断用得上的; 其余原样进"全部字段"折叠。
 *
 * ⚠️ key 全部来自实测响应(2026-10-16 NVDA 年报), 上游是**原样透传**,
 * 字段可能缺失 → 取值一律走 ``stmtValue``, 缺值显示 "—", 绝不补 0。
 */
const CORE_ROWS: Record<StatementKind, { key: string; label: string; fmt: 'usd' | 'num' | 'int' }[]> = {
  income: [
    { key: 'total_revenue', label: '营业总收入', fmt: 'usd' },
    { key: 'cost_of_revenue', label: '营业成本', fmt: 'usd' },
    { key: 'gross_profit', label: '毛利润', fmt: 'usd' },
    { key: 'research_and_development_expense', label: '研发费用', fmt: 'usd' },
    { key: 'operating_income', label: '营业利润', fmt: 'usd' },
    { key: 'net_income', label: '净利润', fmt: 'usd' },
    { key: 'ebitda', label: 'EBITDA', fmt: 'usd' },
    { key: 'diluted_earnings_per_share', label: '摊薄每股收益', fmt: 'num' },
  ],
  balance: [
    { key: 'cash_and_cash_equivalents', label: '现金及等价物', fmt: 'usd' },
    { key: 'inventories', label: '存货', fmt: 'usd' },
    { key: 'total_current_assets', label: '流动资产合计', fmt: 'usd' },
    { key: 'total_assets', label: '资产总计', fmt: 'usd' },
    { key: 'current_liabilities', label: '流动负债合计', fmt: 'usd' },
    { key: 'total_liabilities_net_minority_interest', label: '负债合计', fmt: 'usd' },
    { key: 'common_stock_equity', label: '普通股股东权益', fmt: 'usd' },
    { key: 'total_debt', label: '总债务', fmt: 'usd' },
    { key: 'net_debt', label: '净债务', fmt: 'usd' },
  ],
  cash: [
    { key: 'operating_cash_flow', label: '经营活动现金流', fmt: 'usd' },
    { key: 'capital_expenditure', label: '资本开支', fmt: 'usd' },
    { key: 'free_cash_flow', label: '自由现金流', fmt: 'usd' },
    { key: 'investing_cash_flow', label: '投资活动现金流', fmt: 'usd' },
    { key: 'financing_cash_flow', label: '筹资活动现金流', fmt: 'usd' },
    { key: 'depreciation_and_amortization', label: '折旧与摊销', fmt: 'usd' },
    { key: 'stock_based_compensation', label: '股权激励', fmt: 'usd' },
    { key: 'net_change_in_cash_and_equivalents', label: '现金净变动', fmt: 'usd' },
  ],
}

/** 报表金额动辄百亿, 用 $xB/M/K 缩写; 取不到值显示 "—"(绝不显示 0)。 */
function usdCompact(n: number): string {
  const a = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  if (a >= 1e12) return `${sign}$${(a / 1e12).toFixed(2)}T`
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(2)}M`
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(1)}K`
  return `${sign}$${a.toFixed(2)}`
}

/** 上游原样透传 → 值可能是 number / 数字字符串 / null / 缺失, 一律防御性转换。 */
function stmtValue(v: unknown, fmt: 'usd' | 'num' | 'int'): string {
  if (v == null || v === '') return '—'
  const n = typeof v === 'number' ? v : Number(v)
  if (Number.isNaN(n)) return '—'
  if (fmt === 'usd') return usdCompact(n)
  if (fmt === 'int') return n.toLocaleString('en-US')
  return n.toFixed(2)
}

function TabButton({ active, onClick, children }: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded px-2 py-1 text-xs transition-colors ${
        active
          ? 'bg-elevated font-medium text-foreground'
          : 'text-muted hover:text-foreground'
      }`}
    >
      {children}
    </button>
  )
}

export function USStatementPanel({ symbol, limit = 4 }: {
  symbol: string
  /** 取几期, 1..12 (后端校验) */
  limit?: number
}) {
  const [kind, setKind] = useState<StatementKind>('income')
  const [period, setPeriod] = useState<StatementPeriod>('annual')

  const query = useQuery({
    queryKey: QK.usDeepStatement(symbol, kind, period, limit),
    queryFn: () => api.usDeepStatement(symbol, { kind, period, limit }),
    staleTime: 600_000,
    // 切 tab(三表)/年度季度时保留上一份, 避免整块闪成"加载中";
    // 但**切标的必须清空** —— 否则上一只票的报表会挂在新票的页面上, 而面板
    // 标题里又不写 ticker, 用户看到的就是一份张冠李戴的"真"报表。
    placeholderData: (prev) =>
      prev?.symbol != null && String(prev.symbol).replace(/\.US$/, '') !== symbol
        ? undefined
        : prev,
  })

  return (
    <PanelShell
      icon={<Table2 className="h-3.5 w-3.5 text-muted" />}
      title="财报三表"
      subtitle="OpenBB · yfinance 口径"
    >
      <div className="mb-2 flex flex-wrap items-center gap-1">
        {STATEMENT_KINDS.map(k => (
          <TabButton key={k.key} active={kind === k.key} onClick={() => setKind(k.key)}>
            {k.label}
          </TabButton>
        ))}
        <span className="mx-1 h-4 w-px bg-border" />
        {PERIODS.map(p => (
          <TabButton key={p.key} active={period === p.key} onClick={() => setPeriod(p.key)}>
            {p.label}
          </TabButton>
        ))}
      </div>

      {body(query, (rows: UsDeepStatementRow[]) => {
        if (rows.length === 0) return <StateLine text="无报表数据" />
        const core = CORE_ROWS[kind]
        // "全部字段" = 各期 key 的并集, 顺序沿用上游返回顺序
        const allKeys: string[] = []
        for (const r of rows) {
          for (const k of Object.keys(r)) if (!allKeys.includes(k)) allKeys.push(k)
        }
        return (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-muted">
                  <th className="py-1 pr-3 text-left font-normal">科目</th>
                  {rows.map((r, i) => (
                    <th key={`${r.period_ending ?? 'p'}-${i}`} className="py-1 pl-2 text-right font-normal">
                      {r.period_ending ?? '—'}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {core.map(f => (
                  <tr key={f.key} className="border-t border-border/60">
                    <td className="py-1 pr-3 text-xs">{f.label}</td>
                    {rows.map((r, i) => (
                      <td key={`${f.key}-${i}`} className="py-1 pl-2 text-right font-mono text-xs">
                        {stmtValue(r[f.key], f.fmt)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>

            <details className="mt-2">
              <summary className="cursor-pointer text-[11px] text-muted hover:text-foreground">
                全部字段（{allKeys.length} 项，上游原样透传）
              </summary>
              <div className="mt-1.5 max-h-72 overflow-auto">
                <table className="w-full text-sm">
                  <tbody>
                    {allKeys.map(k => (
                      <tr key={k} className="border-t border-border/40">
                        <td className="py-0.5 pr-3 font-mono text-[11px] text-muted">{k}</td>
                        {rows.map((r, i) => (
                          <td key={`${k}-${i}`} className="py-0.5 pl-2 text-right font-mono text-[11px]">
                            {stmtValue(r[k], 'num')}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>

            <p className="mt-1.5 text-[11px] text-muted">
              上游原样透传，字段随 provider 版本增减；单位美元，缺值显示 “—”（不补 0）。
              仅列核心科目，展开可看全部字段。
            </p>
          </div>
        )
      })}
    </PanelShell>
  )
}

// ───────────────────────────────────────────── 中文摘要

export function USSummaryPanel({ symbol }: { symbol: string }) {
  const query = useQuery({
    queryKey: QK.usDeepSummary(symbol),
    queryFn: () => api.usDeepSummary(symbol),
    staleTime: 600_000,
  })

  const resp = query.data
  const rawText = (resp?.data?.text ?? '').trim()
  /**
   * ⚠️ 后端契约: available=false 时响应体**没有 data 键**, reason 是唯一线索
   * (provider_error: 上游没起 / no_data: 真空数据)。所以失败时必须**原样透出**
   * 后端 reason —— 反过来写(失败也走"无数据"分支)会把上游宕机显示成"没有数据",
   * 运维没法区分。只有 available=true 但 text 是空/哨兵串时才归一成不可用。
   */
  const sentinel = !!resp?.available && (!rawText || rawText === US_DEEP_NO_DATA_TEXT)
  const view: Resp<unknown> | undefined = sentinel
    ? { available: false, source: resp?.source, reason: 'no_data: 上游未给出盘口摘要' }
    : (resp as Resp<unknown> | undefined)

  return (
    <PanelShell
      icon={<FileText className="h-3.5 w-3.5 text-muted" />}
      title="盘口摘要"
      subtitle="OpenBB · 由实时盘口拼装"
    >
      {body(
        { isLoading: query.isLoading, error: query.error, data: view },
        (d: UsDeepSummary) => (
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{d.text}</p>
        ),
      )}
    </PanelShell>
  )
}
