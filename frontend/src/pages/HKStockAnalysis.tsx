import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useParams, Link } from 'react-router-dom'
import { MarketWatchlistButton } from '@/components/MarketWatchlistButton'
import { StockDailyKChart } from '@/components/StockDailyKChart'
import { KlineToolsBar, useKlineTools } from '@/components/KlineChartTools'
import { buildSignalMarkers } from '@/lib/klineSignals'
import { DeepCapabilityNote, KPeriodTabs } from '@/components/KPeriodTabs'
import { PERIOD_DEFAULT_BARS, type KPeriod } from '@/lib/klinePeriod'
import type { OHLC } from '@/components/EChartsCandlestick'

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
  // 港美股都只支持 日/周/月/年 —— 分钟K无真源(见 DeepCapabilityNote)
  const [kPeriod, setKPeriod] = useState<KPeriod>('day')
  const [visibleBars, setVisibleBars] = useState(PERIOD_DEFAULT_BARS.day)

  const caps = <DeepCapabilityNote market="HK" />
  const signalMarkers = useMemo(
    () => buildSignalMarkers(klineRows, tools.signals),
    [klineRows, tools.signals],
  )

  const r = realtime.data
  const changeColor =
    r?.change_pct == null
      ? 'text-fg-muted'
      : r.change_pct >= 0
        ? 'text-rose-500'
        : 'text-emerald-500'

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-baseline gap-3">
        <Link
          to="/hk"
          className="text-sm text-fg-muted hover:text-fg"
        >
          ← 港股龙头池
        </Link>
        <span className="text-fg-muted">/</span>
        <h1 className="text-2xl font-semibold text-fg">
          {r?.name || symbol} <span className="text-sm text-fg-muted font-mono">{symbol}</span>
        </h1>
        <span className="inline-block px-1.5 py-0.5 text-xs rounded bg-rose-500/10 text-rose-500">
          HK · HKD
        </span>
        <span className="ml-auto">
          <MarketWatchlistButton symbol={symbol} market="hk" />
        </span>
        {r && (
          <span className="text-xs text-fg-muted">
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
      <div className="text-xs text-fg-muted p-3 rounded border border-border bg-surface/40">
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
          onChange={(p, bars) => { setKPeriod(p); if (bars > 0) setVisibleBars(bars) }}
        />
      </div>
      <StockDailyKChart
        symbol={symbol}
        height={560}
        period={kPeriod}
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
    </div>
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
