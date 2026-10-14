/**
 * 标注点 forward-return 统计 —— 回答"这些标注有没有信息量"。
 *
 * 不做这件事就去谈"要不要上火箭标注"是空谈: 一个标注点如果事后 N 日的收益分布
 * 和随机挑一天买入没有区别, 那把它画成 🚀 就是在凭空制造叙事。
 *
 * 方法(严格遵守 PIT 与撮合纪律):
 *   1. 逐日推进: 第 i 天只用 rows[:i+1] 的数据算标注 —— 全历史样本, 且天然 PIT。
 *   2. 撮合用 open_t+1: 信号在第 i 根**收盘**才能确认, 因此成交在第 i+1 根**开盘**。
 *      绝不用九章那种 same-bar fill(用含 close 的第 i 根判定、成交于第 i 根 open),
 *      实测那会让期末权益虚增 18.2%。
 *   3. 与"随机日买入"基准对照, 2000 次置换检验给 p 值 —— 样本小, 光看均值会自欺。
 *
 * 跑法:
 *   unset CODEBUDDY_TOOL_CALL_ID
 *   node --experimental-strip-types scripts/signal_forward_returns.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import { KLINE_SIGNALS, buildSignalMarkers } from '../frontend/src/lib/klineSignals.ts'
import { computeIndicators } from '../frontend/src/lib/klinePeriod.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const BARS = resolve(HERE, '../../deliverables/jiuzhang-signal-probe/bars.json')
const ALL = new Set(KLINE_SIGNALS.map(s => s.key))
const HOLDS = [5, 10, 20]
const WARMUP = 70          // 前 70 根指标还在收敛, 不参与统计
const PERM = 2000

/** 固定种子伪随机 —— 置换检验必须可复现。 */
function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
const rand = mulberry32(20261015)

const data = JSON.parse(readFileSync(BARS, 'utf8'))

/** 收集 (label -> 信号出现的索引列表), 逐日推进保证 PIT。 */
function collectMarks(rows) {
  const out = new Map()
  for (let i = WARMUP; i < rows.length; i += 1) {
    const sub = rows.slice(0, i + 1)
    const marks = buildSignalMarkers(sub, ALL)
    const today = sub[sub.length - 1].date
    for (const m of marks) {
      if (m.date !== today) continue      // 只看"今天新出现"的标注
      if (!out.has(m.label)) out.set(m.label, [])
      out.get(m.label).push(i)
    }
  }
  return out
}

/** 第 i 根收盘确认 → 第 i+1 根开盘买入 → 持有 n 根后收盘卖出。 */
function fwdReturn(rows, i, n) {
  const b = i + 1
  const s = i + 1 + n
  if (s >= rows.length) return null
  const buy = rows[b].open
  const sell = rows[s].close
  if (!(buy > 0) || !(sell > 0)) return null
  return sell / buy - 1
}

// ── 汇总 ────────────────────────────────────────────────────────────
/** 基准池: 所有可交易起点的收益(全样本, 不分信号)。 */
const basePool = new Map()   // hold -> number[]
const sigRets = new Map()    // label -> { hold -> number[] }

for (const [sym, raw] of Object.entries(data.symbols)) {
  const rows = computeIndicators(raw)
  if (!rows || rows.length < WARMUP + 30) continue

  for (const n of HOLDS) {
    if (!basePool.has(n)) basePool.set(n, [])
    for (let i = WARMUP; i < rows.length; i += 1) {
      const r = fwdReturn(rows, i, n)
      if (r != null) basePool.get(n).push(r)
    }
  }

  const marks = collectMarks(rows)
  for (const [label, idxs] of marks) {
    if (!sigRets.has(label)) {
      sigRets.set(label, new Map(HOLDS.map(n => [n, []])))
    }
    const per = sigRets.get(label)
    for (const i of idxs) {
      for (const n of HOLDS) {
        const r = fwdReturn(rows, i, n)
        if (r != null) per.get(n).push(r)
      }
    }
  }
}

const mean = a => a.reduce((s, x) => s + x, 0) / a.length
const median = a => { const b = [...a].sort((x, y) => x - y); const m = b.length >> 1
  return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2 }
const winRate = a => a.filter(x => x > 0).length / a.length * 100

/** 置换检验: 从基准池随机抽 n 个, 重复 PERM 次, 看实际均值落在什么分位。 */
function permTest(sample, pool) {
  if (sample.length < 8) return null
  const actual = mean(sample)
  let ge = 0, le = 0
  const n = sample.length
  for (let k = 0; k < PERM; k += 1) {
    let s = 0
    for (let j = 0; j < n; j += 1) s += pool[(rand() * pool.length) | 0]
    const m = s / n
    if (m >= actual) ge += 1
    if (m <= actual) le += 1
  }
  // 双尾 p: 取两侧较小者 ×2, 封顶 1
  return Math.min(1, 2 * Math.min(ge, le) / PERM)
}

// ── 输出 ────────────────────────────────────────────────────────────
const pct = v => `${(v * 100).toFixed(2)}%`
const pad = (s, n) => String(s).padEnd(n)
const padL = (s, n) => String(s).padStart(n)

