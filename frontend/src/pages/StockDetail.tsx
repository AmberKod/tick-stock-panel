import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, GitCompare, Loader2, Maximize2, Minimize2, Minus, Newspaper, Plus, RotateCcw, X } from 'lucide-react'

import { api, request } from '@/lib/api'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import { PERIOD_DEFAULT_BARS, type KPeriod } from '@/lib/klinePeriod'
import { KLINE_SIGNALS, buildSignalMarkers, type SignalKey } from '@/lib/klineSignals'
import { ADJUST_MODES, type AdjustMode } from '@/lib/adjust'
import { computeChips } from '@/lib/chips'
import { DRAW_TOOLS, loadDrawings, saveDrawings, type Drawing, type DrawTool } from '@/lib/chartDrawings'
import { ChipDistributionPanel } from '@/components/ChipDistributionPanel'
import { TickTapePanel } from '@/components/TickTapePanel'
import { QK } from '@/lib/queryKeys'
import { EstimateTag } from '@/components/EstimateNotice'
import {
  MAIN_CHART_TYPES,
  StockDailyKChart,
  type MainChartType,
  type MklinePeriod,
  type StockDailyKChartResult,
} from '@/components/StockDailyKChart'
import type { ChartPriceLine } from '@/components/EChartsCandlestick'
import { StockMinuteChart } from '@/components/StockMinuteChart'
import { MarketWatchlistButton } from '@/components/MarketWatchlistButton'
import {
  AnnouncementsPanel,
  DragonTigerPanel,
  FinancialsPanel,
  FundFlowPanel,
  ResearchPanel,
} from '@/components/astock/AstockDeepPanels'

/**
 * 个股详情整页 (/stock/:symbol) —— 对齐九章 `#/stock/{code}` 的信息架构。
 *
 * 与弹窗(StockPreviewDialog)的分工: 弹窗=快速预览, 本页=研究主页。
 * 弹窗受 max-width/最大高度限制, 装不下研究型内容 —— 这是它观感差的根本原因。
 *
 * ⚠️ 刻意**不做**九章的三块, 因为它们的数据是假的:
 *   - 综合投资建议(评分进度条): 九章 synthesis.py 18 个特征里 14 个是 random();
 *   - 另类人群指数: 同上;
 *   - 盘口五档: 九章 demo_orderbook() 是 md5 种子随机 px±0.1%。
 * 我方三纪律(不可用显式剔除不计分母 / 不引未回测打分 / fail-closed),
 * 宁可少一块也不放假数据。缺 PE/PB/总市值/换手 同理: 无真源就不显示。
 */

const PERIODS = [
  { label: '3月', months: 3 },
  { label: '半年', months: 6 },
  { label: '1年', months: 12 },
  { label: '3年', months: 36 },
] as const

/**
 * K线周期。周/月/年**不是**另一份数据 —— 由日K聚合而来(见 lib/klinePeriod),
 * 每根都能回溯到具体日K区间。
 *
 * 「分时」是唯一一个走独立数据源的档位: 腾讯 web.ifzq.gtimg.cn 当日分钟(实测可达),
 * 不需要本地分钟库 —— 之前判断"没分钟库就做不了"是错的, 那个端点是当日全量回传。
 */
const K_PERIODS: { label: string; value: KPeriod }[] = [
  { label: '分时', value: 'minute' },
  { label: '日K', value: 'day' },
  { label: '周K', value: 'week' },
  { label: '月K', value: 'month' },
  { label: '年K', value: 'year' },
]

/**
 * 分钟档。九章周期条有 1/5/15/30/60 分, 我方本地 kline_minute 库是空的(补数要 Pro+),
 * 但腾讯 mkline 端点直连就有真数据 —— 走它, 不做空图。
 */
const MK_TABS: { label: string; value: MklinePeriod }[] = [
  { label: '1分', value: 'm1' },
  { label: '5分', value: 'm5' },
  { label: '15分', value: 'm15' },
  { label: '30分', value: 'm30' },
  { label: '60分', value: 'm60' },
]

/** 实时行情契约(与后端 app/api/astock_deep.py 的 realtime_quote 对齐)。 */
interface AstockQuoteResp {
  available: boolean
  source: string
  reason?: string
  data?: {
    code: string
    name: string | null
    price: number | null
    prev_close: number | null
    open: number | null
    high: number | null
    low: number | null
    change: number | null
    change_pct: number | null
    volume: number | null      // 股
    amount: number | null      // 元
    turnover_pct: number | null
    amplitude_pct: number | null
    pe_ttm: number | null
    pb: number | null
    float_cap: number | null   // 元(流通市值)
    total_cap: number | null   // 元(总市值)
    limit_up: number | null
    limit_down: number | null
    quote_time: string | null
    updated_at: string
  }
}

/**
 * 应用内 A股代码的主流写法是 **带后缀的 ts-code**(`688825.SH` / `000001.SZ`),
 * 也有 `sh600519` / 裸 `600519`。所以判定不能只认 6 位裸数字 —— 那会让带后缀的
 * A股被当成非A股, 深度五块整块不渲染, 页面下半部分直接空掉。
 */
function astockCode(symbol: string): string | null {
  const s = symbol.trim().toUpperCase()
  if (!s || /\.(HK|US)$/.test(s)) return null
  const core = s.replace(/^(SH|SZ|BJ)[.\-]?/, '').replace(/[.\-](SH|SZ|BJ)$/, '')
  return /^\d{6}$/.test(core) ? core : null
}

/** 板块归属 —— 只做展示标签, 不参与任何数据判定。 */
function marketLabel(code: string): string {
  if (code.startsWith('688') || code.startsWith('689')) return '科创板'
  if (code.startsWith('300') || code.startsWith('301')) return '创业板'
  if (code.startsWith('8') || code.startsWith('4')) return '北交所'
  if (code.startsWith('6')) return '沪市'
  if (code.startsWith('0') || code.startsWith('3')) return '深市'
  return 'A股'
}

/** 所属板块/题材契约(与后端 stock_sectors 对齐)。 */
interface AstockSectorsResp {
  available: boolean
  source: string
  reason?: string
  data?: {
    code: string
    name: string | null
    industry: {
      em?: string
      em_parts?: string[]
      csrc?: string
      csrc_parts?: string[]
    } | null
    boards: {
      name: string
      board_code: string | null
      precise: boolean
      rank: number | null
      reason: string | null
    }[]
    updated_at: string
  }
}

