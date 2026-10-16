// 美股深度面板浏览器验收 —— 财报三表 / 盘口摘要 / 分析师预期 / 机构持股
// 覆盖 CONTRIBUTING 五态: 加载中 / 空数据 / 请求错误 / 不可用(available=false+reason) / 正常数据,
// 外加「切换标的不残留上一标的数据」与「缺值必须 —(U+2014) 不得渲染成 0」。
// 用法:
//   unset CODEBUDDY_TOOL_CALL_ID && \
//   NODE_PATH="C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules" \
//   node scripts/us_deep_verify.mjs
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const artifacts = path.resolve(frontend, '../docs/us-deep/qa')
const id = String(process.pid)
const fixtureName = '.us-deep-' + id
const htmlPath = path.join(frontend, fixtureName + '.html')
const sourcePath = path.join(frontend, fixtureName + '.tsx')

const harness = [
  "import React, { useState } from 'react'",
  "import { createRoot } from 'react-dom/client'",
  "import { QueryClient, QueryClientProvider } from '@tanstack/react-query'",
  "import { USEstimatesPanel, USInstitutionalPanel, USStatementPanel, USSummaryPanel } from './src/components/us/USDeepPanels'",
  "import './src/index.css'",
  "",
  "window.__errors = []",
  "window.__requests = []",
  "window.addEventListener('error', event => window.__errors.push(event.message))",
  "window.addEventListener('unhandledrejection', event => window.__errors.push(String(event.reason)))",
  "",
  "// 三大报表的一期样本: 第 2 期刻意缺 gross_profit / ebitda, 用于验证缺值渲染成 — 而不是 0",
  "const ROWS = {",
  "  income: [",
  "    { period_ending: '2026-01-31', fiscal_year: 2026, total_revenue: 416160000000, cost_of_revenue: 120000000000, gross_profit: 296160000000, research_and_development_expense: 20000000000, operating_income: 100000000000, net_income: 90000000000, ebitda: 110000000000, diluted_earnings_per_share: 3.65, unusual_item: 12.5 },",
  "    { period_ending: '2025-01-31', fiscal_year: 2025, total_revenue: 300000000000, cost_of_revenue: 90000000000, operating_income: 70000000000, net_income: 60000000000, diluted_earnings_per_share: 2.45 },",
  "  ],",
  "  balance: [",
  "    { period_ending: '2026-01-31', cash_and_cash_equivalents: 30000000000, inventories: 5000000000, total_current_assets: 80000000000, total_assets: 150000000000, current_liabilities: 20000000000, total_liabilities_net_minority_interest: 40000000000, common_stock_equity: 110000000000, total_debt: 12000000000, net_debt: -18000000000 },",
  "  ],",
  "  cash: [",
  "    { period_ending: '2026-01-31', operating_cash_flow: 95000000000, capital_expenditure: -5000000000, free_cash_flow: 90000000000, investing_cash_flow: -20000000000, financing_cash_flow: -40000000000, depreciation_and_amortization: 3000000000, stock_based_compensation: 6000000000, net_change_in_cash_and_equivalents: 35000000000 },",
  "  ],",
  "}",
  "const ESTIMATES = { symbol: 'AAPL', currency: 'USD', current_price: 230.48, target_low: 200, target_high: 300, target_consensus: 265, target_median: 260, recommendation: 'strong_buy', recommendation_mean: 1.4, number_of_analysts: 45 }",
  "const INSTITUTIONAL = [{ date: '2026-06-30', owner: 'Vanguard Group Inc', shares_held: 1200000000, shares_change: -3000000, shares_change_pct: 0.01919, market_value_raw: 276576000000, url: 'https://example.test/vanguard' }]",
  "const SUMMARY_TEXT = 'AAPL 最新价 230.48，盘口来源 OpenBB（验收 fixture）'",
  "const UNAVAILABLE = { available: false, source: 'openbb', reason: 'provider_error: OpenBB 未启动' }",
  "",
  "function envelope(data) { return { available: true, source: 'openbb', symbol: 'AAPL', market: 'US', data: data } }",
  "",
  "window.fetch = async (input, init = {}) => {",
  "  const url = new URL(String(input), location.origin)",
  "  window.__requests.push({ url: url.pathname + url.search, method: init.method || 'GET' })",
  "  const parts = url.pathname.split('/').filter(Boolean)",
  "  const symbol = decodeURIComponent(parts[parts.length - 1] || '')",
  "  const mode = new URLSearchParams(location.search).get('mode') || 'ok'",
  "  // MSFT 一律不可用 —— 用于「切换标的后不残留上一标的数据」用例。",
  "  // 三表故意延迟 1200ms 返回, 好在**过渡态**里检查有没有残留上一标的数据。",
  "  if (symbol === 'MSFT') {",
  "    const body = JSON.stringify({ available: false, source: 'openbb', reason: 'provider_error: MSFT 上游不可用' })",
  "    if (url.pathname.indexOf('/api/us/deep/statement/') === 0) {",
  "      return new Promise(resolve => setTimeout(() => resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } })), 1200))",
  "    }",
  "    return new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "  }",
  "  if (mode === 'loading') return new Promise(() => {})",
  "  if (mode === 'error') return new Response(JSON.stringify({ detail: '深度服务内部错误' }), { status: 500, headers: { 'Content-Type': 'application/json' } })",
  "  if (url.pathname.indexOf('/api/us/deep/statement/') === 0) {",
  "    if (mode === 'unavailable') return new Response(JSON.stringify(UNAVAILABLE), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "    if (mode === 'empty') return new Response(JSON.stringify(envelope([])), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "    return new Response(JSON.stringify(envelope(ROWS[url.searchParams.get('kind') || 'income'] || ROWS.income)), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "  }",
  "  if (url.pathname.indexOf('/api/us/deep/summary/') === 0) {",
  "    if (mode === 'unavailable') return new Response(JSON.stringify(UNAVAILABLE), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "    if (mode === 'empty') return new Response(JSON.stringify(envelope({ text: '' })), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "    // 哨兵态: 后端仍给 available:true, text 是「无此标的数据」哨兵串",
  "    if (mode === 'sentinel') return new Response(JSON.stringify(envelope({ text: 'OpenBB 无此标的数据' })), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "    return new Response(JSON.stringify(envelope({ text: SUMMARY_TEXT })), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "  }",
  "  if (url.pathname.indexOf('/api/us/deep/estimates/') === 0) {",
  "    if (mode === 'unavailable') return new Response(JSON.stringify(UNAVAILABLE), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "    return new Response(JSON.stringify(envelope(ESTIMATES)), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "  }",
  "  if (url.pathname.indexOf('/api/us/deep/institutional/') === 0) {",
  "    if (mode === 'unavailable') return new Response(JSON.stringify(UNAVAILABLE), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "    if (mode === 'empty') return new Response(JSON.stringify(envelope([])), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "    return new Response(JSON.stringify(envelope(INSTITUTIONAL)), { status: 200, headers: { 'Content-Type': 'application/json' } })",
  "  }",
  "  return new Response(JSON.stringify({ detail: 'unexpected path ' + url.pathname }), { status: 404, headers: { 'Content-Type': 'application/json' } })",
  "}",
  "",
  "const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })",
  "function Harness() {",
  "  const [symbol, setSymbol] = useState('AAPL')",
  "  window.__setSymbol = setSymbol",
  "  return React.createElement(QueryClientProvider, { client: client },",
  "    React.createElement('div', { className: 'p-4 space-y-4' },",
  "      React.createElement('button', { id: 'btn-switch', onClick: () => setSymbol('MSFT') }, '切到 MSFT'),",
  "      React.createElement('div', { id: 'p-statement' }, React.createElement(USStatementPanel, { symbol: symbol, limit: 4 })),",
  "      React.createElement('div', { id: 'p-summary' }, React.createElement(USSummaryPanel, { symbol: symbol })),",
  "      React.createElement('div', { id: 'p-estimates' }, React.createElement(USEstimatesPanel, { symbol: symbol })),",
  "      React.createElement('div', { id: 'p-inst' }, React.createElement(USInstitutionalPanel, { symbol: symbol, limit: 20 })),",
  "    ),",
  "  )",
  "}",
  "createRoot(document.getElementById('root')).render(React.createElement(Harness))",
].join('\n')

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const port = Number(process.env.US_DEEP_UI_PORT || 5199)
const debugPort = Number(process.env.US_DEEP_CDP_PORT || 9399)
const origin = 'http://127.0.0.1:' + port
const chrome = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const profile = path.join(tmpdir(), 'tsp-us-deep-ui-' + id)
let browser
let vite
let ws
let passed = 0
let failed = 0
const pending = new Map()
let commandId = 0

async function until(callback, message, timeout = 20000) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    try { const value = await callback(); if (value) return value } catch {}
    await sleep(100)
  }
  throw new Error(message)
}
function command(method, params = {}) {
  const id = ++commandId
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)) }, 15000)
    pending.set(id, { resolve: value => { clearTimeout(timer); resolve(value) }, reject })
    ws.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression) {
  const result = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text)
  return result.result.value
}
async function check(expression, message) {
  const ok = await evaluate(expression)
  if (ok) { passed += 1; console.log('PASS ' + message) }
  else { failed += 1; console.log('FAIL ' + message) }
}
async function navigate(query) {
  await command('Page.navigate', { url: origin + '/' + fixtureName + '.html?' + query })
  await until(() => evaluate("Boolean(window.__requests && document.getElementById('p-statement')?.textContent)"), 'Fixture did not render')
}
async function clickText(text) {
  await evaluate('(() => { const buttons = [...document.querySelectorAll("button")]; const text = ' + JSON.stringify(text) + '; const button = buttons.find(item => item.textContent.trim() === text); if (!button) throw new Error("Button not found: " + text); button.click() })()')
  await sleep(300)
}
const text = panel => "document.getElementById(" + JSON.stringify(panel) + ").textContent"
async function screenshot(name) {
  writeFileSync(path.join(artifacts, name), Buffer.from((await command('Page.captureScreenshot', { format: 'png' })).data, 'base64'))
}

