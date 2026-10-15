/**
 * K线标注的 point-in-time 验证 —— 直接跑**前端真实代码**(node --experimental-strip-types),
 * 不是照抄一份逻辑来验。逻辑抄一份就等于没验。
 *
 * 验两件事:
 *   1. 每个标注在"当天"是否可见 —— 用 rows[:i+1] 重算, 该标注必须还在。
 *      这是九章 kline_signals 栽的地方(实测 0% 可自触发)。
 *   2. 日K / 周K / 月K 各自口径正确 —— 周K的 MA5 是 5 周均线, 不是日线 MA5 抽样。
 *
 * 另外跑一个**故意注入未来函数**的对照版: 验证器必须把它判成 0%, 否则说明
 * 验证器本身是恒真的, 前面那个 100% 毫无意义。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import {
  KLINE_SIGNALS, buildSignalMarkers,
} from '../frontend/src/lib/klineSignals.ts'
import { aggregateKline, computeIndicators } from '../frontend/src/lib/klinePeriod.ts'

const HERE = dirname(fileURLToPath(import.meta.url))
// 数据在仓库外的 deliverables/ 目录(调研产物, 不进主仓); 取不到时给出可操作的提示。
const BARS = resolve(HERE, '../../deliverables/jiuzhang-signal-probe/bars.json')

const ALL = new Set(KLINE_SIGNALS.map(s => s.key))

/** 用"截至标注当天"的数据重算, 检查该标注是否还在。 */
function pitCheck(rows, markers) {
  let total = 0
  let visible = 0
  const missing = []
  for (const m of markers) {
    const idx = rows.findIndex(r => r.date === m.date)
    if (idx < 0) continue
    const sub = rows.slice(0, idx + 1)
    const subMarks = buildSignalMarkers(sub, ALL)
    total += 1
    if (subMarks.some(x => x.date === m.date && x.label === m.label)) {
      visible += 1
    } else {
      missing.push(`${m.date} ${m.label}`)
    }
  }
  return { total, visible, missing }
}

/**
 * 对照版: 完全模仿九章的写法 —— 用第 i+1 根的 MACD 金叉确认, 却把标记画回第 i 根。
 * 这段代码不该进生产, 它唯一的用途是证明上面的验证器抓得住问题。
 */
function buildFutureLeakingMarkers(rows) {
  const marks = []
  const dif = rows.map(r => r.macd_dif ?? NaN)
  const dea = rows.map(r => r.macd_dea ?? NaN)
  for (let i = 0; i < rows.length - 1; i += 1) {
    if (Number.isFinite(dif[i]) && Number.isFinite(dif[i + 1])
        && Number.isFinite(dea[i]) && Number.isFinite(dea[i + 1])) {
      if (dif[i] <= dea[i] && dif[i + 1] > dea[i + 1]) {
        marks.push({ date: rows[i].date, kind: 'buy', label: '金', above: true })
      }
    }
  }
  return marks
}

const data = JSON.parse(readFileSync(BARS, 'utf8'))
const symbols = Object.keys(data.symbols)
console.log(`数据: ${data.source}`)
console.log(`样本: ${symbols.length} 只票\n`)

let pass = true
const summary = []

for (const period of ['day', 'week', 'month']) {
  let tot = 0
  let vis = 0
  const bad = []
  for (const sym of symbols) {
    const daily = data.symbols[sym]
    const rows = period === 'day'
      ? computeIndicators(daily)
      : aggregateKline(daily, period)
    const marks = buildSignalMarkers(rows, ALL)
    const r = pitCheck(rows, marks)
    tot += r.total
    vis += r.visible
    if (r.missing.length) bad.push(`${sym}: ${r.missing.slice(0, 3).join(', ')}`)
  }
  const pct = tot ? (vis / tot) * 100 : 100
  summary.push({ period, total: tot, visible: vis, pct })
  console.log(`[${period}K] 标注 ${tot} 个 → 当天可见 ${vis} (${pct.toFixed(1)}%)`)
  if (bad.length) {
    console.log(`   ✗ 不可见样例: ${bad.slice(0, 3).join(' | ')}`)
    pass = false
  }
}

// ── 对照: 注入未来函数后, 验证器必须报 0% ──────────────────────────────────
console.log('\n' + '-'.repeat(64))
let fTot = 0
let fVis = 0
for (const sym of symbols) {
  const rows = computeIndicators(data.symbols[sym])
  const marks = buildFutureLeakingMarkers(rows)
  const r = pitCheck(rows, marks)
  fTot += r.total
  fVis += r.visible
}
const fPct = fTot ? (fVis / fTot) * 100 : 0
console.log(`[对照·故意偷看 i+1] 标注 ${fTot} 个 → 当天可见 ${fVis} (${fPct.toFixed(1)}%)`)
console.log(fPct < 5
  ? '   ✓ 验证器抓得住 —— 上面那个 100% 是有意义的'
  : '   ✗ 验证器失效: 偷看未来居然也算"可见"')