type DeepTab = 'fundflow' | 'dragon' | 'research' | 'announcements' | 'financials'

const DEEP_TABS: { key: DeepTab; label: string }[] = [
  { key: 'fundflow', label: '资金流向' },
  { key: 'dragon', label: '龙虎榜' },
  { key: 'research', label: '研报' },
  { key: 'announcements', label: '公告' },
  { key: 'financials', label: '财务摘要' },
]

function rangeFromMonths(months: number): { start: string; end: string } {
  const now = new Date()
  const end = now.toISOString().slice(0, 10)
  const s = new Date(now)
  s.setMonth(s.getMonth() - months)
  return { start: s.toISOString().slice(0, 10), end }
}

function fmtBig(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  const abs = Math.abs(v)
  if (abs >= 1e8) return `${(v / 1e8).toFixed(digits)}亿`
  if (abs >= 1e4) return `${(v / 1e4).toFixed(digits)}万`
  return v.toFixed(digits)
}

/**
 * 头部一行行情 —— 对齐九章行情条(今开/昨收/最高/最低/成交量/成交额/换手/PE/PB/总市值)
 * 并多给两项(振幅/流通市值/涨跌额)。
 *
 * 数据优先级: 腾讯实时行情 > 日K末行。实时源拿不到时**留空而不是编造** ——
 * 九章那些 PE/PB/市值在真源失败时会退化成 random 兜底(见它 server.py demo_quote),
 * 我们不这么做。
 *
 * 🔴 九章 tx_quote 把腾讯 44/45 字段写反, 它的"总市值"实际是流通市值。
 * 后端已按正确口径返回(44=流通 / 45=总), 前端照用即可。
 */
