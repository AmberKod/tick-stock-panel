/**
 * 浏览器验收 —— A股「成交明细」升级成**真逐笔**(通达信标准市 0x0fb5)。
 *
 * 用法(服务已在 3018 跑、前端已部署):
 *   unset CODEBUDDY_TOOL_CALL_ID
 *   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
 *     node scripts/astock_tick_verify.mjs
 *
 * 断言的是**语义**而不是"有渲染":
 *   1. 面板必须自称"真逐笔 · 一行一笔", 且页面上再不许出现旧口径的词
 *      ("3 秒快照" / "Level-2") —— 出现过就是还有人在卖 3 秒快照;
 *   2. 表格里每行是**一笔**: 时间 HH:MM、手数是整数、方向 ∈ 买/卖/竞价/—;
 *   3. 校验徽章必须是绿色"收盘价咬合" —— 这是这次替换的硬承诺,
 *      黄色/红色/缺失都要当成失败;
 *   4. 清洗说明必须写明剔除了多少行 vol=0 标记行;
 *   5. 日期下拉要有多个交易日可选(不能让人选到非交易日上再怪数据)。
 */
import { chromium } from 'file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'
import { mkdirSync } from 'node:fs'

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const BASE = 'http://127.0.0.1:3018'
const OUT = 'E:/ai_codes/ai_personal_panel/tsp-fresh/gui-test-screenshots'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--no-proxy-server'] })
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1200 } })
// index.html 引了外链字体, 取不到会拖到超时 —— 非本机请求一律 abort
await ctx.route(url => !url.href.startsWith('http://127.0.0.1'), route => route.abort())
const page = await ctx.newPage()

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok, detail })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`)
}

async function openPanel(symbol) {
  await page.goto(`${BASE}/stock/${symbol}`, { waitUntil: 'load', timeout: 120000 })
  await page.waitForTimeout(8000)
  const clicked = await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll('button'))
      .find(b => b.innerText.trim().startsWith('成交明细'))
    if (!el) return false
    el.click()
    return true
  })
  await page.waitForTimeout(6000)
  return clicked
}

async function inspect(symbol, label) {
  const clicked = await openPanel(symbol)
  check(`${label} 工具条含「成交明细」开关`, clicked)
  if (!clicked) return

  const info = await page.evaluate(() => {
    // ⚠️ 只认 <section>: PanelShell 渲染 section; 把 div 也算进来会抓到外层大容器,
    //    把别的面板的"不可用"文字当成这一个面板的 —— 曾因此误报失败。
    const sections = Array.from(document.querySelectorAll('section'))
    const sec = sections.find(s => s.innerText.includes('真逐笔 · 一行一笔'))
      ?? sections.find(s => s.innerText.includes('成交明细') && s.querySelector('table'))
    if (!sec) return { found: false, pageText: document.body.innerText }

    const rows = Array.from(sec.querySelectorAll('tbody tr')).map(tr => {
      const td = tr.querySelectorAll('td')
      return Array.from(td).map(x => x.innerText.trim())
    })
    const selects = Array.from(sec.querySelectorAll('select')).map(s => ({
      value: s.value,
      options: Array.from(s.options).map(o => o.value),
    }))
    const badges = Array.from(sec.querySelectorAll('span'))
      .map(s => s.innerText.trim()).filter(Boolean)
    return {
      found: true,
      text: sec.innerText,
      rows: rows.filter(r => r.length >= 4),
      selects,
      badges,
      tableHeaders: Array.from(sec.querySelectorAll('thead th')).map(th => th.innerText.trim()),
    }
  })

  if (!info.found) {
    check(`${label} 逐笔面板渲染`, false, '页面上找不到逐笔面板')
    if (info.pageText) {
      check(`${label} 失败排查: 页面无旧口径残留`,
        !info.pageText.includes('3 秒快照'),
        info.pageText.slice(0, 300))
    }
    return
  }

  check(`${label} 面板自称「真逐笔 · 一行一笔」`, info.text.includes('真逐笔 · 一行一笔'))
  check(`${label} 页面上不再有旧口径(3 秒快照 / Level-2)`,
    !info.text.includes('3 秒快照') && !info.text.includes('Level-2'),
    info.text.includes('Level-2') ? '还写着"逐笔需 Level-2"' : '')

  const timeOk = info.rows.length >= 5
    && info.rows.every(r => /^\d{2}:\d{2}([开收盘后]+)?$/.test(r[0].replace(/\s+/g, '')))
  check(`${label} 至少 5 行且时间列是 HH:MM`, timeOk, `行数=${info.rows.length} 首行=${info.rows[0]?.[0]}`)

  const lotOk = info.rows.every(r => /^[1-9]\d*$/.test((r[2] ?? '').replace(/,/g, '')))
  check(`${label} 手数列都是正整数(真成交)`, lotOk, `样例=${info.rows.slice(0, 3).map(r => r[2]).join('/')}`)

  const sideOk = info.rows.every(r => ['买', '卖', '竞价', '—'].includes(r[4]))
  check(`${label} 方向列 ∈ 买/卖/竞价/—`, sideOk,
    `取值=${Array.from(new Set(info.rows.map(r => r[4]))).join('/')}`)

  check(`${label} 表头含「手数」「成交额」`,
    info.tableHeaders.includes('手数') && info.tableHeaders.includes('成交额'),
    info.tableHeaders.join('|'))

  // 校验徽章 —— 这条是硬承诺, 不许黄/红/缺
  const badgeOk = info.badges.some(b => b.includes('收盘价咬合'))
  const badBadge = info.badges.find(b => b.includes('校验未通过'))
  check(`${label} 校验徽章为绿色「收盘价咬合」`, badgeOk && !badBadge, badBadge ?? '')

  // 清洗说明: 不能把"剔除了多少标记行"藏起来
  const cleaningOk = /上游夹了\s+\d+\s+行/.test(info.text)
  check(`${label} 写明剔除了多少 vol=0 标记行`, cleaningOk,
    (info.text.match(/上游夹了\s+\d+\s+行[^\n]{0,60}/) ?? [''])[0])

  // 日期下拉 —— 不能让人选到非交易日
  const dateSel = info.selects.find(s => s.options.some(o => /^\d{4}-\d{2}-\d{2}$/.test(o)))
  check(`${label} 日期下拉给出多个交易日`,
    Boolean(dateSel) && dateSel.options.length >= 5,
    dateSel ? `${dateSel.options.length} 个: ${dateSel.options.slice(0, 3).join(',')}…` : '没有日期下拉')

  check(`${label} 汇总含「成交量(手)」且非零`,
    /\b成交量\(手\)\s*\n?\s*\S+/.test(info.text) && !/成交量\(手\)\s*\n\s*[-—]/.test(info.text),
    (info.text.match(/成交量\(手\)\s*\n\s*(\S+)/) ?? [''])[1])

  await page.screenshot({ path: `${OUT}/astock-tick-${symbol}.png`, fullPage: false })
}

try {
  await inspect('600519', '贵州茅台')
  await inspect('300750', '宁德时代')
} finally {
  await browser.close()
}

const failed = results.filter(r => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} passed`)
if (failed.length) {
  console.log('\n失败项:')
  for (const f of failed) console.log(`  - ${f.name}${f.detail ? ' — ' + f.detail : ''}`)
  process.exitCode = 1
}
