import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useParams, Link } from 'react-router-dom'
import { MarketWatchlistButton } from '@/components/MarketWatchlistButton'
import { StockDailyKChart, type MklinePeriod } from '@/components/StockDailyKChart'
import { KlineToolsBar, useKlineTools } from '@/components/KlineChartTools'
import { buildSignalMarkers } from '@/lib/klineSignals'
import { DeepCapabilityNote, KPeriodTabs, MinuteDepthTabs, MinutePeriodTabs } from '@/components/KPeriodTabs'
import {
  HKAnnouncementsPanel,
  HKFinancialsPanel,
  HKForecastsPanel,
} from '@/components/hk/HKDeepPanels'
import { PERIOD_DEFAULT_BARS, type KPeriod } from '@/lib/klinePeriod'
import { cn } from '@/lib/cn'
import type { OHLC } from '@/components/EChartsCandlestick'

/**
 * 港股深度 tab —— 只放**有免费真源**的三块。
 *
 * ⚠️ 刻意不为「对齐 A股」而补空面板: 港股没有 A股那套龙虎榜 / 资金流的免费真源
 * (新浪港股资金流实测返回空数组, 东财 push2 被墙), 硬摆上去只会得到一排"不可用"。
 * 这里只接已实测可取的: 券商评级(东财港股通) + 公告(披露易) + 财报(东财公告原文)。
 *
 * 「券商评级」补齐的是 A股「研报」那个信息位 —— 2026-10-12 实测: 东财港股通口径
 * 有数十家券商对同一财年的盈利预测 + 评级 + 目标价(00700 返回 50 行), 全真实观点。
 */
type DeepTab = 'forecasts' | 'announcements' | 'financials'

const DEEP_TABS: { key: DeepTab; label: string }[] = [
  { key: 'forecasts', label: '券商评级' },
  { key: 'announcements', label: '公告' },
  { key: 'financials', label: '财报' },
]

interface HKRealtime {
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

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json() as Promise<T>
}

/**
 * 港股个股页 — 实时行情卡片 + A股同款日K蜡烛图 (MA/MACD/KDJ/RSI/BOLL)。
 *
 * K线走 /api/kline/daily (repository.get_daily 对 .HK symbol 分流读
 * kline_hk_us_enriched 本地全量 enriched), 与 A股 StockDailyKChart 完全同源。
 * 港股无涨跌停, 关闭 limitMarkers。
 */
