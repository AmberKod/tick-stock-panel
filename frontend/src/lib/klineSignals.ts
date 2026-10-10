/**
 * K线内标注 —— **全部是 OHLCV 的确定性派生**, 不是打分、不是预测信号。
 *
 * 为什么要写清楚这一句: 九章个股页的「K线内标注」里有一部分是模型/情绪分, 那类
 * 东西我方不做(三纪律: 不引未回测打分)。这里每一条标注都能写成一个可复核的公式,
 * 任何人拿同一串 K 线都能算出同样的结果 —— 它是**事实标记**, 不是"建议买卖"。
 *
 * 标注只描述"发生了什么"(放量 / 创阶段新高 / MACD 交叉), 不描述"该不该买"。
 */
import type { OHLC } from '@/components/EChartsCandlestick'
import type { ChartMarker } from '@/components/EChartsCandlestick'

export type SignalKey = 'volume' | 'newHigh' | 'newLow' | 'macd'

export interface SignalDef {
  key: SignalKey
  label: string
  /** 口径说明 —— 直接展示给用户, 标注是算出来的, 不是猜出来的。 */
  desc: string
}

export const KLINE_SIGNALS: SignalDef[] = [
  { key: 'volume', label: '放量', desc: '成交量 ≥ 前 20 根均量 × 2' },
  { key: 'newHigh', label: '新高', desc: '收盘价创近 60 根新高' },
  { key: 'newLow', label: '新低', desc: '收盘价创近 60 根新低' },
  { key: 'macd', label: 'MACD', desc: 'DIF 上穿 DEA(金叉) / 下穿(死叉)' },
]

/** 标注太多会把 K 线糊住 —— 只在最近 N 根里打标。 */
const RECENT_WINDOW = 120

function sma(values: number[], n: number, endIdx: number): number | null {
  // [endIdx-n, endIdx) 前 n 根(不含当前根)
  const start = endIdx - n
  if (start < 0) return null
  let sum = 0
  for (let i = start; i < endIdx; i += 1) sum += values[i]
  return sum / n
}

/**
 * 生成标注。
 * @param rows 已按时间升序、且指标已算好的 K 线(日K / 周K / 分钟K 都行)
 * @param enabled 用户勾选的标注类型
 */
export function buildSignalMarkers(rows: OHLC[], enabled: ReadonlySet<string>): ChartMarker[] {
  if (rows.length === 0 || enabled.size === 0) return []

  const marks: ChartMarker[] = []
  const from = Math.max(0, rows.length - RECENT_WINDOW)

  const volumes = rows.map(r => r.volume ?? 0)
  const closes = rows.map(r => r.close)
  const dif = rows.map(r => r.macd_dif ?? NaN)
  const dea = rows.map(r => r.macd_dea ?? NaN)

  for (let i = from; i < rows.length; i += 1) {
    const r = rows[i]
    const date = r.date

    if (enabled.has('volume')) {
      const base = sma(volumes, 20, i)
      if (base && base > 0 && (r.volume ?? 0) >= base * 2) {
        marks.push({ date, kind: 'neutral', label: '量', above: true, color: '#38BDF8' })
      }
    }

    if (enabled.has('newHigh') || enabled.has('newLow')) {
      const win = closes.slice(Math.max(0, i - 60), i)
      if (win.length >= 20) {
        const hi = Math.max(...win)
        const lo = Math.min(...win)
        if (enabled.has('newHigh') && r.close > hi) {
          marks.push({ date, kind: 'buy', label: '高', above: true, color: '#F87171' })
        } else if (enabled.has('newLow') && r.close < lo) {
          marks.push({ date, kind: 'sell', label: '低', above: true, color: '#4ADE80' })
        }
      }
    }

    if (enabled.has('macd')) {
      const d0 = dif[i]
      const d1 = dif[i - 1]
      const s0 = dea[i]
      const s1 = dea[i - 1]
      if (Number.isFinite(d0) && Number.isFinite(d1) && Number.isFinite(s0) && Number.isFinite(s1)) {
        if (d1 <= s1 && d0 > s0) {
          marks.push({ date, kind: 'buy', label: '金', above: true, color: '#C74040' })
        } else if (d1 >= s1 && d0 < s0) {
          marks.push({ date, kind: 'sell', label: '死', above: true, color: '#2D9B65' })
        }
      }
    }
  }

  return marks
}
