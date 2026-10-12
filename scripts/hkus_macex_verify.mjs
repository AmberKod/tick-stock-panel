/**
 * 浏览器验收 —— 港美股 MAC-EX 四块新能力(快照 / 五档 / 板块 / 多日分时)。
 *
 * 用法: node scripts/hkus_macex_verify.mjs
 * 前置: 服务已在 3018 运行并已部署最新前端。
 *
 * 说明: 前端是 createBrowserRouter(不是 hash 路由), URL 用 /stock/xxx;
 * index.html 引了外链字体, 本机取不到会拖垮 goto, 所以非 127.0.0.1 的请求一律 abort。
 */
import { chromium } from 'file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const BASE = 'http://127.0.0.1:3018'
const OUT = 'E:/ai_codes/ai_personal_panel/tsp-fresh/gui-test-screenshots'

const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--no-proxy-server'] })
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1100 } })
await ctx.route(url => !url.href.startsWith('http://127.0.0.1'), route => route.abort())
const page = await ctx.newPage()

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`)
}

// ── 1) 港股整页: 快照 + 五档 + 所属板块 + 多日分时 ───────────────────────────
await page.goto(`${BASE}/stock/00700.HK`, { waitUntil: 'load', timeout: 120000 })
await page.waitForTimeout(6000)

const hk = await page.evaluate(() => {
  const txt = document.body.innerText
  const has = s => txt.includes(s)
  return {
    snapshot: has('实时行情 / 五档'),
    prevClose: /昨收[\s\S]{0,40}/.exec(txt)?.[0]?.replace(/\s+/g, ' ') ?? '',
    noDepthNote: has('不返回买卖档'),
    boards: has('所属板块 / 同业联动'),
    multiday: has('多日分时'),
    canvases: document.querySelectorAll('canvas').length,
    quoteLine: /MAC-EX · 通达信/.test(txt),
  }
})
check('00700 快照面板出现', hk.snapshot, hk.prevClose.slice(0, 40))
check('00700 港股无五档时写明原因', hk.noDepthNote)
check('00700 所属板块面板出现', hk.boards)
check('00700 多日分时面板出现', hk.multiday)
check('00700 分时图已渲染(canvas)', hk.canvases >= 1, `canvas=${hk.canvases}(主图为 SVG 渲染器)`)
await page.screenshot({ path: `${OUT}/macex-hk-00700.png`, timeout: 60000 }).catch(() => {})

// 点一个板块 chip, 看成分股表格是否出来
const membersOk = await page.evaluate(() => {
  const btns = Array.from(document.querySelectorAll('button'))
  const b = btns.find(x => /港股-/.test(x.innerText))
  if (b) b.click()
  return !!b
})
await page.waitForTimeout(4000)
const members = await page.evaluate(() => {
  const txt = document.body.innerText
  return { hasTable: txt.includes('量比') && txt.includes('市值'), sample: /量比[\s\S]{0,120}/.exec(txt)?.[0]?.replace(/\s+/g, ' ') ?? '' }
})
check('00700 点板块出成分股表', membersOk && members.hasTable, members.sample.slice(0, 60))

// ── 2) 美股整页: 真五档 ─────────────────────────────────────────────────────
await page.goto(`${BASE}/stock/AAPL.US`, { waitUntil: 'load', timeout: 120000 })
await page.waitForTimeout(6000)
const us = await page.evaluate(() => {
  const txt = document.body.innerText
  return {
    snapshot: txt.includes('实时行情 / 五档'),
    bidAsk: /卖1[\s\S]{0,60}/.exec(txt)?.[0]?.replace(/\s+/g, ' ') ?? '',
    boards: txt.includes('所属板块 / 同业联动'),
    multidayHidden: !txt.includes('多日分时'),
  }
})
check('AAPL 快照面板出现', us.snapshot)
check('AAPL 真五档渲染(卖1/买1)', /卖1/.test(us.bidAsk), us.bidAsk.slice(0, 50))
check('AAPL 所属板块面板出现', us.boards)
check('AAPL 不显示多日分时(上游不自洽)', us.multidayHidden)
await page.screenshot({ path: `${OUT}/macex-us-AAPL.png`, timeout: 60000 }).catch(() => {})

// ── 3) 板块榜页面 ────────────────────────────────────────────────────────────
await page.goto(`${BASE}/hkus-boards`, { waitUntil: 'load', timeout: 120000 })
await page.waitForTimeout(6000)
const boards = await page.evaluate(() => {
  const txt = document.body.innerText
  return {
    title: txt.includes('港美股板块 / 概念榜'),
    rows: (document.querySelectorAll('tbody tr').length),
    firstRow: /美股-|港股-/.exec(txt)?.[0] ?? '',
    leader: txt.includes('领涨股'),
  }
})
check('板块榜页面可达', boards.title)
check('板块榜有数据行', boards.rows > 3, `rows=${boards.rows} first=${boards.firstRow}`)
check('板块榜有领涨股列', boards.leader)
await page.screenshot({ path: `${OUT}/macex-boards-us.png`, timeout: 60000 }).catch(() => {})

// 切到港股板块
await page.evaluate(() => {
  const b = Array.from(document.querySelectorAll('button')).find(x => x.innerText.includes('港股·行业'))
  if (b) b.click()
})
await page.waitForTimeout(4000)
const hkBoards = await page.evaluate(() => {
  const txt = document.body.innerText
  return { hasHK: /港股-/.test(txt), rows: document.querySelectorAll('tbody tr').length }
})
check('切换到港股板块榜', hkBoards.hasHK, `rows=${hkBoards.rows}`)
await page.screenshot({ path: `${OUT}/macex-boards-hk.png`, timeout: 60000 }).catch(() => {})

await browser.close()

const failed = results.filter(r => !r.ok)
console.log(`\n=== ${results.length - failed.length}/${results.length} passed ===`)
if (failed.length) {
  console.log('failed:', failed.map(f => f.name).join(' | '))
  process.exit(1)
}