function QuoteStrip({ kline, quote }: {
  kline: StockDailyKChartResult | null
  quote?: AstockQuoteResp
}) {
  const rows = kline?.rawRows ?? []
  const last = rows[rows.length - 1]
  const prev = rows[rows.length - 2]
  const q = quote?.available ? quote.data : undefined
  if (!last && !q) return null

  const kPrevClose = prev?.close ?? NaN
  const kChangeAmt = last ? last.close - kPrevClose : NaN
  const kAmplitude = kPrevClose ? ((last!.high - last!.low) / kPrevClose) * 100 : NaN

  const num = (v: number | null | undefined, d = 2) =>
    v == null || Number.isNaN(v) ? '—' : v.toFixed(d)

  const items: [string, string][] = [
    ['今开', num(q?.open ?? last?.open)],
    ['昨收', num(q?.prev_close ?? (Number.isFinite(kPrevClose) ? kPrevClose : null))],
    ['最高', num(q?.high ?? last?.high)],
    ['最低', num(q?.low ?? last?.low)],
    ['振幅', q?.amplitude_pct != null ? `${num(q.amplitude_pct)}%`
      : Number.isFinite(kAmplitude) ? `${kAmplitude.toFixed(2)}%` : '—'],
    ['成交量', q?.volume != null ? fmtBig(q.volume, 0) : last ? fmtBig(last.volume, 0) : '—'],
    ['成交额', q?.amount != null ? fmtBig(q.amount) : '—'],
    ['换手', q?.turnover_pct != null ? `${num(q.turnover_pct)}%` : '—'],
    ['PE(TTM)', num(q?.pe_ttm)],
    ['PB', num(q?.pb)],
    ['总市值', q?.total_cap != null ? fmtBig(q.total_cap) : '—'],
    ['流通市值', q?.float_cap != null ? fmtBig(q.float_cap) : '—'],
    ['涨跌额', q?.change != null
      ? `${q.change > 0 ? '+' : ''}${num(q.change)}`
      : Number.isFinite(kChangeAmt) ? `${kChangeAmt > 0 ? '+' : ''}${kChangeAmt.toFixed(2)}` : '—'],
  ]

  return (
    <div className="rounded-lg border border-border bg-surface px-3 py-2">
      {/* auto-fill 栅格: 放在宽主列时自动排成多列, 放进窄侧栏时自动收成 2 列 ——
          不用为两种容器各写一份布局。 */}
      <div
        className="grid gap-x-3 gap-y-1.5 text-xs"
        style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(6.5rem, 1fr))' }}
      >
        {items.map(([k, v]) => (
          <div key={k} className="min-w-0">
            <div className="truncate text-[11px] text-fg-muted">{k}</div>
            <div className="truncate font-mono text-[13px] text-fg">{v}</div>
          </div>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 border-t border-border/60 pt-1.5 text-[11px] text-fg-muted">
        {q?.limit_up != null && q?.limit_down != null && (
          <span>涨停 <b className="font-mono text-fg">{num(q.limit_up)}</b> · 跌停{' '}
            <b className="font-mono text-fg">{num(q.limit_down)}</b></span>
        )}
        {quote?.available ? (
          <span>行情源 {quote.source} · {q?.quote_time ? `快照 ${q.quote_time.slice(0, 8)} ${q.quote_time.slice(8, 10)}:${q.quote_time.slice(10, 12)}` : q?.updated_at}</span>
        ) : quote?.reason ? (
          <span className="text-amber-500">实时行情不可用（{quote.reason}）— 上方仅日K末行数据</span>
        ) : null}
      </div>
    </div>
  )
}

/**
 * 所属板块 —— 对齐九章个股页的 "所属板块" chips, 但比它多两层信息:
 *   1. 每个题材 hover 显示**东财给的入选理由**(为什么归进这个板块);
 *   2. 紫色=行业(F10 公司资料)、蓝色=核心题材(IS_PRECISE=1)、灰色=宽泛归类。
 * 九章只有板块名, 且真源拿不到时会退回硬编码映射表照样显示; 我方取不到就整行不显示。
 */
function SectorRow({ symbol }: { symbol: string }) {
  const query = useQuery({
    queryKey: ['astock', 'sectors', symbol],
    queryFn: () => request<AstockSectorsResp>(`/api/astock/sectors/${encodeURIComponent(symbol)}`),
    staleTime: 3_600_000,
    retry: false,
  })

  const d = query.data?.available ? query.data.data : undefined
  // 取不到就一行都不显示 —— 不编板块名, 也不用"暂无"占位伪装成有内容
  if (!d) return null

  const industryLeaf = d.industry?.em_parts?.[d.industry.em_parts.length - 1]
    ?? d.industry?.em ?? null

  return (
    <div className="rounded-lg border border-border bg-surface px-3 py-2">
      <div className="mb-1.5 flex items-center gap-2">
        <h3 className="text-sm font-semibold text-fg">所属板块</h3>
        <span className="ml-auto text-[11px] text-fg-muted">源 {query.data?.source}</span>
      </div>

      <div className="flex flex-wrap gap-1.5 text-xs">
        {d.industry?.em && (
          <Link
            to={`/industry-analysis?board=${encodeURIComponent(industryLeaf ?? d.industry.em)}`}
            title={`东财行业: ${d.industry.em}${d.industry.csrc ? ` · 证监会: ${d.industry.csrc}` : ''}`}
            className="rounded border border-violet-500/40 bg-violet-500/10 px-1.5 py-0.5 text-violet-300 hover:border-violet-400 hover:text-violet-200"
          >
            {d.industry.em}
          </Link>
        )}

        {d.boards.map(b => (
          <Link
            key={b.name}
            to={`/concept-analysis?board=${encodeURIComponent(b.name)}`}
            title={b.reason ? `${b.reason}` : `${b.name} — 东财宽泛归类(非核心题材)`}
            className={cn(
              'rounded border px-1.5 py-0.5 transition-colors',
              b.precise
                ? 'border-sky-500/40 bg-sky-500/10 text-sky-300 hover:border-sky-400 hover:text-sky-200'
                : 'border-border bg-elevated text-fg-muted hover:text-fg-secondary',
            )}
          >
            {b.name}
          </Link>
        ))}
      </div>

      <div className="mt-1.5 text-[11px] text-fg-muted">
        紫=行业 · 蓝=核心题材 · 悬停看入选理由
      </div>
    </div>
  )
}

function NewsCard({ symbol, name }: { symbol: string; name?: string | null }) {
  const query = useQuery({
    queryKey: ['news', 'stock', symbol],
    queryFn: () => api.newsStock(symbol, name ?? undefined, 7, 10),
    staleTime: 300_000,
  })

  return (
    <div className="rounded-lg border border-border bg-surface p-3">
      <div className="mb-2 flex items-center gap-2">
        <Newspaper className="h-4 w-4 text-fg-muted" />
        <h3 className="text-sm font-semibold text-fg">资讯 / 舆情</h3>
        <span className="text-xs text-fg-muted">近 7 天</span>
      </div>
      {query.isLoading && (
        <div className="flex items-center gap-2 py-3 text-xs text-fg-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载中…
        </div>
      )}
      {query.error && <div className="py-3 text-xs text-amber-500">请求失败: {(query.error as Error).message}</div>}
      {query.data && !query.data.success && (
        <div className="py-3 text-xs text-fg-muted">
          资讯不可用: {query.data.error_message ?? '无结果'}（fail-closed，不显示占位内容）
        </div>
      )}
      {query.data?.success && query.data.results.length === 0 && (
        <div className="py-3 text-xs text-fg-muted">近 7 天无相关资讯。</div>
      )}
      <ul className="space-y-1.5">
        {(query.data?.results ?? []).map((n, i) => (
          <li key={`${n.url}-${i}`} className="text-xs">
            <a href={n.url} target="_blank" rel="noreferrer"
               className="text-fg hover:underline" title={n.snippet}>
              {n.title}
            </a>
            <span className="ml-2 text-[11px] text-fg-muted">
              {n.source}{n.published_date ? ` · ${String(n.published_date).slice(0, 10)}` : ''}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function StockDetail() {
  const { symbol: rawSymbol } = useParams<{ symbol: string }>()
  const symbol = (rawSymbol ?? '').trim()

  const [months, setMonths] = useState(6)
  const [kPeriod, setKPeriod] = useState<KPeriod>('day')
  /** 分钟档; 非空时 kPeriod 无意义(图表走 mkline 源)。 */
  const [mkPeriod, setMkPeriod] = useState<MklinePeriod | null>(null)
  const [visibleBars, setVisibleBars] = useState(PERIOD_DEFAULT_BARS.day)
  const [showMA, setShowMA] = useState(true)
  const [kline, setKline] = useState<StockDailyKChartResult | null>(null)
  const [deepTab, setDeepTab] = useState<DeepTab>('fundflow')
  /**
   * 宽屏铺满 = 页面宽度跟随窗口, 不留左右大白边(默认)。
   * 只是"阅读舒适度"的取舍, 不是功能约束 —— 想要窄一点随时点右上角切换, 选择记本地。
   */
  const [wide, setWide] = useState<boolean>(() => localStorage.getItem('stockDetail:wide') !== '0')
  /**
   * K线内标注(全部由 OHLCV 确定性派生, 见 lib/klineSignals)。
   * 默认只开「放量」—— 全开会把 K 线糊住, 而且标注不是建议, 别喧宾夺主。
   */
  const [signals, setSignals] = useState<Set<SignalKey>>(() => new Set<SignalKey>(['volume']))
  /**
   * 主图画法(蜡烛/空心/美国线/收盘线)。画的是同一份 OHLC, 纯视觉编码差异。
   * 记本地 —— 这是长期口味偏好, 每次进页面都要重选很烦。
   */
  /**
   * 复权档。默认前复权(看长期趋势的标准口径)。
   * ⚠️ 注意: 本地 enriched 存的是**不复权价**(TickFlow 除权因子返回 0 行,
   * compute_enriched 的复权是空转), 所以"前复权/后复权"要乘腾讯给的系数才是真的。
   */
  const [adjust, setAdjust] = useState<AdjustMode>(() => {
    const raw = localStorage.getItem('stockDetail:adjust')
    const ok = ADJUST_MODES.some(t => t.value === raw)
    return ok ? (raw as AdjustMode) : 'qfq'
  })
  const changeAdjust = useCallback((next: AdjustMode) => {
    setAdjust(next)
    try { localStorage.setItem('stockDetail:adjust', next) } catch { /* 忽略 */ }
  }, [])
  /** 当前档位是否真的发生过复权(没除权事件时三档一致, 需要说明一下)。 */
  const [adjustInfo, setAdjustInfo] = useState<{ mode: AdjustMode; hasAdjust: boolean; available: boolean } | null>(null)

  const [mainType, setMainType] = useState<MainChartType>(() => {
    const raw = localStorage.getItem('stockDetail:mainType')
    const ok = MAIN_CHART_TYPES.some(t => t.value === raw)
    return ok ? (raw as MainChartType) : 'candle'
  })
  const changeMainType = useCallback((next: MainChartType) => {
    setMainType(next)
    try { localStorage.setItem('stockDetail:mainType', next) } catch { /* 忽略 */ }
  }, [])
  /**
   * 成交量分布(画在 K 线右侧)。按**当前可见区间**统计 —— 缩放后跟着变,
   * 看的是"眼睛里这一段"的成交密集区, 不是全区间。
   */
  const [showVP, setShowVP] = useState<boolean>(() => localStorage.getItem('stockDetail:vp') === '1')
  const changeVP = useCallback((on: boolean) => {
    setShowVP(on)
    try { localStorage.setItem('stockDetail:vp', on ? '1' : '0') } catch { /* 忽略 */ }
  }, [])
  /**
   * 筹码分布。只有拿到流通股本才算得出来 —— 算不出来就不显示(返回 null),
   * 不用"拍一个股本"凑版面。
   */
  const [showChips, setShowChips] = useState<boolean>(() => localStorage.getItem('stockDetail:chips') === '1')
  const changeChips = useCallback((on: boolean) => {
    setShowChips(on)
    try { localStorage.setItem('stockDetail:chips', on ? '1' : '0') } catch { /* 忽略 */ }
  }, [])
  /**
   * 成交明细(腾讯 3 秒快照)。默认关 —— 打开才会去打那个要翻页的上游接口。
   * tail 也持久化, 因为"想看多长时间的流水"是个稳定偏好。
   */
  const [showTicks, setShowTicks] = useState<boolean>(() =>
    localStorage.getItem('stockDetail:ticks') === '1')
  const changeTicks = useCallback((on: boolean) => {
    setShowTicks(on)
    try { localStorage.setItem('stockDetail:ticks', on ? '1' : '0') } catch { /* 忽略 */ }
  }, [])
  const [tickTail, setTickTail] = useState<number>(() => {
    const n = Number(localStorage.getItem('stockDetail:ticksTail'))
    return [100, 200, 500, 1000, 3000].includes(n) ? n : 200
  })
  const changeTickTail = useCallback((n: number) => {
    setTickTail(n)
    try { localStorage.setItem('stockDetail:ticksTail', String(n)) } catch { /* 忽略 */ }
  }, [])
  const ticksQuery = useQuery({
    queryKey: QK.astockTicks(symbol, tickTail),
    queryFn: () => api.astockTicks(symbol, tickTail),
    enabled: showTicks,
    staleTime: 15_000,
    retry: false,
  })
  const ticks = ticksQuery.data
  /** 取不到就整块不显示, 并在工具条上说明原因 —— 不编一行假的成交明细。 */
  const ticksBlocked = showTicks && !ticksQuery.isLoading && (!ticks?.available || !ticks.data)

  /** 双击图表加的水平价格线(本地持久化, 按标的隔离)。 */
  const [priceLines, setPriceLines] = useState<ChartPriceLine[]>(() => {
    try {
      const raw = localStorage.getItem(`stockDetail:priceLines:${(rawSymbol ?? '').trim()}`)
      const parsed = raw ? JSON.parse(raw) : []
      return Array.isArray(parsed) ? parsed.filter((x: any) => Number.isFinite(x?.value)) : []
    } catch {
      return []
    }
  })

  /**
   * A股判定 —— 必须兼容 `688825.SH` 这种带后缀写法(应用内主流格式), 否则深度
   * 五块会被误判成非A股而整块不渲染。港股/美股走另一套深度源, 本页只给行情+K线+资讯。
   */
  const aCode = astockCode(symbol)
  const isAStock = aCode != null

  const dateRange = useMemo(() => rangeFromMonths(months), [months])

  // 标注基于**当前周期那串 K**算(日K就用日K, 分钟K就用分钟K), 不跨周期混用。
  const signalMarkers = useMemo(
    () => buildSignalMarkers(kline?.rows ?? [], signals),
    [kline?.rows, signals],
  )

  /**
   * 筹码分布。输入全为真数据(日K量价 + 本地 stock_info 的流通股本), 输出是这三者
   * 之上的确定性模型结果 —— 流通股本取不到就不算, 不拿估算股本凑版面。
   * 分时档/分钟档不做: 分钟 K 只有最近几百根(几天), 谈不上"筹码沉淀"。
   */
  const chips = useMemo(() => {
    if (!showChips || kPeriod === 'minute' || mkPeriod) return null
    const shares = kline?.stockInfo?.float_shares
    if (!shares || shares <= 0) return null
    const rows = (kline?.rows ?? []).map(r => ({
      date: r.date, high: r.high, low: r.low, close: r.close, volume: r.volume ?? 0,
    }))
    return computeChips(rows, shares)
  }, [showChips, kline?.rows, kline?.stockInfo, kPeriod, mkPeriod])
  /** 开了筹码但拿不到流通股本 —— 明确说清楚为什么没有, 而不是空着让人猜。 */
  const chipsBlocked = showChips && kPeriod !== 'minute' && !mkPeriod && !chips

  /**
   * 画图工具(趋势线/水平线/矩形/斐波那契)。
   * 全部存本地、按标的隔离; 锚点是「日期 + 价格」而不是像素, 缩放平移后仍钉在同一根 K 上。
   * 每条图形带坐标上下文(周期 + 复权档): 复权档一换价格体系就变了,
   * 前复权下画的线放到不复权图上会指错价 —— 所以上下文不匹配时**不渲染**, 而不是画错的位置。
   */
  const [drawings, setDrawings] = useState<Drawing[]>(() => loadDrawings(symbol))
  const [drawTool, setDrawTool] = useState<DrawTool | null>(null)
  const persistDrawings = useCallback((next: Drawing[]) => {
    setDrawings(next)
    saveDrawings(symbol, next)
  }, [symbol])
  // 换标的 → 换成那个标的的图形, 并退出画图模式(避免手上一个工具误画到新标的)
  useEffect(() => {
    setDrawings(loadDrawings(symbol))
    setDrawTool(null)
  }, [symbol])
  /** 当前视图的坐标上下文: 周期 + 分钟档 + 复权档 */
  const drawCtx = `${kPeriod}|${mkPeriod ?? ''}|${adjust}`
  const visibleDrawings = useMemo(
    () => drawings.filter(d => d.ctx === drawCtx),
    [drawings, drawCtx],
  )
  const hiddenDrawings = drawings.length - visibleDrawings.length

  const persistPriceLines = useCallback((next: ChartPriceLine[]) => {
    setPriceLines(next)
    try {
      localStorage.setItem(`stockDetail:priceLines:${symbol}`, JSON.stringify(next))
    } catch { /* 存不下就算了, 不影响看图 */ }
  }, [symbol])

  const toggleSignal = useCallback((key: SignalKey) => {
    setSignals(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  // 实时行情(腾讯) —— 补齐日K里没有的 成交额/换手/PE/PB/市值。10s 短缓存。
  const quoteQuery = useQuery({
    queryKey: ['astock', 'quote', symbol],
    queryFn: () => request<AstockQuoteResp>(`/api/astock/quote/${encodeURIComponent(symbol)}`),
    staleTime: 10_000,
    retry: false,
  })
  const quote = quoteQuery.data
  const rt = quote?.available ? quote.data : undefined

  const rows = kline?.rawRows ?? []
  const last = rows[rows.length - 1]
  const prev = rows[rows.length - 2]
  // 头部价格优先用实时行情(盘中会跳动), 拿不到才退回日K末行
  const headPrice = rt?.price ?? last?.close ?? null
  const changePct = rt?.change_pct ?? last?.change_pct ?? null
  const changeAmt = rt?.change ?? (last && prev ? last.close - prev.close : null)

  if (!symbol) {
    return (
      <div className="p-6 text-sm text-fg-muted">
        缺少股票代码。<Link to="/" className="underline">返回首页</Link>
      </div>
    )
  }

  return (
    // 默认铺满整屏(不再有 1800 上限带来的左右白边); 想要窄版可点右上角按钮切回。
    <div className={cn('w-full space-y-3 px-3 py-3', wide ? '' : 'mx-auto max-w-[1400px]')}>
      {/* ── 头部 ── */}
      <div className="flex flex-wrap items-baseline gap-3">
        <Link to="/" className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-fg">
          <ArrowLeft className="h-3.5 w-3.5" /> 返回
        </Link>
        <h1 className="text-xl font-semibold text-fg">{rt?.name || kline?.name || symbol}</h1>
        <span className="rounded bg-elevated px-1.5 py-0.5 font-mono text-xs text-fg-secondary">{symbol}</span>
        {aCode && (
          <span className="rounded border border-border px-1.5 py-0.5 text-[11px] text-fg-muted">
            {marketLabel(aCode)}
          </span>
        )}
        {headPrice != null && (
          <>
            <span className={cn('text-2xl font-bold tabular-nums', priceColorClass(changePct))}>
              {headPrice.toFixed(2)}
            </span>
            <span className={cn('text-sm tabular-nums', priceColorClass(changePct))}>
              {changeAmt != null ? `${changeAmt > 0 ? '+' : ''}${changeAmt.toFixed(2)}` : ''}
              {changePct != null ? ` (${changePct > 0 ? '+' : ''}${changePct.toFixed(2)}%)` : ''}
            </span>
          </>
        )}
        <span className="ml-auto inline-flex items-center gap-2">
          {/* A股不带后缀 → 组件内部按 'cn' 匹配自选, 不传 market */}
          <MarketWatchlistButton symbol={symbol} />
          <Link
            to={`/compare?symbols=${encodeURIComponent(symbol)}&days=250`}
            className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-1 text-[11px] text-fg-muted hover:text-fg"
            title="把这只票带进多股对比页，再添加其他标的做归一化叠加对比"
          >
            <GitCompare className="h-3 w-3" /> 对比
          </Link>
          <button
            type="button"
            onClick={() => setWide(v => { localStorage.setItem('stockDetail:wide', v ? '0' : '1'); return !v })}
            className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-1 text-[11px] text-fg-muted hover:text-fg"
            title={wide ? '切回标准宽度（左右留白，阅读更聚焦）' : '宽屏铺满（消除左右留白）'}
          >
            {wide ? <Minimize2 className="h-3 w-3" /> : <Maximize2 className="h-3 w-3" />}
            {wide ? '标准宽度' : '铺满'}
          </button>
        </span>
      </div>

      {/* ── 主体两栏: 左=图表+深度(吃满剩余宽度) 右=行情/板块/资讯 ── */}
      <div className="grid items-start gap-3 xl:grid-cols-[minmax(0,1fr)_23rem]">

      {/* 右列 —— 用 order 保证窄屏时仍排在图表之后(DOM 顺序不改, 只改视觉序) */}
      <aside className="order-2 min-w-0 space-y-3">
        {/* ── 行情条 ── */}
        <QuoteStrip kline={kline} quote={quote} />

        {/* ── 所属板块 ── */}
        <SectorRow symbol={symbol} />

        {/* ── 资讯 ── */}
        <NewsCard symbol={symbol} name={kline?.name ?? rt?.name} />
      </aside>

      {/* 左主列 */}
      <div className="order-1 min-w-0 space-y-3">
      {/* ── K 线 + 工具栏 ── */}
      <div className="rounded-lg border border-border bg-surface p-3 space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <div className="inline-flex items-center rounded border border-border bg-elevated p-0.5">
            {K_PERIODS.map(p => (
              <button
                key={p.value}
                type="button"
                onClick={() => {
                  setKPeriod(p.value)
                  setMkPeriod(null)
                  // 分时不用根数控制(0 = 不适用), 保持上一次的缩放以免切回日K被重置
                  const bars = PERIOD_DEFAULT_BARS[p.value]
                  if (bars > 0) setVisibleBars(bars)
                }}
                className={cn('rounded px-2 py-0.5 transition-colors',
                  kPeriod === p.value && !mkPeriod ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
              >
                {p.label}
              </button>
            ))}
          </div>

          {/* 分钟档(真源: 腾讯 mkline)。摆在第二组: 它跟日/周/月/年是并列关系,
              混进第一组会让周期条太长, 而且语义不同(分钟档是另一份数据)。 */}
          {aCode && (
            <div className="inline-flex items-center rounded border border-border bg-elevated p-0.5">
              {MK_TABS.map(p => (
                <button
                  key={p.value}
                  type="button"
                  onClick={() => {
                    setMkPeriod(p.value)
                    setVisibleBars(120)
                  }}
                  className={cn('rounded px-2 py-0.5 transition-colors',
                    mkPeriod === p.value ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
                >
                  {p.label}
                </button>
              ))}
            </div>
          )}

          {/* 分时/分钟档没有「区间」这组控件(区间是日K的回溯窗口, 对当日分时与
              固定 320 根的分钟K都没意义), 摆上去点了没反应反而像坏了 —— 不渲染。
              MA/缩放对分钟K仍然有效, 保留。 */}
          {kPeriod !== 'minute' && !mkPeriod && (
            <>
              <span className="mx-1 h-4 w-px bg-border" />

              <span className="text-fg-muted">区间</span>
              <div className="inline-flex items-center rounded border border-border bg-elevated p-0.5">
                {PERIODS.map(p => (
                  <button
                    key={p.label}
                    type="button"
                    onClick={() => setMonths(p.months)}
                    className={cn('rounded px-2 py-0.5 transition-colors',
                      months === p.months ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
                  >
                    {p.label}
                  </button>
                ))}
              </div>

              <span className="mx-1 h-4 w-px bg-border" />

              <label className="inline-flex items-center gap-1 text-fg-muted">
                <input type="checkbox" checked={showMA} onChange={e => setShowMA(e.target.checked)} />
                MA
              </label>

              <span className="mx-1 h-4 w-px bg-border" />

              <span className="text-fg-muted">缩放</span>
              <button
                type="button"
                onClick={() => setVisibleBars(v => Math.min(500, v + 20))}
                className="rounded border border-border p-1 text-fg-secondary hover:text-fg"
                title="缩小(看更多)"
              >
                <Minus className="h-3 w-3" />
              </button>
              <button
                type="button"
                onClick={() => setVisibleBars(v => Math.max(20, v - 20))}
                className="rounded border border-border p-1 text-fg-secondary hover:text-fg"
                title="放大(看更少)"
              >
                <Plus className="h-3 w-3" />
              </button>
              <button
                type="button"
                onClick={() => setVisibleBars(60)}
                className="rounded border border-border p-1 text-fg-secondary hover:text-fg"
                title="复位"
              >
                <RotateCcw className="h-3 w-3" />
              </button>
              <span className="text-[11px] text-fg-muted">
                当前 {visibleBars} 根 · 副图指标用图表右上控件切换
              </span>
            </>
          )}

          {/* ── 复权 ──
              只作用于日K(以及由日K聚合出的周/月/年)。分钟K与分时没有复权概念
              (腾讯 mkline/分时端点不提供复权序列), 所以这两档下不渲染。 */}
          {kPeriod !== 'minute' && !mkPeriod && (
            <>
              <span className="mx-1 h-4 w-px bg-border" />
              <span className="text-fg-muted">复权</span>
              <div className="inline-flex items-center rounded border border-border bg-elevated p-0.5">
                {ADJUST_MODES.map(t => (
                  <button
                    key={t.value}
                    type="button"
                    onClick={() => changeAdjust(t.value)}
                    title={t.hint}
                    className={cn('rounded px-2 py-0.5 transition-colors',
                      adjust === t.value ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              {adjustInfo && !adjustInfo.hasAdjust && adjustInfo.available && adjust !== 'none' && (
                <span className="text-[11px] text-fg-muted" title="该股在腾讯返回的区间内没有除权事件, 所以三档数值一致 —— 不是开关坏了">
                  (无除权记录)
                </span>
              )}
            </>
          )}

          {/* ── 主图画法 ──
              四种画法共用同一份 OHLC, 换的只是视觉编码(实心/空心/美国线/收盘线)。
              分时档是独立组件(固定画法), 所以这里只在 K 线档出现。 */}
          {kPeriod !== 'minute' && (
            <>
              <span className="mx-1 h-4 w-px bg-border" />
              <span className="text-fg-muted">主图</span>
              <div className="inline-flex items-center rounded border border-border bg-elevated p-0.5">
                {MAIN_CHART_TYPES.map(t => (
                  <button
                    key={t.value}
                    type="button"
                    onClick={() => changeMainType(t.value)}
                    title={t.hint}
                    className={cn('rounded px-2 py-0.5 transition-colors',
                      mainType === t.value ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            </>
          )}

          {/* ── 分布: 成交量分布(画在图上) + 筹码分布(图下面板) ──
              前者是纯统计(把量按价格重新分组), 后者是**模型估算**(换手衰减),
              两者的可信度不一样, 所以按钮 title 里分别写清口径。 */}
          {kPeriod !== 'minute' && (
            <>
              <span className="mx-1 h-4 w-px bg-border" />
              <span className="text-fg-muted">分布</span>
              <button
                type="button"
                onClick={() => changeVP(!showVP)}
                title="成交量分布(Volume Profile): 把可见区间的成交量按价格重新分组, 画在 K 线右侧。单根 K 的量在 [low, high] 内按价格均摊 —— 行情源不给日内逐笔分布, 均摊是不引入额外假设的那一种"
                className={cn('rounded border px-1.5 py-0.5 transition-colors',
                  showVP
                    ? 'border-sky-400/40 bg-sky-400/10 text-sky-500'
                    : 'border-border text-fg-muted hover:text-fg-secondary')}
              >
                成交量
                <EstimateTag className="ml-0.5" title="行情源不提供日内逐笔成交的价格分布, 单根 K 的量在 [low, high] 内按价格均摊 —— 是分摊假设, 不是逐笔真实分布">均摊</EstimateTag>
              </button>
              <button
                type="button"
                onClick={() => changeChips(!showChips)}
                title="筹码分布: 按'每日换手率 + 三角形分布 + 历史筹码衰减'估算的成本分布。这是模型, 不是交易所真实持仓数据"
                className={cn('rounded border px-1.5 py-0.5 transition-colors',
                  showChips
                    ? 'border-amber-400/40 bg-amber-400/10 text-amber-500'
                    : 'border-border text-fg-muted hover:text-fg-secondary')}
              >
                筹码
                <EstimateTag className="ml-0.5" title="筹码分布是模型估算: 交易所不公开各价位持仓量, 图是用日K量价 + 流通股本反推的, 不是真实持仓成本">估算</EstimateTag>
              </button>

              {/* ── 成交明细 ──
                  真数据, 但粒度有上限: 每 3 秒一行的成交快照, 不是逐笔(逐笔要 Level-2)。
                  刻意不把它叫"逐笔" —— 面板里也写死了这个口径。 */}
              <button
                type="button"
                onClick={() => changeTicks(!showTicks)}
                title="成交明细(腾讯): 每 3 秒一行的成交快照, 一行是该时间窗内所有成交的合计。真正的逐笔需要 Level-2 付费行情, 这里取不到也不假装取到。买卖方向是行情商判定, 不是交易所披露字段"
                className={cn('rounded border px-1.5 py-0.5 transition-colors',
                  showTicks
                    ? 'border-sky-400/40 bg-sky-400/10 text-sky-500'
                    : 'border-border text-fg-muted hover:text-fg-secondary')}
              >
                成交明细
                <EstimateTag className="ml-0.5" title="3 秒一行的成交快照, 不是逐笔成交">3秒快照</EstimateTag>
              </button>
            </>
          )}

          {/* ── K线内标注 ──
              全部由当前那串 K 线**确定性派生**(放量/新高/新低/MACD交叉), 不是打分、
              不是买卖建议。口径写在 title 里, 免得被当成某种"信号推荐"。 */}
          {kPeriod !== 'minute' && (
            <>
              <span className="mx-1 h-4 w-px bg-border" />
              <span className="text-fg-muted">标注</span>
              {KLINE_SIGNALS.map(s => (
                <button
                  key={s.key}
                  type="button"
                  onClick={() => toggleSignal(s.key)}
                  title={s.desc}
                  className={cn('rounded border px-1.5 py-0.5 transition-colors',
                    signals.has(s.key)
                      ? 'border-sky-400/40 bg-sky-400/10 text-sky-500'
                      : 'border-border text-fg-muted hover:text-fg-secondary')}
                >
                  {s.label}
                </button>
              ))}
            </>
          )}

          {/* ── 画图工具 ──
              画出来的东西不是数据, 是标注: 只存本地、不参与任何计算、也不会被当成信号。
              锚点存日期+价格, 所以缩放/平移/换主题后仍钉在同一根 K 上。 */}
          {kPeriod !== 'minute' && (
            <>
              <span className="mx-1 h-4 w-px bg-border" />
              <span className="text-fg-muted">画图</span>
              {DRAW_TOOLS.map(t => (
                <button
                  key={t.value}
                  type="button"
                  onClick={() => setDrawTool(cur => (cur === t.value ? null : t.value))}
                  title={t.hint}
                  className={cn('rounded border px-1.5 py-0.5 transition-colors',
                    drawTool === t.value
                      ? 'border-amber-400/40 bg-amber-400/10 text-amber-500'
                      : 'border-border text-fg-muted hover:text-fg-secondary')}
                >
                  {t.label}
                </button>
              ))}
              {drawings.length > 0 && (
                <button
                  type="button"
                  onClick={() => persistDrawings([])}
                  title="清除这只股票上画的所有图形(只清本地存储)"
                  className="rounded border border-border px-1.5 py-0.5 text-fg-muted transition-colors hover:border-red-400/40 hover:text-red-400"
                >
                  清除 {drawings.length}
                </button>
              )}
            </>
          )}

          <span className="ml-auto inline-flex items-center gap-1 text-[11px] text-fg-muted">
            {kPeriod !== 'minute' && <span>双击图表可加水平线 · 滚轮缩放 · 拖动平移</span>}
            <Maximize2 className="h-3 w-3" /> 图表区
          </span>
        </div>

        {/* 画图模式的代价要提前说清: 图层吃掉了鼠标事件, 这段时间滚轮缩放/拖动平移用不了。 */}
        {drawTool && (
          <div className="flex flex-wrap items-baseline gap-x-2 px-1 text-[10.5px] text-fg-muted">
            <span className="text-amber-500">画图模式</span>
            <span>
              滚轮缩放与拖动平移暂时不可用（鼠标事件归画图层），退出工具即恢复；画完点一下当前工具按钮即可退出。
              选中图形后按 Delete 删除，拖动两端圆点可改位置；画到一半按 Esc 取消。
              {drawTool === 'channel' && ' 平行通道分两步：先拖出基准线，再点一下定宽度。'}
              {drawTool === 'text' && ' 文字标注就地输入，回车确认（留空自动丢弃），双击已画的文字可改。'}
            </span>
          </div>
        )}
        {/* 别的周期/复权档下画的图形, 在当前坐标系里没有意义 —— 说明为什么不显示, 而不是默默消失 */}
        {hiddenDrawings > 0 && (
          <div className="px-1 text-[10.5px] text-fg-muted">
            另有 {hiddenDrawings} 条图形是在
            <span className="text-fg-secondary">其他周期/复权档</span>
            下画的（价格体系不同），当前视图不显示 —— 切回去就能看到。
          </div>
        )}

        {/* 已加的水平线 —— 可删。放在图表上方而不是图里, 免得遮 K 线。 */}
        {kPeriod !== 'minute' && priceLines.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 px-1 text-[11px]">
            <span className="text-fg-muted">水平线</span>
            {priceLines.map((l, i) => (
              <span key={`${l.value}-${i}`}
                    className="inline-flex items-center gap-1 rounded border border-border bg-elevated px-1.5 py-0.5 font-mono text-fg-secondary">
                {l.label ?? l.value.toFixed(2)}
                <button
                  type="button"
                  onClick={() => persistPriceLines(priceLines.filter((_, j) => j !== i))}
                  className="text-fg-muted hover:text-fg"
                  title="删除这条水平线"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        {/* 分时走独立的当日分钟源(腾讯), 日/周/月/年走日K聚合 —— 两条路不混。 */}
        {kPeriod === 'minute' ? (
          aCode ? (
            <StockMinuteChart code={aCode} height={430} />
          ) : (
            <div className="flex h-[430px] flex-col items-center justify-center gap-1 px-4 text-center text-xs text-fg-muted">
              <span>当日分时仅覆盖 A股</span>
              <span className="text-[11px] opacity-70">
                {symbol} 不是 A股代码（腾讯分时端点只认沪深北） —— 不拿别的品种的曲线冒充
              </span>
            </div>
          )
        ) : (
          <>
            {/* 图上右侧那条分布带容易被当成"逐笔成交堆出来的真实分布" —— 在图表正上方
                写清它的分摊假设, 别让人默认它比实际更精确。 */}
            {showVP && (
              <div className="mb-1 flex items-start gap-1.5 px-1 text-[10.5px] leading-relaxed text-fg-muted">
                <EstimateTag title="行情源不提供日内逐笔成交的价格分布">均摊口径</EstimateTag>
                <span>
                  K 线右侧的成交量分布：把可见区间每根 K 的成交量在 [low, high]
                  内<b className="text-fg-secondary">按价格均摊</b>后统计（行情源只给日内的开高低收和总量，不给逐笔分布）。
                  均摊是不引入额外假设的那一种，但仍是分摊而非真实分布。
                </span>
              </div>
            )}
            <StockDailyKChart
              symbol={symbol}
              height={430}
              dateRange={dateRange}
              showMA={showMA}
              visibleBars={visibleBars}
              period={kPeriod}
              mklinePeriod={mkPeriod ?? undefined}
              mainType={mainType}
              showVolumeProfile={showVP}
              adjust={adjust}
              onAdjustInfo={setAdjustInfo}
              markers={signalMarkers}
              priceLines={priceLines}
              onPriceDoubleClick={(price) => persistPriceLines([
                ...priceLines,
                { value: price, label: price.toFixed(2), color: '#F59E0B' },
              ])}
              onDataChange={setKline}
              drawings={visibleDrawings}
              drawTool={drawTool}
              drawCtx={drawCtx}
              onDrawingsChange={persistDrawings}
              onDrawToolChange={setDrawTool}
            />
          </>
        )}
      </div>

      {/* 筹码分布面板 —— 放在图表下方而不是图里: 它跟 K 线不是同一坐标(横轴是价格占比),
          叠在图上会挤掉蜡烛的地方。 */}
      {chips && (
        <ChipDistributionPanel result={chips} name={kline?.name} className="mt-2" />
      )}
      {chipsBlocked && (
        <div className="mt-2 rounded-md border border-border bg-surface px-3 py-2 text-[11px] leading-relaxed text-fg-muted">
          筹码分布需要「流通股本」才能算（每日换手率 = 成交量 / 流通股本）—— 本地 stock_info 没给到
          {' '}<span className="font-mono text-fg-secondary">{symbol}</span>{' '}的流通股本，所以这里不做。
          不拿估算股本凑版面。
        </div>
      )}

      {/* ── 成交明细 ──
          腾讯 3 秒快照。取不到就明说原因(停牌/当日无成交/接口没覆盖), 不编一行假的。
          默认关闭 —— 这个接口要翻页, 不该在首屏就打。 */}
      {showTicks && ticks?.available && ticks.data && (
        <TickTapePanel
          rows={ticks.data.rows}
          summary={ticks.data.summary}
          date={ticks.data.date}
          name={kline?.name}
          tail={tickTail}
          loading={ticksQuery.isFetching}
          onRefresh={() => ticksQuery.refetch()}
          onTailChange={changeTickTail}
          className="mt-2"
        />
      )}
      {showTicks && ticksQuery.isLoading && (
        <div className="mt-2 rounded-md border border-border bg-surface px-3 py-2 text-[11px] text-fg-muted">
          正在拉取成交明细（后端要先探到最后一页再回溯取最近 {tickTail} 行）…
        </div>
      )}
      {ticksBlocked && (
        <div className="mt-2 rounded-md border border-border bg-surface px-3 py-2 text-[11px] leading-relaxed text-fg-muted">
          成交明细取不到 —— {ticksQuery.data?.reason ?? '上游无返回'}。
          {' '}常见情形是当日无成交（停牌 / 尚未开盘）或该标的不在腾讯明细接口的覆盖范围内；
          这里不补一行占位数据。
        </div>
      )}

      {/* ── 深度数据 5 tab ──
          这五块的后端(A股深度)只认 A股代码, 港股/美股会 available:false。
          fail-closed 是对的, 但把五个"不可用"面板摆上去观感很差 —— 非 A股
          直接整块不渲染, 并说明原因, 而不是让用户看到一排空面板。 */}
      {!isAStock ? (
        <div className="rounded-lg border border-border bg-surface p-3 text-xs text-fg-muted">
          深度数据（资金流 / 龙虎榜 / 研报 / 公告 / 财务摘要）当前仅覆盖 A股 ——
          非 A股代码不显示这五块，也不会用占位内容冒充。
        </div>
      ) : (
        <div className="rounded-lg border border-border bg-surface p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-fg">深度数据</h3>
            <span className="text-xs text-fg-muted">A股全栈 · 全部为真实数据（过 source_gate 白名单）</span>
            <div className="ml-auto inline-flex items-center rounded border border-border bg-elevated p-0.5">
              {DEEP_TABS.map(t => (
                <button
                  key={t.key}
                  type="button"
                  onClick={() => setDeepTab(t.key)}
                  className={cn('rounded px-2 py-0.5 text-xs transition-colors',
                    deepTab === t.key ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg-secondary')}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>

          {/* 面板自带左右内边距; -mx-3 让分隔线与卡片边缘齐平(卡片 p-3) */}
          <div className="-mx-3 border-t border-border">
            {deepTab === 'fundflow' && <FundFlowPanel symbol={symbol} days={30} />}
            {deepTab === 'dragon' && <DragonTigerPanel symbol={symbol} />}
            {deepTab === 'research' && <ResearchPanel symbol={symbol} limit={20} />}
            {deepTab === 'announcements' && <AnnouncementsPanel symbol={symbol} limit={20} />}
            {deepTab === 'financials' && <FinancialsPanel symbol={symbol} periods={8} />}
          </div>
        </div>
      )}
      </div>{/* /左主列 */}
      </div>{/* /两栏栅格 */}
    </div>
  )
}
