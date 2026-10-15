import { useEffect, useRef, useCallback, useMemo, useState } from 'react'
import { chartTheme, getTheme, useTheme } from '@/lib/theme'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { ChartDrawingLayer } from '@/components/ChartDrawingLayer'
import type { Drawing, DrawTool } from '@/lib/chartDrawings'

export interface OHLC {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  ma5?: number | null
  ma10?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi_6?: number | null
  rsi_14?: number | null
  rsi_24?: number | null
  kdj_k?: number | null
  kdj_d?: number | null
  kdj_j?: number | null
  boll_upper?: number | null
  boll_lower?: number | null
}

export interface ChartMarker {
  date: string
  kind: 'buy' | 'sell' | 'neutral'
  label?: string
  /**
   * 若为 true，标记放在蜡烛上方（如涨停连板标签）。
   * 不设时由渲染层按 kind 决定：buy 画在下方箭头朝上、sell 画在上方箭头朝下 ——
   * 这是通达信/同花顺的惯例，买入信号从下往上指着 K 线。
   */
  above?: boolean
  /** 自定义标签颜色，覆盖默认的 kind 对应色。 */
  color?: string
  /** 同一根 K 线、同一方向上合并了几条信号（>1 时徽章里显示数字）。 */
  count?: number
  /** 合并进来的每一条信号，hover 时展开。key 用于查回测证据。 */
  detail?: { key: string; label: string; desc?: string; strength?: number }[]
  /**
   * 强度档 1/2/3（常规 / 较强 / 极强），由回测证据表的全市场分位数切出来。
   *
   * 只有"训练段与测试段都单调"的信号才会分档 —— 强度跟后续走势没有剂量-反应
   * 关系时, 把箭头画大一点只是假装更有信息。
   */
  tier?: 1 | 2 | 3
}

export interface ChartRange {
  start: string
  end: string
  label?: string
  color?: string
}

export interface ChartPriceLine {
  value: number
  label?: string
  color?: string
  start?: string
  end?: string
}

/**
 * 主图类型。四种画法画的**是同一份 OHLC**, 只是视觉编码不同 —— 不存在"换了个数据源"。
 * - candle: 实心蜡烛(涨红跌绿实心, 通达信默认)
 * - hollow: 空心蜡烛(涨空心/跌实心 —— 上涨日更醒目, 密集区不糊成一团)
 * - ohlc:   美国线(竖线=高低, 左横=开, 右横=收; 蜡烛太密时比蜡烛干净)
 * - line:   收盘线(只看收盘价走势, 叠加 MA 最清爽)
 */
export type MainChartType = 'candle' | 'hollow' | 'ohlc' | 'line'

export const MAIN_CHART_TYPES: { label: string; value: MainChartType; hint: string }[] = [
  { label: '蜡烛', value: 'candle', hint: '实心蜡烛(涨红跌绿)' },
  { label: '空心', value: 'hollow', hint: '空心蜡烛: 涨空心, 跌实心' },
  { label: '美国线', value: 'ohlc', hint: '美国线: 竖线高低, 左开右收' },
  { label: '收盘线', value: 'line', hint: '收盘线: 只连收盘价' },
]

export interface StockInfo {
  name?: string
  total_shares?: number
  float_shares?: number
  /** 扩展数据（key: configId__fieldName），来自 klineDaily 的 ext_columns */
  ext?: Record<string, unknown>
}

export interface VolumeCompareConfig {
  enabled: boolean
  days: number
}

interface SubChartContext {
  compact: boolean
  volumeCompare: VolumeCompareConfig
}

/** 子图定义 */
export interface SubChartDef {
  key: string
  label: string
  /** 子图固定高度 px */
  height: number
  /** 构建 series 数组 */
  buildSeries: (data: OHLC[], context: SubChartContext) => any[]
  /** 构建信息栏文字 (当前数据行 -> 显示内容) */
  buildInfo: (d: OHLC | null) => { label: string; color: string; value: string }[]
  /** Y 轴特殊配置 */
  yAxisConfig?: Record<string, any>
}

// ===== 成交量 N 日均量 =====
function volMaN(data: OHLC[], n: number): (number | null)[] {
  const result: (number | null)[] = []
  for (let i = 0; i < data.length; i++) {
    if (i < n - 1) { result.push(null); continue }
    let sum = 0
    for (let j = i - n + 1; j <= i; j++) sum += data[j].volume ?? 0
    result.push(sum / n)
  }
  return result
}

function fmtVol(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v.toFixed(0)
}

function volumeRatioAt(data: OHLC[], index: number, days: number): number | null {
  const window = Math.max(1, Math.min(20, Math.round(days)))
  if (index < window) return null
  let sum = 0
  for (let offset = 1; offset <= window; offset++) {
    const volume = data[index - offset]?.volume
    if (volume == null || !Number.isFinite(volume)) return null
    sum += volume
  }
  const average = sum / window
  const current = data[index]?.volume
  if (current == null || !Number.isFinite(current) || average <= 0) return null
  return current / average
}

function fmtVolumeRatio(value: number | null, digits = 2): string {
  return value == null ? '—' : `${value.toFixed(digits)}x`
}

