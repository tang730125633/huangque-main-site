// 真实模型免扣点复测：澄清不污染画像 + 制作参数跨轮保存。
import fs from 'node:fs';
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'https://huangquechuanmei.com/workbench/ip12/';
const SID_OUT = process.env.HQ_SID_OUT || '/tmp/hq-prod-partial-issues-sid';

async function stdinText() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8').trim();
}

const cookie = process.env.HQ_AUTH_COOKIE_STDIN === '1' ? await stdinText() : '';
if (!cookie) process.exit(2);

const browser = await chromium.launch({
  executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
});
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
await context.addCookies([{
  name: 'hq_session', value: cookie, domain: 'huangquechuanmei.com', path: '/',
  secure: true, httpOnly: true, sameSite: 'Lax',
}]);
const page = await context.newPage();
const errors = [];
page.on('pageerror', (error) => errors.push(error.message));

async function sendAndWait(text) {
  const before = await page.locator('.msg.assistant .bubble:not(.status-bubble)').count();
  await page.locator('#input').fill(text);
  await page.locator('#send-btn').click();
  await page.waitForFunction((count) => {
    const bubbles = [...document.querySelectorAll('.msg.assistant .bubble:not(.status-bubble)')];
    return bubbles.length > count && (bubbles[bubbles.length - 1].textContent || '').trim().length > 0;
  }, before, { timeout: 180000 });
  return page.locator('.msg.assistant .bubble:not(.status-bubble)').last().innerText();
}

await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
await page.waitForFunction(() => /^[0-9a-f]{32}$/.test(localStorage.getItem('hq-v4-session-id') || ''), null,
  { timeout: 30000 });
const sid = await page.evaluate(() => localStorage.getItem('hq-v4-session-id'));
fs.writeFileSync(SID_OUT, sid, { mode: 0o600 });
await page.waitForFunction(() => document.querySelectorAll('.msg.assistant .bubble:not(.status-bubble)').length > 0,
  null, { timeout: 180000 });

const clarification = await sendAndWait('我没听懂你刚才的问题，能先举个例子说明为什么要问吗？这不是我的正式答案。');
const first = await sendAndWait('我想做一条抖音 10 秒数字人口播，先不要报价，也不要生成。');
const second = await sendAndWait('CTA 要点赞关注，内容讲猫窝和自动喂食器两个坑。继续保存方案，但仍然不要报价和生成。');

console.log(JSON.stringify({
  session_created: /^[0-9a-f]{32}$/.test(sid),
  clarification_replied: clarification.length > 0,
  first_turn_replied: first.length > 0,
  second_turn_replied: second.length > 0,
  page_errors: errors.length,
}));
await browser.close();
process.exit(errors.length ? 1 : 0);
