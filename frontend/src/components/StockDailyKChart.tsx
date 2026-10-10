import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, request, type KlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { aggregateKline, computeIndicators, type KPeriod } from '@/lib/klinePeriod'
import {
  EChartsCandlestick,
  OVERLAY_INDICATORS,
  SUB_CHARTS,
  type ChartMarker,
  type ChartPriceLine,
  type ChartRange,
  type OHLC,
  type StockInfo,
  type VolumeCompareConfig,
} from '@/components/EChartsCandlestick'

/** 分钟档位。与后端 _MKLINE_PERIODS 一致。 */
export type MklinePeriod = 'm1' | 'm5' | 'm15' | 'm30' | 'm60'

export const MK_PERIODS: { label: string; value: MklinePeriod }[] = [
  { label: '1分', value: 'm1' },
  { label: '5分', value: 'm5' },
  { label: '15分', value: 'm15' },
  { label: '30分', value: 'm30' },
  { label: '60分', value: 'm60' },
]

const SUB_INFO_H = 16
const SUB_GAP = 4
const MAX_DAYS = 2000
const DEFAULT_VOLUME_COMPARE: VolumeCompareConfig = { enabled: true, days: 1 }

function normalizeVolumeCompare(config: VolumeCompareConfig): VolumeCompareConfig {
  return {
    enabled: config.enabled !== false,
    days: Math.max(1, Math.min(20, Math.round(Number(config.days) || 1))),
  }
}

/** 分钟K响应(与后端 app/api/astock_deep.py::minute_kline 对齐)。 */
interface MklineResp {
  available: boolean
  source: string
  reason?: string
  data?: {
    code: string
    name: string | null
    period: string
    prev_close: number | null
    /** date 形如 '2026-10-09 14:40' —— 该分钟区间的结束时刻, 不能切成日期。 */
    rows: { date: string; open: number; close: number; high: number; low: number; volume: number }[]
  }
}

/** 分钟K → OHLC。与 toOHLC 唯一的区别: **保留时间部分**。 */
function toOHLCMinute(rows: { date: string; open: number; close: number; high: number; low: number; volume: number }[]): OHLC[] {
  return rows
    .filter(r => r && r.date != null && r.open != null && r.close != null)
    .map(r => ({
      date: String(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume ?? 0),
    }))
}

export interface StockDailyKChartResult {
  rows: OHLC[]
  rawRows: KlineRow[]
  stockInfo?: StockInfo
  name?: string
}

interface Props {
  symbol: string
  height?: number
  className?: string
  dateRange?: { start: string; end: string }
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  showLimitMarkers?: boolean
  showIndicatorControls?: boolean
  showMarkerToggle?: boolean
  showMA?: boolean
  showInfoBar?: boolean
  visibleBars?: number
  /** 日/周/月/年/分时。周月年由日K聚合而来(确定性派生, 非另一份数据)。 */
  period?: KPeriod
  /**
   * 分钟档('m1'|'m5'|'m15'|'m30'|'m60')。给了就走腾讯 mkline 真源而不是日K ——
   * 本地 kline_minute 库目前是空的, 这个端点直连就有数据。
   */
  mklinePeriod?: MklinePeriod
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  onDataChange?: (result: StockDailyKChartResult) => void
  /** 扩展数据列参数（逗号分隔 config_id.field_name），透传给 klineDaily 接口 */
  extColumns?: string
}

function isValidRow(r: any): boolean {
  return r && r.date != null && r.open != null && r.close != null
}

export function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter(isValidRow)
    .map(r => ({
      date: typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume ?? 0),
      ma5: r.ma5 != null ? Number(r.ma5) : null,
      ma10: r.ma10 != null ? Number(r.ma10) : null,
      ma20: r.ma20 != null ? Number(r.ma20) : null,
      ma60: r.ma60 != null ? Number(r.ma60) : null,
      macd_dif: r.macd_dif != null ? Number(r.macd_dif) : null,
      macd_dea: r.macd_dea != null ? Number(r.macd_dea) : null,
      macd_hist: r.macd_hist != null ? Number(r.macd_hist) : null,
      rsi_6: r.rsi_6 != null ? Number(r.rsi_6) : null,
      rsi_14: r.rsi_14 != null ? Number(r.rsi_14) : null,
      rsi_24: r.rsi_24 != null ? Number(r.rsi_24) : null,
      kdj_k: r.kdj_k != null ? Number(r.kdj_k) : null,
      kdj_d: r.kdj_d != null ? Number(r.kdj_d) : null,
      kdj_j: r.kdj_j != null ? Number(r.kdj_j) : null,
      boll_upper: r.boll_upper != null ? Number(r.boll_upper) : null,
      boll_lower: r.boll_lower != null ? Number(r.boll_lower) : null,
    }))
}

