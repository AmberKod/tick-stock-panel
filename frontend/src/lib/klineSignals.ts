/**
 * K线内标注 —— **全部是 OHLCV 的确定性派生**, 不是打分、不是预测信号。
 *
 * 为什么要写清楚这一句: 九章个股页的「K线内标注」里有一部分是模型/情绪分, 那类
 * 东西我方不做(三纪律: 不引未回测打分)。这里每一条标注都能写成一个可复核的公式,
 * 任何人拿同一串 K 线都能算出同样的结果 —— 它是**事实标记**, 不是"建议买卖"。
 *
 * 标注只描述"发生了什么"(放量 / 创阶段新高 / MACD 交叉), 不描述"该不该买"。
 *
 * 🔴 铁律: 判定**只能读第 i 根与第 i-1 根**, 一律不许读 i+1 及以后。
 *   这是九章 kline_signals 踩过的坑 —— 它用 dif[i+1]/dea[i+1] 的次日金叉确认,
 *   却把标记画回第 i 根, 结果信号画在阶段最低点上看着像神预言, 实盘当天根本
 *   看不到(实测 40 票 × 10400 次走查, point-in-time 可自触发率 0%)。
 *   详见 deliverables/jiuzhang-signal-probe/20261014-jiuzhang-kline-signal-调研.md。
 *
 * 周期无关: 入参是"当前周期那串 K"(日K / 周K / 月K / 分钟K), 指标由调用方算好 ——
 * 日K 用后端 enriched 的权威值, 周/月/年由 lib/klinePeriod 在新周期序列上重算。
 * 所以同一条"MA5 上穿 MA20"在周K上就是**5 周均线上穿 20 周均线**, 不是日线抽样。
 */
import type { OHLC } from '@/components/EChartsCandlestick'
import type { ChartMarker } from '@/components/EChartsCandlestick'

export type SignalKey =
  | 'volume'
  | 'newHigh'
  | 'newLow'
  | 'macd'
  | 'rsi'
  | 'boll'
  | 'maCross'
  | 'maGolden60'
  | 'ma20Cross'

/** 标注按钮按组排列 —— 9 个平铺会挤爆工具条。 */
export type SignalGroup = '量能' | '极值' | '均线' | '摆动'

export const SIGNAL_GROUPS: SignalGroup[] = ['量能', '极值', '均线', '摆动']

export interface SignalDef {
  key: SignalKey
  label: string
  group: SignalGroup
  /** 口径说明 —— 直接展示给用户, 标注是算出来的, 不是猜出来的。 */
  desc: string
}

export const KLINE_SIGNALS: SignalDef[] = [
  {
    key: 'volume', group: '量能', label: '放量',
    desc: '成交量 ≥ 前 20 根均量 × 2。只说明成交突然活跃, 不说明方向',
  },
  {
    key: 'newHigh', group: '极值', label: '新高',
    desc: '收盘价创近 60 根新高',
  },
  {
    key: 'newLow', group: '极值', label: '新低',
    desc: '收盘价创近 60 根新低',
  },
  {
    key: 'macd', group: '摆动', label: 'MACD',
    desc: 'DIF 上穿 DEA(金叉) / 下穿(死叉)。按当前周期收盘价算, 周K就是周线 MACD',
  },
  {
    key: 'maCross', group: '均线', label: 'MA5/20',
    desc: 'MA5 上穿 MA20(金叉) / 下穿(死叉)',
  },
  {
    key: 'maGolden60', group: '均线', label: 'MA20/60',
    desc: 'MA20 上穿 MA60(金叉) / 下穿(死叉)。中期趋势转向, 比 MA5/20 稀少',
  },
  {
    key: 'ma20Cross', group: '均线', label: '穿MA20',
    desc: '收盘价上穿 MA20 / 跌破 MA20',
  },
  {
    key: 'rsi', group: '摆动', label: 'RSI',
    desc: 'RSI14 < 30(超卖, 红) / > 70(超买, 绿)。只说明涨跌速度与幅度, 超卖不等于该买',
  },
  {
    key: 'boll', group: '摆动', label: '布林',
    desc: '收盘价突破布林上轨 / 跌破下轨(MA20 ± 2σ)',
  },
]