console.log('=' .repeat(96))
console.log('标注点 forward-return 统计：信号后 N 日收益 vs 随机日买入基准')
console.log(`撮合: 第 i 根收盘确认 → 第 i+1 根开盘买入 → 持有 N 根后收盘卖出`)
console.log(`样本: ${Object.keys(data.symbols).length} 只票 × ${WARMUP}~320 根，逐日推进(PIT)，置换 ${PERM} 次`)
console.log('='.repeat(96))

for (const n of HOLDS) {
  const pool = basePool.get(n) || []
  const bm = mean(pool)
  console.log(`\n【持有 ${n} 个交易日】基准: 均值 ${pct(bm)}  中位 ${pct(median(pool))}  ` +
              `胜率 ${winRate(pool).toFixed(1)}%  样本 ${pool.length}`)
  console.log('-'.repeat(96))
  console.log(pad('标注', 8) + padL('样本', 6) + padL('均值', 10) + padL('中位', 10)
    + padL('胜率', 8) + padL('vs基准', 10) + padL('p值', 8) + '  判定')
  console.log('-'.repeat(96))

  const rowsOut = []
  for (const [label, per] of sigRets) {
    const arr = per.get(n) || []
    if (arr.length < 8) continue
    const m = mean(arr)
    const p = permTest(arr, pool)
    rowsOut.push({ label, n: arr.length, m, p, diff: m - bm, wr: winRate(arr) })
  }
  rowsOut.sort((a, b) => b.m - a.m)
  for (const r of rowsOut) {
    const sig = r.p != null && r.p < 0.05
    const verdict = r.p == null ? '样本不足'
      : sig ? (r.m > bm ? '✓ 显著跑赢' : '✓ 显著跑输')
      : '· 与随机无异'
    console.log(pad(r.label, 8) + padL(r.n, 6) + padL(pct(r.m), 10) + padL(pct(median(per2(sigRets, r.label, n))), 10)
      + padL(r.wr.toFixed(1) + '%', 8) + padL((r.diff > 0 ? '+' : '') + pct(r.diff), 10)
      + padL(r.p.toFixed(3), 8) + '  ' + verdict)
  }
}

function per2(map, label, n) {
  return (map.get(label)?.get(n)) || []
}

// ── 稳健性: 样本切成前半 / 后半, 看方向是否一致 ────────────────────
// 单一时段的显著可能是"这段行情刚好均值回归"。两段都同向才敢说信号本身有信息。
console.log('\n' + '='.repeat(96))
console.log('稳健性检验：样本按时间切前半 / 后半，看 10 日超额收益方向是否一致')
console.log('='.repeat(96))
console.log(pad('标注', 8) + padL('前半超额', 12) + padL('后半超额', 12)
  + padL('全样本超额', 12) + '  两半同向?')
console.log('-'.repeat(96))

const halfRets = new Map()   // label -> { first: [], second: [] }
for (const [sym, raw] of Object.entries(data.symbols)) {
  const rows = computeIndicators(raw)
  if (!rows || rows.length < WARMUP + 30) continue
  const mid = WARMUP + Math.floor((rows.length - WARMUP) / 2)
  const marks = collectMarks(rows)
  for (const [label, idxs] of marks) {
    if (!halfRets.has(label)) halfRets.set(label, { first: [], second: [] })
    const h = halfRets.get(label)
    for (const i of idxs) {
      const r = fwdReturn(rows, i, 10)
      if (r != null) h[i < mid ? 'first' : 'second'].push(r)
    }
  }
}
const pool10 = basePool.get(10) || []
const bm10 = mean(pool10)
const halfRows = []
for (const [label, h] of halfRets) {
  if (h.first.length < 8 || h.second.length < 8) continue
  const a = mean(h.first) - bm10
  const b = mean(h.second) - bm10
  halfRows.push({ label, a, b, all: mean([...h.first, ...h.second]) - bm10 })
}
halfRows.sort((x, y) => y.all - x.all)
for (const r of halfRows) {
  const same = (r.a > 0) === (r.b > 0)
  console.log(pad(r.label, 8) + padL((r.a > 0 ? '+' : '') + pct(r.a), 12)
    + padL((r.b > 0 ? '+' : '') + pct(r.b), 12)
    + padL((r.all > 0 ? '+' : '') + pct(r.all), 12)
    + '  ' + (same ? '✓ 同向' : '✗ 反转'))
}

console.log('\n' + '='.repeat(96))
console.log('怎么读这张表')
console.log('='.repeat(96))
console.log('· p 值是双尾置换检验: 「从同一批随机起点抽同样多的样本, 出现这么大偏离的概率」。')
console.log('· p < 0.05 且方向一致 → 这个标注点后面确实有可分辨的走势, 值得画。')
console.log('· p ≥ 0.05 → 它就是噪音。画成 🚀 只会让人以为看到了规律。')
console.log('· 局限: 12 只大盘蓝筹 / 单一段区间 / 未含交易成本 —— 只用于判断"有没有信息",')
console.log('  不足以支撑"能赚多少"。真要上评分, 需要多时段 + 多风格 + 扣费后的稳健性检验。')
