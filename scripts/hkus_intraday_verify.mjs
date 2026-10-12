// 港美股「当日分时」UI 验收 —— 整页 /stock/00700.HK 与 /stock/AAPL.US
//
// 两个必须遵守的环境事实(踩过):
//  1. 前端是 createBrowserRouter(**不是** hash 路由) ⇒ URL 必须是 /stock/xxx, 写 /#/stock/xxx
//     只会落到看板, 所有断言假失败;
//  2. index.html 引了 rsms.me / fonts.googleapis.com 的外链资源, 本机取不到 ⇒ 会拖到 goto
//     与 screenshot 超时。这里直接掐掉非 127.0.0.1 的请求。
import { chromium } from 'file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs';
import fs from 'node:fs';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = 'http://127.0.0.1:3018';
const SHOT = 'E:/ai_codes/ai_personal_panel/tsp-fresh/gui-test-screenshots/hkus-intraday';
fs.mkdirSync(SHOT, { recursive: true });

const results = [];
const log = (name, pass, note = '') => { results.push({ name, pass }); console.log(`${pass ? 'PASS' : 'FAIL'} | ${name}${note ? ' | ' + note : ''}`); };

const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--no-proxy-server'] });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
await ctx.route(url => !url.href.startsWith(BASE), route => route.abort());
const page = await ctx.newPage();
const errs = [];
page.on('pageerror', (e) => errs.push(String(e)));
const flat = () => page.evaluate(() => document.body.innerText.replace(/\s+/g, ' '));

async function openMinute(symbol, file) {
  await page.goto(`${BASE}/stock/${symbol}`, { waitUntil: 'load', timeout: 120000 });
  await page.waitForTimeout(3000);
  const clicked = await page.evaluate(() => {
    const b = Array.from(document.querySelectorAll('button')).find(x => x.innerText.trim() === '分时');
    if (!b) return false;
    b.click();
    return true;
  });
  log(`${symbol} 找到「分时」页签`, clicked);
  await page.waitForTimeout(5000);
  const t = await flat();
  const canvases = await page.evaluate(() => document.querySelectorAll('canvas').length);
  await page.screenshot({ path: `${SHOT}-${file}.png`, timeout: 60000 }).catch(() => {});
  return { t, canvases };
}

try {
  const hk = await openMinute('00700.HK', 'hk-00700');
  log('HK 画出了 canvas', hk.canvases >= 1, `canvas=${hk.canvases}`);
  log('HK 摘要有昨收', hk.t.includes('昨收 424.800'));
  log('HK 不再是"A股专用"拒绝文案', !hk.t.includes('当日分时仅覆盖 A股'));
  log('HK 标了合成口径', hk.t.includes('MAC-EX') && hk.t.includes('昨收取自上一会话收盘'));
  log('HK 提示会话未收盘', hk.t.includes('未收盘'));

  const us = await openMinute('AAPL.US', 'us-AAPL');
  log('US 画出了 canvas', us.canvases >= 1, `canvas=${us.canvases}`);
  log('US 摘要有昨收(上一交易日收盘)', us.t.includes('昨收 340.420'));
  log('US 是完整会话(390 分钟点, 无未收盘)', us.t.includes('390 个分钟点') && !us.t.includes('未收盘'));
  log('US 说明了跨零点口径', us.t.includes('跨零点'));

  log('无 JS 运行时错误', errs.length === 0, errs.slice(0, 2).join(' | '));
} finally {
  await browser.close();
  const bad = results.filter(r => !r.pass);
  console.log(`\n${results.length - bad.length}/${results.length} passed`);
  process.exit(bad.length ? 1 : 0);
}
