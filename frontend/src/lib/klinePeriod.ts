/**
 * 日K → 周K / 月K / 年K 聚合 + 技术指标按新周期重算。
 *
 * 为什么在前端做: 后端只有日K(事实上的最小粒度真数据), 周/月/年都是日K的
 * **确定性派生**, 不是另一份数据 —— 聚合出来的每根都能回溯到具体的日K区间,
 * 不存在"编"的成分。
 *
 * ⚠️ 指标为什么要重算而不是沿用日K值: 周K的 MACD 就该按周收盘算。若直接把
 * 日K的 MACD 值搬到周K上, 副图看着像数据, 实际是"日频指标按周抽样"—— 一种
 * 更隐蔽的失真。这里 MA / BOLL / MACD / RSI / KDJ 全部在新周期序列上重算,
 * 与通达信周K口径一致(SMA 用 Wilder 平滑、MACD 用 EMA12/26/9、KDJ 9 日 RSV)。
 *
 * 日K(period='day')原样返回, 一个字段都不动 —— 后端算好的就是权威值。
 */
import type { OHLC } from '@/components/EChartsCandlestick'

/**
 * 'minute' 是**当日分时**(独立的腾讯分钟源), 不走本文件的日K聚合 ——
 * 它跟日/周/月/年不是一个坐标系(分时以昨收为中轴对称, 周月年是 K 线聚合)。
 */
export type KPeriod = 'day' | 'week' | 'month' | 'year' | 'minute'

/**
 * 周期 → 该周期默认可见根数(对齐九章 defView, 避免切到年K时还是 60 根)。
 * `minute` 是当日全量(240+ 点), 不存在"看多少根"的概念, 占位 0 —— 调用方
 * 拿到 0 应当忽略, 不要拿去 setVisibleBars。
 */
export const PERIOD_DEFAULT_BARS: Record<KPeriod, number> = {
  minute: 0,
  day: 120,
  week: 100,
  month: 90,
  year: 22,
}

/** '2026-10-09' → 该周周一的 '2026-10-05'。周一为一周起点(与 A股周线一致)。 */
function weekStart(iso: string): string {
  const d = new Date(`${iso}T00:00:00`)
  const dow = d.getDay() // 0=周日
  const back = dow === 0 ? 6 : dow - 1
  d.setDate(d.getDate() - back)
  return d.toISOString().slice(0, 10)
}

function bucketKey(iso: string, period: KPeriod): string {
  if (period === 'week') return weekStart(iso)
  if (period === 'month') return iso.slice(0, 7)
  if (period === 'year') return iso.slice(0, 4)
  return iso
}

function sma(values: number[], n: number): (number | null)[] {
  const out: (number | null)[] = []
  let sum = 0
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i]
    if (i >= n) sum -= values[i - n]
    out.push(i >= n - 1 ? sum / n : null)
  }
  return out
}

function ema(values: number[], n: number): number[] {
  const k = 2 / (n + 1)
  const out: number[] = []
  let prev = values[0] ?? 0
  out.push(prev)
  for (let i = 1; i < values.length; i += 1) {
    prev = values[i] * k + prev * (1 - k)
    out.push(prev)
  }
  return out
}

/** Wilder 平滑(RSI / KDJ 用的都是它, 不是简单均值)。 */
function wilder(values: number[], n: number): number[] {
  const out: number[] = []
  let prev = 0
  for (let i = 0; i < values.length; i += 1) {
    prev = i === 0 ? values[i] : (prev * (n - 1) + values[i]) / n
    out.push(prev)
  }
  return out
}

/**
 * 聚合。入参必须已按日期升序(日K接口保证)。
 * 返回的 date 是该周期**首个交易日**(周K显示周一所在的那一周起点, 月K是 1 号)。
 */
/**
 * 在**给定的这一串** bar 上重算全部技术指标。
 *
 * 为什么要能单独调: 分钟K(1/5/15/30/60 分)是另一份数据, 后端只给 OHLCV、
 * 不带指标 —— 指标必须在这串分钟 bar 上按同样的口径算出来。直接沿用日K的
 * 指标值是最隐蔽的一种错(图上看着有 MACD, 实际是日频值按分钟抽样)。
 */
export function computeIndicators(bars: OHLC[]): OHLC[] {
  return attachIndicators(bars)
}

