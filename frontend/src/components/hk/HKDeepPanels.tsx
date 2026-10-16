import { useState } from 'react'
import { AlertTriangle, BarChart3, FileText, Loader2, Star } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'

import { request } from '@/lib/api'
import { priceColorClass } from '@/lib/format'

/**
 * 港股深度面板 —— 公告(港交所披露易) + 财报(东财公告原文解析)。
 *
 * 契约与 A股 ``AstockDeepPanels`` 完全一致(改任一侧都要同步改另一侧):
 * - 失败/不可用 → HTTP 200 + {available:false, reason}, 显示 reason, **不编造**;
 * - **窗口内没发过公告是真实答案** —— 显示"窗口内无公告", 不能显示"取不到数据";
 * - 而**财报解析出 0 行是真失败**, 后端已转 available:false, 这里照 reason 显示。
 *
 * 数据源(都过 source_gate 白名单, 无任何 demo/随机兜底):
 *   公告=港交所披露易(live-hkexnews) · 财报=东财公告原文(eastmoney_hk_announcement)
 *
 * ⚠️ 港股没有 A股那套龙虎榜/资金流/研报的免费真源(新浪港股资金流实测返回空数组),
 *    所以这里**只做有真源的两块**, 不为了"看起来对齐"去补空面板。
 */

interface AnnRow {
  title: string
  category: string
  sub_category: string
  announce_at: string | null
  file_type: string | null
  file_size: string | null
  url: string | null
  news_id: string | null
}

interface AnnResp {
  available: boolean
  source: string
  reason?: string
  symbol?: string
  data?: {
    symbol: string
    stock_id: number
    rows: AnnRow[]
    window_days: number
    total_in_window: number
    truncated: boolean
  }
}

interface FinRow {
  period_end: string | null
  announce_date: string | null
  report_currency: string | null
  gross_margin: number | null
  net_margin: number | null
  revenue_yoy: number | null
  net_income_yoy: number | null
  source_url: string | null
  roe: number | null
  bps: number | null
  eps_ttm: number | null
  total_shares: number | null
  provenance: Record<string, {
    formula?: string
    original_lines?: Record<string, string>
    source_url?: string
  }> | null
}

interface FinResp {
  available: boolean
  source: string
  reason?: string
  symbol?: string
  data?: {
    symbol: string
    rows: FinRow[]
    degraded: string | null
    requested_periods: number
    indicators: { key: string; label: string; unit: string | null }[]
    note: string
  }
}