// ── 周期语义: 周K 的 MA5 必须是"5 周均线", 不是日K MA5 按周抽样 ────────────
// 数量对比证明不了这件事(交叉次数跟序列长度有关), 只能直接比数值。
console.log('\n' + '-'.repeat(64))
let periodOk = true
for (const sym of symbols.slice(0, 4)) {
  const dRows = computeIndicators(data.symbols[sym])
  const wRows = aggregateKline(data.symbols[sym], 'week')
  const i = wRows.length - 1
  const wMa5 = wRows[i].ma5
  // 周K 自身最后 5 根收盘的均值 —— 这才是"5 周均线"的定义
  const expect = wRows.slice(i - 4, i + 1).reduce((s, b) => s + b.close, 0) / 5
  // 同一天(周K首日)在日K序列上的 ma5 —— 若沿用日K指标, wMa5 会等于它
  const dSameDay = dRows.find(r => r.date === wRows[i].date)
  const dMa5 = dSameDay ? dSameDay.ma5 : null
  const matchesWeekly = wMa5 != null && Math.abs(wMa5 - expect) < 1e-6
  // 若周K沿用了日K指标(抽样), 周K末根 ma5 会等于日K末根 ma5 —— 必须不等。
  // 不能拿"周K日期那天的日K ma5"比: 周K的 date 是周一, 常不是交易日, 查出来
  // 是 undefined, 那种"因为查不到而通过"是假验证。
  const dLastMa5 = dRows[dRows.length - 1].ma5
  const differsFromDaily = wMa5 != null && dLastMa5 != null
    && Math.abs(wMa5 - dLastMa5) / dLastMa5 > 1e-4
  const ok = matchesWeekly && differsFromDaily
  if (!ok) periodOk = false
  console.log(`${sym} 周K末根 ma5=${wMa5?.toFixed(3)} | 5周均值=${expect.toFixed(3)}`
    + ` | 日K末根 ma5=${dLastMa5?.toFixed(3)}  → ${ok ? '✓' : '✗'}`)
}
console.log(periodOk
  ? '   ✓ 周K 指标按周线序列重算, 未沿用日K值'
  : '   ✗ 周K 指标异常')

// ── 徽章布局: 同一根 K 线最多上下各一个, 不许叠成一坨 ──────────────────────
// 这是"美观"能不能站住的硬指标: 标注合并前, 928 个标记里有 169 个是同日同
// 方向重复的, 画出来就是文字压文字。合并后一天只剩 上/下 两个槽位。
console.log('\n' + '-'.repeat(64))
console.log('徽章布局')
console.log('-'.repeat(64))
let layoutOk = true
let totalMarks = 0, totalDetail = 0, maxPerSlot = 0, multiDay = 0
for (const sym of symbols) {
  const rows = computeIndicators(data.symbols[sym])
  const marks = buildSignalMarkers(rows, ALL)
  totalMarks += marks.length
  const slotCount = new Map()
  for (const m of marks) {
    const slot = `${m.date}|${m.kind}`
    // 一个槽位只允许一个徽章 —— 出现重复说明 mergeByDate 漏了
    slotCount.set(slot, (slotCount.get(slot) ?? 0) + 1)
    totalDetail += m.detail?.length ?? 1
    if ((m.count ?? 1) > 1) multiDay += 1
  }
  for (const c of slotCount.values()) maxPerSlot = Math.max(maxPerSlot, c)
}
const dedup = totalDetail - totalMarks
console.log(`徽章 ${totalMarks} 个, 合并掉 ${dedup} 条重复`
  + ` (${(dedup / Math.max(totalDetail, 1) * 100).toFixed(1)}%)`)
console.log(`同一槽位最多 ${maxPerSlot} 个徽章 (要求 = 1) → ${maxPerSlot === 1 ? '✓' : '✗'}`)
console.log(`带角标(同日≥2 条)的徽章 ${multiDay} 个 → ${multiDay > 0 ? '✓ 合并生效' : '✗ 没合并'}`)
if (maxPerSlot !== 1 || multiDay === 0) layoutOk = false

console.log('\n' + '='.repeat(64))
const allOk = pass && periodOk && layoutOk
  && summary.every(s => s.pct === 100) && fPct < 5
console.log(allOk ? 'PASS: 全部标注均为 point-in-time' : 'FAIL: 存在非 point-in-time 标注')
process.exit(allOk ? 0 : 1)