export const SUB_CHARTS: SubChartDef[] = [
  {
    key: 'vol',
    label: '成交量',
    height: 84,
    yAxisConfig: { min: 0 },
    buildSeries: (data, context) => {
      const ma5Data = volMaN(data, 5)
      const ma10Data = volMaN(data, 10)
      const compareDays = context.volumeCompare.days
      return [
        {
          name: '成交量',
          type: 'bar',
          data: data.map((d, index) => {
            const ratio = volumeRatioAt(data, index, compareDays)
            return {
              value: d.volume ?? 0,
              volumeRatioLabel: ratio == null ? '' : fmtVolumeRatio(ratio, 1),
              itemStyle: {
                color: d.close >= d.open ? 'rgba(240,68,56,0.6)' : 'rgba(18,183,106,0.6)',
              },
            }
          }),
          barWidth: '60%',
          label: {
            show: context.volumeCompare.enabled && !context.compact,
            position: 'top',
            distance: 2,
            color: CT().text,
            fontSize: 8,
            fontFamily: 'JetBrains Mono, monospace',
            formatter: (params: any) => params.data?.volumeRatioLabel ?? '',
          },
          labelLayout: { hideOverlap: true },
          animation: false,
        },
        {
          name: 'VOL5',
          type: 'line',
          data: ma5Data,
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#FACC15' },
          itemStyle: { color: '#FACC15' },
        },
        {
          name: 'VOL10',
          type: 'line',
          data: ma10Data,
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#8B5CF6' },
          itemStyle: { color: '#8B5CF6' },
        },
      ]
    },
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: '量', color: d.close >= d.open ? '#C74040' : '#2D9B65', value: fmtVol(d.volume) },
      ]
    },
  },
  {
    key: 'macd',
    label: 'MACD',
    height: 72,
    buildSeries: (data) => [
      {
        name: 'DIF',
        type: 'line',
        data: data.map(d => d.macd_dif != null ? Number(d.macd_dif) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'DEA',
        type: 'line',
        data: data.map(d => d.macd_dea != null ? Number(d.macd_dea) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
      {
        name: 'MACD',
        type: 'bar',
        data: data.map(d => {
          const v = d.macd_hist
          if (v == null) return '-'
          return {
            value: Number(v),
            itemStyle: { color: Number(v) >= 0 ? 'rgba(240,68,56,0.6)' : 'rgba(18,183,106,0.6)' },
          }
        }),
        barWidth: '40%',
        animation: false,
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'DIF', color: '#FACC15', value: d.macd_dif != null ? d.macd_dif.toFixed(3) : '—' },
        { label: 'DEA', color: '#8B5CF6', value: d.macd_dea != null ? d.macd_dea.toFixed(3) : '—' },
        { label: 'MACD', color: d.macd_hist != null && d.macd_hist >= 0 ? '#C74040' : '#2D9B65', value: d.macd_hist != null ? d.macd_hist.toFixed(3) : '—' },
      ]
    },
  },
  {
    key: 'rsi',
    label: 'RSI',
    height: 72,
    yAxisConfig: { min: 0, max: 100 },
    buildSeries: (data) => [
      {
        name: 'RSI6',
        type: 'line',
        data: data.map(d => d.rsi_6 != null ? Number(d.rsi_6) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'RSI14',
        type: 'line',
        data: data.map(d => d.rsi_14 != null ? Number(d.rsi_14) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#3B82F6' },
        itemStyle: { color: '#3B82F6' },
      },
      {
        name: 'RSI24',
        type: 'line',
        data: data.map(d => d.rsi_24 != null ? Number(d.rsi_24) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'RSI6', color: '#FACC15', value: d.rsi_6 != null ? d.rsi_6.toFixed(1) : '—' },
        { label: 'RSI14', color: '#3B82F6', value: d.rsi_14 != null ? d.rsi_14.toFixed(1) : '—' },
        { label: 'RSI24', color: '#8B5CF6', value: d.rsi_24 != null ? d.rsi_24.toFixed(1) : '—' },
      ]
    },
  },
  {
    key: 'kdj',
    label: 'KDJ',
    height: 72,
    buildSeries: (data) => [
      {
        name: 'K',
        type: 'line',
        data: data.map(d => d.kdj_k != null ? Number(d.kdj_k) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'D',
        type: 'line',
        data: data.map(d => d.kdj_d != null ? Number(d.kdj_d) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#3B82F6' },
        itemStyle: { color: '#3B82F6' },
      },
      {
        name: 'J',
        type: 'line',
        data: data.map(d => d.kdj_j != null ? Number(d.kdj_j) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'K', color: '#FACC15', value: d.kdj_k != null ? d.kdj_k.toFixed(1) : '—' },
        { label: 'D', color: '#3B82F6', value: d.kdj_d != null ? d.kdj_d.toFixed(1) : '—' },
        { label: 'J', color: '#8B5CF6', value: d.kdj_j != null ? d.kdj_j.toFixed(1) : '—' },
      ]
    },
  },
]

/** 向后兼容的 INDICATORS 导出 (不含 vol) */
export const INDICATORS = SUB_CHARTS.filter(s => s.key !== 'vol')

/** 主图叠加指标 (画在 K 线上方, 不占副图空间) */
export const OVERLAY_INDICATORS: { key: string; label: string }[] = [
  { key: 'boll', label: 'BOLL' },
]

interface Props {
  data: OHLC[]
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  height?: number
  showMA?: boolean
  showInfoBar?: boolean
  showMarkers?: boolean
  onToggleMarkers?: () => void
  stockInfo?: StockInfo
  symbol?: string
  linkedPrice?: number | null
  onDateClick?: (date: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /** 默认可见蜡烛根数, 默认 60 */
  visibleBars?: number
  /** 已激活的子图 key 列表 (含 vol, 按点击顺序) */
  activeIndicators?: string[]
  /** 成交量柱相对前 N 个交易日均量的显示设置 */
  volumeCompare?: VolumeCompareConfig
  /** 主图画法: 蜡烛 / 空心蜡烛 / 美国线 / 收盘线。默认 candle。 */
  mainType?: MainChartType
  /**
   * 成交量分布(Volume Profile)叠加层。传 null / 不传则不画。
   * 画在主图**右侧的预留留白里**(不是压在蜡烛上), 与主图共用同一价格标尺。
   */
  volumeProfile?: VolumeProfileOverlay | null
  /**
   * 缩放/平移后回传当前可见区间(闭区间下标)。调用方据此重算成交量分布 ——
   * 分布必须跟着可见区间走, 否则缩放后图上柱子还是全区间统计, 与眼睛看到的蜡烛对不上。
   */
  onVisibleRangeChange?: (from: number, to: number) => void
  /** 用户自己画的图形(趋势线/水平线/矩形/斐波那契)。null/空 = 不画。 */
  drawings?: Drawing[]
  /** 当前画图工具。null = 关闭画图(图层不吃鼠标事件, 缩放平移照旧)。 */
  drawTool?: DrawTool | null
  /**
   * 新建图形时记下的坐标上下文(周期 + 复权档)。
   * 复权档一换价格体系就变了, 前复权下画的线放到不复权图上会指错价 ——
   * 页面按 ctx 过滤后再传进来, 这里是"当前视图的 ctx"。
   */
  drawCtx?: string
  onDrawingsChange?: (next: Drawing[]) => void
  /** 画完一次性标注(文字)后让页面退回"选择"模式 */
  onDrawToolChange?: (t: DrawTool) => void
}

/**
 * 成交量分布叠加层的数据结构。字段与 lib/volumeProfile 的计算结果同源,
 * 这里重新声明一次只为避开"组件 ↔ lib"的循环 import。
 */
export interface VolumeProfileOverlay {
  bins: { lo: number; hi: number; mid: number; volume: number; pct: number; inValueArea: boolean }[]
  poc: number
  vah: number
  val: number
  totalVolume: number
  bars: number
  from: string
  to: string
}

// 序列颜色 (双主题通用); 画布轴/网格/文字等主题相关色走 CT() 动态取
const THEME = {
  bull: '#C74040',
  bear: '#2D9B65',
  bullAlpha: 'rgba(240,68,56,0.7)',
  bearAlpha: 'rgba(18,183,106,0.7)',
  ma5: '#A1A1AA',
  ma10: '#3B82F6',
  ma20: '#F97316',
  ma60: '#8B5CF6',
  bg: 'transparent',
}

/** 当前主题的图表调色板 (buildOption/信息栏在渲染时调用; 主题切换由组件 effect 触发重建)。 */
const CT = () => chartTheme(getTheme())

/**
 * 美国线(OHLC bar)的 custom renderItem。
 * ECharts 没有内置 OHLC 类型，只能自己画: 竖线贯穿 low~high，
 * 左短横=开盘、右短横=收盘; 涨跌由 close>=open 判定，颜色与蜡烛一致。
 * data 项形如 [index, open, close, low, high]。
 */
function renderOhlcItem(_params: any, api: any) {
  const idx = api.value(0)
  const open = api.value(1)
  const close = api.value(2)
  const low = api.value(3)
  const high = api.value(4)
  const x = api.coord([idx, low])[0]
  const yLow = api.coord([idx, low])[1]
  const yHigh = api.coord([idx, high])[1]
  const yOpen = api.coord([idx, open])[1]
  const yClose = api.coord([idx, close])[1]
  // 一根 bar 占 60% 的类目宽度, 左右各伸出一半
  const halfW = Math.max(1, (api.size([1, 0])[0] || 6) * 0.3)
  const up = close >= open
  const stroke = up ? THEME.bull : THEME.bear
  const style = { stroke, lineWidth: 1 }
  return {
    type: 'group',
    children: [
      { type: 'line', shape: { x1: x, y1: yLow, x2: x, y2: yHigh }, style },
      { type: 'line', shape: { x1: x - halfW, y1: yOpen, x2: x, y2: yOpen }, style },
      { type: 'line', shape: { x1: x, y1: yClose, x2: x + halfW, y2: yClose }, style },
    ],
  }
}

/** 可见蜡烛超过此数量时，涨停/炸板标签切换为小圆点。 */
const COMPACT_THRESHOLD = 60

/** 子图上方信息栏高度 (px) */
const INFO_BAR_H = 16
/** 子图之间的间距 (px) */
const SUB_GAP_PX = 4

function buildSubInfoGraphics(
  data: OHLC[],
  infoIdx: number,
  activeIndicators: string[],
  subStartTop: number,
  volumeCompare: VolumeCompareConfig,
): any[] {
  const d = infoIdx >= 0 && infoIdx < data.length ? data[infoIdx] : null
  const graphics: any[] = []
  let curTop = subStartTop

  activeIndicators.forEach((key) => {
    const def = SUB_CHARTS.find(s => s.key === key)
    if (!def) return

    const items = def.buildInfo(d)
    if (def.key === 'vol' && d) {
      const calcVolMa = (n: number) => {
        if (infoIdx < n - 1) return null
        let sum = 0
        for (let j = infoIdx - n + 1; j <= infoIdx; j++) sum += data[j].volume ?? 0
        return sum / n
      }
      const vol5 = calcVolMa(5)
      const vol10 = calcVolMa(10)
      items.push({ label: 'VOL5', color: '#FACC15', value: fmtVol(vol5) })
      items.push({ label: 'VOL10', color: '#8B5CF6', value: fmtVol(vol10) })
      if (volumeCompare.enabled) {
        const ratio = volumeRatioAt(data, infoIdx, volumeCompare.days)
        items.push({
          label: `量比${volumeCompare.days}`,
          color: ratio != null && ratio >= 1 ? '#C74040' : '#2D9B65',
          value: fmtVolumeRatio(ratio),
        })
      }
    }

    // 每个元素加固定 id，确保 ECharts 增量更新时能正确匹配
    graphics.push({
      id: `sub-sep-${key}`,
      type: 'line',
      shape: { x1: 0, y1: curTop, x2: 2000, y2: curTop },
      style: { stroke: 'rgba(255,255,255,0.08)', lineWidth: 1 },
      silent: true, z: 0,
    })
    graphics.push({
      id: `sub-label-${key}`,
      type: 'text',
      style: {
        text: def.label,
        x: 4, y: curTop + 4,
        fill: '#8E8E96',
        fontSize: 10, fontFamily: 'JetBrains Mono, monospace',
        fontWeight: 'bold',
      },
      silent: true, z: 10,
    })

    const richTextParts: string[] = []
    const rich: Record<string, any> = {}
    items.forEach((item, idx) => {
      const styleKey = `s${idx}`
      richTextParts.push(`{${styleKey}|${item.label}:${item.value}}`)
      rich[styleKey] = {
        fill: item.color,
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      }
    })
    graphics.push({
      id: `sub-val-${key}`,
      type: 'text',
      right: 24,
      style: {
        text: richTextParts.join(`{gap|  }`),
        y: curTop + 3,
        rich: {
          gap: { fill: 'transparent', fontSize: 10 },
          ...rich,
        },
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
        textAlign: 'right',
        textVerticalAlign: 'top',
      },
      silent: true, z: 10,
    })

    curTop += INFO_BAR_H + def.height + SUB_GAP_PX
  })

  return graphics
}

/**
 * 可见区间内的价格极值(含叠加线与价格线), 并套用与主图一致的上下 3% 边距。
 *
 * 只在开成交量分布时用 —— 为了把主图 y 标尺钉成固定值, 好让右侧分布图
 * (独立 grid, 标尺不会自动同步)与蜡烛严格对齐。
 */
function priceExtentFor(
  data: OHLC[],
  from: number,
  to: number,
  withMA: boolean,
  withBOLL: boolean,
  priceLines: ChartPriceLine[] | undefined,
): { min: number; max: number } {
  const lo0 = Math.max(0, Math.min(from, data.length - 1))
  const hi0 = Math.max(lo0, Math.min(to, data.length - 1))
  let min = Infinity
  let max = -Infinity
  const eat = (v: unknown) => {
    const n = typeof v === 'number' ? v : Number(v)
    if (Number.isFinite(n)) {
      if (n < min) min = n
      if (n > max) max = n
    }
  }
  for (let i = lo0; i <= hi0; i++) {
    const d = data[i]
    eat(d.low)
    eat(d.high)
    if (withMA) {
      eat(d.ma5)
      eat(d.ma10)
      eat(d.ma20)
      eat(d.ma60)
    }
    if (withBOLL) {
      eat(d.boll_upper)
      eat(d.boll_lower)
    }
  }
  for (const line of priceLines ?? []) eat(line.value)
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { min: 0, max: 1 }
  if (max - min < 1e-9) {
    const pad = Math.max(max * 0.001, 0.01)
    return { min: min - pad, max: max + pad }
  }
  const pad = (max - min) * 0.03
  return { min: min - pad, max: max + pad }
}

/** 朝上的箭头尖（顶点在上）—— 转强方向，画在 K 线下方指着它。 */
const ARROW_UP = 'path://M0,-5.5 L5.5,4.5 L-5.5,4.5 Z'
/** 朝下的箭头尖（顶点在下）—— 转弱方向，画在 K 线上方指着它。 */
const ARROW_DOWN = 'path://M0,5.5 L5.5,-4.5 L-5.5,-4.5 Z'

/**
 * 单个标注 → ECharts markPoint。
 *
 * 位置语义（交易软件惯例，别按"文字标签该在上面"的直觉来）：
 *   转强(buy)  → 画在最低价**下方**，箭头朝上指着这根 K 线
 *   转弱(sell) → 画在最高价**上方**，箭头朝下指着这根 K 线
 *   中性       → 画在最高价上方的小圆点
 * 这样箭头本身就在说方向，不用读字；而且两个方向的徽章一上一下，永远不会叠住。
 *
 * `above === true` 的老标记（涨停连板标签等）走另一条分支，保持原样不动。
 *
 * name 带方向后缀：同一天可能有上下两个徽章，重名会互相覆盖。
 * 点击回调里按 '|' 切回日期。
 */
function buildMarkPoint(m: ChartMarker, d: OHLC, compact: boolean): any | null {
  const isBuy = m.kind === 'buy'
  const isSell = m.kind === 'sell'

  if (m.above) {
    const dotColor = m.color ?? (isBuy ? '#FACC15' : CT().text)
    if (compact) {
      return {
        name: m.date, coord: [m.date, d.high],
        symbol: 'circle', symbolSize: 4, symbolOffset: [0, -10],
        itemStyle: { color: dotColor, cursor: 'pointer' },
        label: { show: false }, z: 100, zlevel: 10,
      }
    }
    return {
      name: m.date, coord: [m.date, d.high],
      symbol: 'circle', symbolSize: 12, symbolOffset: [0, -2],
      itemStyle: { color: 'transparent' },
      label: {
        show: true, formatter: m.label ?? '', position: 'top', distance: 0,
        color: dotColor, fontSize: 10, fontWeight: 'normal',
        fontFamily: 'JetBrains Mono, monospace',
      },
      z: 100, zlevel: 10,
    }
  }

  const isNeutral = !isBuy && !isSell
  const color = m.color ?? (isBuy ? THEME.bull : isSell ? THEME.bear : CT().text)

  return {
    name: `${m.date}|${m.kind}`,
    coord: [m.date, isBuy ? d.low : d.high],
    symbol: isBuy ? ARROW_UP : isSell ? ARROW_DOWN : 'circle',
    // 强度档把"极端程度"画出来: 1 常规 / 2 较强 / 3 极强。只有回测证明该信号的
    // 强度与后续走势有剂量-反应关系(且幅度没塌)时才有 tier —— 否则一律常规大小。
    symbolSize: isNeutral
      ? [6, 6]
      : m.tier === 3 ? [15, 14] : m.tier === 2 ? [13, 12] : [11, 10],
    // symbolOffset 是像素、y 向下为正：箭头推离 K 线，别压在影线上
    symbolOffset: [0, isBuy ? 17 : -17],
    itemStyle: {
      color,
      borderColor: CT().tooltipBg,
      borderWidth: 1,
      cursor: 'pointer',
    },
    label: compact
      ? { show: false }
      : {
          show: !!m.label,
          formatter: m.label ?? '',
          position: isBuy ? 'bottom' : 'top',
          distance: 2,
          color,
          fontSize: 9,
          fontFamily: 'JetBrains Mono, monospace',
        },
    z: 100, zlevel: 10,
  }
}

function buildOption(
  data: OHLC[],
  dates: string[],
  dateIndexMap: Map<string, number>,
  markers: ChartMarker[] | undefined,
  ranges: ChartRange[] | undefined,
  priceLines: ChartPriceLine[] | undefined,
  showMA: boolean,
  compact: boolean,
  activeIndicators: string[],
  containerHeight: number,
  infoIdx: number,
  linkedPrice: number | null | undefined,
  volumeCompare: VolumeCompareConfig,
  mainType: MainChartType,
  volumeProfile: VolumeProfileOverlay | null,
  visibleFrom: number,
  visibleTo: number,
): EChartsOption {
  const candleData = data.map(d => [d.open, d.close, d.low, d.high])

  const hasMA = showMA && data.some(d => d.ma5 != null || d.ma10 != null || d.ma20 != null || d.ma60 != null)

  const markPointData: any[] = []
  if (markers && markers.length > 0) {
    for (const m of markers) {
      const idx = dateIndexMap.get(m.date)
      if (idx == null) continue
      const p = buildMarkPoint(m, data[idx], compact)
      if (p) markPointData.push(p)
    }
  }

  // ====== 布局计算 ======
  const left = 60
  const right = 20
  const topPad = 8
  const candleBottomPad = 22

  let subTotalH = 0
  const activeSubDefs: SubChartDef[] = []
  activeIndicators.forEach(key => {
    const def = SUB_CHARTS.find(s => s.key === key)
    if (!def) return
    activeSubDefs.push(def)
    subTotalH += INFO_BAR_H + def.height
  })
  if (activeSubDefs.length > 0) subTotalH += activeSubDefs.length * SUB_GAP_PX

  const candleAvail = Math.max(containerHeight - topPad - candleBottomPad - subTotalH, 100)

  // ===== 成交量分布(Volume Profile): 在主图右侧预留一条窄带 =====
  // 不画在蜡烛上面(会挡住最后几根), 也不另起一个组件(那样价格标尺对不齐)。
  const PROFILE_W = 92
  const PROFILE_GAP = 10
  const showProfile = !!volumeProfile && volumeProfile.bins.length > 0
  const grid0Right = right + (showProfile ? PROFILE_W + PROFILE_GAP : 0)

  // 开叠加层时主图 y 标尺必须钉死: 分布图在**另一个 grid**里, 两边各自自动缩放
  // 会差几个像素 —— 表现就是"柱子和蜡烛对不上"。这里按可见区间自己算极值,
  // 口径对齐 ECharts 的 scale:true + 上下 3% 边距, 且必须把 MA/BOLL/价格线算进去
  // (只按 OHLC 算会把这些线裁掉)。
  const pinnedY = showProfile
    ? priceExtentFor(data, visibleFrom, visibleTo, hasMA, activeIndicators.includes('boll'), priceLines)
    : null

  const grids: any[] = []
  const xAxes: any[] = []
  const yAxes: any[] = []
  const series: any[] = []
  const xAxisIndices: number[] = []

  const priceLineValues = (priceLines ?? [])
    .map(line => line.value)
    .filter(value => Number.isFinite(value) && value > 0)
  const axisMin = priceLineValues.length > 0
    ? ({ min, max }: { min: number; max: number }) => {
        const nextMin = Math.min(min, ...priceLineValues)
        const nextMax = Math.max(max, ...priceLineValues)
        return nextMin - Math.max((nextMax - nextMin) * 0.03, nextMax * 0.001)
      }
    : undefined
  const axisMax = priceLineValues.length > 0
    ? ({ min, max }: { min: number; max: number }) => {
        const nextMin = Math.min(min, ...priceLineValues)
        const nextMax = Math.max(max, ...priceLineValues)
        return nextMax + Math.max((nextMax - nextMin) * 0.03, nextMax * 0.001)
      }
    : undefined

  // ===== grid 0: K线主图 =====
  grids.push({ left, right: grid0Right, top: topPad, height: candleAvail })
  xAxes.push({
    type: 'category', data: dates, boundaryGap: true,
    axisLine: { lineStyle: { color: CT().border } },
    axisLabel: { color: CT().text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
    axisTick: { show: false },
    splitLine: { show: false },
  })
  yAxes.push({
    scale: true,
    min: pinnedY ? pinnedY.min : axisMin,
    max: pinnedY ? pinnedY.max : axisMax,
    // 上下各留 3% 边距: 防止最高/最低点的蜡烛贴边, 涨停/炸板标签被遮挡
    boundaryGap: [0.03, 0.03],
    splitArea: { show: false },
    axisLine: { show: false }, axisTick: { show: false },
    splitLine: { lineStyle: { color: CT().grid } },
    axisLabel: { color: CT().text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
  })
  xAxisIndices.push(0)

  const markAreaData = (ranges ?? [])
    .filter(r => dateIndexMap.has(r.start) && dateIndexMap.has(r.end))
    .map(r => ([
      {
        name: r.label ?? '',
        xAxis: r.start,
        itemStyle: { color: r.color ?? 'rgba(59,130,246,0.08)' },
        label: {
          show: !!r.label,
          position: 'insideTop',
          distance: 8,
          color: CT().tooltipText,
          backgroundColor: CT().tooltipBg,
          borderColor: 'rgba(59,130,246,0.35)',
          borderWidth: 1,
          borderRadius: 4,
          padding: [2, 6],
          fontSize: 10,
          fontFamily: 'JetBrains Mono, monospace',
        },
      },
      { xAxis: r.end },
    ]))

  const markLineData: any[] = (priceLines ?? [])
    .filter(line => Number.isFinite(line.value))
    .map(line => {
      const lineStyle = {
        color: line.color ?? CT().text,
        type: 'dashed' as const,
        width: 1,
        opacity: 0.92,
      }
      const label = {
        show: !!line.label,
        formatter: line.label ?? '',
        position: 'insideEndTop' as const,
        color: line.color ?? CT().text,
        backgroundColor: CT().tooltipBg,
        borderRadius: 4,
        padding: [2, 6],
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      }
      if (line.start && line.end && dateIndexMap.has(line.start) && dateIndexMap.has(line.end)) {
        return [
          { xAxis: line.start, yAxis: line.value },
          { xAxis: line.end, yAxis: line.value, lineStyle, label, symbol: 'none' },
        ]
      }
      return { yAxis: line.value, lineStyle, label, symbol: 'none' }
    })

  if (linkedPrice != null) {
    markLineData.push({
      yAxis: linkedPrice,
      lineStyle: { color: '#3B82F6', type: 'dashed', width: 1, opacity: 0.7 },
      label: {
        show: true,
        formatter: linkedPrice.toFixed(2),
        position: 'insideEndTop',
        color: '#3B82F6',
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
        backgroundColor: CT().tooltipBg,
        borderColor: '#3B82F6',
        borderWidth: 1,
        padding: [1, 4],
        borderRadius: 2,
      },
      symbol: 'none',
    })
  }

  // 标注/区间/价格线挂在主图上 —— 四种画法共用同一组 markPoint/markArea/markLine,
  // series 名字统一叫 'K'(增量更新 markPoint 时按 name 匹配, 见 updateCompactPresentation)。
  const markExtras = {
    markPoint: markPointData.length > 0 ? { data: markPointData, animation: false } : undefined,
    markArea: markAreaData.length > 0 ? { silent: true, data: markAreaData } : undefined,
    markLine: markLineData.length > 0 ? { silent: true, symbol: 'none', data: markLineData, animation: false } : undefined,
  }

  if (mainType === 'line') {
    // 收盘线: 单条折线。triggerLineEvent 让"点击看当日分时"在线型下依然可用
    // (默认只有点中 symbol 才触发, symbol 关掉后整条线都点不中)。
    series.push({
      name: 'K', type: 'line',
      data: data.map(d => d.close),
      smooth: false, symbol: 'none', triggerLineEvent: true,
      animation: false,
      lineStyle: { width: 1.4, color: THEME.ma10 },
      itemStyle: { color: THEME.ma10 },
      ...markExtras,
    })
  } else if (mainType === 'ohlc') {
    series.push({
      name: 'K', type: 'custom',
      data: data.map((d, i) => [i, d.open, d.close, d.low, d.high]),
      // 四个价格维度都登记进 y, 否则 y 轴不会把影线范围算进极值
      encode: { x: 0, y: [1, 2, 3, 4] },
      renderItem: renderOhlcItem,
      clip: true,
      animation: false,
      ...markExtras,
    })
  } else {
    const hollow = mainType === 'hollow'
    series.push({
      name: 'K', type: 'candlestick', data: candleData,
      animation: false,
      itemStyle: {
        // 空心: 上涨日只留边框(填充透明), 下跌日实心 —— 与实心蜡烛相反的处理
        color: hollow ? 'transparent' : THEME.bull,
        color0: THEME.bear,
        borderColor: THEME.bull,
        borderColor0: THEME.bear,
        borderWidth: hollow ? 1.2 : 1,
        cursor: 'pointer',
      },
      ...markExtras,
    })
  }

  if (hasMA) {
    const maLine = (key: keyof OHLC, color: string, name: string) => ({
      name, type: 'line',
      data: data.map(d => (d[key] != null ? Number(d[key]) : '-')),
      smooth: true, symbol: 'none', animation: false,
      silent: true,
      lineStyle: { width: 1, color }, itemStyle: { color },
    })
    series.push(maLine('ma5', THEME.ma5, 'MA5'))
    series.push(maLine('ma10', THEME.ma10, 'MA10'))
    series.push(maLine('ma20', THEME.ma20, 'MA20'))
    series.push(maLine('ma60', THEME.ma60, 'MA60'))
  }

  // BOLL 布林带 — 需在 activeIndicators 中激活
  const showBOLL = activeIndicators.includes('boll') && data.some(d => d.boll_upper != null || d.boll_lower != null)
  if (showBOLL) {
    const bollLine = (key: keyof OHLC, color: string, name: string) => ({
      name, type: 'line',
      data: data.map(d => (d[key] != null ? Number(d[key]) : '-')),
      smooth: true, symbol: 'none', animation: false,
      silent: true,
      lineStyle: { width: 1, color, type: 'dashed' as const }, itemStyle: { color },
    })
    series.push(bollLine('boll_upper', '#E879F9', 'BOLL上'))
    series.push(bollLine('boll_lower', '#E879F9', 'BOLL下'))
  }

  // ===== 子图区域 =====
  let curTop = topPad + candleAvail + candleBottomPad

  activeSubDefs.forEach((def, i) => {
    const gridIdx = i + 1
    const xAxisIdx = i + 1
    const yAxisIdx = i + 1

    const chartTop = curTop + INFO_BAR_H
    grids.push({
      left, right,
      top: chartTop,
      height: def.height,
      show: true,
      borderColor: CT().grid,
      borderWidth: 1,
    })

    xAxes.push({
      type: 'category', gridIndex: gridIdx, data: dates, boundaryGap: true,
      axisLine: { show: false }, axisLabel: { show: false },
      axisTick: { show: false }, splitLine: { show: false },
      axisPointer: { label: { show: false } },
    })

    const isFixedRange = !!def.yAxisConfig
    yAxes.push({
      scale: !isFixedRange,
      ...(isFixedRange ? def.yAxisConfig : {}),
      gridIndex: gridIdx,
      splitNumber: 2,
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: CT().grid } },
      axisLabel: {
        show: true, color: CT().text, fontSize: 9,
        fontFamily: 'JetBrains Mono, monospace',
      },
    })

    xAxisIndices.push(xAxisIdx)

    const subSeries = def.buildSeries(data, { compact, volumeCompare })
    subSeries.forEach((s: any) => {
      series.push({ ...s, xAxisIndex: xAxisIdx, yAxisIndex: yAxisIdx })
    })

    curTop += INFO_BAR_H + def.height + SUB_GAP_PX
  })

  // ===== 成交量分布叠加层(独立 grid, 贴在主图右侧预留带内) =====
  // 用独立 grid 而不是挂在主图 series 上, 是为了绕开 dataZoom 的过滤 ——
  // 主图 series 会被 dataZoom 按可见区间裁剪, 挂在上面分布图会整片消失。
  if (showProfile && pinnedY && volumeProfile) {
    const vp = volumeProfile
    const gridIdx = grids.length
    const xAxisIdx = xAxes.length
    const yAxisIdx = yAxes.length
    const maxPct = Math.max(...vp.bins.map(b => b.pct), 1e-9)

    grids.push({
      right, width: PROFILE_W,
      top: topPad, height: candleAvail,
      show: false,
    })
    xAxes.push({
      type: 'value', gridIndex: gridIdx, min: 0, max: maxPct,
      show: false,
    })
    yAxes.push({
      type: 'value', gridIndex: gridIdx,
      min: pinnedY.min, max: pinnedY.max,
      show: false,
    })

    const renderProfileItem = (params: any, api: any) => {
      const row = params.data as number[]
      // row = [kind, pct, lo, hi]   kind: 0=量柱 1=POC 2=价值区边界
      const kind = row[0]
      const pct = row[1]
      const lo = row[2]
      const hi = row[3]
      const p1 = api.coord([0, hi])
      const p2 = api.coord([pct, lo])
      const yTop = Math.min(p1[1], p2[1])
      const yBot = Math.max(p1[1], p2[1])
      if (kind === 0) {
        const inVa = row[4] === 1
        return {
          type: 'rect',
          shape: {
            x: p1[0],
            y: yTop,
            width: Math.max(p2[0] - p1[0], 0.6),
            height: Math.max(yBot - yTop, 0.8),
          },
          style: {
            fill: inVa ? 'rgba(59,130,246,0.50)' : 'rgba(148,163,184,0.28)',
          },
          silent: true,
        }
      }
      const y = api.coord([0, lo])[1]
      return {
        type: 'line',
        shape: { x1: p1[0], y1: y, x2: p1[0] + PROFILE_W, y2: y },
        style: {
          stroke: kind === 1 ? '#F59E0B' : 'rgba(148,163,184,0.8)',
          lineWidth: 1,
          lineDash: kind === 1 ? undefined : [3, 3],
        },
        silent: true,
      }
    }

    const vpData: number[][] = vp.bins.map(b => [
      0, b.pct, b.lo, b.hi, b.inValueArea ? 1 : 0,
    ])
    vpData.push([1, maxPct, vp.poc, vp.poc, 0])
    vpData.push([2, maxPct, vp.vah, vp.vah, 0])
    vpData.push([2, maxPct, vp.val, vp.val, 0])

    series.push({
      name: 'VP', type: 'custom',
      xAxisIndex: xAxisIdx, yAxisIndex: yAxisIdx,
      data: vpData,
      renderItem: renderProfileItem,
      clip: false,
      silent: true,
      animation: false,
      z: 2,
    })
  }

  // 子图信息栏 graphic
  const subStartTop = topPad + candleAvail + candleBottomPad
  const infoGraphics = buildSubInfoGraphics(data, infoIdx, activeIndicators, subStartTop, volumeCompare)

  return {
    animation: false,
    backgroundColor: THEME.bg,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', crossStyle: { color: CT().crosshair } },
      backgroundColor: 'transparent',
      borderWidth: 0,
      textStyle: { fontSize: 0 },
      formatter: () => '',
    },
    axisPointer: {
      link: [{ xAxisIndex: 'all' }],
      label: {
        backgroundColor: CT().crosshairLabelBg,
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10,
      },
    },
    graphic: infoGraphics.length > 0 ? infoGraphics : undefined,
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: xAxisIndices,
        start: 0,
        end: 100,
        moveOnMouseMove: true,
        zoomOnMouseWheel: true,
      },
    ],
    series,
  }
}


export function EChartsCandlestick({
  data,
  markers,
  ranges,
  priceLines,
  height = 480,
  showMA = true,
  showInfoBar = true,
  showMarkers: showMarkersProp = true,
  onToggleMarkers: _onToggleMarkers,
  stockInfo,
  symbol: _symbol,
  linkedPrice,
  onDateClick,
  onPriceDoubleClick,
  visibleBars = 60,
  activeIndicators = [],
  volumeCompare = { enabled: true, days: 1 },
  mainType = 'candle',
  volumeProfile = null,
  onVisibleRangeChange,
  drawings,
  drawTool = null,
  drawCtx = '',
  onDrawingsChange,
  onDrawToolChange,
}: Props) {
  // 画图图层要拿到 ECharts 实例才能做像素↔数据坐标换算。init 在 effect 里,
  // 所以这里用 state 存实例, 让图层有能力在图表真正就绪后再挂载。
  const [chartInst, setChartInst] = useState<ECharts | null>(null)
  // 可见区间(闭区间下标)。缩放/平移后要重算成交量分布, 否则分布图统计的区间
  // 与眼睛看到的蜡烛不是同一段 —— 看起来就像"柱子对不上"。
  const [visRange, setVisRange] = useState(() => ({
    from: Math.max(0, data.length - Math.max(1, visibleBars)),
    to: Math.max(0, data.length - 1),
  }))
  const onVisRef = useRef(onVisibleRangeChange)
  onVisRef.current = onVisibleRangeChange
  const visTimerRef = useRef<number | null>(null)
  const pendingRangeRef = useRef<{ from: number; to: number } | null>(null)
  useEffect(() => {
    onVisRef.current?.(visRange.from, visRange.to)
  }, [visRange])
  useEffect(() => () => {
    if (visTimerRef.current != null) window.clearTimeout(visTimerRef.current)
  }, [])
  // mainType 只影响画法; 放进 ref 是为了 updateCompactPresentation 里也能读到最新值
  const mainTypeRef = useRef(mainType)
  mainTypeRef.current = mainType
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const dataRef = useRef(data)
  dataRef.current = data
  const onDateClickRef = useRef(onDateClick)
  onDateClickRef.current = onDateClick
  const onPriceDoubleClickRef = useRef(onPriceDoubleClick)
  onPriceDoubleClickRef.current = onPriceDoubleClick
  // 主题: buildOption/信息栏内部通过 CT() 动态取调色板, 这里只负责切换时触发重建
  const theme = useTheme()

  // --- 全部用 ref，避免高频交互触发 React 重渲染 ---
  const infoIdxRef = useRef<number>(data.length - 1)
  const compactRef = useRef(false)
  const userZoomRef = useRef<{ start: number; end: number } | null>(null)

  // 需要在闭包中访问最新值的变量 — 先声明占位，后面赋值
  const activeIndicatorsRef = useRef(activeIndicators)
  activeIndicatorsRef.current = activeIndicators
  const volumeCompareRef = useRef(volumeCompare)
  volumeCompareRef.current = volumeCompare
  const chartHeightRef = useRef(300)
  const subTotalHRef = useRef(0)
  const getInfoBarHTMLRef = useRef<() => string>(() => '')

  // 强制刷新信息栏 DOM 的回调
  const infoBarRef = useRef<HTMLDivElement>(null)
  const triggerInfoBarUpdate = useRef(() => {
    const idx = infoIdxRef.current
    const curData = dataRef.current
    const d = idx >= 0 && idx < curData.length ? curData[idx] : null
    if (!d) return
    const chart = chartRef.current
    if (!chart) return
    const subStartTop = chartHeightRef.current - subTotalHRef.current
    const infoGraphics = buildSubInfoGraphics(
      curData,
      idx,
      activeIndicatorsRef.current,
      subStartTop,
      volumeCompareRef.current,
    )
    if (infoGraphics.length > 0) {
      chart.setOption({ graphic: infoGraphics }, { lazyUpdate: true })
    }
  }).current

  // 计算子图总高度
  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is SubChartDef => !!d)

  let subTotalH = 0
  activeSubDefs.forEach(def => { subTotalH += INFO_BAR_H + def.height })
  if (activeSubDefs.length > 0) subTotalH += activeSubDefs.length * SUB_GAP_PX

  const mainInfoBarH = showInfoBar ? 40 : 0
  const minCandleH = 120

  const chartHeight = Math.max(height - mainInfoBarH, 8 + minCandleH + 14 + subTotalH)
  chartHeightRef.current = chartHeight
  subTotalHRef.current = subTotalH

  // 预计算 date→index Map (O(1) 查找)
  const dates = useMemo(() => data.map(d => d.date), [data])
  const dateIndexMap = useMemo(() => {
    const m = new Map<string, number>()
    dates.forEach((d, i) => m.set(d, i))
    return m
  }, [dates])

  // 计算 dataZoom 初始范围
  const initialZoom = useMemo(() => ({
    start: Math.max(0, 100 - (visibleBars / Math.max(data.length, 1)) * 100),
    end: 100,
  }), [visibleBars, data.length])

  // ===== 信息栏 HTML 内容 (基于 infoIdxRef.current) =====
  const getInfoBarHTML = useCallback(() => {
    let idx = infoIdxRef.current
    let d = idx >= 0 && idx < data.length ? data[idx] : null
    // fallback: 如果当前 idx 无数据，取最后一根 K 线
    if (!d && data.length > 0) {
      idx = data.length - 1
      d = data[idx]
    }
    if (!d) return ''
    const prev = idx > 0 ? data[idx - 1] : null
    const chg = prev ? d.close - prev.close : 0
    const isUp = chg >= 0
    const clr = isUp ? THEME.bull : THEME.bear
    const floatShares = stockInfo?.float_shares
    const turnoverRate = floatShares && d.volume ? (d.volume * 100 / floatShares * 100) : null

    let html = `<div style="display:flex;align-items:center;gap:6px;padding:0 8px;font:11px 'JetBrains Mono',monospace;select:none;height:20px;flex-wrap:wrap">`
    html += `<span style="color:${CT().text}">${d.date}</span>`
    html += `<span style="color:${CT().text}">开</span>`
    html += `<span style="color:${d.open >= d.close ? THEME.bear : THEME.bull}">${d.open.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">高</span>`
    html += `<span style="color:${THEME.bull}">${d.high.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">低</span>`
    html += `<span style="color:${THEME.bear}">${d.low.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">收</span>`
    html += `<span style="color:${clr};font-weight:600">${d.close.toFixed(2)}</span>`
    // 涨跌幅 (收盘后, 换手前; 和收间隔一些距离)
    if (prev) {
      const chgPct = (chg / prev.close * 100)
      html += `<span style="color:${clr};margin-left:8px">${isUp ? '+' : ''}${chgPct.toFixed(2)}%</span>`
    }
    if (turnoverRate != null) {
      html += `<span style="color:${CT().text}">换手</span>`
      html += `<span style="color:${CT().text}">${turnoverRate.toFixed(2)}%</span>`
    }
    html += `</div>`

    // 第二行: MA + BOLL
    if (showMA) {
      html += `<div style="display:flex;align-items:center;gap:10px;padding:0 8px;font:11px 'JetBrains Mono',monospace;select:none;height:20px;flex-wrap:wrap">`
      if (d.ma5 != null) html += `<span style="color:${THEME.ma5}">MA5:${Number(d.ma5).toFixed(2)}</span>`
      if (d.ma10 != null) html += `<span style="color:${THEME.ma10}">MA10:${Number(d.ma10).toFixed(2)}</span>`
      if (d.ma20 != null) html += `<span style="color:${THEME.ma20}">MA20:${Number(d.ma20).toFixed(2)}</span>`
      if (d.ma60 != null) html += `<span style="color:${THEME.ma60}">MA60:${Number(d.ma60).toFixed(2)}</span>`
      if (d.boll_upper != null && activeIndicators.includes('boll')) {
        html += `<span style="color:#E879F9">BOLL:${Number(d.boll_upper).toFixed(2)}/${Number(d.ma20).toFixed(2)}/${Number(d.boll_lower).toFixed(2)}</span>`
      }
      html += `</div>`
    }

    return html
  }, [data, stockInfo, showMA, activeIndicators])
  getInfoBarHTMLRef.current = getInfoBarHTML

  // data 变化时重置 infoIdx
  useEffect(() => {
    infoIdxRef.current = data.length - 1
    compactRef.current = false
    userZoomRef.current = null
  }, [data.length])

  // ===== 初始化 chart (只在 chartHeight 变化时重建) =====
  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const chart = echarts.init(el, undefined, { renderer: 'canvas' })
    chartRef.current = chart
    setChartInst(chart)

    // 鼠标移动 → 只更新 ref + DOM，不触发 React re-render
    // 设计原则: 找不到有效数据时保持上次显示，永远不清空信息栏
    chart.on('updateAxisPointer', (event: any) => {
      const axesInfo = event.axesInfo
      if (!axesInfo) return // 鼠标移出图表区域，保持当前显示
      for (const info of Object.values(axesInfo)) {
        const val = (info as any)?.value
        if (val == null) continue
        const d = dataRef.current
        const idx = typeof val === 'number' ? val : d.findIndex(x => x.date === val)
        if (idx >= 0 && idx < d.length) {
          if (infoIdxRef.current === idx) return
          infoIdxRef.current = idx

          // 直接更新信息栏 DOM (通过 ref 读取最新的生成函数)
          const infoEl = infoBarRef.current
          if (infoEl) {
            const html = getInfoBarHTMLRef.current()
            if (html) infoEl.innerHTML = html  // 只在有内容时更新
          }

          // 更新子图 graphic
          triggerInfoBarUpdate()
          return
        }
      }
      // 没有找到有效数据 — 不做任何操作，保持上次显示
    })

    chart.on('click', (params: any) => {
      if (params.componentType === 'markPoint' && params.name) {
        // name 形如 '2026-10-13|buy'(同日上下两个徽章靠后缀区分), 取回日期
        onDateClickRef.current?.(String(params.name).split('|')[0])
        return
      }
      if (params.seriesName !== 'K' || params.dataIndex == null) return
      const d = dataRef.current
      const idx = params.dataIndex
      if (idx >= 0 && idx < d.length) {
        onDateClickRef.current?.(d[idx].date)
      }
    })

    const handlePriceDoubleClick = (event: { offsetX: number; offsetY: number }) => {
      const pixel: [number, number] = [event.offsetX, event.offsetY]
      if (!chart.containPixel({ gridIndex: 0 }, pixel)) return
      const coordinate = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, pixel)
      const price = Array.isArray(coordinate) ? Number(coordinate[1]) : NaN
      const currentPrice = dataRef.current[dataRef.current.length - 1]?.close
      if (Number.isFinite(price) && price > 0 && Number.isFinite(currentPrice) && currentPrice > 0) {
        onPriceDoubleClickRef.current?.(price, currentPrice)
      }
    }
    chart.getZr().on('dblclick', handlePriceDoubleClick)

    // dataZoom → 只更新 ref，不触发 React re-render
    // compact 变化时需要增量更新 markPoint
    chart.on('dataZoom', () => {
      const opt = chart.getOption() as any
      const zoom = opt?.dataZoom?.[0]
      if (!zoom) return
      userZoomRef.current = { start: zoom.start, end: zoom.end }

      const d = dataRef.current
      const total = d.length
      const visibleCount = Math.round(total * (zoom.end - zoom.start) / 100)
      const newCompact = visibleCount > COMPACT_THRESHOLD
      if (newCompact !== compactRef.current) {
        compactRef.current = newCompact
        updateCompactPresentation()
      }

      // 可见区间变化 → 通知调用方重算成交量分布。
      // 节流: 滚轮缩放会连发几十次, 每次都整图 rebuild 会卡; 停手 140ms 后再同步
      // (期间分布图仍按上一区间画, 松手即对齐)。
      const fromIdx = Math.max(0, Math.min(total - 1, Math.round((total * zoom.start) / 100)))
      const toIdx = Math.max(fromIdx, Math.min(total - 1, Math.round((total * zoom.end) / 100) - 1))
      pendingRangeRef.current = { from: fromIdx, to: toIdx }
      if (visTimerRef.current == null) {
        visTimerRef.current = window.setTimeout(() => {
          visTimerRef.current = null
          const p = pendingRangeRef.current
          if (!p) return
          // 值没变就返回 prev, 让 React 跳过这次渲染 —— 否则
          // "rebuild → dispatch dataZoom → dataZoom 事件 → setState" 会自激成死循环。
          setVisRange((prev: { from: number; to: number }) => (prev.from === p.from && prev.to === p.to ? prev : p))
        }, 140)
      }
    })

    const ro = new ResizeObserver(() => { chart.resize() })
    ro.observe(el)

    return () => {
      chart.off('updateAxisPointer')
      chart.off('click')
      chart.off('dataZoom')
      chart.getZr().off('dblclick', handlePriceDoubleClick)
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
      setChartInst(null)
    }
  }, [chartHeight]) // eslint-disable-line react-hooks/exhaustive-deps

  // 缩放跨过紧凑阈值时，仅增量更新标签，不重建整张图。
  function updateCompactPresentation() {
    const chart = chartRef.current
    if (!chart) return
    const mkrs = showMarkersProp ? markers : undefined
    const compact = compactRef.current
    const seriesUpdates: any[] = []
    const markPointData: any[] = []
    for (const m of mkrs ?? []) {
      const idx = dateIndexMap.get(m.date)
      if (idx == null) continue
      const p = buildMarkPoint(m, data[idx], compact)
      if (p) markPointData.push(p)
    }
    if (mkrs?.length) {
      seriesUpdates.push({
        name: 'K',
        markPoint: markPointData.length > 0 ? { data: markPointData, animation: false } : undefined,
      })
    }
    if (activeIndicatorsRef.current.includes('vol')) {
      seriesUpdates.push({
        name: '成交量',
        label: { show: volumeCompareRef.current.enabled && !compact },
      })
    }
    if (seriesUpdates.length > 0) chart.setOption({ series: seriesUpdates })
  }

  // ===== 核心: 仅在数据/配置变更时全量 setOption =====
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    const option = buildOption(
      data, dates, dateIndexMap,
      showMarkersProp ? markers : undefined,
      ranges,
      priceLines,
      showMA, compactRef.current,
      activeIndicators, chartHeight,
      infoIdxRef.current,
      linkedPrice,
      volumeCompare,
      mainTypeRef.current,
      volumeProfile,
      visRange.from,
      visRange.to,
    )

    chart.setOption(option, true)

    // 恢复用户缩放位置
    const zoom = userZoomRef.current
    if (zoom) {
      chart.dispatchAction({ type: 'dataZoom', start: zoom.start, end: zoom.end })
    } else {
      chart.dispatchAction({ type: 'dataZoom', start: initialZoom.start, end: initialZoom.end })
    }

    // 初始信息栏
    const infoEl = infoBarRef.current
    if (infoEl) {
      infoEl.innerHTML = getInfoBarHTML()
    }
  }, [data, markers, ranges, priceLines, linkedPrice, showMA, showMarkersProp, activeIndicators, volumeCompare, mainType, chartHeight, dates, dateIndexMap, initialZoom, getInfoBarHTML, theme, volumeProfile, visRange])

  // 渲染信息栏容器 (内容由 JS 直接写入)
  const initialHTML = useMemo(() => {
    const idx = data.length - 1
    const d = idx >= 0 && idx < data.length ? data[idx] : null
    if (!d) return ''
    const floatShares = stockInfo?.float_shares
    const turnoverRate = floatShares && d.volume ? (d.volume * 100 / floatShares * 100) : null
    let html = `<div style="display:flex;align-items:center;gap:6px;padding:0 8px;font:11px 'JetBrains Mono',monospace;height:20px;flex-wrap:wrap">`
    html += `<span style="color:${CT().text}">${d.date}</span>`
    html += `<span style="color:${CT().text}">开</span>`
    html += `<span style="color:${d.open >= d.close ? THEME.bear : THEME.bull}">${d.open.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">高</span>`
    html += `<span style="color:${THEME.bull}">${d.high.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">低</span>`
    html += `<span style="color:${THEME.bear}">${d.low.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">收</span>`
    const prevClose0 = data[idx-1]?.close ?? d.close
    const clr0 = d.close >= prevClose0 ? THEME.bull : THEME.bear
    html += `<span style="color:${clr0};font-weight:600">${d.close.toFixed(2)}</span>`
    // 涨跌幅 (收盘后, 换手前; 和收间隔一些距离)
    if (idx > 0) {
      const chgPct0 = ((d.close - prevClose0) / prevClose0 * 100)
      html += `<span style="color:${clr0};margin-left:8px">${chgPct0 >= 0 ? '+' : ''}${chgPct0.toFixed(2)}%</span>`
    }
    if (turnoverRate != null) {
      html += `<span style="color:${CT().text}">换手</span>`
      html += `<span style="color:${CT().text}">${turnoverRate.toFixed(2)}%</span>`
    }
    html += `</div>`
    if (showMA) {
      html += `<div style="display:flex;align-items:center;gap:10px;padding:0 8px;font:11px 'JetBrains Mono',monospace;height:20px;flex-wrap:wrap">`
      if (d.ma5 != null) html += `<span style="color:${THEME.ma5}">MA5:${Number(d.ma5).toFixed(2)}</span>`
      if (d.ma10 != null) html += `<span style="color:${THEME.ma10}">MA10:${Number(d.ma10).toFixed(2)}</span>`
      if (d.ma20 != null) html += `<span style="color:${THEME.ma20}">MA20:${Number(d.ma20).toFixed(2)}</span>`
      if (d.ma60 != null) html += `<span style="color:${THEME.ma60}">MA60:${Number(d.ma60).toFixed(2)}</span>`
      if (d.boll_upper != null && activeIndicators.includes('boll')) {
        html += `<span style="color:#E879F9">BOLL:${Number(d.boll_upper).toFixed(2)}/${Number(d.ma20).toFixed(2)}/${Number(d.boll_lower).toFixed(2)}</span>`
      }
      html += `</div>`
    }
    return html
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className="w-full">
      {/* 主图信息栏 — 内容由 JS 直接操作 innerHTML */}
      {showInfoBar && (
        <div ref={infoBarRef} style={{ backgroundColor: CT().infoBarBg }}
          dangerouslySetInnerHTML={{ __html: initialHTML }} />
      )}

      {/* ECharts canvas + 画图图层(图层与 canvas 同区域, 只盖 canvas 那一块) */}
      <div className="relative w-full">
        <div ref={containerRef} className="w-full" style={{ height: chartHeight }} />
        {chartInst && (drawTool || (drawings && drawings.length > 0)) && (
          <ChartDrawingLayer
            chart={chartInst}
            dates={dates}
            dateIndexMap={dateIndexMap}
            rows={data}
            drawings={drawings ?? []}
            ctx={drawCtx}
            tool={drawTool ?? null}
            onChange={next => onDrawingsChange?.(next)}
            onToolChange={onDrawToolChange}
          />
        )}
      </div>
    </div>
  )
}