function pct(v: number | null): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`
}

function StateLine({ loading, warn, text }: { loading?: boolean; warn?: boolean; text: string }) {
  return (
    <div className={`flex items-center gap-2 py-3 text-xs ${warn ? 'text-amber-500' : 'text-muted'}`}>
      {loading
        ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
        : warn
          ? <AlertTriangle className="h-3.5 w-3.5" />
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
    <section className="rounded-lg border border-border bg-surface p-3">
      <div className="mb-2 flex items-center gap-2">
        {icon}
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        <span className="text-xs text-muted">{subtitle}</span>
      </div>
      {children}
    </section>
  )
}

// ───────────────────────────────────────────── 公告(港交所披露易)

export function HKAnnouncementsPanel({ symbol, days = 180, limit = 40 }: {
  symbol: string
  days?: number
  limit?: number
}) {
  const query = useQuery({
    queryKey: ['hk', 'announcements', symbol, days, limit],
    queryFn: () => request<AnnResp>(
      `/api/hk/announcements/${encodeURIComponent(symbol)}?days=${days}&limit=${limit}`),
    staleTime: 600_000,
  })

  const body = () => {
    if (query.isLoading) return <StateLine loading text="加载中…" />
    if (query.error) return <StateLine warn text={`请求失败: ${(query.error as Error).message}`} />
    const resp = query.data
    if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
    const data = resp.data
    if (!data) return <StateLine warn text="不可用: 返回结构异常" />

    if (data.rows.length === 0) {
      return (
        <div className="py-3 text-xs text-muted">
          近 {data.window_days} 天披露易没有这只票的公告记录（窗口内确实未披露）。
        </div>
      )
    }

    return (
      <>
        <div className="max-h-96 overflow-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-surface text-muted">
              <tr className="text-left">
                <th className="py-1 pr-2 font-normal">披露时间</th>
                <th className="py-1 pr-2 font-normal">标题</th>
                <th className="py-1 pr-2 font-normal">类别</th>
                <th className="py-1 pr-2 font-normal text-right">文件</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map(r => (
                <tr key={r.news_id ?? r.title} className="border-t border-border/60 align-top">
                  <td className="py-1.5 pr-2 tabular-nums text-muted whitespace-nowrap">
                    {(r.announce_at ?? '—').replace('T', ' ').slice(0, 16)}
                  </td>
                  <td className="py-1.5 pr-2 text-secondary">
                    {r.url
                      ? <a href={r.url} target="_blank" rel="noreferrer"
                           className="hover:text-sky-400 hover:underline">{r.title}</a>
                      : r.title}
                  </td>
                  <td className="py-1.5 pr-2 text-muted">
                    {r.category}
                    {r.sub_category && <span className="ml-1 opacity-70">· {r.sub_category}</span>}
                  </td>
                  <td className="py-1.5 text-right text-muted whitespace-nowrap">
                    {[r.file_type, r.file_size].filter(Boolean).join(' ') || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {data.truncated && (
          <div className="mt-1 text-[11px] text-muted">
            窗口内共 {data.total_in_window} 条, 已按时间倒序显示最近 {data.rows.length} 条。
          </div>
        )}
      </>
    )
  }

  return (
    <PanelShell
      icon={<FileText className="h-3.5 w-3.5 text-muted" />}
      title="公告 / 公司行动"
      subtitle={`港交所披露易 · 近 ${days} 天 · 全类别`}
    >
      {body()}
    </PanelShell>
  )
}

// ───────────────────────────────────────────── 财报(东财公告原文)

function ProvenanceDetails({ row }: { row: FinRow }) {
  /** 溯源: 把"这个比率怎么算出来的"摊开 —— 原始财报行 + 公式 + 公告链接。 */
  const prov = row.provenance
  if (!prov || Object.keys(prov).length === 0) return null
  return (
    <details className="mt-1 text-[11px] text-muted">
      <summary className="cursor-pointer select-none hover:text-secondary">
        溯源（原始财报行 + 公式）
      </summary>
      <div className="mt-1 space-y-1 pl-2 border-l border-border">
        {Object.entries(prov).map(([field, p]) => (
          <div key={field}>
            <div className="text-secondary">{field}</div>
            {p.formula && <div className="font-mono">公式: {p.formula}</div>}
            {p.original_lines && Object.entries(p.original_lines).map(([k, line]) => (
              <div key={k} className="font-mono whitespace-pre-wrap break-all opacity-80">{line}</div>
            ))}
          </div>
        ))}
        {row.source_url && (
          <a href={row.source_url} target="_blank" rel="noreferrer"
             className="inline-block hover:text-sky-400 hover:underline">
            查看公告原文 →
          </a>
        )}
      </div>
    </details>
  )
}

export function HKFinancialsPanel({ symbol, periods = 8 }: {
  symbol: string
  periods?: number
}) {
  const query = useQuery({
    queryKey: ['hk', 'financials', symbol, periods],
    queryFn: () => request<FinResp>(
      `/api/hk/financials/${encodeURIComponent(symbol)}?periods=${periods}`),
    // 财报是季频数据, 且单次抓取 2~15s, 缓存 30min 避免反复打东财被限流
    staleTime: 1_800_000,
  })

  const body = () => {
    if (query.isLoading) return <StateLine loading text="加载中（首次需翻公告原文, 可能要十几秒）…" />
    if (query.error) return <StateLine warn text={`请求失败: ${(query.error as Error).message}`} />
    const resp = query.data
    if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
    const data = resp.data
    if (!data) return <StateLine warn text="不可用: 返回结构异常" />

    if (data.rows.length === 0) {
      return <StateLine warn text="不可用: 未解析出任何报告期" />
    }

    return (
      <>
        {/* 降级提示: 东财限流时只拿到最新一期, 必须说清楚, 不能装成完整多期 */}
        {data.degraded && (
          <div className="mb-2 flex items-start gap-1.5 rounded border border-amber-500/30 bg-amber-500/5 px-2 py-1 text-[11px] text-amber-500">
            <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
            <span>
              已降级: {data.degraded}
              （请求 {data.requested_periods} 期, 实际拿到 {data.rows.length} 期; 10 分钟后会自动重试完整多期）
            </span>
          </div>
        )}
        <div className="overflow-auto">
          <table className="w-full text-xs">
            <thead className="text-muted">
              <tr className="text-left">
                <th className="py-1 pr-2 font-normal">报告期</th>
                <th className="py-1 pr-2 font-normal">公告日</th>
                <th className="py-1 pr-2 font-normal">币种</th>
                <th className="py-1 pr-2 font-normal text-right">毛利率</th>
                <th className="py-1 pr-2 font-normal text-right">净利率</th>
                <th className="py-1 pr-2 font-normal text-right">收入同比</th>
                <th className="py-1 pr-2 font-normal text-right">归母净利同比</th>
                <th className="py-1 pr-2 font-normal text-right">ROE</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r, i) => (
                <tr key={`${r.period_end ?? 'na'}-${r.announce_date ?? i}`}
                    className="border-t border-border/60 align-top">
                  <td className="py-1.5 pr-2 tabular-nums text-foreground whitespace-nowrap">{r.period_end ?? '—'}</td>
                  <td className="py-1.5 pr-2 tabular-nums text-muted whitespace-nowrap">{r.announce_date ?? '—'}</td>
                  <td className="py-1.5 pr-2 text-muted">{r.report_currency ?? '—'}</td>
                  <td className="py-1.5 pr-2 text-right tabular-nums text-secondary">{pct(r.gross_margin)}</td>
                  <td className="py-1.5 pr-2 text-right tabular-nums text-secondary">{pct(r.net_margin)}</td>
                  <td className={`py-1.5 pr-2 text-right tabular-nums ${priceColorClass(r.revenue_yoy)}`}>
                    {pct(r.revenue_yoy)}
                  </td>
                  <td className={`py-1.5 pr-2 text-right tabular-nums ${priceColorClass(r.net_income_yoy)}`}>
                    {pct(r.net_income_yoy)}
                  </td>
                  {/* 该 provider 刻意不产 ROE/BPS/EPS/总股本(见后端注释), 显示 — 不补 0 */}
                  <td className="py-1.5 text-right tabular-nums text-muted">{pct(r.roe)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {data.rows[0] && <ProvenanceDetails row={data.rows[0]} />}
        <div className="mt-2 text-[11px] text-muted">{data.note}</div>
      </>
    )
  }

  return (
    <PanelShell
      icon={<BarChart3 className="h-3.5 w-3.5 text-muted" />}
      title="财务比率"
      subtitle="东财公告原文解析 · 逐字段可溯源"
    >
      {body()}
    </PanelShell>
  )
}


// ───────────────────────────────────────── 券商盈利预测 / 评级 / 目标价
// 源: 东财港股通口径(akshare_hk_forecast), 2026-10-12 实测 00700 返回 50 行。
// 这是港股侧对标 A股「研报」位的数据 —— 每行都是**某家券商对某财年**的真实观点,
// 汇总里的目标价区间与评级分布由这些行聚合得出, 不合成、不预测。
//
// ⚠️ 只有**最近财年**的行才带评级与目标价(券商习惯如此), 远财年只有盈利预测。
//    所以默认「仅看有评级/目标价的」—— 否则一进来满屏 "—" 反而看不出重点。

interface ForecastRow {
  fiscal_year: string | null
  broker: string | null
  rating: string | null
  rating_bucket: string | null
  net_profit_raw: number | null
  eps_raw: number | null
  dps_raw: number | null
  net_profit_yi_hkd: number | null
  eps_hkd: number | null
  dps_hkd: number | null
  target_price: number | null
  update_date: string | null
}

interface ForecastSummary {
  count: number
  brokers: number
  target_high: number | null
  target_low: number | null
  target_mean: number | null
  target_median: number | null
  rating_dist: Record<string, number>
  distinct_fiscal_years: string[]
}

interface ForecastResp {
  available: boolean
  source: string
  reason?: string
  symbol?: string
  data?: {
    symbol: string
    currency: string
    rows: ForecastRow[]
    summary: ForecastSummary
    note: string
  }
}

const RATING_STYLE_HK: Record<string, string> = {
  buy: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400',
  overweight: 'bg-green-500/15 text-green-700 dark:text-green-400',
  hold: 'bg-amber-500/15 text-amber-700 dark:text-amber-500',
  sell: 'bg-rose-500/15 text-rose-700 dark:text-rose-400',
}

function RatingChip({ row }: { row: ForecastRow }) {
  if (!row.rating) return <span className="text-xs text-muted">—</span>
  const cls = row.rating_bucket ? RATING_STYLE_HK[row.rating_bucket] : 'bg-elevated text-secondary'
  return <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${cls}`}>{row.rating}</span>
}

