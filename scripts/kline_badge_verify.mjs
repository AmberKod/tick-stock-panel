// K线箭头徽章 UI 验收 — 打开个股详情页, 逐项核对标注开关 / 徽章渲染 / 证据角标
import { chromium } from 'file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs';
import fs from 'node:fs';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = 'http://127.0.0.1:3018';
const SYMBOL = process.env.VERIFY_SYMBOL || '600519.SH';
const SHOT = 'E:/ai_codes/ai_personal_panel/tsp-fresh/gui-test-screenshots/kline-badge';
const results = [];
const log = (name, pass, note = '') => {
  results.push({ name, pass });
  console.log(`${pass ? 'PASS' : 'FAIL'} | ${name}${note ? ' | ' + note : ''}`);
};

fs.mkdirSync('E:/ai_codes/ai_personal_panel/tsp-fresh/gui-test-screenshots', { recursive: true });

const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--no-proxy-server'] });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
await ctx.route('**/*', (route) => {
  const u = route.request().url();
  if (u.includes('127.0.0.1') || u.includes('localhost')) return route.continue();
  return route.abort();
});
const page = await ctx.newPage();
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e)));

try {
  await page.goto(`${BASE}/stock/${SYMBOL}`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4500);

  // 1. 标注按钮组存在
  const labels = ['放量', '新高', '新低', 'MACD', 'MA5/20', 'MA20/60', '穿MA20', 'RSI', '布林'];
  const btn = (l) => page.locator('button').filter({ hasText: new RegExp(`^${l}`) }).first();
  const found = [];
  for (const l of labels) {
    if (await btn(l).count()) found.push(l);
  }
  log('1a 9 个标注按钮齐全', found.length === 9, `found=${found.join(',')}`);

  // 2. 默认开启 4 个 (sky 高亮样式)
  const activeCount = await page.evaluate(() => {
    return [...document.querySelectorAll('button')]
      .filter((b) => b.className.includes('border-sky-400'))
      .map((b) => b.textContent.trim().slice(0, 6));
  });
  log('2a 默认开启 4 个标注', activeCount.length === 4, activeCount.join(' / '));

  // 3. 证据角标: 跑过全市场回测后, 每个标注都应带"方向判断"结论(不再显示"未回测")
  const titles = await page.evaluate(() =>
    [...document.querySelectorAll('button[title]')]
      .map((b) => b.getAttribute('title'))
      .filter((t) => t && t.includes('方向判断：'))
  );
  const tested = titles.filter((t) => !t.includes('未回测'));
  log('3a 9 个标注均带回测证据', titles.length >= 9 && tested.length >= 9,
    `${titles.length} 个有证据行, ${tested.length} 个已回测`);

  // 3b 双向信号的结论必须指明取用的是哪条腿(合并数会把"一条灵一条哑"掩盖掉);
  //     做空腿还要写明 A 股无融券, 不能让人当成开空信号。
  const byLeg = titles.filter((t) => t.includes('看涨侧') || t.includes('看跌侧'));
  // 只认"结论取用的那条腿"(紧跟在 prefix 冒号后); note 里"另一侧看跌侧…"不算
  const shortLeg = titles.filter((t) => t.includes('：看跌侧'));
  log('3b 结论指明取用腿', byLeg.length >= 6, `${byLeg.length} 个带腿标注`);
  log('3c 做空腿注明无融券', shortLeg.length === 0 || shortLeg.every((t) => t.includes('无融券')),
    `${shortLeg.length} 个做空腿`);

  // 4. ECharts canvas 渲染出来
  const canvasCount = await page.locator('canvas').count();
  log('4a K线 canvas 已渲染', canvasCount > 0, `${canvasCount} canvas`);

  await page.screenshot({ path: `${SHOT}-01-default.png` });

  // 5. 全开 9 个标注 -> 截图
  for (const l of labels) {
    const b = btn(l);
    const cls = await b.getAttribute('class');
    if (!cls.includes('border-sky-400')) await b.click(), await page.waitForTimeout(120);
  }
  await page.waitForTimeout(1800);
  const allOn = await page.evaluate(() =>
    [...document.querySelectorAll('button')].filter((b) => b.className.includes('border-sky-400')).length
  );
  log('5a 9 个标注全部开启', allOn === 9, `${allOn} 个高亮`);
  await page.screenshot({ path: `${SHOT}-02-all-signals.png` });

  // 6. 切周K / 月K 也应有标注
  for (const [name, sel] of [['周K', /^周K$/], ['月K', /^月K$/]]) {
    const el = page.locator('button').filter({ hasText: sel }).first();
    if (await el.count()) {
      await el.click();
      await page.waitForTimeout(2600);
      await page.screenshot({ path: `${SHOT}-03-${name}.png` });
      log(`6a 切${name}后页面无 JS 报错`, pageErrors.length === 0);
    } else {
      log(`6a 切${name}后页面无 JS 报错`, false, '按钮未找到');
    }
  }

  log('7a 全程无 JS 异常', pageErrors.length === 0, pageErrors.slice(0, 2).join(' | '));
} catch (e) {
  log('EXCEPTION', false, String(e).slice(0, 300));
  await page.screenshot({ path: `${SHOT}-99-error.png` }).catch(() => {});
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.pass);
console.log(`\n==== ${results.length - failed.length}/${results.length} passed ====`);
process.exit(failed.length ? 1 : 0);