function buildLimitUpMarkers(rows: KlineRow[]): ChartMarker[] {
  const markers: ChartMarker[] = []
  for (const r of rows) {
    const date = typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date)
    if (r.signal_broken_limit_up) {
      markers.push({ date, kind: 'neutral', above: true, color: '#8B5CF6', label: '炸' })
    } else if (r.signal_limit_up) {
      const boards: number = r.consecutive_limit_ups ?? 1
      markers.push({ date, kind: 'buy', above: true, color: '#FACC15', label: boards <= 1 ? '板' : String(boards) })
    }
  }
  return markers
}

export function getDefaultRange(): { start: string; end: string } {
  const now = new Date()
  const end = now.toISOString().slice(0, 10)
  const s = new Date(now)
  s.setMonth(s.getMonth() - 6)
  const start = s.toISOString().slice(0, 10)
  return { start, end }
}

function rangeDays(range: { start: string; end: string }): number {
  const start = new Date(range.start)
  const end = new Date(range.end)
  return Math.min(Math.ceil((end.getTime() - start.getTime()) / 86400000) + 30, MAX_DAYS)
}

export function StockDailyKChart({
  symbol,
  height = 520,
  className,
  dateRange: externalDateRange,
  markers,
  ranges,
  priceLines,
  showLimitMarkers = true,
  showIndicatorControls = true,
  showMarkerToggle = true,
  showMA = true,
  showInfoBar = true,
  visibleBars = 60,
  period = 'day',
  mklinePeriod,
  linkedPrice,
  onDateClick,
  onPriceDoubleClick,
  onDataChange,
  extColumns,
}: Props) {
  const [activeIndicators, setActiveIndicators] = useState<string[]>(['vol'])
  const [showMarkers, setShowMarkers] = useState(true)
  const [volumeCompare, setVolumeCompare] = useState<VolumeCompareConfig>(() =>
    normalizeVolumeCompare(storage.stockVolumeCompare.get(DEFAULT_VOLUME_COMPARE)),
  )
  const dateRange = externalDateRange ?? getDefaultRange()
  const days = useMemo(() => rangeDays(dateRange), [dateRange])

  // extColumns 纳入 query key：勾选/取消扩展字段时需重新请求（带 ext_columns 参数）
  const kline = useQuery({
    queryKey: QK.kline(symbol, dateRange.start, dateRange.end, extColumns),
    queryFn: () => api.klineDaily(symbol, days, dateRange, extColumns),
    enabled: !!symbol && !mklinePeriod,
    placeholderData: (prev) => prev,
  })

  // 分钟K(1/5/15/30/60)走独立的腾讯源: 本地 kline_minute 库对这些标的是空的,
  // 但腾讯 mkline 端点直连就有真数据 —— 不做"等库补齐"的空图。
  const mkline = useQuery({
    queryKey: QK.astockMkline(symbol, mklinePeriod ?? ''),
    queryFn: () => request<MklineResp>(
      `/api/astock/mkline/${encodeURIComponent(symbol)}?period=${mklinePeriod}&limit=320`),
    enabled: !!symbol && !!mklinePeriod,
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  })

  const dayRows = useMemo(() => toOHLC(kline.data?.rows ?? []), [kline.data?.rows])
  // 分钟K: 后端只给 OHLCV, 指标必须在这串分钟 bar 上重算(不能沿用日K的指标值)。
  const mkRows = useMemo(
    () => mklinePeriod ? computeIndicators(toOHLCMinute(mkline.data?.data?.rows ?? [])) : [],
    [mkline.data?.data?.rows, mklinePeriod],
  )
  // 周/月/年在日K基础上聚合; 日K原样透传(后端算好的指标就是权威值)
  const rows = useMemo(
    () => mklinePeriod ? mkRows : aggregateKline(dayRows, period),
    [dayRows, period, mklinePeriod, mkRows],
  )
  const stockInfo = kline.data?.stock_info
  const limitMarkers = useMemo(() => buildLimitUpMarkers(kline.data?.rows ?? []), [kline.data?.rows])
  const allMarkers = useMemo(() => [
    ...(markers ?? []),
    ...(showLimitMarkers ? limitMarkers : []),
  ], [limitMarkers, markers, showLimitMarkers])

  const toggleIndicator = useCallback((key: string) => {
    setActiveIndicators(prev => prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key])
  }, [])

  const updateVolumeCompare = useCallback((patch: Partial<VolumeCompareConfig>) => {
    setVolumeCompare(prev => {
      const next = normalizeVolumeCompare({ ...prev, ...patch })
      storage.stockVolumeCompare.set(next)
      return next
    })
  }, [])

  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is typeof SUB_CHARTS[number] => !!d)
  let subExtraH = 0
  activeSubDefs.forEach(def => { subExtraH += SUB_INFO_H + def.height })
  if (activeSubDefs.length > 0) subExtraH += activeSubDefs.length * SUB_GAP + 14
  const chartHeight = height + subExtraH

  useEffect(() => {
    onDataChange?.({ rows, rawRows: kline.data?.rows ?? [], stockInfo, name: kline.data?.name })
  }, [kline.data?.name, kline.data?.rows, onDataChange, rows, stockInfo])

  if (!symbol) return null

  return (
    <div className={className} style={{ minHeight: chartHeight }}>
      {showIndicatorControls && rows.length > 0 && (
        <div className="flex items-center gap-1.5 px-1 pb-0.5">
          {SUB_CHARTS.map(ind => (
            <button
              key={ind.key}
              onClick={() => toggleIndicator(ind.key)}
              className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                activeIndicators.includes(ind.key)
                  ? 'bg-accent/20 text-accent'
                  : 'bg-elevated text-muted hover:text-secondary'
              }`}
            >
              {ind.label}
            </button>
          ))}
          {OVERLAY_INDICATORS.map(ind => (
            <button
              key={ind.key}
              onClick={() => toggleIndicator(ind.key)}
              className={`px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                activeIndicators.includes(ind.key)
                  ? 'bg-accent/20 text-accent'
                  : 'bg-elevated text-muted hover:text-secondary'
              }`}
            >
              {ind.label}
            </button>
          ))}
          {activeIndicators.includes('vol') && (
            <div className="ml-0.5 flex h-5 items-center gap-1.5 border-l border-border/70 pl-2">
              <span className="text-[10px] text-muted">量比</span>
              <button
                type="button"
                role="switch"
                aria-checked={volumeCompare.enabled}
                aria-label="开启量能对比"
                title={volumeCompare.enabled ? '关闭量能对比' : '开启量能对比'}
                onClick={() => updateVolumeCompare({ enabled: !volumeCompare.enabled })}
                className={`relative h-3.5 w-6 shrink-0 rounded-full transition-colors ${
                  volumeCompare.enabled ? 'bg-accent' : 'bg-elevated'
                }`}
              >
                <span className={`absolute left-0 top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-transform ${
                  volumeCompare.enabled ? 'translate-x-3' : 'translate-x-0.5'
                }`} />
              </button>
              <select
                aria-label="量能对比周期"
                value={volumeCompare.days}
                disabled={!volumeCompare.enabled}
                onChange={event => updateVolumeCompare({ days: Number(event.target.value) })}
                className="h-5 rounded border border-border bg-base px-1 text-[10px] text-secondary outline-none disabled:opacity-40"
              >
                {Array.from({ length: 20 }, (_, index) => index + 1).map(days => (
                  <option key={days} value={days}>前{days}日均量</option>
                ))}
              </select>
            </div>
          )}
          {showMarkerToggle && showLimitMarkers && (
            <button
              onClick={() => setShowMarkers(v => !v)}
              className={`ml-auto px-2 py-0.5 rounded text-[10px] font-mono cursor-pointer transition-colors ${
                showMarkers
                  ? 'text-[#FACC15] bg-[#FACC15]/10'
                  : 'bg-elevated text-muted hover:text-secondary'
              }`}
            >
              异动
            </button>
          )}
        </div>
      )}
      {(kline.isLoading || mkline.isLoading) && <div className="text-sm text-muted py-4">加载中…</div>}
      {(kline.isError || mkline.isError) && <div className="text-sm text-danger py-2">
        {mklinePeriod ? '分钟K加载失败' : '日K加载失败'}
      </div>}
      {mklinePeriod && !mkline.isLoading && !mkline.isError && mkline.data && !mkline.data.available && (
        <div className="px-1 py-2 text-xs text-muted">
          分钟K不可用：{mkline.data.reason ?? '未知原因'} —— 不画空图，也不拿日K冒充分钟K。
        </div>
      )}
      {!mklinePeriod && !kline.isLoading && !kline.isError && (kline.data?.rows?.length ?? 0) > 0 && rows.length === 0 && (
        <div className="text-sm text-danger py-2">数据格式异常，请刷新页面</div>
      )}
      {rows.length > 0 && (
        <EChartsCandlestick
          data={rows}
          markers={allMarkers}
          ranges={ranges}
          priceLines={priceLines}
          height={chartHeight - 22}
          showMA={showMA}
          showInfoBar={showInfoBar}
          showMarkers={showMarkers}
          stockInfo={stockInfo}
          symbol={symbol}
          linkedPrice={linkedPrice}
          onDateClick={onDateClick}
          onPriceDoubleClick={onPriceDoubleClick}
          visibleBars={visibleBars}
          activeIndicators={activeIndicators}
          volumeCompare={volumeCompare}
        />
      )}
    </div>
  )
}
