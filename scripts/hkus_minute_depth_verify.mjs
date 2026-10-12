/**
 * 浏览器验收 —— 港美股分钟K 的「深度」页签(1 日 原生蜡烛 / 3·5·10 日 聚合蜡烛)。
 *
 * 要锁的三件事:
 *  1. 分钟档打开后才会出现「1 日 / 3 日 / 5 日 / 10 日」这组深度页签;
 *  2. 切到 >1 日时, 图上必须写明"开/高/低由 1 分钟价格聚合"(不标 = 把聚合值
 *     当交易所原生 OHLC 卖);
 *  3. 切回 1 日时, 那句标注要消失(原生蜡烛不需要它)。
 */
import { chromium } from 'file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'

const BASE = process.env.BASE || 'http://127.0.0.1:3018'
const OUT = process.env.OUT || 'gui-test-screenshots'

let pass = 0, fail = 0
function check(name, ok, detail = '') {
  if (ok) { pass++; console.log(`PASS  ${name}${detail ? `  — ${detail}` : ''}`) }
  else { fail++; console.log(`FAIL  ${name}${detail ? `  — ${detail}` : ''}`) }
}

const browser = await chromium.launch({
  executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  headless: true,
})
const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
// index.html 引了 rsms.me 等外链字体, 本机取不到会拖死 goto —— 全部 abort
await ctx.route('**/*', route => {
  const u = route.request().url()
  if (u.startsWith('http://127.0.0.1') || u.startsWith('http://localhost')) return route.continue()
  return route.abort()
})
const page = await ctx.newPage()

// ── 1) 港股分析页: 分钟档 + 深度页签 ─────────────────────────────────────────
await page.goto(`${BASE}/hk/00700.HK`, { waitUntil: 'load', timeout: 120000 }).catch(async () => {
  // 路由可能不是 /hk/:symbol, 退回整页
  await page.goto(`${BASE}/stock/00700.HK`, { waitUntil: 'load', timeout: 120000 })
})
await page.waitForTimeout(5000)

const clickText = async (txt) => page.evaluate((t) => {
  const b = [...document.querySelectorAll('button')].find(e => e.textContent?.trim() === t)
  if (b) b.click()
  return !!b
}, txt)

// 打开分钟档(5 分)
const opened = await clickText('5分') || await clickText('5 分') || await clickText('m5')
check('分钟档可打开(5分)', opened)
await page.waitForTimeout(4000)

const depthTabs = await page.evaluate(() => {
  const txt = document.body.innerText
  // 别用 \b: JS 的 \w 不含中文, "3 日" 末尾的 \b 永远不成立
  return {
    has1: txt.includes('1 日'),
    has3: txt.includes('3 日'),
    has10: txt.includes('10 日'),
    depthNote: txt.includes('开/高/低由上游 1 分钟价格聚合') || txt.includes('聚合'),
  }
})
check('出现深度页签(1/3/10 日)', depthTabs.has1 && depthTabs.has3 && depthTabs.has10,
  `1日=${depthTabs.has1} 3日=${depthTabs.has3} 10日=${depthTabs.has10}`)

// 切到 3 日 → 应出现聚合标注
await clickText('3 日')
await page.waitForTimeout(5000)
const at3 = await page.evaluate(() => {
  const txt = document.body.innerText
  return {
    aggregateNote: /开\/高\/低由上游 1 分钟价格聚合/.test(txt)
      || /由上游 1 分钟价格聚合/.test(txt),
    countNote: /共 \d+ 根 \/ 3 个交易日/.exec(txt)?.[0] ?? '',
  }
})
check('3 日档标注"聚合蜡烛"', at3.aggregateNote, at3.countNote)

// 切回 1 日 → 聚合标注应消失
await clickText('1 日')
await page.waitForTimeout(5000)
const at1 = await page.evaluate(() => {
  const txt = document.body.innerText
  return {
    aggregateNote: /由上游 1 分钟价格聚合/.test(txt),
    capNote: /已取满上游单次上限/.test(txt),
  }
})
check('1 日档不再宣称聚合(原生蜡烛)', !at1.aggregateNote)
await page.screenshot({ path: `${OUT}/minute-depth-hk-1day.png`, timeout: 60000 }).catch(() => {})

await browser.close()
console.log(`\n=== ${pass}/${pass + fail} passed ===`)
process.exit(fail ? 1 : 0)