function hkNum(v: number | null, digits = 2): string {
  return v == null || Number.isNaN(v) ? '—' : v.toFixed(digits)
}

export function HKForecastsPanel({ symbol, limit = 60 }: {
  symbol: string
  limit?: number
}) {
  // 默认只看有评级/目标价的行: 远财年行几乎全空, 混在一起会稀释重点
  const [onlyRated, setOnlyRated] = useState(true)

  const query = useQuery({
    queryKey: ['hk', 'forecasts', symbol, limit],
    queryFn: () => request<ForecastResp>(
      `/api/hk/forecasts/${encodeURIComponent(symbol)}?limit=${limit}`),
    staleTime: 600_000,
  })

  const render = () => {
    if (query.isLoading) return <StateLine loading text="加载中…" />
    if (query.error) return <StateLine warn text={`请求失败: ${(query.error as Error).message}`} />
    const resp = query.data
    if (!resp?.available) return <StateLine warn text={`不可用: ${resp?.reason ?? '服务不可用'}`} />
    const data = resp.data
    if (!data) return <StateLine warn text="不可用: 返回结构异常" />

    const rows = onlyRated
      ? data.rows.filter(r => r.rating != null || r.target_price != null)
      : data.rows
    const s = data.summary
    const distEntries = Object.entries(s.rating_dist ?? {})

    return (
      <div className="space-y-2.5">
        {/* 汇总: 目标价区间 + 覆盖券商 + 评级分布 */}
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded border border-border/60 bg-elevated px-3 py-2">
          <Metric label="覆盖券商" value={s.brokers ? `${s.brokers} 家` : '—'} />
          <Metric label="目标价中位" value={hkNum(s.target_median, 1)} unit="港元" />
          <Metric
            label="目标价区间"
            value={s.target_low != null && s.target_high != null
              ? `${hkNum(s.target_low, 1)} ~ ${hkNum(s.target_high, 1)}` : '—'}
            unit={s.target_low != null ? '港元' : undefined}
          />
          {distEntries.length > 0 && (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-muted">评级分布</span>
              {distEntries.map(([bucket, n]) => (
                <span key={bucket}
                      className={`rounded px-1.5 py-0.5 text-xs ${RATING_STYLE_HK[bucket] ?? 'bg-elevated text-secondary'}`}>
                  {bucket} {n}
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-secondary">
            <input
              type="checkbox"
              checked={onlyRated}
              onChange={e => setOnlyRated(e.target.checked)}
              className="h-3 w-3"
            />
            仅看有评级/目标价的（券商通常只给最近财年评级）
          </label>
          <span className="ml-auto text-xs text-muted">
            {rows.length} / {s.count} 条
          </span>
        </div>

        {rows.length === 0
          ? <StateLine text="该标的暂无券商给出的评级或目标价" />
          : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-muted">
                    <th className="py-1 pr-2 text-left font-normal">财年</th>
                    <th className="py-1 pr-2 text-left font-normal">券商</th>
                    <th className="py-1 pr-2 text-left font-normal">评级</th>
                    <th className="py-1 pr-2 text-right font-normal">目标价</th>
                    <th className="py-1 pr-2 text-right font-normal">EPS</th>
                    <th className="py-1 pr-2 text-right font-normal">纯利(亿)</th>
                    <th className="py-1 text-right font-normal">更新</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={`${r.broker ?? 'bk'}-${r.fiscal_year ?? i}-${i}`}
                        className="border-t border-border/60">
                      <td className="py-1 pr-2 text-xs text-muted">{r.fiscal_year ?? '—'}</td>
                      <td className="py-1 pr-2">{r.broker ?? '—'}</td>
                      <td className="py-1 pr-2"><RatingChip row={r} /></td>
                      <td className="py-1 pr-2 text-right font-mono text-xs">{hkNum(r.target_price, 1)}</td>
                      <td className="py-1 pr-2 text-right font-mono text-xs">{hkNum(r.eps_hkd, 2)}</td>
                      <td className="py-1 pr-2 text-right font-mono text-xs">{hkNum(r.net_profit_yi_hkd, 1)}</td>
                      <td className="py-1 text-right text-xs text-muted">{r.update_date ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

        <p className="text-[11px] text-muted">{data.note}</p>
      </div>
    )
  }

  return (
    <PanelShell
      icon={<Star className="h-3.5 w-3.5 text-muted" />}
      title="券商评级 · 目标价"
      subtitle="东财港股通 · 各家券商实盘观点"
    >
      {render()}
    </PanelShell>
  )
}

function Metric({ label, value, unit }: {
  label: string
  value: string
  unit?: string
}) {
  return (
    <div>
      <div className="text-xs text-muted">{label}</div>
      <div className="text-sm font-medium text-foreground">
        {value}
        {unit && <span className="ml-0.5 text-xs font-normal text-muted">{unit}</span>}
      </div>
    </div>
  )
}
