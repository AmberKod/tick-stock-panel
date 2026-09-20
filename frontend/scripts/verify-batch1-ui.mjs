import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const artifacts = path.resolve(frontend, '../docs/batch1/qa')
const id = String(process.pid)
const fixtureName = '.batch1-ui-' + id
const htmlPath = path.join(frontend, fixtureName + '.html')
const sourcePath = path.join(frontend, fixtureName + '.tsx')
const harness = "import React from 'react'\nimport { createRoot } from 'react-dom/client'\nimport { BrowserRouter, useSearchParams } from 'react-router-dom'\nimport { QueryClient, QueryClientProvider } from '@tanstack/react-query'\nimport { Backtest } from './src/pages/Backtest'\nimport { MarketDataStatus } from './src/components/MarketDataStatus'\nimport { MarketScreener } from './src/components/MarketScreener'\nimport './src/index.css'\n\nwindow.__requests = []\nwindow.__streams = []\nwindow.__errors = []\nwindow.addEventListener('error', event => window.__errors.push(event.message))\nwindow.addEventListener('unhandledrejection', event => window.__errors.push(String(event.reason)))\nconst detail = asset => ({\n  id: 'fixture_daily', name: '日线测试策略', description: '', source: 'custom',\n  execution_backend: 'polars_expr', asset_types: [asset], timeframes: ['1d'], tags: [],\n  basic_filter: { enabled: true, price_min: 0, exclude_st: false, boards: [] },\n  portfolio: null, params: [], params_defaults: {}, scoring: { close: 1 },\n  scoring_directions: {}, entry_signals: [], exit_signals: [], stop_loss: null,\n  take_profit: null, trailing_stop: null, trailing_take_profit_activate: null,\n  trailing_take_profit_drawdown: null, max_hold_days: 5, order_by: 'score',\n  descending: true, limit: 30, minute_exit_trigger_supported_signals: [],\n})\nconst coverage = (asset, empty = false) => ({\n  market: asset.toUpperCase(), currency: asset === 'hk' ? 'HKD' : 'USD',\n  checked_at: '2026-09-09T12:00:00Z', source: ['fixture'], data_generation: 'fixture-1',\n  instruments: { symbols: 2, lot_size_available: 1, lot_size_missing: 1 },\n  daily: { symbols: empty ? 0 : 2, target_symbols: empty ? 0 : 1, extra_symbols: empty ? 0 : 1,\n    missing_symbols: empty ? 2 : 1, rows: empty ? 0 : 6, target_rows: empty ? 0 : 3,\n    first_date: empty ? null : '2026-09-01', last_date: empty ? null : '2026-09-08', target_last_date: empty ? null : '2026-09-07' },\n  enriched: { symbols: empty ? 0 : 2, target_symbols: empty ? 0 : 1, extra_symbols: empty ? 0 : 1,\n    missing_symbols: empty ? 2 : 1, rows: empty ? 0 : 6, target_rows: empty ? 0 : 3,\n    first_date: empty ? null : '2026-09-01', last_date: empty ? null : '2026-09-08', target_last_date: empty ? null : '2026-09-07' },\n  missing_fields: { daily: {}, enriched: {} }, warnings: ['固定样本：1 只标的缺少指标'],\n  capabilities: { daily_download: true, daily_provider: 'fixture', daily_download_reason: null,\n    recompute_enriched: !empty, lot_size_sync: asset === 'hk' },\n})\nwindow.__jobDone = false\nwindow.fetch = async (input, init = {}) => {\n  const url = new URL(String(input), location.origin)\n  window.__requests.push({ url: url.pathname + url.search, method: init.method || 'GET', body: init.body })\n  const path = url.pathname\n  const asset = url.searchParams.get('asset_type') || 'stock'\n  const market = path.includes('/us/') ? 'us' : 'hk'\n  let data = {}\n  let status = 200\n  if (path === '/api/capabilities') data = { label: 'fixture', capabilities: { 'kline.minute.batch': {}, financial: {} } }\n  else if (path === '/api/data/status') data = { enriched: { earliest_date: '2026-09-01' }, etf_enriched: { earliest_date: '2026-09-01' } }\n  else if (path.endsWith('/data/status')) {\n    data = coverage(market, new URLSearchParams(location.search).get('case') === 'empty')\n    if (new URLSearchParams(location.search).get('case') === 'unsupported') data.capabilities.daily_download = false\n    if (new URLSearchParams(location.search).get('case') === 'error') { data = { detail: '状态测试失败' }; status = 503 }\n  } else if (path === '/api/strategies') data = { strategies: [detail(asset)], load_errors: [] }\n  else if (path.startsWith('/api/strategies/')) data = detail(asset)\n  else if (path === '/api/watchlist') data = { symbols: [\n    { symbol: '00700.HK', name: '港股样本', market: 'hk' },\n    { symbol: 'AAPL.US', name: '美股样本', market: 'us' },\n    { symbol: '000001.SZ', name: 'A 股样本', market: 'cn' },\n  ] }\n  else if (path.endsWith('/stocks')) data = { results: market === 'hk'\n    ? [{ symbol: '00700.HK', name: '港股样本' }, { symbol: '00005.HK', name: '另一港股' }]\n    : [{ symbol: 'AAPL.US', name: '美股样本' }, { symbol: 'MSFT.US', name: '另一美股' }] }\n  else if (path === '/api/screener/run_preset') data = {\n    as_of: '2026-09-08', strategy: '日线测试策略', total: 1, elapsed_ms: 1,\n    warnings: ['1 个标的缺少评分字段，已排除'],\n    rows: [{ symbol: '00700.HK', name: '港股样本', close: 10, change_pct: 0.02, score: 75 }],\n  }\n  else if (path.endsWith('/daily/sync')) data = { operation: 'daily_download', market: market.toUpperCase(), status: 'started', requested: 2, succeeded: 0, failed: 0, skipped: 0, enriched_dates_written: 0, data_generation: 'fixture-1', failures: [], job_id: 'fixture-job' }\n  else if (path.endsWith('/pipeline/jobs/fixture-job')) data = {\n    id: 'fixture-job', status: window.__jobDone ? 'succeeded' : 'running', progress: window.__jobDone ? 100 : 30,\n    stage: 'daily', stage_pct: 30, log: [], error: null,\n    result: window.__jobDone ? { operation: 'daily_download', status: 'completed_with_errors', market: 'HK', requested: 2, succeeded: 1, failed: 1, skipped: 0, enriched_dates_written: 1, data_generation: 'fixture-2', failures: [{ symbol: '00005.HK', reason: '下载超时' }] } : null,\n  }\n  else if (path === '/api/backtest/strategy/cancel') data = { ok: true, cancelled_count: 1 }\n  else if (path.includes('/custom-signals')) data = { signals: [] }\n  else if (path === '/api/kline/daily') data = { symbol: url.searchParams.get('symbol'), source: 'enriched', rows: [{ date: '2026-09-01', open: 10, high: 11, low: 9, close: 10, volume: 100 }] }\n  else if (path === '/api/kline/instruments/search') data = { results: [{ symbol: asset === 'hk' ? '00700.HK' : 'AAPL.US', name: '证券测试' }] }\n  else if (path.endsWith('/watchlist/groups')) data = { groups: [] }\n  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })\n}\nclass FixtureEventSource {\n  constructor(url) { this.url = url; this.handlers = {}; this.closed = false; window.__streams.push(this); queueMicrotask(() => this.onopen?.({})) }\n  addEventListener(name, callback) { this.handlers[name] = callback }\n  close() { this.closed = true }\n  emit(name, data) { this.handlers[name]?.({ data: JSON.stringify(data) }) }\n}\nwindow.EventSource = FixtureEventSource\nwindow.__finish = asset => {\n  const stream = [...window.__streams].reverse().find(item => new URL(item.url, location.origin).searchParams.get('asset_type') === asset && !item.closed)\n  const config = Object.fromEntries(new URL(stream.url, location.origin).searchParams)\n  const symbol = asset === 'hk' ? '00700.HK' : 'AAPL.US'\n  stream.emit('done', {\n    run_id: 'fixture-' + asset, config: { ...config, market: asset.toUpperCase(), currency: asset === 'hk' ? 'HKD' : 'USD', benchmark_available: false, benchmark_symbol: asset === 'hk' ? 'HSI.HI' : '^GSPC', execution_assumptions: { price_basis_note: '使用存储日线价格', corporate_actions_simulated: false } },\n    stats: { mode: 'position', n_trades: 1, total_return: 0.01, max_drawdown: -0.005, win_rate: 1, execution: { buy_lot_size_missing: 1 } },\n    equity_curve: [{ date: '2026-09-01', value: 10000, cash: 9900 }, { date: '2026-09-02', value: 10100, cash: 10100 }],\n    drawdown_curve: [{ date: '2026-09-01', value: 0 }, { date: '2026-09-02', value: 0 }],\n    benchmark_curve: [],\n    trades: [{ symbol, name: asset + ' 交易样本', entry_date: '2026-09-01', exit_date: '2026-09-02', entry_price: 10, exit_price: 11, pnl_pct: 0.1, duration: 1, exit_reason: 'signal', shares: asset === 'us' ? 1 : 100, lots: 1, entry_value: 10, exit_value: 11, pnl_amount: 1 }],\n    per_symbol_stats: [], daily_picks: [], strategy_info: { ...detail(asset), entry_signals: [], exit_signals: [] },\n    warnings: ['固定样本：基准数据缺失'],\n  })\n}\nconst client = new QueryClient({ defaultOptions: { queries: { retry: false } } })\nfunction Harness() {\n  const [query] = useSearchParams()\n  const market = query.get('market') === 'us' ? 'us' : 'hk'\n  const view = query.get('view')\n  return view === 'status' ? <MarketDataStatus market={market} /> : view === 'screener' ? <MarketScreener market={market} /> : <Backtest />\n}\ncreateRoot(document.getElementById('root')).render(<QueryClientProvider client={client}><BrowserRouter><Harness /></BrowserRouter></QueryClientProvider>)\n"
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
const port = Number(process.env.BATCH1_UI_PORT || 5194)
const debugPort = Number(process.env.BATCH1_CDP_PORT || 9394)
const origin = 'http://127.0.0.1:' + port
let browser
let vite
let ws
let passed = 0
const pending = new Map()
let commandId = 0
const chrome = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const profile = path.join(tmpdir(), 'tick-stock-batch1-ui-' + id)
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
  assert.ok(await evaluate(expression), message)
  passed += 1
  console.log('PASS ' + message)
}
async function navigate(query) {
  await command('Page.navigate', { url: origin + '/' + fixtureName + '.html?' + query })
  await until(() => evaluate("Boolean(window.__requests && document.getElementById('root')?.textContent)"), 'Fixture did not render')
}
async function clickText(text) {
  await evaluate('(() => { const buttons = [...document.querySelectorAll("button")]; const text = ' + JSON.stringify(text) + '; const button = buttons.find(item => item.textContent.trim() === text) ?? buttons.find(item => item.textContent.trim().startsWith(text)); if (!button || button.disabled) throw new Error("Button unavailable"); button.click() })()')
  await sleep(250)
}
async function select(value) {
  await evaluate('(() => { const select = document.getElementById("backtest-asset"); select.value = ' + JSON.stringify(value) + '; select.dispatchEvent(new Event("change", { bubbles: true })) })()')
  await sleep(350)
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
  await command('Emulation.setDeviceMetricsOverride', { width: 1366, height: 900, deviceScaleFactor: 1, mobile: false })
  await navigate('market=hk&strategy_id=fixture_daily&symbols=00700.HK')
  await until(() => evaluate('[...document.querySelectorAll("button")].some(button => button.textContent.trim() === "运行回测" && !button.disabled)'), 'Backtest not ready')
  await check('JSON.parse(localStorage.getItem("strategy-backtest-last-hk")).symbols === "00700.HK"', 'URL symbols prefill HK settings')
  await check('window.__requests.some(request => request.url === "/api/strategies/fixture_daily?asset_type=hk")', 'Strategy detail uses explicit market')
  await check('[...document.querySelectorAll("button")].filter(button => ["因子", "验证", "候选方案"].includes(button.textContent.trim())).every(button => button.disabled)', 'Unsupported HK research modes are disabled')
  await clickText('运行回测')
  await check('new URL(window.__streams[0].url, location.origin).searchParams.get("asset_type") === "hk" && new URL(window.__streams[0].url, location.origin).searchParams.get("commission_pct") === "0"', 'HK task uses HK context and independent fee defaults')
  await check('Object.keys(JSON.parse(new URL(window.__streams[0].url, location.origin).searchParams.get("overrides")).basic_filter).length === 0', 'Unmodified filters do not override market defaults')
  await select('us')
  await check('!window.__streams[0].closed && JSON.parse(localStorage.getItem("strategy-backtest-last-us")).symbols === ""', 'Switching market preserves HK task and clears HK symbols')
  await clickText('日线测试策略')
  await until(() => evaluate('[...document.querySelectorAll("button")].some(button => button.textContent.trim() === "运行回测" && !button.disabled)'), 'US backtest not ready')
  await clickText('运行回测')
  await check('window.__streams.length === 2 && !window.__streams[0].closed', 'US and HK tasks coexist')
  await evaluate('window.__finish("us")')
  await sleep(350)
  await check('document.body.textContent.includes("美股 · 所有金额以 USD 计价") && !document.body.textContent.includes("同期上证")', 'US result currency and benchmark are market aware')
  await evaluate('window.__finish("hk")')
  await sleep(250)
  await check('document.body.textContent.includes("美股 · 所有金额以 USD 计价") && !document.body.textContent.includes("港股 · 所有金额以 HKD")', 'Background HK completion cannot replace US result')
  await select('hk')
  await check('document.body.textContent.includes("港股 · 所有金额以 HKD 计价") && JSON.parse(localStorage.getItem("strategy-backtest-last-hk")).symbols === "00700.HK"', 'Returning to HK restores its form and result')
  await clickText('运行回测')
  await clickText('停止回测')
  await check('window.__requests.some(request => request.url === "/api/backtest/strategy/cancel" && new URLSearchParams(JSON.parse(request.body).qs).get("asset_type") === "hk")', 'Cancel uses the exact HK task query')
  await command('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true })
  await sleep(300)
  await check('document.documentElement.scrollWidth <= window.innerWidth', 'Narrow backtest page stays within viewport')
  writeFileSync(path.join(artifacts, 'backtest-hk-narrow.png'), Buffer.from((await command('Page.captureScreenshot', { format: 'png' })).data, 'base64'))
  await command('Emulation.setDeviceMetricsOverride', { width: 1366, height: 900, deviceScaleFactor: 1, mobile: false })
  await navigate('view=status&market=hk')
  await until(() => evaluate('document.body.textContent.includes("池外存量 1 只")'), 'Coverage did not render')
  await check('document.body.textContent.includes("当前池最近记录：2026-09-07") && document.body.textContent.includes("池外存量 1 只")', 'Coverage separates current pool from extra stored symbols')
  await clickText('下载当前池日 K')
  await check('!document.body.textContent.includes("成功 0 只") && document.body.textContent.includes("下载日 K中")', 'Started job is not reported as completed')
  await evaluate('window.__jobDone = true')
  await until(() => evaluate('document.body.textContent.includes("部分完成")'), 'Final pipeline result did not render')
  await check('document.body.textContent.includes("成功 1 只") && document.body.textContent.includes("失败 1 只") && document.body.textContent.includes("下载超时")', 'Pipeline final counts and failure reasons are displayed')
  await check('window.__requests.filter(request => request.url === "/api/hk/data/status").length >= 2 && !window.__requests.some(request => request.url.includes("/api/us/"))', 'Sync refresh stays scoped to HK')
  writeFileSync(path.join(artifacts, 'data-hk-partial.png'), Buffer.from((await command('Page.captureScreenshot', { format: 'png' })).data, 'base64'))
  await navigate('view=status&market=us&case=unsupported')
  await until(() => evaluate('document.body.textContent.includes("日 K 下载不可用")'), 'Unsupported state did not render')
  await check('[...document.querySelectorAll("button")].find(button => button.textContent.includes("下载当前池")).disabled', 'Unavailable provider disables download')
  await navigate('view=status&market=us&case=empty')
  await until(() => evaluate('document.body.textContent.includes("当前没有可重算")'), 'Empty state did not render')
  await check('[...document.querySelectorAll("button")].find(button => button.textContent.trim() === "重算策略指标").disabled', 'Empty daily coverage disables recompute')
  await navigate('view=status&market=us&case=error')
  await until(() => evaluate('document.body.textContent.includes("状态测试失败")'), 'Error state did not render')
  await check('document.body.textContent.includes("数据状态读取失败")', 'Status errors remain visible')
  await navigate('view=screener&market=hk')
  await until(() => evaluate('document.querySelector("#hk-strategy option[value=fixture_daily]") !== null'), 'Screener did not load')
  await evaluate('(() => { const select = document.getElementById("hk-strategy"); select.value = "fixture_daily"; select.dispatchEvent(new Event("change", { bubbles: true })) })()')
  await sleep(250)
  await clickText('运行筛选')
  await until(() => evaluate('document.body.textContent.includes("75.00")'), 'Screener result did not render')
  await check('document.body.textContent.includes("1 个标的缺少评分字段，已排除") && !document.body.textContent.includes("null")', 'Screener displays scoring diagnostics and finite scores')
  await clickText('回测')
  await check('new URLSearchParams(location.search).get("market") === "hk" && new URLSearchParams(location.search).get("strategy_id") === "fixture_daily" && new URLSearchParams(location.search).get("symbols") === "00700.HK"', 'Screener link carries market, strategy and symbols')
  await evaluate('(() => { const saved = JSON.parse(localStorage.getItem("strategy-backtest-last-hk")); saved.symbols = "00005.HK"; saved.fees = "7"; localStorage.setItem("strategy-backtest-last-hk", JSON.stringify(saved)) })()')
  await navigate('market=hk&strategy_id=fixture_daily&symbols=00700.HK')
  await until(() => evaluate('JSON.parse(localStorage.getItem("strategy-backtest-last-hk"))?.symbols === "00700.HK"'), 'URL did not replace the saved symbol')
  await check('JSON.parse(localStorage.getItem("strategy-backtest-last-hk")).fees === "7"', 'URL overrides saved symbols while retaining HK fee settings')
  await evaluate('localStorage.setItem("backtest_reconnect", "strategy_id=fixture_daily&asset_type=us&symbols=AAPL.US&commission_pct=0.0004&buy_stamp_tax_pct=0.001")')
  await navigate('market=hk')
  await check('window.__streams.length === 0 && localStorage.getItem("backtest_reconnect")?.includes("asset_type=us")', 'HK does not reconnect a saved US task')
  await select('us')
  await until(() => evaluate('window.__streams.length === 1'), 'US task did not reconnect')
  await check('new URL(window.__streams[0].url, location.origin).searchParams.get("buy_stamp_tax_pct") === "0.001" && localStorage.getItem("backtest_reconnect") === null', 'US reconnect preserves exact fees and migrates the legacy task key')
  await clickText('停止回测')
  await evaluate('localStorage.setItem("strategy-backtest-last", JSON.stringify({ assetType: "etf", selectedStrategy: "fixture_daily", symbols: "510300.SH", fees: "4" }))')
  await navigate('market=etf')
  await until(() => evaluate('localStorage.getItem("strategy-backtest-last-etf") !== null'), 'ETF legacy settings did not migrate')
  await check('JSON.parse(localStorage.getItem("strategy-backtest-last-etf")).symbols === "510300.SH"', 'Legacy ETF settings remain readable')
  await select('stock')
  await check('JSON.parse(localStorage.getItem("strategy-backtest-last-stock")).symbols === ""', 'Legacy ETF symbols do not enter the stock market form')
  await check('window.__errors.length === 0', 'Fixture has no uncaught browser errors')
  console.log(JSON.stringify({ passed, screenshots: artifacts, fixture: 'Mock API and SSE; no user data or live providers used.' }))
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
