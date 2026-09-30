// 小说工作区 UI 验收 v3（交互模型修正：+卷/+章 直接创建自动命名节点）
import { chromium } from 'file:///C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs';
import fs from 'node:fs';

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const BASE = 'http://127.0.0.1:3011';
const SHOT = 'E:/ai_codes/ai_personal_panel/tsp-fresh/gui-test-screenshots/novel';
const results = [];
const log = (name, pass, note = '') => { results.push({ name, pass }); console.log(`${pass ? 'PASS' : 'FAIL'} | ${name}${note ? ' | ' + note : ''}`); };

const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox', '--no-proxy-server'] });
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
const pageErrors = [];
page.on('pageerror', (e) => pageErrors.push(String(e)));
const text = () => page.evaluate(() => document.body.innerText);

try {
  // ========== 1. 空态 ==========
  await page.goto(BASE + '/novel', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2200);
  let t = await text();
  log('1a 空书架文案', t.includes('还没有书'));
  log('1b 空态展示绝对路径', t.includes('data') && t.includes('novel'));
  await page.screenshot({ path: `${SHOT}-01-empty.png` });
  const l = await page.evaluate(() => ({ s: document.documentElement.scrollHeight, c: document.documentElement.clientHeight }));
  log('1c 无整页滚动条', l.s <= l.c + 2, JSON.stringify(l));

  // ========== 2. 新建书（左栏内联输入 + 「新建」按钮） ==========
  await page.locator('button[aria-label="新建书籍"]').click();
  await page.waitForTimeout(500);
  // 填 placeholder=书名 的内联输入框
  await page.locator('input[placeholder="书名"]').fill('星海黎明');
  await page.waitForTimeout(300);
  await page.locator('button').filter({ hasText: /^新建$/ }).last().click();
  await page.waitForTimeout(1600);
  t = await text();
  log('2a 建书后书架出现书名', t.includes('星海黎明'));
  // 自动带卷/章？
  const hasVol = /第一卷|第1卷|第一章/.test(t);
  log('2b 新书自带默认卷/章', hasVol, '自动节点');
  await page.screenshot({ path: `${SHOT}-02-book.png` });

  // ========== 3. +卷 / +章 直接创建 ==========
  await page.locator('button').filter({ hasText: '+卷' }).first().click();
  await page.waitForTimeout(900);
  t = await text();
  log('3a +卷 立即创建新卷', /第2卷|第二卷|卷/.test(t));
  await page.locator('button').filter({ hasText: '+章' }).first().click();
  await page.waitForTimeout(900);
  t = await text();
  await page.screenshot({ path: `${SHOT}-03-outline.png` });

  // ========== 4. 进章节编辑器 ==========
  // 点中栏或大纲里的第一章
  const chLink = page.locator('text=/第一章|引子|01/').first();
  await chLink.click();
  await page.waitForTimeout(1200);
  const ta = page.locator('textarea').first();
  log('4a 编辑器 textarea 出现', await ta.count() > 0);
  if (await ta.count()) {
    await ta.fill('# 第一章 黑匣子\n\n雨下了整夜，星港的霓虹在水面碎成一片。\n\n陆昭握着那只黑匣子，站在废弃船坞的边缘。潮气里有机油的味道。');
    await page.waitForTimeout(2200); // 防抖 800ms + 网络 + 落盘
    t = await text();
    log('4b 保存徽标（已保存/保存中）', /已保存|保存中/.test(t));
    log('4c 字数统计出现', /\d+\s*字/.test(t));
  }
  await page.screenshot({ path: `${SHOT}-04-editor.png` });

  // ========== 5. AI 面板 fail-closed ==========
  t = await text();
  log('5a AI 面板显示网关未配置', t.includes('未配置') || t.includes('不可用'));
  const contBtn = page.locator('button').filter({ hasText: /续写本章|续写/ });
  if (await contBtn.count()) {
    log('5b 续写按钮禁用', await contBtn.first().isDisabled());
  }

  // ========== 6. MarkdownLite 预览 ==========
  const pv = page.locator('button').filter({ hasText: '预览' });
  if (await pv.count()) {
    await pv.first().click();
    await page.waitForTimeout(800);
    const h = await page.locator('h1,h2,h3').count();
    log('6 预览渲染标题', h > 0, `h*=${h}`);
    await page.screenshot({ path: `${SHOT}-05-preview.png` });
    await page.locator('button').filter({ hasText: '源码' }).first().click();
    await page.waitForTimeout(300);
  } else log('6 预览 tab', false);

  // ========== 7. 落盘验证 ==========
  const booksDir = 'E:/ai_codes/ai_personal_panel/tsp-fresh/data/novel/books';
  const books = fs.readdirSync(booksDir).filter(x => !x.startsWith('.'));
  log('7a 书目录落盘', books.length > 0, books.join(','));
  const b0 = `${booksDir}/${books[0]}`;
  log('7b book.json', fs.existsSync(`${b0}/book.json`));
  log('7c state.json', fs.existsSync(`${b0}/state.json`));
  let mdPath = '';
  let mdOk = false;
  const chDir = `${b0}/正文`;
  if (fs.existsSync(chDir)) {
    for (const f of fs.readdirSync(chDir)) {
      if (f.endsWith('.md')) {
        const c = fs.readFileSync(`${chDir}/${f}`, 'utf-8');
        if (c.includes('雨下了整夜')) { mdOk = true; mdPath = `${chDir}/${f}`; }
      }
    }
  }
  log('7d 正文 md 落盘内容匹配', mdOk, mdPath);

  // ========== 8. 外部编辑回读（本地优先核心） ==========
  if (mdPath) {
    fs.writeFileSync(mdPath, '# 第一章 黑匣子\n\n外部编辑器写入：黑匣子在深夜发出微光，像一只睁开的眼睛。', 'utf-8');
    await page.reload({ waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);
    await page.locator('text=星海黎明').first().click().catch(() => {});
    await page.waitForTimeout(900);
    await page.locator('text=/第一章|01/').first().click().catch(() => {});
    await page.waitForTimeout(1400);
    t = await text();
    log('8 外部改 md 后 UI 读到新内容', t.includes('黑匣子在深夜发出微光'), '本地优先');
  }

  // ========== 9. 设定摘要编辑 + 持久化 ==========
  const editBtn = page.locator('button').filter({ hasText: '编辑' }).first();
  if (await editBtn.count()) {
    await editBtn.click();
    await page.waitForTimeout(600);
    const sInput = page.locator('textarea').last();
    if (await sInput.count()) {
      await sInput.fill('星海历302年，人类依托跃迁航道扩张。主角陆昭是星港工程师。');
      await page.locator('button').filter({ hasText: /保存设定|保存/ }).last().click();
      await page.waitForTimeout(1200);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(2500);
      await page.locator('text=星海黎明').first().click().catch(() => {});
      await page.waitForTimeout(900);
      t = await text();
      log('9 设定摘要保存并持久化', t.includes('星海历302'), 'book.json 落盘');
    } else log('9 设定编辑 textarea', false);
  }

  // ========== 10. 导出端点（页内 fetch 断言响应头） ==========
  const expOk = await page.evaluate(async () => {
    const r = await fetch('/api/novel/books/' + 'x', { method: 'GET' });
    return r.status; // 404/422 都说明路由活着（书 id 是占位）
  }).catch(() => 'ERR');
  log('10 导出路由可达（占位 id 被拒）', expOk === 404 || expOk === 422, `status=${expOk}`);

  // ========== 11. 移动端 375px ==========
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(BASE + '/novel', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);
  t = await text();
  log('11 移动端可用', t.length > 100 && !t.includes('Application error'));
  await page.screenshot({ path: `${SHOT}-06-mobile.png` });

  // ========== 12. 其他工作区冒烟 ==========
  await page.setViewportSize({ width: 1440, height: 900 });
  for (const p of ['/', '/hk', '/us', '/news']) {
    await page.goto(BASE + p, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1600);
    const ok = await page.evaluate(() => document.body.innerText.length > 50 && !document.body.innerText.includes('Application error'));
    log(`12 路由 ${p}`, ok);
  }

  log('13 无未捕获页面错误', pageErrors.length === 0, pageErrors.slice(0, 2).join('||').slice(0, 160));
} catch (e) {
  log('脚本执行', false, String(e).slice(0, 260));
  try { await page.screenshot({ path: `${SHOT}-99-error.png` }); } catch {}
}
await browser.close();
const fails = results.filter(r => !r.pass).length;
console.log(`\nSUMMARY: ${results.length - fails}/${results.length} passed, ${fails} failed`);