export function HKStockAnalysisPage() {
  const { symbol: rawSymbol = '00700.HK' } = useParams<{ symbol: string }>()
  const symbol = rawSymbol.toUpperCase()

  const realtime = useQuery({
    queryKey: ['hk', 'realtime', symbol],
    queryFn: () => fetchJson<HKRealtime>(`/api/hk/realtime/${encodeURIComponent(symbol)}`),
    refetchInterval: 10_000,  // 10s 轮询
  })

  /**
   * 图上三件套(主图画法 / K线内标注 / 画图) —— 全是纯前端能力:
   * 吃的只是同一串 OHLC, 不碰任何市场差异, 所以与 A股个股页共用同一套实现。
   * 坐标上下文写死 `day||none`: 本页日K + 不复权(港股暂不接复权源), 图形锚点才不会串档。
   */
  const tools = useKlineTools(symbol, 'day||none', 'hkDetail')
  const [klineRows, setKlineRows] = useState<OHLC[]>([])
  const [kPeriod, setKPeriod] = useState<KPeriod>('day')
  const [visibleBars, setVisibleBars] = useState(PERIOD_DEFAULT_BARS.day)
  /**
   * 分钟档。null = 看日/周/月/年。
   *
   * 2026-10-12 之前这里是"港美股分钟K无真源"(依据是腾讯 mkline 返回 param
   * error)。实测发现那只针对腾讯源 —— 通达信扩展市场(MAC-EX 7727) 对港美股
   * 都返回真实分钟K, 所以这里现在**有数据**。
   */
  const [mkPeriod, setMkPeriod] = useState<MklinePeriod | null>(null)
  /** 分钟K 天数: 1 = 原生蜡烛(上限 700 根); >1 = 0x240C 按日循环(聚合蜡烛)。 */
  const [mkDays, setMkDays] = useState(1)
  const [deepTab, setDeepTab] = useState<DeepTab>('forecasts')

  const caps = <DeepCapabilityNote market="HK" />
  const signalMarkers = useMemo(
    () => buildSignalMarkers(klineRows, tools.signals),
    [klineRows, tools.signals],
  )

  const r = realtime.data
  const changeColor =
    r?.change_pct == null
      ? 'text-muted'
      : r.change_pct >= 0
        ? 'text-rose-500'
        : 'text-emerald-500'

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-baseline gap-3">
        <Link
          to="/hk"
          className="text-sm text-muted hover:text-foreground"
        >
          ← 港股龙头池
        </Link>
        <span className="text-muted">/</span>
        <h1 className="text-2xl font-semibold text-foreground">
          {r?.name || symbol} <span className="text-sm text-muted font-mono">{symbol}</span>
        </h1>
        <span className="inline-block px-1.5 py-0.5 text-xs rounded bg-rose-500/10 text-rose-500">
          HK · HKD
        </span>
        <span className="ml-auto">
          <MarketWatchlistButton symbol={symbol} market="hk" />
        </span>
        {r && (
          <span className="text-xs text-muted">
            数据源: {r.source}
          </span>
        )}
      </div>

      {/* 实时行情卡片 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Card label="现价">
          {r?.price != null ? (
            <span className={`text-2xl font-bold ${changeColor}`}>
              {r.price.toFixed(2)}
            </span>
          ) : (
            <span className="text-muted">--</span>
          )}
        </Card>
        <Card label="涨跌幅">
          {r?.change_pct != null ? (
            <span className={`text-2xl font-bold ${changeColor}`}>
              {r.change_pct >= 0 ? '+' : ''}
              {r.change_pct.toFixed(2)}%
            </span>
          ) : (
            <span className="text-muted">--</span>
          )}
        </Card>
        <Card label="今开">
          {r?.open != null ? r.open.toFixed(2) : '--'}
        </Card>
        <Card label="昨收">
          {r?.pre_close != null ? r.pre_close.toFixed(2) : '--'}
        </Card>
        <Card label="最高">
          {r?.high != null ? r.high.toFixed(2) : '--'}
        </Card>
        <Card label="最低">
          {r?.low != null ? r.low.toFixed(2) : '--'}
        </Card>
        <Card label="成交量">
          {r?.volume != null ? formatVolume(r.volume) : '--'}
        </Card>
        <Card label="成交额">
          {r?.amount != null ? formatAmount(r.amount) : '--'}
        </Card>
      </div>

      {/* 港股特性说明 */}
      <div className="text-xs text-muted p-3 rounded border border-border bg-surface/40">
        港股无涨跌停制度 · T+0 交收 · 货币 HKD 原币（不折算 CNY）· 行情源: 腾讯优先, 新浪降级
      </div>

      {/* 日K蜡烛图 (A股同款组件, 本地 enriched 全指标) —— 工具条同款移植:
          主图四类 / K线内标注 / 画图工具全都能用, 港股无涨跌停所以关掉 limitMarkers,
          复权档固定「不复权」(本页还没接港股复权源, 不假装有)。 */}
      <KlineToolsBar tools={tools} className="space-y-1" />
      {caps}
      <div className="flex flex-wrap items-center gap-2">
        <KPeriodTabs
          value={kPeriod}
          onChange={(p, bars) => { setKPeriod(p); setMkPeriod(null); if (bars > 0) setVisibleBars(bars) }}
        />
        <MinutePeriodTabs value={mkPeriod} onChange={setMkPeriod} />
        {mkPeriod && <MinuteDepthTabs value={mkDays} onChange={setMkDays} />}
      </div>
      <StockDailyKChart
        symbol={symbol}
        height={560}
        period={kPeriod}
        deepMinutePeriod={mkPeriod ?? undefined}
        deepMinuteDays={mkDays}
        visibleBars={visibleBars}
        showLimitMarkers={false}
        adjust="none"
        mainType={tools.mainType}
        markers={signalMarkers}
        priceLines={tools.priceLines}
        onPriceDoubleClick={tools.addPriceLine}
        onDataChange={result => setKlineRows(result.rows)}
        drawings={tools.visibleDrawings}
        drawTool={tools.drawTool}
        drawCtx={tools.ctx}
        onDrawingsChange={tools.persistDrawings}
        onDrawToolChange={tools.setDrawTool}
      />

      {/* ── 深度数据 3 tab ── */}
      <div className="rounded-lg border border-border bg-surface p-3">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-foreground">深度数据</h3>
          <span className="text-xs text-muted">
            港股真源 · 评级走东财港股通，公告走港交所披露易，财报走东财公告原文
          </span>
          <div className="ml-auto inline-flex items-center rounded border border-border bg-elevated p-0.5">
            {DEEP_TABS.map(t => (
              <button
                key={t.key}
                type="button"
                onClick={() => setDeepTab(t.key)}
                className={cn('rounded px-2 py-0.5 text-xs transition-colors',
                  deepTab === t.key ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary')}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>
        {deepTab === 'forecasts' && <HKForecastsPanel symbol={symbol} limit={60} />}
        {deepTab === 'announcements' && <HKAnnouncementsPanel symbol={symbol} days={180} limit={40} />}
        {deepTab === 'financials' && <HKFinancialsPanel symbol={symbol} periods={8} />}
      </div>
    </div>
  )
}

function Card({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-surface/60 p-3">
      <div className="text-xs text-muted mb-1">{label}</div>
      <div className="text-lg font-semibold text-foreground">{children}</div>
    </div>
  )
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
