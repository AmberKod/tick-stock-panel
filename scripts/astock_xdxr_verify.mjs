/**
 * 浏览器验收 —— A股「分红送配」页签(通达信标准市 GetXdxrInfoCmd 全事件)。
 *
 * 用法: node scripts/astock_xdxr_verify.mjs
 * 前置: 服务已在 3018 运行并已部署最新前端。
 *
 * 断言的是**语义**而不是"有渲染":
 *   1. 页签能打开, 且数字来自真源(source=tdx-xdxr);
 *   2. 年度派息条与事件表都渲染出**具体金额**(不是占位横线);
 *   3. 偏差说明必须摆在页面上 —— 有人会拿这些事件自己去推复权价,
 *      不写清"回购注销股有 0.1%~0.5% 偏差"等于害人;
 *   4. 单位说明必须在(每股 / 万股 两套口径混在一个列表里)。
 */
import { chromium } from 'file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const BASE = 'http://127.0.0.1:3018'
const OUT = 'E:/ai_codes/ai_personal_panel/tsp-fresh/gui-test-screenshots'

const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--no-proxy-server'] })
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1100 } })
// index.html 引了外链字体, 取不到会拖到超时 —— 非本机请求一律 abort
await ctx.route(url => !url.href.startsWith('http://127.0.0.1'), route => route.abort())
const page = await ctx.newPage()

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`)
}

async function clickText(text) {
  await page.evaluate((t) => {
    const el = Array.from(document.querySelectorAll('button'))
      .find(b => b.innerText.trim() === t)
    if (el) el.click()
  }, text)
  await page.waitForTimeout(6000)
}

async function inspect(symbol, label, expectCash) {
  await page.goto(`${BASE}/stock/${symbol}`, { waitUntil: 'load', timeout: 120000 })
  await page.waitForTimeout(8000)
  const hasTab = await page.evaluate(() =>
    Array.from(document.querySelectorAll('button')).some(b => b.innerText.trim() === '分红送配'))
  check(`${label} 深度页签含「分红送配」`, hasTab)
  if (!hasTab) return

  await clickText('分红送配')

  const info = await page.evaluate(() => {
    // ⚠️ 只认 <section>: PanelShell 渲染的是 section, 若把 div 也算进来会抓到外层大容器,
    //    把别的面板的"不可用"一起算成这个面板的 —— 曾因此误报失败。
    const sections = Array.from(document.querySelectorAll('section'))
    const sec = sections.find(s => s.querySelector('h3')?.innerText.trim() === '分红送配')
    const txt = sec ? sec.innerText : document.body.innerText
    const rows = sec ? Array.from(sec.querySelectorAll('tbody tr')).map(tr => tr.innerText.replace(/\s+/g, ' ').trim()) : []
    return {
      text: txt,
      rows,
      yearly: /(\d{4})\s+[\d.]+\s+元\/股/.test(txt),
      cashCount: /上市以来现金分红\s*\d+\s*次/.exec(txt)?.[0] ?? '',
      unitNote: /每股/.test(txt) && /万股/.test(txt),
      deviation: /0\.467%/.test(txt) && /回购/.test(txt),
      source: /GetXdxrInfoCmd/.test(txt),
      unavailable: /不可用/.test(txt),
    }
  })

  check(`${label} 事件表有数据行`, info.rows.length > 0, `${info.rows.length} 行`)
  check(`${label} 年度派息条渲染出金额`, info.yearly, info.cashCount)
  check(`${label} 每股/万股单位说明在位`, info.unitNote)
  check(`${label} 偏差边界写在页面上(回购注销)`, info.deviation)
  check(`${label} 标注真源 GetXdxrInfoCmd`, info.source)

  if (expectCash) {
    // 事件表里至少一行有真实派息金额, 不能全是占位横线
    const withAmount = info.rows.filter(r => /\d+\.\d{4}/.test(r))
    check(`${label} 事件含每股派息数值`, withAmount.length > 0,
      withAmount.length ? withAmount[0].slice(0, 60) : info.rows[0]?.slice(0, 60) ?? '')
  }
  check(`${label} 未显示"不可用"`, !info.unavailable)

  await page.screenshot({ path: `${OUT}/astock-xdxr-${symbol}.png`, fullPage: false })
}

await inspect('600519', '茅台', true)
await inspect('300750', '宁德', true)

await browser.close()

const pass = results.filter(r => r.ok).length
console.log(`\n=== ${pass}/${results.length} passed ===`)
process.exit(pass === results.length ? 0 : 1)
