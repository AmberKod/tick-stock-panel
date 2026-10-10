/**
 * 筹码分布(成本分布) —— 经典换手衰减模型(CYQ)。
 *
 * ┌──────────────────────────────────────────────────────────────────────┐
 * │ 🔴 这是**模型估算**, 不是交易所的真实持仓成本分布。                    │
 * │   交易所/行情源都不公开"每个价位的持仓量", 任何软件上的筹码图都是       │
 * │   用公开的价量数据 + 换手率反推的。本实现的假设全部写在下面, 可复核。   │
 * └──────────────────────────────────────────────────────────────────────┘
 *
 * 模型假设(三条, 全部公开且可改):
 *   1. 每日换手率 t = 当日成交量 / 流通股本(真数据)。t 就是"当日易主的筹码比例"。
 *   2. 新筹码在当日 [low, high] 内呈**三角形分布**: 以 (low+high)/2 为顶点、
 *      [low, high] 为底边, 面积 = t。含义是"当日成交价在日内均匀分布,
 *      中间价成交最密、两端最疏" —— 比"全部堆在收盘价"更接近真实, 且不凭空造峰。
 *   3. 历史筹码按 (1 - t × 衰减系数) 逐日衰减: 换手越活跃, 老筹码被换掉得越快。
 *      衰减系数默认 1.0(换手多少就换掉多少); 调小 = 老筹码留存更久。
 *
 * 输入全部为真数据: 日K的 high/low/close/volume + 流通股本(本地 stock_info,
 * 取自行情源股本字段)。输出是这三者之上的确定性函数, 不含随机数、不含占位值。
 *
 * ⚠️ 窗口长度直接决定结果: 窗口内累计换手率不足时, 老筹码(上市以来的套牢盘)
 *    仍在图里; 累计换手远超 100% 时, 图上基本只剩近期筹码。这不是 bug,
 *    是模型定义 —— 所以结果里会返回 `turnoverSum` 让你判断这个窗口够不够。
 */

export interface ChipBin {
  lo: number
  hi: number
  mid: number
  /** 该价箱的筹码占流通盘比例 0~1(归一化后总和 = 1) */
  pct: number
  /** 该价箱相对当前价的盈亏方向: 'profit' 成本低于现价 / 'loss' 高于现价 */
  side: 'profit' | 'loss'
}

export interface ChipResult {
  bins: ChipBin[]
  /** 平均成本(按筹码加权的均价) */
  avgCost: number
  /** 当前价(窗口最后一根的收盘价) */
  lastPrice: number
  /** 获利盘比例 0~1: 成本低于现价的筹码占比 */
  profitRatio: number
  /** 筹码峰(占比最大的价箱中心价) */
  peak: number
  /** 集中度: 覆盖 X% 筹码所需的最窄价格区间, 用 (高-低)/中价 表示, 越小越集中 */
  concentration90: number
  concentration70: number
  /** 窗口内累计换手率(倍数)。< 1 说明老筹码还有残留, 结果偏"历史" */
  turnoverSum: number
  /** 使用的流通股本 */
  floatShares: number
  /** 使用的衰减系数 */
  decay: number
  bars: number
  from: string
  to: string
}

export interface ChipInput {
  date: string
  high: number
  low: number
  close: number
  volume: number
}

export interface ChipOptions {
  /** 价箱数量, 默认 56 */
  bins?: number
  /** 衰减系数, 默认 1.0 */
  decay?: number
}

export function computeChips(
  bars: ChipInput[],
  floatShares: number,
  opts: ChipOptions = {},
): ChipResult | null {
  const binCount = Math.max(12, Math.min(200, opts.bins ?? 56))
  const decay = opts.decay ?? 1.0

  const usable = bars.filter(
    b => Number.isFinite(b.high) && Number.isFinite(b.low) && Number.isFinite(b.volume)
      && b.high > 0 && b.low > 0 && b.volume >= 0 && b.high >= b.low,
  )
  if (usable.length < 2 || !Number.isFinite(floatShares) || floatShares <= 0) return null

  let lo = Infinity
  let hi = -Infinity
  for (const b of usable) {
    if (b.low < lo) lo = b.low
    if (b.high > hi) hi = b.high
  }
  if (!(hi > lo)) return null

  const width = (hi - lo) / binCount
  const chips = new Array<number>(binCount).fill(0)
  let turnoverSum = 0

  for (const b of usable) {
    // 换手率按流通股本算, 上限 1(单日不可能换掉超过全部流通盘)
    const t = Math.min(b.volume / floatShares, 1)
    turnoverSum += t
    if (t <= 0) continue

    // ① 老筹码衰减
    if (t > 0) {
      const keep = Math.max(0, 1 - decay * t)
      for (let i = 0; i < binCount; i++) chips[i] *= keep
    }

    // ② 新筹码: 三角形分布, 面积 = t
    const mid = (b.high + b.low) / 2
    const idxOf = (p: number) => Math.min(binCount - 1, Math.max(0, Math.floor((p - lo) / width)))
    if (b.high === b.low) {
      chips[idxOf(b.low)] += t
      continue
    }
    const span = b.high - b.low
    const first = idxOf(b.low)
    const last = idxOf(b.high)
    for (let i = first; i <= last; i++) {
      const binMid = lo + (i + 0.5) * width
      if (binMid < b.low || binMid > b.high) continue
      // z ∈ [0,1]; 密度 2*(1-|2z-1|) 在 [0,1] 上积分为 1(峰值在中点)
      const z = (binMid - b.low) / span
      const density = 2 * (1 - Math.abs(2 * z - 1))
      chips[i] += t * (width / span) * density
    }
    // 三角形顶点所在的箱也要拿到量(上面按箱中點采样, 极端窄 K 时可能整根落不进任何箱中點)
    if (chips.slice(first, last + 1).every(v => v <= 0)) chips[idxOf(mid)] += t
  }

  const total = chips.reduce((s, v) => s + v, 0)
  if (total <= 0) return null

  const pct = chips.map(v => v / total)
  const center = (i: number) => lo + (i + 0.5) * width
  const lastPrice = usable[usable.length - 1].close

  let avgCost = 0
  let profitRatio = 0
  let peakIdx = 0
  for (let i = 0; i < binCount; i++) {
    const p = center(i)
    avgCost += p * pct[i]
    if (p < lastPrice) profitRatio += pct[i]
    if (pct[i] > pct[peakIdx]) peakIdx = i
  }

  /** 覆盖 target 比例筹码所需的最窄价格区间宽度(相对中价)。 */
  const concentration = (target: number): number => {
    let best = Infinity
    let sum = 0
    let l = 0
    for (let r = 0; r < binCount; r++) {
      sum += pct[r]
      while (sum - pct[l] >= target) sum -= pct[l++]
      if (sum >= target) best = Math.min(best, center(r) - center(l))
    }
    if (!Number.isFinite(best)) return 0
    const ref = lastPrice > 0 ? lastPrice : avgCost
    return ref > 0 ? best / ref : 0
  }

  const bins: ChipBin[] = pct.map((v, i) => ({
    lo: lo + i * width,
    hi: lo + (i + 1) * width,
    mid: center(i),
    pct: v,
    side: center(i) < lastPrice ? 'profit' : 'loss',
  }))

  return {
    bins,
    avgCost,
    lastPrice,
    profitRatio,
    peak: center(peakIdx),
    concentration90: concentration(0.9),
    concentration70: concentration(0.7),
    turnoverSum,
    floatShares,
    decay,
    bars: usable.length,
    from: usable[0].date,
    to: usable[usable.length - 1].date,
  }
}