/**
 * 默认开启的四个 —— 覆盖"量 / 价 / 位置 / 摆动"四个维度各一种, 图上不至于糊。
 * 其余全部默认关, 用户按需勾选(偏好存 localStorage)。
 */
export const DEFAULT_SIGNALS: SignalKey[] = ['volume', 'newHigh', 'newLow', 'macd']

/** 标注太多会把 K 线糊住 —— 只在最近 N 根里打标。 */
const RECENT_WINDOW = 120

/** 新高/新低的回看窗口(根)。与后端 high_60d/low_60d 同为 60。 */
const EXTREME_WINDOW = 60

const C_UP = '#C74040'      // 金叉 / 上穿 / 新高 / 超卖 —— 低位转强方向
const C_DOWN = '#2D9B65'    // 死叉 / 跌破 / 新低 / 超买 —— 高位转弱方向
const C_VOL = '#38BDF8'     // 放量 —— 本身不带方向

/**
 * 指标值规范化。后端列与前端重算列都可能是 null(数据不足/周期太短),
 * 遇到 null 一律**不标** —— 宁可漏标也不拿不完整的数据凑一个标记出来。
 */
function num(v: number | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * 上穿 / 下穿。前一根缺失时返回 false —— 不知道前值就无法判定"穿越",
 * 这种情况下宁可不标(否则新股第一根就会被标成金叉)。
 */
function crossed(
  cur: number | null, ref: number | null,
  prevCur: number | null, prevRef: number | null,
): 'up' | 'down' | null {
  if (cur == null || ref == null || prevCur == null || prevRef == null) return null
  if (prevCur <= prevRef && cur > ref) return 'up'
  if (prevCur >= prevRef && cur < ref) return 'down'
  return null
}

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
 * @param rows 已按时间升序、且指标已算好的 K 线(日K / 周K / 月K / 分钟K 都行)
 * @param enabled 用户勾选的标注类型
 */
/**
 * 标注「强度」= 超出基准的相对幅度(%), 与回测脚本 scripts/signal_backtest.py
 * 同一公式 —— 两边必须逐条对齐, 否则回测出来的强度分层根本画不到对应的标注上。
 * 分母非法返回 undefined(该标注就没有强度档, 按常规大小画)。
 */
function pct(delta: number, base: number | null): number | undefined {
  if (base == null || !base || !Number.isFinite(base) || !Number.isFinite(delta)) return undefined
  return (delta / base) * 100
}

/** 生成阶段用的内部标记 —— 带 key，合并后 key 收进 detail。 */
type RawMark = ChartMarker & { key: SignalKey; strength?: number }

export function buildSignalMarkers(
  rows: OHLC[],
  enabled: ReadonlySet<string>,
  tiers?: ReadonlyMap<SignalKey, { q50: number; q75: number }>,
): ChartMarker[] {
  if (rows.length === 0 || enabled.size === 0) return []

  const marks: RawMark[] = []
  const from = Math.max(0, rows.length - RECENT_WINDOW)

  const volumes = rows.map(r => r.volume ?? 0)
  const closes = rows.map(r => r.close)

  for (let i = from; i < rows.length; i += 1) {
    const r = rows[i]
    const prev = i > 0 ? rows[i - 1] : null
    const date = r.date

    // ── 量能 ──
    if (enabled.has('volume')) {
      const base = sma(volumes, 20, i)
      if (base && base > 0 && (r.volume ?? 0) >= base * 2) {
        marks.push({ date, kind: 'neutral', label: '量', color: C_VOL, key: 'volume',
          strength: ((r.volume ?? 0) / base) * 100 })
      }
    }

    // ── 极值(近 60 根) ──
    if (enabled.has('newHigh') || enabled.has('newLow')) {
      const win = closes.slice(Math.max(0, i - EXTREME_WINDOW), i)
      // 回看窗口太短就别标: 前 20 根里随便一个价都可能"创新高", 没有信息量
      if (win.length >= 20) {
        const hi = Math.max(...win)
        const lo = Math.min(...win)
        if (enabled.has('newHigh') && r.close > hi) {
          marks.push({ date, kind: 'buy', label: '高', color: C_UP, key: 'newHigh',
            strength: pct(r.close - hi, hi) })
        } else if (enabled.has('newLow') && r.close < lo) {
          marks.push({ date, kind: 'sell', label: '低', color: C_DOWN, key: 'newLow',
            strength: pct(lo - r.close, r.close) })
        }
      }
    }

    // ── MACD 交叉 ──
    if (enabled.has('macd')) {
      const d = crossed(num(r.macd_dif), num(r.macd_dea), num(prev?.macd_dif), num(prev?.macd_dea))
      const macdStrength = pct(Math.abs((num(r.macd_dif) ?? 0) - (num(r.macd_dea) ?? 0)), r.close)
      if (d === 'up') marks.push({ date, kind: 'buy', label: '金', color: C_UP, key: 'macd', strength: macdStrength })
      else if (d === 'down') marks.push({ date, kind: 'sell', label: '死', color: C_DOWN, key: 'macd', strength: macdStrength })
    }

    // ── MA5/MA20 交叉 ──
    if (enabled.has('maCross')) {
      const d = crossed(num(r.ma5), num(r.ma20), num(prev?.ma5), num(prev?.ma20))
      const maCrossStrength = pct(Math.abs((num(r.ma5) ?? 0) - (num(r.ma20) ?? 0)), num(r.ma20))
      if (d === 'up') marks.push({ date, kind: 'buy', label: 'M金', color: C_UP, key: 'maCross', strength: maCrossStrength })
      else if (d === 'down') marks.push({ date, kind: 'sell', label: 'M死', color: C_DOWN, key: 'maCross', strength: maCrossStrength })
    }

    // ── MA20/MA60 交叉 ──
    if (enabled.has('maGolden60')) {
      const d = crossed(num(r.ma20), num(r.ma60), num(prev?.ma20), num(prev?.ma60))
      const goldenStrength = pct(Math.abs((num(r.ma20) ?? 0) - (num(r.ma60) ?? 0)), num(r.ma60))
      if (d === 'up') marks.push({ date, kind: 'buy', label: '季金', color: C_UP, key: 'maGolden60', strength: goldenStrength })
      else if (d === 'down') marks.push({ date, kind: 'sell', label: '季死', color: C_DOWN, key: 'maGolden60', strength: goldenStrength })
    }

    // ── 收盘价穿越 MA20 ──
    if (enabled.has('ma20Cross')) {
      const d = crossed(num(r.close), num(r.ma20), num(prev?.close), num(prev?.ma20))
      const ma20Strength = pct(Math.abs(r.close - (num(r.ma20) ?? 0)), num(r.ma20))
      if (d === 'up') marks.push({ date, kind: 'buy', label: '穿', color: C_UP, key: 'ma20Cross', strength: ma20Strength })
      else if (d === 'down') marks.push({ date, kind: 'sell', label: '破', color: C_DOWN, key: 'ma20Cross', strength: ma20Strength })
    }

    // ── RSI 超买/超卖 ──
    if (enabled.has('rsi')) {
      const v = num(r.rsi_14)
      if (v != null && v < 30) {
        marks.push({ date, kind: 'buy', label: '超卖', color: C_UP, key: 'rsi', strength: Math.abs(v - 50) })
      } else if (v != null && v > 70) {
        marks.push({ date, kind: 'sell', label: '超买', color: C_DOWN, key: 'rsi', strength: Math.abs(v - 50) })
      }
    }

    // ── 布林上下轨 ──
    if (enabled.has('boll')) {
      const hi = num(r.boll_upper)
      const lo = num(r.boll_lower)
      const bw = hi != null && lo != null ? hi - lo : 0
      if (hi != null && r.close > hi) {
        marks.push({ date, kind: 'buy', label: '轨↑', color: C_UP, key: 'boll', strength: pct(r.close - hi, bw) })
      } else if (lo != null && r.close < lo) {
        marks.push({ date, kind: 'sell', label: '轨↓', color: C_DOWN, key: 'boll', strength: pct(lo - r.close, bw) })
      }
    }
  }

  // 强度分档必须在合并**之前**逐条打: 合并后一个徽章里可能混着不同 key 的信号,
  // 各自的阈值不同, 只有先各自归一成档才可比。合并时取最强的那一档。
  if (tiers && tiers.size > 0) {
    for (const m of marks) m.tier = tierOf(m.strength, tiers.get(m.key))
  }
  return mergeByDate(marks)
}

/** 强度 → 档位。没有强度值或该信号没有可信阈值 → 不分档(按常规大小画)。 */
function tierOf(
  strength: number | undefined,
  th: { q50: number; q75: number } | undefined,
): 1 | 2 | 3 | undefined {
  if (strength == null || !th || !Number.isFinite(strength)) return undefined
  if (strength >= th.q75) return 3
  if (strength >= th.q50) return 2
  return 1
}

/**
 * 从回测证据表取每个信号的强度分档阈值(全市场分位数)。
 *
 * 只有 trustworthy(训练/测试两段都单调 **且** 幅度保留率 ≥ 0.5) 的信号才给阈值。
 * 光方向单调不够: rsi 做空腿训练段最强档 +1.86%、测试段只剩 +0.21%, 单调是真的,
 * 但按训练段幅度把箭头画大就是拿过拟合的数字当信息。
 */
export function strengthTiersFromStats(
  stats?: Record<string, {
    best_leg?: string
    strength?: Record<string, {
      q50?: number; q75?: number; monotone?: boolean; trustworthy?: boolean
    } | null>
  }>,
): Map<SignalKey, { q50: number; q75: number }> {
  const out = new Map<SignalKey, { q50: number; q75: number }>()
  if (!stats) return out
  for (const [key, v] of Object.entries(stats)) {
    const st = v?.strength?.[v.best_leg ?? 'long']
    if (st?.trustworthy && st.q50 != null && st.q75 != null) {
      out.set(key as SignalKey, { q50: st.q50, q75: st.q75 })
    }
  }
  return out
}

/**
 * 同一根 K 线上经常同时命中好几条信号(比如放量 + MACD 金叉)。
 * 逐条画会把徽章叠成一坨, 所以按「日期 + 方向」合并成一个:
 *   - 看涨类全部收进下方那支朝上的箭头
 *   - 看跌类全部收进上方那支朝下的箭头
 *   - 中性(放量)收进上方的圆点
 * 于是**一天最多两个徽章**, 上下各一个, 永远不会互相压住。
 * 合并掉的明细放进 detail, hover 时展开 —— 信息一条没少, 只是不挤在一起。
 */
function mergeByDate(marks: RawMark[]): ChartMarker[] {
  if (marks.length === 0) return []

  const slots = new Map<string, RawMark[]>()
  for (const m of marks) {
    const slot = `${m.date}|${m.kind}`
    const arr = slots.get(slot)
    if (arr) arr.push(m)
    else slots.set(slot, [m])
  }

  const out: ChartMarker[] = []
  for (const arr of slots.values()) {
    const head = arr[0]
    out.push({
      date: head.date,
      kind: head.kind,
      // 单条就写本名(金/死/量); 多条写"金+2" —— 数字也在角标里, 这里只是防歧义
      label: arr.length === 1 ? head.label : `${head.label ?? ''}+${arr.length - 1}`,
      color: head.color,
      count: arr.length,
      // 多条合并时画最强的那一档 —— 取 max, 不是取第一条
      tier: arr.reduce<1 | 2 | 3 | undefined>(
        (acc, m) => (m.tier != null && (acc == null || m.tier > acc) ? m.tier : acc),
        undefined,
      ),
      detail: arr.map(m => ({
        key: m.key,
        label: m.label ?? '',
        desc: KLINE_SIGNALS.find(s => s.key === m.key)?.desc,
        // 强度值带出来, 供 hover 展示与回测对拍(证明阈值画在了对的标注上)
        strength: m.strength,
      })),
    })
  }
  return out
}
