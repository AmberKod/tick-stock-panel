/**
 * 成交量分布(Volume Profile) —— 把一段行情的成交量按**价格**重新分组统计。
 *
 * 与成交量柱的区别: 成交量柱是"按时间"排(每天一根), 成交量分布是"按价格"排
 * (横着放在 K 线右侧) —— 回答的是"这段时间里, 哪个价位成交最多", 也就是
 * 市场公认的**价值区**在哪。
 *
 * ⚠️ 口径(必须可复核, 不接受含糊):
 *   单根 K 的成交量在 [low, high] 区间内**按价格均匀分配**。
 *   这是 Volume Profile 最通用的近似 —— 因为交易所/行情源都不会给日内逐笔的
 *   价量分布, 除权除息前的真实分布无法还原。均匀分配是"不引入额外假设"的
 *   那一种: 它不假定成交集中在收盘价(那会凭空造出一个假的峰)。
 *   一字板(high == low)的 K 整根量落进它所在的那一个价箱。
 *
 *   POC  = 成交量最大的价箱(密集成交价)
 *   价值区 = 从 POC 向两侧逐箱扩展、累计成交量首次达到 valueAreaPct(默认 70%)
 *           的区间, 上下界即 VAH / VAL。
 */
import type { OHLC } from '@/components/EChartsCandlestick'

export interface VPBin {
  /** 价箱下沿 */
  lo: number
  /** 价箱上沿 */
  hi: number
  /** 价箱中心价 */
  mid: number
  /** 该价箱成交量(股) */
  volume: number
  /** 占区间总成交量比例 0~1 */
  pct: number
  /** 是否落在价值区内 */
  inValueArea: boolean
}

export interface VolumeProfileResult {
  bins: VPBin[]
  /** Point of Control —— 成交量最大的价箱中心价 */
  poc: number
  /** Value Area High / Low —— 价值区上下沿 */
  vah: number
  val: number
  /** 区间总成交量(股) */
  totalVolume: number
  /** 参与统计的 K 线根数与日期区间(显示在图上, 让口径可核对) */
  bars: number
  from: string
  to: string
}

/**
 * 按价格分箱统计成交量。
 *
 * @param bars 参与统计的 K 线(调用方负责裁剪到可见区间)
 * @param binCount 价箱数量。默认 42 —— 再密会出现大量空箱(视觉上碎), 再粗则 POC 定位不准。
 * @param valueAreaPct 价值区占比, 默认 0.70(行业惯例 70%)
 */
export function computeVolumeProfile(
  bars: OHLC[],
  binCount = 42,
  valueAreaPct = 0.7,
): VolumeProfileResult | null {
  const usable = bars.filter(
    b => Number.isFinite(b.high) && Number.isFinite(b.low) && Number.isFinite(b.volume)
      && b.high > 0 && b.low > 0 && (b.volume ?? 0) > 0 && b.high >= b.low,
  )
  if (usable.length === 0) return null

  let lo = Infinity
  let hi = -Infinity
  for (const b of usable) {
    if (b.low < lo) lo = b.low
    if (b.high > hi) hi = b.high
  }
  if (!(hi > lo)) return null

  const width = (hi - lo) / binCount
  const acc = new Array<number>(binCount).fill(0)

  for (const b of usable) {
    const vol = b.volume ?? 0
    if (b.high === b.low) {
      // 一字板: 整根量落进单一价箱
      const idx = Math.min(binCount - 1, Math.max(0, Math.floor((b.low - lo) / width)))
      acc[idx] += vol
      continue
    }
    // 把 [low, high] 与每个价箱的重叠长度按成交量均摊
    const span = b.high - b.low
    const first = Math.max(0, Math.floor((b.low - lo) / width))
    const last = Math.min(binCount - 1, Math.floor((b.high - lo) / width))
    for (let i = first; i <= last; i++) {
      const binLo = lo + i * width
      const binHi = binLo + width
      const overlap = Math.min(binHi, b.high) - Math.max(binLo, b.low)
      if (overlap > 0) acc[i] += (vol * overlap) / span
    }
  }

  const totalVolume = acc.reduce((s, v) => s + v, 0)
  if (totalVolume <= 0) return null

  // ── POC: 量最大的价箱 ──
  let pocIdx = 0
  for (let i = 1; i < binCount; i++) if (acc[i] > acc[pocIdx]) pocIdx = i

  // ── 价值区: 从 POC 向两侧逐箱扩展, 每次选相邻箱中量更大的一侧 ──
  const inVa = new Array<boolean>(binCount).fill(false)
  inVa[pocIdx] = true
  let vaSum = acc[pocIdx]
  const target = totalVolume * valueAreaPct
  let vaLo = pocIdx
  let vaHi = pocIdx
  while (vaSum < target && (vaLo > 0 || vaHi < binCount - 1)) {
    const leftVol = vaLo > 0 ? acc[vaLo - 1] : -1
    const rightVol = vaHi < binCount - 1 ? acc[vaHi + 1] : -1
    if (rightVol >= leftVol) {
      vaHi += 1
      vaSum += acc[vaHi]
    } else {
      vaLo -= 1
      vaSum += acc[vaLo]
    }
    for (let i = vaLo; i <= vaHi; i++) inVa[i] = true
  }

  const center = (i: number) => lo + (i + 0.5) * width
  const bins: VPBin[] = acc.map((v, i) => ({
    lo: lo + i * width,
    hi: lo + (i + 1) * width,
    mid: center(i),
    volume: v,
    pct: v / totalVolume,
    inValueArea: inVa[i],
  }))

  return {
    bins,
    poc: center(pocIdx),
    vah: bins[vaHi].hi,
    val: bins[vaLo].lo,
    totalVolume,
    bars: usable.length,
    from: usable[0].date,
    to: usable[usable.length - 1].date,
  }
}