try {
  assert.ok(existsSync(chrome), 'Chrome executable is required; set CHROME_PATH if necessary.')
  if (existsSync(htmlPath) || existsSync(sourcePath)) throw new Error('Refusing to overwrite an existing fixture')
  mkdirSync(artifacts, { recursive: true })
  writeFileSync(sourcePath, harness, { flag: 'wx' })
  writeFileSync(htmlPath, '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script type="module" src="/' + fixtureName + '.tsx"></script></body></html>', { flag: 'wx' })
  vite = spawn(process.execPath, [path.join(frontend, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', String(port), '--strictPort'], { cwd: frontend, windowsHide: true, stdio: 'ignore' })
  await until(async () => (await fetch(origin)).ok, 'Vite did not start')
  browser = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=' + debugPort, '--user-data-dir=' + profile, 'about:blank'], { windowsHide: true, stdio: 'ignore' })
  const tabs = await until(async () => { const response = await fetch('http://127.0.0.1:' + debugPort + '/json'); return response.json() }, 'Chrome did not start')
  ws = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject })
  ws.onmessage = event => {
    const response = JSON.parse(event.data)
    const promise = pending.get(response.id)
    if (!promise) return
    pending.delete(response.id)
    if (response.error) promise.reject(new Error(response.error.message))
    else promise.resolve(response.result)
  }
  await command('Page.enable')
  await command('Runtime.enable')
  await command('Emulation.setDeviceMetricsOverride', { width: 1366, height: 1400, deviceScaleFactor: 1, mobile: false })

  // ── 态 1: 加载中
  await navigate('mode=loading')
  await until(() => evaluate(text('p-statement') + ".includes('加载中')"), 'Loading state did not render')
  await check(text('p-statement') + ".includes('加载中') && " + text('p-summary') + ".includes('加载中')", 'T1 加载中: 三表与摘要都显示加载中')
  await check("!" + text('p-statement') + ".includes('不可用') && !" + text('p-statement') + ".includes('营业总收入')", 'T1 加载中: 不误报不可用、不渲染空表')
  await screenshot('us-deep-01-loading.png')

  // ── 态 2: 空数据 (available:true + data:[])
  await navigate('mode=empty')
  await until(() => evaluate(text('p-statement') + ".includes('无报表数据')"), 'Empty state did not render')
  await check(text('p-statement') + ".includes('无报表数据') && " + text('p-inst') + ".includes('无机构持股披露')", 'T2 空数据: 空列表显示专属空态文案')
  await check("!" + text('p-statement') + ".includes('加载中') && !" + text('p-estimates') + ".includes('不可用')", 'T2 空数据: 不卡在加载中、不把空数据报成不可用')
  await check(text('p-summary') + ".includes('不可用')", 'T2 空数据: 摘要空文本归一为不可用')
  await screenshot('us-deep-02-empty.png')

  // ── 态 3: 请求错误 (HTTP 500)
  await navigate('mode=error')
  await until(() => evaluate(text('p-statement') + ".includes('请求失败')"), 'Error state did not render')
  await check(text('p-statement') + ".includes('请求失败') && " + text('p-statement') + ".includes('深度服务内部错误')", 'T3 请求错误: 显示请求失败并带上后端 detail')
  await check(text('p-summary') + ".includes('请求失败')", 'T3 请求错误: 摘要面板同样走错误分支')
  await screenshot('us-deep-03-error.png')

  // ── 态 4: 不可用 (available:false + reason)
  await navigate('mode=unavailable')
  await until(() => evaluate(text('p-statement') + ".includes('不可用')"), 'Unavailable state did not render')
  await check(text('p-statement') + ".includes('不可用: provider_error: OpenBB 未启动')", 'T4 不可用: 三表原样透出后端 reason')
  await check(text('p-estimates') + ".includes('不可用: provider_error: OpenBB 未启动')", 'T4 不可用: 分析师预期原样透出 reason')
  console.log('  [诊断] T4 摘要面板实际文本: ' + JSON.stringify(await evaluate(text('p-summary'))))
  await check(text('p-summary') + ".includes('provider_error: OpenBB 未启动')", 'T4 不可用: 摘要必须透出后端真实 reason（不得被通用 no_data 覆盖）')
  await check("!" + text('p-statement') + ".includes('营业总收入') && !" + text('p-statement') + ".includes('$0.00')", 'T4 不可用: 不渲染空表头、不把缺失渲染成 0')
  await screenshot('us-deep-04-unavailable.png')

  // ── 态 5: 正常数据
  await navigate('mode=ok')
  await until(() => evaluate(text('p-statement') + ".includes('416.16B')"), 'Normal state did not render')
  await check(text('p-statement') + ".includes('$416.16B') && " + text('p-statement') + ".includes('2026-01-31') && " + text('p-statement') + ".includes('2025-01-31')", 'T5 正常数据: 三表渲染金额缩写与两期表头')
  await check(text('p-statement') + ".includes('全部字段（11 项')", 'T5 正常数据: 全部字段折叠统计上游字段数')
  await check(text('p-summary') + ".includes('最新价 230.48')", 'T5 正常数据: 盘口摘要渲染正文')
  await check(text('p-estimates') + ".includes('强力买入') && " + text('p-estimates') + ".includes('+12.8%')", 'T5 正常数据: strong_buy 映射成强力买入 + 上行空间计算正确')
  await check(text('p-inst') + ".includes('+1.92%')", 'T5 正常数据: shares_change_pct 小数口径 ×100 展示')
  await check("!" + text('p-inst') + ".includes('276576000000') && !" + text('p-inst') + ".includes('2.77e')", 'T5 正常数据: market_value_raw 禁止展示')

  // 缺值必须 — (U+2014), 不得渲染成 0
  const dashExpr = "JSON.stringify((() => { const rows = [...document.querySelectorAll('#p-statement tbody tr')]; const row = rows.find(r => r.textContent.includes('毛利润')); return [...row.querySelectorAll('td')].slice(1).map(td => td.textContent.trim() + '|' + td.textContent.trim().charCodeAt(0)) })())"
  console.log('  毛利润行单元格(值|首字符码): ' + await evaluate(dashExpr))
  await check(dashExpr + ' === ' + JSON.stringify(JSON.stringify(['$296.16B|36', '—|8212'])), 'T5 正常数据: 缺失科目渲染为 — (U+2014) 而非 0')

  // kind / period 切换真的换了 queryKey 并发新请求
  await clickText('资产负债表')
  await until(() => evaluate("window.__requests.some(r => r.url.includes('kind=balance'))"), 'kind=balance request missing')
  await check("window.__requests.some(r => r.url === '/api/us/deep/statement/AAPL?kind=balance&period=annual&limit=4') && " + text('p-statement') + ".includes('资产总计')", 'T5 正常数据: 切到资产负债表 → kind=balance 请求 + 科目切换')
  await clickText('现金流量表')
  await until(() => evaluate("window.__requests.some(r => r.url.includes('kind=cash'))"), 'kind=cash request missing')
  await check(text('p-statement') + ".includes('经营活动现金流')", 'T5 正常数据: 切到现金流量表 → 科目切换')
  await clickText('季度')
  await until(() => evaluate("window.__requests.some(r => r.url.includes('period=quarter'))"), 'period=quarter request missing')
  await check("window.__requests.some(r => r.url.includes('kind=cash') && r.url.includes('period=quarter'))", 'T5 正常数据: 年度/季度档位进入请求')
  await clickText('利润表')
  await clickText('年度')
  await until(() => evaluate(text('p-statement') + ".includes('营业总收入')"), 'income/annual did not restore')
  await screenshot('us-deep-05-normal.png')

  // ── 哨兵态: available:true + 哨兵文本必须归一成不可用
  await navigate('mode=sentinel')
  await until(() => evaluate(text('p-summary') + ".includes('不可用')"), 'Sentinel state did not render')
  await check(text('p-summary') + ".includes('不可用') && !" + text('p-summary') + ".includes('最新价')", 'T-sentinel: 哨兵文本不被当成摘要正文渲染')
  await screenshot('us-deep-06-summary-sentinel.png')

  // ── 切换标的: 不残留上一标的数据
  await navigate('mode=ok')
  await until(() => evaluate(text('p-statement') + ".includes('416.16B')"), 'AAPL statement did not render')
  await check(text('p-statement') + ".includes('416.16B') && " + text('p-summary') + ".includes('最新价 230.48')", '切换前: AAPL 数据已渲染')
  await clickText('切到 MSFT')
  // 过渡态(三表延迟 1200ms): 必须清掉上一标的的数字, 不能靠 placeholderData 继续显示 AAPL
  console.log('  [诊断] 切换后 300ms 三表实际文本: ' + JSON.stringify(await evaluate(text('p-statement'))))
  await check("!" + text('p-statement') + ".includes('416.16B') && !" + text('p-statement') + ".includes('2026-01-31')", '切换标的(过渡态): 三表立刻清掉上一标的数据，不靠 placeholderData 顶着')
  await until(() => evaluate(text('p-statement') + ".includes('不可用')"), 'MSFT unavailable state did not render')
  console.log('  [诊断] 切换后摘要面板实际文本: ' + JSON.stringify(await evaluate(text('p-summary'))))
  await check("!" + text('p-statement') + ".includes('416.16B') && !" + text('p-statement') + ".includes('2026-01-31')", '切换标的: 三表不残留上一标的的金额与期数')
  await check("!" + text('p-summary') + ".includes('最新价 230.48') && " + text('p-summary') + ".includes('MSFT 上游不可用')", '切换标的: 摘要不残留上一标的正文，显示新标的 reason')
  await check("!" + text('p-estimates') + ".includes('强力买入') && " + text('p-estimates') + ".includes('MSFT 上游不可用')", '切换标的: 分析师预期不残留上一标的评级')
  await check("!" + text('p-inst') + ".includes('Vanguard')", '切换标的: 机构持股不残留上一标的的机构名')
  await check("window.__requests.some(r => r.url.startsWith('/api/us/deep/statement/MSFT'))", '切换标的: 请求打到新标的')
  await screenshot('us-deep-07-switch-symbol.png')

  // ── 请求全部收敛到 /api/us/deep/*
  await check("window.__requests.length > 0 && window.__requests.every(r => r.url.startsWith('/api/us/deep/'))", '所有请求都走 /api/us/deep/* 契约路径')

  await check('window.__errors.length === 0', 'Fixture 无未捕获浏览器错误')
  console.log(JSON.stringify({ passed, failed, screenshots: artifacts }))
  if (failed > 0) throw new Error('US deep verification failed: ' + failed + ' assertion(s)')
} finally {
  if (ws?.readyState === WebSocket.OPEN) {
    try { await command('Browser.close') } catch {}
    ws.close()
  }
  browser?.kill()
  vite?.kill()
  for (const file of [htmlPath, sourcePath]) {
    if (path.dirname(file) !== frontend || !path.basename(file).startsWith(fixtureName)) throw new Error('Fixture cleanup path mismatch')
    if (existsSync(file)) rmSync(file)
  }
}
