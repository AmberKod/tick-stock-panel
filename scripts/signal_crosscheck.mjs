/**
 * 回测用的信号发生器 vs 前端实际画图的信号 —— 逐条对拍。
 *
 * 为什么必须有这一验: scripts/signal_backtest.py 为了能在 Python 里跑全市场,
 * 把 frontend/src/lib/klineSignals.ts 的逻辑复刻了一份。抄一份逻辑就等于没验 ——
 * 一旦口径漂移, 证据卡统计的就不再是用户在图上看到的那个标注, 那些 p 值全是装饰品。
 *
 * 对拍方式:
 *   对每个交易日 i, 用 rows.slice(0, i+1) 跑**前端真实代码**
 *   (computeIndicators + buildSignalMarkers), 取当天那一根上的标注 —— 这正是用户
 *   当天打开页面会看到的。与 Python 版在同一根上生成的标注逐条比对。
 *
 * 跑法:
 *   unset CODEBUDDY_TOOL_CALL_ID
 *   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
 *     node --experimental-strip-types scripts/signal_crosscheck.mjs
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import {
  KLINE_SIGNALS, buildSignalMarkers,
} from '../frontend/src/lib/klineSignals.ts'
import { computeIndicators } from '../frontend/src/lib/klinePeriod.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
const BARS = resolve(HERE, '../../deliverables/jiuzhang-signal-probe/bars_crosscheck.json')
const MARKS_PY = resolve(HERE, '../../deliverables/jiuzhang-signal-probe/marks_py.json')

const ALL = new Set(KLINE_SIGNALS.map((s) => s.key))
const WARMUP = 70

const bars = JSON.parse(readFileSync(BARS, 'utf8'))
const py = JSON.parse(readFileSync(MARKS_PY, 'utf8'))

/** 一天的标注 → Set("key|dir") */
function tsMarksAt(rows, i) {
  const sub = rows.slice(0, i + 1)
  const withInd = computeIndicators(sub.map((r) => ({ ...r })))
  const markers = buildSignalMarkers(withInd, ALL)
  const mine = markers.filter((m) => m.date === rows[i].date)
  const out = new Set()
  for (const m of mine) {
    const detail = m.detail ?? [{ label: m.label, desc: m.desc ?? '' }]
    for (const d of detail) out.add(`${m.kind}|${d.label}`)
  }
  return out
}

// label -> key(反查, 与 klineSignals.ts 的 label 一一对应)
const LABEL_TO_KEY = {
  '量': 'volume', '高': 'newHigh', '低': 'newLow',
  '金': 'macd', '死': 'macd',
  'M金': 'maCross', 'M死': 'maCross',
  '季金': 'maGolden60', '季死': 'maGolden60',
  '穿': 'ma20Cross', '破': 'ma20Cross',
  '超卖': 'rsi', '超买': 'rsi',
  '轨↑': 'boll', '轨↓': 'boll',
}
const KIND_TO_DIR = { buy: 'buy', sell: 'sell', neutral: 'neutral' }

// key -> 判定用到的两条线(取 TS 侧该日与前一日的差值, 判断是否贴着浮点边界)
const KEY_TO_LINES = {
  macd: ['macd_dif', 'macd_dea'],
  maCross: ['ma5', 'ma20'],
  maGolden60: ['ma20', 'ma60'],
  ma20Cross: ['close', 'ma20'],
  boll: ['close', 'boll_upper'],
}

/** 不一致的地方是否只是浮点边界: 取前后两天两线之差的相对量级。 */
function isFloatBoundary(ind, i, key) {
  const lines = KEY_TO_LINES[key]
  if (!lines) return false
  const rel = (idx) => {
    const a = ind[idx]?.[lines[0]]
    const b = ind[idx]?.[lines[1]]
    if (a == null || b == null) return Infinity
    const scale = Math.max(Math.abs(a), Math.abs(b), 1)
    return Math.abs(a - b) / scale
  }
  // 1e-9 已经是 double 精度在这类价格量级上的噪声下限附近
  return Math.min(rel(i), rel(i - 1)) < 1e-9
}

