import { useQuery } from '@tanstack/react-query'
import { useParams, Link } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  FileText,
  Loader2,
  type LucideIcon,
} from 'lucide-react'
import { MarketWatchlistButton } from '@/components/MarketWatchlistButton'
import { StockDailyKChart } from '@/components/StockDailyKChart'

interface USRealtime {
  symbol: string
  name?: string
  code?: string
  price: number | null
  pre_close: number | null
  open: number | null
  high: number | null
  low: number | null
  volume: number | null
  amount: number | null
  change_pct: number | null
  source: string
  market?: string
}

// --- 九章融合 P0/P1: OpenBB 深度盘口 / SEC 财报 / SEC 报送 ---
// 失败契约: HTTP 200 + { available: false, reason } — 面板必须 fail-closed 展示 reason, 绝不显示假数据

interface DeepQuoteResp {
  available: boolean
  source: string
  reason?: string
  data?: {
    symbol?: string
    name?: string
    exchange?: string
    last_price?: number | null
    bid?: number | null
    ask?: number | null
    bid_size?: number | null
    ask_size?: number | null
    ma_50d?: number | null
    ma_200d?: number | null
    year_high?: number | null
    year_low?: number | null
    volume_average?: number | null
    currency?: string
  }
}

interface FinancialPoint {
  start?: string
  end?: string
  val: number | null
}

interface FinancialsResp {
  available: boolean
  source: string
  reason?: string
  cik?: string | number
  count?: number
  metrics?: {
    revenue?: { unit?: string; quarterly?: FinancialPoint[] }
    net_income?: { unit?: string; quarterly?: FinancialPoint[] }
  }
}

