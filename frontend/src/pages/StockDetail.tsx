import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Loader2, Maximize2, Minus, Newspaper, Plus, RotateCcw } from 'lucide-react'

import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { priceColorClass } from '@/lib/format'
import { StockDailyKChart, type StockDailyKChartResult } from '@/components/StockDailyKChart'
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

/** 头部一行行情: 项数有限, 只放日K真实有的字段(无 PE/PB/市值/换手源 → 不显示)。 */
function QuoteStrip({ kline }: { kline: StockDailyKChartResult | null }) {
  const rows = kline?.rawRows ?? []
  const last = rows[rows.length - 1]
  const prev = rows[rows.length - 2]
  if (!last) return null

  const prevClose = prev?.close ?? NaN
  const changeAmt = last.close - prevClose
  const amplitude = prevClose ? ((last.high - last.low) / prevClose) * 100 : NaN

  const items: [string, string][] = [
    ['今开', last.open?.toFixed(2) ?? '—'],
    ['昨收', Number.isFinite(prevClose) ? prevClose.toFixed(2) : '—'],
    ['最高', last.high?.toFixed(2) ?? '—'],
    ['最低', last.low?.toFixed(2) ?? '—'],
    ['振幅', Number.isFinite(amplitude) ? `${amplitude.toFixed(2)}%` : '—'],
    ['成交量', fmtBig(last.volume, 0)],
    ['涨跌额', Number.isFinite(changeAmt) ? `${changeAmt > 0 ? '+' : ''}${changeAmt.toFixed(2)}` : '—'],
  ]

  return (
    <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 rounded border border-border bg-surface px-4 py-2 text-xs">
      {items.map(([k, v]) => (
        <span key={k} className="text-fg-muted">
          {k} <b className="font-mono text-fg">{v}</b>
        </span>
      ))}
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
  const [visibleBars, setVisibleBars] = useState(60)
  const [showMA, setShowMA] = useState(true)
  const [kline, setKline] = useState<StockDailyKChartResult | null>(null)
  const [deepTab, setDeepTab] = useState<DeepTab>('fundflow')

  const dateRange = useMemo(() => rangeFromMonths(months), [months])

  const rows = kline?.rawRows ?? []
  const last = rows[rows.length - 1]
  const prev = rows[rows.length - 2]
  const changePct = last?.change_pct ?? null
  const changeAmt = last && prev ? last.close - prev.close : null

  if (!symbol) {
    return (
      <div className="p-6 text-sm text-fg-muted">
        缺少股票代码。<Link to="/" className="underline">返回首页</Link>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-[1400px] space-y-4 p-4">
      {/* ── 头部 ── */}
      <div className="flex flex-wrap items-baseline gap-3">
        <Link to="/" className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-fg">
          <ArrowLeft className="h-3.5 w-3.5" /> 返回
        </Link>
        <h1 className="text-xl font-semibold text-fg">{kline?.name || symbol}</h1>
        <span className="rounded bg-elevated px-1.5 py-0.5 font-mono text-xs text-fg-secondary">{symbol}</span>
        {last && (
          <>
            <span className={cn('text-2xl font-bold tabular-nums', priceColorClass(changePct))}>
              {last.close?.toFixed(2)}
            </span>
            <span className={cn('text-sm tabular-nums', priceColorClass(changePct))}>
              {changeAmt != null ? `${changeAmt > 0 ? '+' : ''}${changeAmt.toFixed(2)}` : ''}
              {changePct != null ? ` (${changePct > 0 ? '+' : ''}${changePct.toFixed(2)}%)` : ''}
            </span>
          </>
        )}
        <span className="ml-auto">
          {/* A股不带后缀 → 组件内部按 'cn' 匹配自选, 不传 market */}
          <MarketWatchlistButton symbol={symbol} />
        </span>
      </div>

      {/* ── 行情条 ── */}
      <QuoteStrip kline={kline} />

      {/* ── K 线 + 工具栏 ── */}
      <div className="rounded-lg border border-border bg-surface p-3 space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
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
            当前 {visibleBars} 根 · 副图指标用图表右上控件切换 · 点击蜡烛可看当日分时
          </span>
          <span className="ml-auto inline-flex items-center gap-1 text-[11px] text-fg-muted">
            <Maximize2 className="h-3 w-3" /> 图表区
          </span>
        </div>

        <StockDailyKChart
          symbol={symbol}
          height={430}
          dateRange={dateRange}
          showMA={showMA}
          visibleBars={visibleBars}
          onDataChange={setKline}
        />
      </div>

      {/* ── 资讯 ── */}
      <NewsCard symbol={symbol} name={kline?.name} />

      {/* ── 深度数据 5 tab ── */}
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

        <div className="-mx-4 border-t border-border">
          {deepTab === 'fundflow' && <FundFlowPanel symbol={symbol} days={30} />}
          {deepTab === 'dragon' && <DragonTigerPanel symbol={symbol} />}
          {deepTab === 'research' && <ResearchPanel symbol={symbol} limit={20} />}
          {deepTab === 'announcements' && <AnnouncementsPanel symbol={symbol} limit={20} />}
          {deepTab === 'financials' && <FinancialsPanel symbol={symbol} periods={8} />}
        </div>
      </div>
    </div>
  )
}