let checked = 0
let mismatch = 0
let boundaryMismatch = 0
const samples = []
const hardSamples = []
const perKey = {}

for (const sym of Object.keys(py)) {
  const rows = bars[sym]
  if (!rows) { console.log(`FAIL | ${sym} 没有 K 线`); mismatch += 1; continue }
  // Python 版: {date, key, dir}[]
  const byDate = new Map()
  for (const e of py[sym]) {
    if (!byDate.has(e.date)) byDate.set(e.date, new Set())
    byDate.get(e.date).add(`${e.dir}|${e.key}`)
  }
  const withIndAll = computeIndicators(rows.map((r) => ({ ...r })))
  for (let i = WARMUP; i < rows.length; i += 1) {
    const date = rows[i].date
    const ts = tsMarksAt(rows, i)
    const pyset = byDate.get(date) ?? new Set()
    // TS 用 label, Python 用 key —— 统一到 "dir|key"
    const norm = new Set()
    for (const item of ts) {
      const [kind, label] = item.split('|')
      const key = LABEL_TO_KEY[label]
      if (!key) { console.log(`FAIL | 未知 label ${label}`); mismatch += 1; continue }
      norm.add(`${KIND_TO_DIR[kind]}|${key}`)
    }
    checked += 1
    for (const v of norm) {
      perKey[v.split('|')[1]] = (perKey[v.split('|')[1]] ?? 0) + 1
      if (!pyset.has(v)) {
        const boundary = isFloatBoundary(withIndAll, i, v.split('|')[1])
        if (boundary) boundaryMismatch += 1
        else if (hardSamples.length < 12) hardSamples.push(`${sym} ${date} TS有 ${v}`)
        if (samples.length < 6) samples.push(`${sym} ${date} TS有 ${v} ${boundary ? '(浮点边界)' : ''}`)
      }
    }
    for (const v of pyset) {
      perKey[v.split('|')[1]] = (perKey[v.split('|')[1]] ?? 0) + 1
      if (!norm.has(v)) {
        const boundary = isFloatBoundary(withIndAll, i, v.split('|')[1])
        if (boundary) boundaryMismatch += 1
        else if (hardSamples.length < 12) hardSamples.push(`${sym} ${date} PY有 ${v}`)
        if (samples.length < 6) samples.push(`${sym} ${date} PY有 ${v} ${boundary ? '(浮点边界)' : ''}`)
      }
    }
    if ([...norm].some((v) => !pyset.has(v)) || [...pyset].some((v) => !norm.has(v))) {
      mismatch += 1
    }
  }
}

// 判定: 全部不一致都必须是"两线贴近到 double 精度噪声内"的边界日。
// 例: 某日 ma5 与 ma20 差 8.9e-15, TS 判定上穿、pandas 因求和顺序差异判定未穿 →
// 信号掉到相邻日。这是 IEEE754 的锅, 不是口径漂移。
const hard = mismatch - boundaryMismatch
console.log(`\n==== 对拍 ${Object.keys(py).length} 只票 / ${checked} 个交易日 ====`)
console.log(`不一致日 ${mismatch} (${((1 - mismatch / checked) * 100).toFixed(3)}% 一致)`)
console.log(`  其中浮点边界(两线差 < 1e-9 相对): ${boundaryMismatch}`)
console.log(`  其中硬不一致(口径真漂移):        ${hard}`)
console.log('各 key 出现次数:', JSON.stringify(perKey))
if (samples.length) console.log('样例:\n  ' + samples.join('\n  '))
if (hard > 0) {
  console.log('\n❌ 存在非边界的不一致 —— 回测口径已经漂移,证据作废,请修 Python 版:')
  console.log('  ' + hardSamples.join('\n  '))
  process.exit(1)
}
const ratio = boundaryMismatch / checked
if (ratio > 0.005) {
  console.log(`\n❌ 边界噪声比例 ${(ratio * 100).toFixed(3)}% 超过 0.5% 阈值, 不像纯浮点边界`)
  process.exit(1)
}
console.log(`\n✅ 口径一致 — 残余差异 ${ratio.toFixed(5)}% 全部落在 double 精度边界`)
process.exit(0)