interface FilingsResp {
  available: boolean
  source: string
  reason?: string
  data?: { form_type?: string | null; form?: string | null; filing_date?: string | null }[]
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

/**
 * 美股个股页 — 实时行情卡片 + 深度数据三面板 + A股同款日K蜡烛图。
 *
 * K线走 /api/kline/daily (repository.get_daily 对 .US symbol 分流读
 * kline_hk_us_enriched 本地全量 enriched), 与 A股 StockDailyKChart 完全同源。
 * 美股无涨跌停, 关闭 limitMarkers。
 *
 * 九章融合 P0/P1 面板 (fail-closed: available=false 或请求失败时显示 reason):
 * - 深度盘口: /api/us/deep/quote (OpenBB) — bid/ask+size, 50/200日均线, 年内高低
 * - SEC 财报: /api/us/financials (sec-edgar) — quarterly 最新4期营收/净利 (序列升序, 末尾最新)
 * - SEC 报送: /api/us/deep/filings (OpenBB) — 最新8条 form_type + filing_date
 */
export function USStockAnalysisPage() {
  const { symbol: rawSymbol = 'AAPL.US' } = useParams<{ symbol: string }>()
  const symbol = rawSymbol.toUpperCase()

  const realtime = useQuery({
    queryKey: ['us', 'realtime', symbol],
    queryFn: () => fetchJson<USRealtime>(`/api/us/realtime/${encodeURIComponent(symbol)}`),
    refetchInterval: 60_000,
  })

  const deepQuote = useQuery({
    queryKey: ['us', 'deep-quote', symbol],
    queryFn: () =>
      fetchJson<DeepQuoteResp>(
        `/api/us/deep/quote/${encodeURIComponent(symbol)}?market=US`,
      ),
    staleTime: 30_000,
  })

  const financials = useQuery({
    queryKey: ['us', 'financials', symbol],
    queryFn: () => fetchJson<FinancialsResp>(`/api/us/financials/${encodeURIComponent(symbol)}`),
    staleTime: 600_000,
  })

  const filings = useQuery({
    queryKey: ['us', 'filings', symbol],
    queryFn: () =>
      fetchJson<FilingsResp>(`/api/us/deep/filings/${encodeURIComponent(symbol)}?limit=8`),
    staleTime: 600_000,
  })

  const r = realtime.data
  const changeColor =
    r?.change_pct == null
      ? 'text-fg-muted'
      : r.change_pct >= 0
        ? 'text-emerald-500'
        : 'text-rose-500'

  // fail-closed 归一化: available=false → reason; available=true 但无数据 → '无数据'
  const quoteReason = respReason(deepQuote.data, !!deepQuote.data?.data)
  const finReason = respReason(
    financials.data,
    (financials.data?.metrics?.revenue?.quarterly?.length ?? 0) > 0,
  )
  const filingsReason = respReason(filings.data, (filings.data?.data?.length ?? 0) > 0)

  const dq = quoteReason ? undefined : deepQuote.data?.data
  // quarterly 升序 (最新在末尾) → 取最新4期并倒序为最新在前
  const revQ = financials.data?.metrics?.revenue?.quarterly ?? []
  const niByEnd = new Map(
    (financials.data?.metrics?.net_income?.quarterly ?? []).map((p) => [p.end ?? '', p.val]),
  )
  const finRows = revQ
    .slice(-4)
    .reverse()
    .map((p) => ({ end: p.end, revenue: p.val, netIncome: p.end ? (niByEnd.get(p.end) ?? null) : null }))
  const filingList = filings.data?.data ?? []

  // 美股惯例: 涨绿跌红 (与中国相反)
  return (
    <div className="p-6 space-y-6">
      <div className="flex items-baseline gap-3">
        <Link to="/us" className="text-sm text-fg-muted hover:text-fg">← 美股热门池</Link>
        <span className="text-fg-muted">/</span>
        <h1 className="text-2xl font-semibold text-fg">
          {r?.name || symbol} <span className="text-sm text-fg-muted font-mono">{symbol}</span>
        </h1>
        <span className="inline-block px-1.5 py-0.5 text-xs rounded bg-blue-500/10 text-blue-500">
          US · USD
        </span>
        <span className="ml-auto">
          <MarketWatchlistButton symbol={symbol} market="us" />
        </span>
        {r && (
          <span className="text-xs text-fg-muted">
            数据源: {r.source} {r.source === 'yfinance' ? '(延迟 15min)' : ''}
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card label="现价">
          {r?.price != null ? (
            <span className={`text-2xl font-bold ${changeColor}`}>
              ${r.price.toFixed(2)}
            </span>
          ) : (
            <span className="text-fg-muted">--</span>
          )}
        </Card>
        <Card label="涨跌幅">
          {r?.change_pct != null ? (
            <span className={`text-2xl font-bold ${changeColor}`}>
              {r.change_pct >= 0 ? '+' : ''}
              {r.change_pct.toFixed(2)}%
            </span>
          ) : (
            <span className="text-fg-muted">--</span>
          )}
        </Card>
        <Card label="今开">{r?.open != null ? `$${r.open.toFixed(2)}` : '--'}</Card>
        <Card label="昨收">{r?.pre_close != null ? `$${r.pre_close.toFixed(2)}` : '--'}</Card>
        <Card label="最高">{r?.high != null ? `$${r.high.toFixed(2)}` : '--'}</Card>
        <Card label="最低">{r?.low != null ? `$${r.low.toFixed(2)}` : '--'}</Card>
        <Card label="成交量">
          {r?.volume != null ? formatVolume(r.volume) : '--'}
        </Card>
        <Card label="成交额">
          {r?.amount != null ? formatAmount(r.amount) : '--'}
        </Card>
      </div>

      <div className="text-xs text-fg-muted p-3 rounded border border-border bg-surface/40">
        美股惯例: 绿涨红跌 · T+0 交收 · 无涨跌停 · K线为本地 enriched 全量日K (新浪源优先, yfinance 兜底)
      </div>

      {/* 深度盘口 (OpenBB, P1) */}
      <Panel
        icon={Activity}
        title="深度盘口"
        hint={dq?.exchange ? `OpenBB · ${dq.exchange}` : 'OpenBB'}
        isLoading={deepQuote.isLoading}
        error={deepQuote.error}
        reason={quoteReason}
      >
        {dq && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Card label="买价 Bid">
              <PriceSize price={dq.bid} size={dq.bid_size} />
            </Card>
            <Card label="卖价 Ask">
              <PriceSize price={dq.ask} size={dq.ask_size} />
            </Card>
            <Card label="50日均线">
              {dq.ma_50d != null ? `$${dq.ma_50d.toFixed(2)}` : '--'}
            </Card>
            <Card label="200日均线">
              {dq.ma_200d != null ? `$${dq.ma_200d.toFixed(2)}` : '--'}
            </Card>
            <Card label="年内最高">
              {dq.year_high != null ? `$${dq.year_high.toFixed(2)}` : '--'}
            </Card>
            <Card label="年内最低">
              {dq.year_low != null ? `$${dq.year_low.toFixed(2)}` : '--'}
            </Card>
            <Card label="平均成交量">
              {dq.volume_average != null ? formatVolume(dq.volume_average) : '--'}
            </Card>
          </div>
        )}
      </Panel>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* SEC 财报 (P0) — quarterly 最新4期 */}
        <Panel
          icon={BarChart3}
          title="SEC 财报 · 季度"
          hint={
            financials.data?.cik != null
              ? `SEC EDGAR · CIK ${financials.data.cik}`
              : 'SEC EDGAR'
          }
          isLoading={financials.isLoading}
          error={financials.error}
          reason={finReason}
        >
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-fg-muted border-b border-border">
                <th className="py-1.5 font-medium">报告期</th>
                <th className="py-1.5 font-medium text-right">营收</th>
                <th className="py-1.5 font-medium text-right">净利</th>
              </tr>
            </thead>
            <tbody>
              {finRows.map((row, i) => (
                <tr key={row.end ?? i} className="border-b border-border/40 last:border-0">
                  <td className="py-1.5 font-mono text-fg-muted">{row.end || '--'}</td>
                  <td className="py-1.5 text-right font-mono text-fg">{formatUSD(row.revenue)}</td>
                  <td
                    className={`py-1.5 text-right font-mono ${
                      row.netIncome == null
                        ? 'text-fg-muted'
                        : row.netIncome >= 0
                          ? 'text-emerald-500'
                          : 'text-rose-500'
                    }`}
                  >
                    {formatUSD(row.netIncome)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        {/* SEC 报送 (P1) — 最新8条 */}
        <Panel
          icon={FileText}
          title="SEC 报送"
          hint="SEC EDGAR"
          isLoading={filings.isLoading}
          error={filings.error}
          reason={filingsReason}
        >
          <ul>
            {filingList.map((f, i) => (
              <li
                key={`${f.filing_date ?? ''}-${i}`}
                className="flex items-center justify-between py-1.5 border-b border-border/40 last:border-0"
              >
                <span className="px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-500 font-mono text-xs">
                  {f.form_type || f.form || '--'}
                </span>
                <span className="text-sm text-fg-muted font-mono">
                  {f.filing_date ? f.filing_date.slice(0, 10) : '--'}
                </span>
              </li>
            ))}
          </ul>
        </Panel>
      </div>

      {/* 日K蜡烛图 (A股同款组件, 本地 enriched 全指标) */}
      <StockDailyKChart symbol={symbol} height={560} showLimitMarkers={false} />
    </div>
  )
}

/** available=false → reason; available=true 但无有效数据 → '无数据' */
function respReason(
  resp: { available: boolean; reason?: string } | undefined,
  hasData: boolean,
): string | undefined {
  if (!resp) return undefined
  if (!resp.available) return resp.reason ?? '服务不可用'
  if (!hasData) return '无数据'
  return undefined
}

/** 面板骨架: 标题 + 加载/失败/不可用/内容 四态, fail-closed */
function Panel({
  icon: Icon,
  title,
  hint,
  isLoading,
  error,
  reason,
  children,
}: {
  icon: LucideIcon
  title: string
  hint?: string
  isLoading: boolean
  error: Error | null
  reason?: string
  children?: React.ReactNode
}) {
  return (
    <section className="rounded-lg border border-border bg-surface/60 p-4">
      <div className="flex items-center gap-2 mb-3">
        <Icon className="h-4 w-4 text-fg-muted" />
        <h2 className="text-sm font-semibold text-fg">{title}</h2>
        {hint && <span className="ml-auto text-xs text-fg-muted">{hint}</span>}
      </div>
      {isLoading ? (
        <div className="flex items-center gap-2 text-sm text-fg-muted py-4">
          <Loader2 className="h-4 w-4 animate-spin" /> 加载中…
        </div>
      ) : error ? (
        <div className="flex items-center gap-2 text-sm text-amber-600 py-3">
          <AlertTriangle className="h-4 w-4 shrink-0" /> 请求失败: {error.message}
        </div>
      ) : reason ? (
        <div className="flex items-center gap-2 text-sm text-amber-600 py-3">
          <AlertTriangle className="h-4 w-4 shrink-0" /> 不可用: {reason}
        </div>
      ) : (
        children
      )}
    </section>
  )
}

function PriceSize({ price, size }: { price: number | null | undefined; size: number | null | undefined }) {
  return (
    <span>
      {price != null ? `$${price.toFixed(2)}` : '--'}
      {size != null && (
        <span className="ml-1 text-xs font-normal text-fg-muted">×{size}</span>
      )}
    </span>
  )
}

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface/60 p-3">
      <div className="text-xs text-fg-muted mb-1">{label}</div>
      <div className="text-lg font-semibold text-fg">{children}</div>
    </div>
  )
}

/** 美元大数: $x.xxB / $x.xxM / $x.xK, 负数带前缀 - */
function formatUSD(v: number | null | undefined): string {
  if (v == null) return '--'
  const abs = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (abs >= 1e12) return `${sign}$${(abs / 1e12).toFixed(2)}T`
  if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(2)}B`
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(2)}M`
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`
  return `${sign}$${abs.toFixed(0)}`
}

function formatVolume(v: number): string {
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  if (v >= 1e4) return `${(v / 1e4).toFixed(2)}万`
  return v.toFixed(0)
}

function formatAmount(a: number): string {
  if (a >= 1e8) return `${(a / 1e8).toFixed(2)}亿`
  if (a >= 1e4) return `${(a / 1e4).toFixed(2)}万`
  return a.toFixed(0)
}
