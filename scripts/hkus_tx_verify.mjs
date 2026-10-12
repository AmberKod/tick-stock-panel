/**
 * 浏览器验收 —— 港美股逐笔成交面板(MAC-EX 0x23FC/0x2406、0x122F)。
 *
 * 用法: node scripts/hkus_tx_verify.mjs
 * 前置: 服务已在 3018 运行并已部署最新前端。
 *
 * 断言的是**语义**而不只是"有渲染": 港股必须不出现买卖方向(上游 nature 未实测),
 * 美股必须出现方向列; 两边都得把价格单位写在面板上。
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

async function inspect(symbol, label) {
  await page.goto(`${BASE}/stock/${symbol}`, { waitUntil: 'load', timeout: 120000 })
  await page.waitForTimeout(8000)
  const info = await page.evaluate(() => {
    const txt = document.body.innerText
    // 逐笔面板的 section: 标题 + 表头行
    const heads = Array.from(document.querySelectorAll('section'))
      .filter(s => s.innerText.includes('逐笔成交'))
    const sec = heads[0]
    const rows = sec ? sec.querySelectorAll('tbody tr').length : 0
    const secText = sec ? sec.innerText : ''
    return {
      panel: !!sec,
      rows,
      protocol: /0x23FC|0x122F/.exec(secText)?.[0] ?? '',
      priceUnit: /HKD|USD/.test(secText),
      // 只看表头 th —— 说明文案里也含"方向"两字, 用文本判定会假阳性
      hasDirectionHeader: sec
        ? Array.from(sec.querySelectorAll('th')).some(th => th.innerText.trim() === '方向')
        : false,
      noDirectionNote: secText.includes('不标方向'),
      firstRow: secText.split('\n').filter(Boolean).slice(2, 5).join(' | '),
    }
  })
  check(`${label} 逐笔面板出现`, info.panel)
  check(`${label} 有逐笔明细行`, info.rows > 0, `rows=${info.rows}`)
  check(`${label} 面板标注协议`, !!info.protocol, info.protocol)
  check(`${label} 面板标注价格单位`, info.priceUnit)
  return info
}

const hk = await inspect('00700.HK', '00700.HK')
check('00700 港股不标买卖方向(上游未实测)', hk.noDirectionNote && !hk.hasDirectionHeader,
  `方向列表头=${hk.hasDirectionHeader}`)
await page.screenshot({ path: `${OUT}/tx-hk-00700.png`, timeout: 60000 }).catch(() => {})

const us = await inspect('AAPL.US', 'AAPL.US')
check('AAPL 美股标买卖方向(bs_flag 有协议口径)', us.hasDirectionHeader, us.firstRow)
await page.screenshot({ path: `${OUT}/tx-us-aapl.png`, timeout: 60000 }).catch(() => {})

await browser.close()

const failed = results.filter(r => !r.ok)
console.log(`\n=== ${results.length - failed.length}/${results.length} passed ===`)
if (failed.length) {
  console.log('failed:', failed.map(f => f.name).join(' | '))
  process.exit(1)
}
