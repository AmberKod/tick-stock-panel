import {
  AlertTriangle, BarChart3, Bell, FileText, Flame, Gift, Loader2,
  TrendingDown, TrendingUp, Waves,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { request } from '@/lib/api'
import { priceColorClass } from '@/lib/format'

/**
 * A股深度数据面板 —— 个股详情整页(/stock/:symbol) 与 弹窗 slot 共用同一份实现。
 *
 * 契约(与后端 app/api/astock_deep.py 严格对齐, 改任一侧都要同步改另一侧):
 * - 失败/不可用 → HTTP 200 + {available:false, reason}, 显示 reason, **不编造**;
 * - **空数组是真实答案, 不是故障** —— 未上榜 / 无研报覆盖 / 无公告 都显示
 *   对应的"确实没有"文案, 不能显示"取不到数据"(那是把"票干净"谎报成"接口坏")。
 *
 * 数据源(全部过 source_gate 白名单, 无任何 demo/随机兜底):
 *   龙虎榜=东财 datacenter · 研报=东财 reportapi · 公告=巨潮 cninfo
 *   资金流=新浪(东财 push2/push2his 被墙) · 财报=akshare(东财财务指标)
 */

interface Seat {
  name: string
  buy_amt: number
  sell_amt: number
  net: number
  is_institution: boolean
  is_northbound: boolean
}

interface DragonRecord {
  date: string
  reason: string
  net_buy: number
  turnover: number
  close_price: number | null
  change_rate: number | null
}

interface DragonResp {
  available: boolean
  source: string
  reason?: string
  data?: {
    code: string
    window_days: number
    records: DragonRecord[]
    seats: { buy: Seat[]; sell: Seat[] }
    institution: { buy_amt: number; sell_amt: number; net_amt: number; seat_count: number }
    northbound: { buy_amt: number; sell_amt: number; net_amt: number; present: boolean }
    updated_at: string
  }
}

interface ReportRow {
  date: string
  org: string
  title: string
  rating: string
  rating_prev: string
  rating_change: string
  target_price: number | null
  eps_this_year: number | null
  eps_next_year: number | null
  url: string
}

interface ResearchResp {
  available: boolean
  source: string
  reason?: string
  data?: { code: string; limit: number; reports: ReportRow[]; updated_at: string }
}

interface AnnRow {
  date: string
  title: string
  type: string
  type_codes: string
  url: string
}

interface AnnResp {
  available: boolean
  source: string
  reason?: string
  data?: { code: string; limit: number; announcements: AnnRow[]; updated_at: string }
}

interface FundFlowRow {
  date: string
  close: number | null
  change_pct: number | null
  turnover: number | null
  net_amount: number | null
  super_large_net: number | null
}

interface FundFlowResp {
  available: boolean
  source: string
  reason?: string
  data?: { code: string; days: number; rows: FundFlowRow[]; updated_at: string }
}

interface FinIndicator {
  name: string
  group: string
  values: { period: string; value: number | null }[]
}

interface FinancialsResp {
  available: boolean
  source: string
  reason?: string
  data?: { code: string; periods: string[]; indicators: FinIndicator[]; updated_at: string }
}

/** 后端金额单位统一为万元 */
export function wan(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : ''
  return `${sign}${v.toFixed(0)}万`
}

/** A股红涨绿跌; 港股/美股走 .HK/.US 后缀, 深度数据是 A股独有 */
export function isAShare(symbol: string): boolean {
  return !/\.(HK|US)$/i.test(symbol)
}

/** 加载/失败/不可用 三种状态行(各面板共用, 保持文案口径一致)。 */
function StateLine({ loading, warn, text }: { loading?: boolean; warn?: boolean; text: string }) {
  return (
    <div className={`flex items-center gap-2 py-3 text-xs ${warn ? 'text-amber-500' : 'text-muted'}`}>
      {loading
        ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
        : warn
          ? <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          : null}
      {text}
    </div>
  )
}

function PanelShell({ icon, title, subtitle, children }: {
  icon: React.ReactNode
  title: string
  subtitle: string
  children: React.ReactNode
}) {
  return (
    <section className="border-t border-border px-4 py-3">
      <div className="mb-1 flex items-center gap-2">
        {icon}
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        <span className="text-xs text-muted">{subtitle}</span>
      </div>
      {children}
    </section>
  )
}

// ───────────────────────────────────────────── 龙虎榜

function SeatRow({ seat }: { seat: Seat }) {
  return (
    <div className="flex items-baseline gap-2 py-0.5">
      <span className="truncate text-secondary" title={seat.name}>
        {seat.is_institution && <span className="mr-1 text-amber-500">机构</span>}
        {seat.is_northbound && <span className="mr-1 text-sky-400">北向</span>}
        {seat.name}
      </span>
      <span className={`ml-auto shrink-0 tabular-nums ${priceColorClass(seat.net)}`}>
        {wan(seat.net)}
      </span>
    </div>
  )
}

export function DragonTigerPanel({ symbol }: { symbol: string }) {
  const query = useQuery({
    queryKey: ['astock', 'dragon', symbol],
    queryFn: () => request<DragonResp>(`/api/astock/dragon/${encodeURIComponent(symbol)}?days=30`),
    staleTime: 300_000,
  })

  const body = () => {
    if (query.isLoading) return <StateLine loading text="加载中…" />
    if (query.error) return <StateLine warn text={`请求失败: ${(query.error as Error).message}`} />
    const resp = query.data
    if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
    const data = resp.data
    if (!data) return <StateLine warn text="不可用: 返回结构异常" />

    if (data.records.length === 0) {
      return (
        <div className="py-3 text-xs text-muted">
          近 {data.window_days} 天未登上龙虎榜（该股期间未触发上榜条件）。
        </div>
      )
    }

    const latest = data.records[0]
    const inst = data.institution
    const nb = data.northbound

    return (
      <div className="space-y-2">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
          <span className="text-muted">上榜 <b className="text-foreground">{data.records.length}</b> 次</span>
          <span className="text-muted">{latest.date} · {latest.reason}</span>
          <span className="text-muted">
            净买 <b className={priceColorClass(latest.net_buy)}>{wan(latest.net_buy)}</b>
          </span>
          <span className="text-muted">换手 {latest.turnover}%</span>
        </div>
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
          <span className="text-muted">
            机构 {inst.seat_count} 席 · 净{' '}
            <b className={priceColorClass(inst.net_amt)}>{wan(inst.net_amt)}</b>
          </span>
          {nb.present && (
            <span className="text-muted">
              北向净 <b className={priceColorClass(nb.net_amt)}>{wan(nb.net_amt)}</b>
            </span>
          )}
        </div>
        {(data.seats.buy.length > 0 || data.seats.sell.length > 0) && (
          <div className="grid gap-3 pt-1 sm:grid-cols-2">
            <div>
              <div className="mb-1 flex items-center gap-1 text-xs text-muted">
                <TrendingUp className="h-3 w-3" /> 买入席位 TOP5
              </div>
              {data.seats.buy.map((s, i) => <SeatRow key={`b${i}`} seat={s} />)}
            </div>
            <div>
              <div className="mb-1 flex items-center gap-1 text-xs text-muted">
                <TrendingDown className="h-3 w-3" /> 卖出席位 TOP5
              </div>
              {data.seats.sell.map((s, i) => <SeatRow key={`s${i}`} seat={s} />)}
            </div>
          </div>
        )}
        <div className="pt-1 text-[11px] text-muted">来源 东方财富 datacenter · {data.updated_at}</div>
      </div>
    )
  }

  return (
    <PanelShell icon={<Flame className="h-4 w-4 text-muted" />} title="龙虎榜" subtitle="近 30 天 · 席位与机构动向">
      {body()}
    </PanelShell>
  )
}

// ───────────────────────────────────────────── 研报

/** 评级着色: 买入/增持偏多(红), 减持/卖出偏空(绿), 其余中性。A股口径红涨绿跌。 */
function ratingClass(rating: string): string {
  if (/买入|增持/.test(rating)) return 'text-danger'
  if (/减持|卖出/.test(rating)) return 'text-success'
  return 'text-secondary'
}

export function ResearchPanel({ symbol, limit = 12 }: { symbol: string; limit?: number }) {
  const query = useQuery({
    queryKey: ['astock', 'research', symbol, limit],
    queryFn: () => request<ResearchResp>(`/api/astock/research/${encodeURIComponent(symbol)}?limit=${limit}`),
    staleTime: 300_000,
  })

  const body = () => {
    if (query.isLoading) return <StateLine loading text="加载中…" />
    if (query.error) return <StateLine warn text={`请求失败: ${(query.error as Error).message}`} />
    const resp = query.data
    if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
    const rows = resp.data?.reports ?? []
    if (rows.length === 0) return <div className="py-3 text-xs text-muted">近期无机构研报覆盖。</div>

    return (
      <div className="space-y-1.5">
        {rows.map((r, i) => (
          <div key={`${r.date}-${i}`} className="flex items-baseline gap-2 text-xs">
            <span className="shrink-0 font-mono text-muted">{r.date}</span>
            <span className="w-24 shrink-0 truncate text-secondary" title={r.org}>{r.org}</span>
            {r.rating && <span className={`shrink-0 font-semibold ${ratingClass(r.rating)}`}>{r.rating}</span>}
            <span className="truncate text-foreground" title={r.title}>
              {r.url
                ? <a href={r.url} target="_blank" rel="noreferrer" className="hover:underline">{r.title}</a>
                : r.title}
            </span>
            <span className="ml-auto shrink-0 tabular-nums text-muted">
              {r.target_price != null && <span>目标 {r.target_price.toFixed(0)} · </span>}
              {r.eps_this_year != null && <span>EPS {r.eps_this_year.toFixed(2)}</span>}
            </span>
          </div>
        ))}
        <div className="pt-1 text-[11px] text-muted">来源 东方财富研报 · {resp.data?.updated_at}</div>
      </div>
    )
  }

  return (
    <PanelShell icon={<FileText className="h-4 w-4 text-muted" />} title="机构研报" subtitle="评级 · 目标价 · EPS 预测">
      {body()}
    </PanelShell>
  )
}

// ───────────────────────────────────────────── 公告

export function AnnouncementsPanel({ symbol, limit = 12 }: { symbol: string; limit?: number }) {
  const query = useQuery({
    queryKey: ['astock', 'announcements', symbol, limit],
    queryFn: () => request<AnnResp>(`/api/astock/announcements/${encodeURIComponent(symbol)}?limit=${limit}`),
    staleTime: 300_000,
  })

  const body = () => {
    if (query.isLoading) return <StateLine loading text="加载中…" />
    if (query.error) return <StateLine warn text={`请求失败: ${(query.error as Error).message}`} />
    const resp = query.data
    if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
    const rows = resp.data?.announcements ?? []
    if (rows.length === 0) return <div className="py-3 text-xs text-muted">暂无公告。</div>

    return (
      <div className="space-y-1.5">
        {rows.map((a, i) => (
          <div key={`${a.date}-${i}`} className="flex items-baseline gap-2 text-xs">
            <span className="shrink-0 font-mono text-muted">{a.date}</span>
            {a.type && (
              <span className="shrink-0 rounded bg-elevated px-1 text-[11px] text-secondary">{a.type}</span>
            )}
            <span className="truncate text-foreground" title={a.title}>
              {a.url
                ? <a href={a.url} target="_blank" rel="noreferrer" className="hover:underline">{a.title}</a>
                : a.title}
            </span>
          </div>
        ))}
        <div className="pt-1 text-[11px] text-muted">来源 巨潮资讯 · {resp.data?.updated_at}</div>
      </div>
    )
  }

  return (
    <PanelShell icon={<Bell className="h-4 w-4 text-muted" />} title="公司公告" subtitle="巨潮全文 · 点击看原文">
      {body()}
    </PanelShell>
  )
}

// ───────────────────────────────────────────── 资金流

export function FundFlowPanel({ symbol, days = 20 }: { symbol: string; days?: number }) {
  const query = useQuery({
    queryKey: ['astock', 'fundflow', symbol, days],
    queryFn: () => request<FundFlowResp>(`/api/astock/fundflow/${encodeURIComponent(symbol)}?days=${days}`),
    staleTime: 300_000,
  })

  const body = () => {
    if (query.isLoading) return <StateLine loading text="加载中…" />
    if (query.error) return <StateLine warn text={`请求失败: ${(query.error as Error).message}`} />
    const resp = query.data
    if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
    const rows = resp.data?.rows ?? []
    if (rows.length === 0) return <div className="py-3 text-xs text-muted">暂无资金流数据。</div>

    return (
      <div className="space-y-1.5">
        <div className="flex items-baseline gap-2 text-[11px] text-muted">
          <span className="w-20 shrink-0">日期</span>
          <span className="w-16 shrink-0 text-right">收盘</span>
          <span className="w-14 shrink-0 text-right">涨跌</span>
          <span className="flex-1 text-right">主力净额</span>
          <span className="flex-1 text-right">超大单净额</span>
        </div>
        {rows.map((r, i) => (
          <div key={`${r.date}-${i}`} className="flex items-baseline gap-2 text-xs">
            <span className="w-20 shrink-0 font-mono text-muted">{r.date}</span>
            <span className="w-16 shrink-0 text-right tabular-nums">{r.close?.toFixed(2) ?? '—'}</span>
            <span className={`w-14 shrink-0 text-right tabular-nums ${priceColorClass(r.change_pct)}`}>
              {r.change_pct != null ? `${r.change_pct > 0 ? '+' : ''}${r.change_pct.toFixed(2)}%` : '—'}
            </span>
            <span className={`flex-1 text-right tabular-nums ${priceColorClass(r.net_amount)}`}>
              {wan(r.net_amount)}
            </span>
            <span className={`flex-1 text-right tabular-nums ${priceColorClass(r.super_large_net)}`}>
              {wan(r.super_large_net)}
            </span>
          </div>
        ))}
        <div className="pt-1 text-[11px] text-muted">
          来源 新浪财经（东财 push2 在本机网络不可达）· {resp.data?.updated_at}
        </div>
      </div>
    )
  }

  return (
    <PanelShell icon={<Waves className="h-4 w-4 text-muted" />} title="资金流向" subtitle="主力净额 · 超大单（万元）">
      {body()}
    </PanelShell>
  )
}

// ───────────────────────────────────────────── 财务摘要

/** 报告期 20260630 → 2026-06-30 */
function fmtPeriod(p: string): string {
  return p.length === 8 ? `${p.slice(0, 4)}-${p.slice(4, 6)}-${p.slice(6, 8)}` : p
}

/** 大数自动缩写成 亿/万, 便于横表阅读 */
function fmtBig(v: number | null): string {
  if (v == null || Number.isNaN(v)) return '—'
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (abs >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return v.toFixed(2)
}

// ───────────────────────────────────────────── 分红送配

interface XdxrEvent {
  date: string
  category: number
  name: string
  cash_per_share?: number
  bonus_share_ratio?: number
  rights_issue_ratio?: number
  rights_issue_price?: number
  pre_total_shares_wan?: number
  post_total_shares_wan?: number
}

interface XdxrResp {
  available: boolean
  source: string
  reason?: string
  data?: {
    code: string
    protocol: string
    events: XdxrEvent[]
    count: number
    category_counts: Record<number, number>
    cash_events: number
    coverage: { from: string; to: string }
    unit_note: string
    adjust_note: string
    updated_at: string
  }
}

/** 按年汇总每股现金分红 —— 一眼看清"这家公司一年给多少钱", 而不用数表格行数。 */
function yearlyCash(events: XdxrEvent[]): { year: string; cash: number; times: number }[] {
  const map = new Map<string, { cash: number; times: number }>()
  for (const e of events) {
    if (e.category !== 1) continue
    const y = e.date.slice(0, 4)
    const cur = map.get(y) ?? { cash: 0, times: 0 }
    cur.cash += e.cash_per_share ?? 0
    cur.times += 1
    map.set(y, cur)
  }
  return [...map.entries()]
    .map(([year, v]) => ({ year, cash: v.cash, times: v.times }))
    .sort((a, b) => b.year.localeCompare(a.year))
}

export function XdxrPanel({ symbol, limit = 12 }: { symbol: string; limit?: number }) {
  const query = useQuery({
    queryKey: ['astock', 'xdxr', symbol],
    queryFn: () => request<XdxrResp>(`/api/astock/xdxr/${encodeURIComponent(symbol)}`),
    staleTime: 3_600_000,   // 除权事件是季度级静态数据, 别跟逐笔一样频繁刷新
  })

  const body = () => {
    if (query.isLoading) return <StateLine loading text="加载中…" />
    if (query.error) return <StateLine warn text={`请求失败: ${(query.error as Error).message}`} />
    const resp = query.data
    if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
    const data = resp.data
    if (!data) return <StateLine warn text="不可用: 返回结构异常" />

    if (data.count === 0 || data.cash_events === 0) {
      // 新股从未分红是真答案 —— 不能显示"取不到"(把"没分过"谎报成"接口坏")
      return (
        <div className="py-3 text-xs text-muted">
          该股自 {data.coverage.from} 以来没有任何分红 / 送转 / 配股记录。
        </div>
      )
    }

    const cash = data.events.filter(e => e.category === 1)
    const latest = cash[cash.length - 1]
    const years = yearlyCash(cash)
    const maxCash = Math.max(...years.map(y => y.cash), 0.0001)
    const rows = cash.slice(-limit).reverse()

    return (
      <div className="space-y-2">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
          <span className="text-muted">
            上市以来现金分红 <b className="text-foreground">{data.cash_events}</b> 次
          </span>
          <span className="text-muted">
            最近一次 {latest.date}
            {latest.cash_per_share ? ` · 每股 ${latest.cash_per_share.toFixed(4)} 元` : ''}
            {latest.bonus_share_ratio ? ` · 每 10 股送转 ${(latest.bonus_share_ratio * 10).toFixed(2)} 股` : ''}
          </span>
          <span className="text-muted">记录覆盖 {data.coverage.from} ~ {data.coverage.to}</span>
        </div>

        {/* 年度派息柱状条 —— 纯 CSS 宽度, 不引第三方图表库 */}
        <div className="space-y-1">
          {years.slice(0, 6).map(y => (
            <div key={y.year} className="flex items-center gap-2 text-xs">
              <span className="w-10 shrink-0 tabular-nums text-muted">{y.year}</span>
              <div className="h-2 flex-1 rounded bg-border/40">
                <div
                  className="h-2 rounded bg-danger/60"
                  style={{ width: `${Math.max(2, (y.cash / maxCash) * 100)}%` }}
                />
              </div>
              <span className="w-24 shrink-0 text-right tabular-nums text-secondary">
                {y.cash.toFixed(4)} 元/股
              </span>
              <span className="w-10 shrink-0 text-right text-muted">{y.times}次</span>
            </div>
          ))}
        </div>

        <div className="-mx-1 overflow-x-auto">
          <table className="w-full min-w-[560px] border-collapse text-xs">
            <thead>
              <tr className="text-muted">
                <th className="px-1 py-1 text-left font-normal">除权日</th>
                <th className="px-1 py-1 text-right font-normal">每股派息</th>
                <th className="px-1 py-1 text-right font-normal">每10股送转</th>
                <th className="px-1 py-1 text-right font-normal">每10股配股</th>
                <th className="px-1 py-1 text-right font-normal">配股价</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e, i) => (
                <tr key={`${e.date}-${i}`} className="border-t border-border/60">
                  <td className="px-1 py-1 tabular-nums text-secondary">{e.date}</td>
                  <td className="px-1 py-1 text-right tabular-nums text-foreground">
                    {e.cash_per_share ? e.cash_per_share.toFixed(4) : '—'}
                  </td>
                  <td className="px-1 py-1 text-right tabular-nums text-foreground">
                    {e.bonus_share_ratio ? (e.bonus_share_ratio * 10).toFixed(2) : '—'}
                  </td>
                  <td className="px-1 py-1 text-right tabular-nums text-foreground">
                    {e.rights_issue_ratio ? (e.rights_issue_ratio * 10).toFixed(2) : '—'}
                  </td>
                  <td className="px-1 py-1 text-right tabular-nums text-foreground">
                    {e.rights_issue_price ? e.rights_issue_price.toFixed(2) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <p className="text-[11px] leading-relaxed text-muted">{data.unit_note}</p>
        {/* 这句话很重要: 有人会拿这些事件自己去推复权价, 得先告诉他偏差边界在哪 */}
        <p className="text-[11px] leading-relaxed text-muted">{data.adjust_note}</p>
        <div className="pt-1 text-[11px] text-muted">
          来源 通达信标准市 GetXdxrInfoCmd · {data.updated_at}
        </div>
      </div>
    )
  }

  return (
    <PanelShell
      icon={<Gift className="h-4 w-4 text-muted" />}
      title="分红送配"
      subtitle="除权除息全事件 · 年度派息与配股明细"
    >
      {body()}
    </PanelShell>
  )
}

// ───────────────────────────────────────────── 财务摘要

export function FinancialsPanel({ symbol, periods = 8 }: { symbol: string; periods?: number }) {
  const query = useQuery({
    queryKey: ['astock', 'financials', symbol, periods],
    queryFn: () => request<FinancialsResp>(`/api/astock/financials/${encodeURIComponent(symbol)}?periods=${periods}`),
    staleTime: 300_000,
  })

  const body = () => {
    if (query.isLoading) return <StateLine loading text="加载中…" />
    if (query.error) return <StateLine warn text={`请求失败: ${(query.error as Error).message}`} />
    const resp = query.data
    if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
    const data = resp.data
    if (!data) return <StateLine warn text="不可用: 返回结构异常" />

    // 按 group 分组, 保持后端返回顺序(常用指标在最前)
    const groups: { name: string; items: FinIndicator[] }[] = []
    for (const ind of data.indicators) {
      let g = groups.find(x => x.name === ind.group)
      if (!g) { g = { name: ind.group || '其它', items: [] }; groups.push(g) }
      g.items.push(ind)
    }

    return (
      <div className="-mx-1 overflow-x-auto">
        {groups.map(g => (
          <div key={g.name} className="mb-3">
            <div className="mb-1 text-xs font-semibold text-secondary">{g.name}</div>
            <table className="w-full min-w-[640px] border-collapse text-xs">
              <thead>
                <tr className="text-muted">
                  <th className="sticky left-0 bg-surface px-1 py-1 text-left font-normal">指标</th>
                  {data.periods.map(p => (
                    <th key={p} className="px-1 py-1 text-right font-normal">{fmtPeriod(p)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {g.items.map(ind => (
                  <tr key={`${g.name}-${ind.name}`} className="border-t border-border/60">
                    <td className="sticky left-0 bg-surface px-1 py-1 text-secondary">{ind.name}</td>
                    {ind.values.map(v => (
                      <td key={v.period} className="px-1 py-1 text-right tabular-nums text-foreground">
                        {fmtBig(v.value)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
        <div className="pt-1 text-[11px] text-muted">来源 东财财务指标（akshare）· {data.updated_at}</div>
      </div>
    )
  }

  return (
    <PanelShell icon={<BarChart3 className="h-4 w-4 text-muted" />} title="财务摘要" subtitle="7 组 80 指标 × 最近报告期">
      {body()}
    </PanelShell>
  )
}