export function aggregateKline(rows: OHLC[], period: KPeriod): OHLC[] {
  // 'minute' 由 StockMinuteChart 单独渲染, 走到这里说明调用方传错了 —— 原样返回,
  // 绝不能把当日分时塞进周/月/年聚合分支(会把日线按月截断, 图直接错)。
  if (period !== 'week' && period !== 'month' && period !== 'year') return rows
  if (rows.length === 0) return rows

  const buckets: { key: string; rows: OHLC[] }[] = []
  let current: { key: string; rows: OHLC[] } | null = null
  for (const r of rows) {
    const key = bucketKey(r.date, period)
    if (!current || current.key !== key) {
      current = { key, rows: [] }
      buckets.push(current)
    }
    current.rows.push(r)
  }

  const bars = buckets.map(b => {
    const first = b.rows[0]
    const last = b.rows[b.rows.length - 1]
    let high = -Infinity
    let low = Infinity
    let volume = 0
    for (const r of b.rows) {
      if (typeof r.high === 'number') high = Math.max(high, r.high)
      if (typeof r.low === 'number') low = Math.min(low, r.low)
      volume += r.volume ?? 0
    }
    return {
      date: period === 'week' ? b.key : first.date.slice(0, period === 'year' ? 4 : 7),
      open: first.open,
      high,
      low,
      close: last.close,
      volume,
    } as OHLC
  })

  return attachIndicators(bars)
}

/**
 * 指标重算(MA/BOLL/MACD/RSI/KDJ)。被 aggregateKline(周月年) 与分钟K共用 ——
 * 分钟K必须自己重算, 不能沿用日K的指标值(见 computeIndicators 注释)。
 */
function attachIndicators(bars: OHLC[]): OHLC[] {
  // ── 在新周期序列上重算指标(不沿用日K值, 见文件头注释) ──
  const closes = bars.map(b => b.close)
  const ma5 = sma(closes, 5)
  const ma10 = sma(closes, 10)
  const ma20 = sma(closes, 20)
  const ma60 = sma(closes, 60)

  // BOLL: MA20 ± 2σ(总体标准差, 与通达信一致)
  const bollUpper: (number | null)[] = []
  const bollLower: (number | null)[] = []
  for (let i = 0; i < closes.length; i += 1) {
    if (ma20[i] == null || i < 19) {
      bollUpper.push(null)
      bollLower.push(null)
      continue
    }
    let sq = 0
    for (let j = i - 19; j <= i; j += 1) sq += (closes[j] - ma20[i]!) ** 2
    const sd = Math.sqrt(sq / 20)
    bollUpper.push(ma20[i]! + 2 * sd)
    bollLower.push(ma20[i]! - 2 * sd)
  }

  // MACD(12,26,9)
  const ema12 = ema(closes, 12)
  const ema26 = ema(closes, 26)
  const dif = ema12.map((v, i) => v - ema26[i])
  const dea = ema(dif, 9)

  // RSI: Wilder 平滑涨跌
  const rsiOf = (n: number): (number | null)[] => {
    const up: number[] = [0]
    const dn: number[] = [0]
    for (let i = 1; i < closes.length; i += 1) {
      const chg = closes[i] - closes[i - 1]
      up.push(Math.max(chg, 0))
      dn.push(Math.max(-chg, 0))
    }
    const au = wilder(up, n)
    const ad = wilder(dn, n)
    return closes.map((_, i) => {
      if (i < n) return null
      const denom = au[i] + ad[i]
      return denom === 0 ? 50 : (au[i] / denom) * 100
    })
  }
  const rsi6 = rsiOf(6)
  const rsi14 = rsiOf(14)
  const rsi24 = rsiOf(24)

  // KDJ(9,3,3)
  const kdjK: (number | null)[] = []
  const kdjD: (number | null)[] = []
  const kdjJ: (number | null)[] = []
  let k = 50
  let d = 50
  for (let i = 0; i < bars.length; i += 1) {
    if (i < 8) {
      kdjK.push(null); kdjD.push(null); kdjJ.push(null)
      continue
    }
    let hh = -Infinity
    let ll = Infinity
    for (let j = i - 8; j <= i; j += 1) {
      hh = Math.max(hh, bars[j].high)
      ll = Math.min(ll, bars[j].low)
    }
    const rsv = hh === ll ? 50 : ((closes[i] - ll) / (hh - ll)) * 100
    k = (2 * k + rsv) / 3
    d = (2 * d + k) / 3
    kdjK.push(k); kdjD.push(d); kdjJ.push(3 * k - 2 * d)
  }

  return bars.map((b, i) => ({
    ...b,
    ma5: ma5[i],
    ma10: ma10[i],
    ma20: ma20[i],
    ma60: ma60[i],
    macd_dif: dif[i] ?? null,
    macd_dea: dea[i] ?? null,
    macd_hist: dif[i] != null && dea[i] != null ? (dif[i] - dea[i]) * 2 : null,
    rsi_6: rsi6[i],
    rsi_14: rsi14[i],
    rsi_24: rsi24[i],
    kdj_k: kdjK[i],
    kdj_d: kdjD[i],
    kdj_j: kdjJ[i],
    boll_upper: bollUpper[i],
    boll_lower: bollLower[i],
  }))
}
