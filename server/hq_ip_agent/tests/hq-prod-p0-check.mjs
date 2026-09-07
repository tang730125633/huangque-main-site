// 线上 P0 回归：网站登录态 → 客户会话 → 客户 CLI 免费读取 → 刷新恢复。
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'https://huangquechuanmei.com/workbench/ip12/';
const errors = [];

async function readCookieFromStdin() {
  if (process.env.HQ_AUTH_COOKIE_STDIN !== '1') return '';
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8').trim();
}

const sessionCookie = await readCookieFromStdin();
if (!sessionCookie) {
  console.error('ERROR: provide a short-lived website session through HQ_AUTH_COOKIE_STDIN=1');
  process.exit(2);
}

const browser = await chromium.launch({
  executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
});
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
await context.addCookies([{
  name: 'hq_session', value: sessionCookie,
  domain: 'huangquechuanmei.com', path: '/', secure: true, httpOnly: true, sameSite: 'Lax',
}]);
const page = await context.newPage();
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
await page.waitForFunction(() => /^[0-9a-f]{32}$/.test(localStorage.getItem('hq-v4-session-id') || ''), null,
  { timeout: 30000 });
const sid = await page.evaluate(() => localStorage.getItem('hq-v4-session-id'));

await page.waitForFunction(() => {
  const bubbles = [...document.querySelectorAll('.msg.assistant .bubble:not(.status-bubble)')];
  return bubbles.some((node) => (node.textContent || '').trim().length > 0);
}, null, { timeout: 180000 });

const before = await page.locator('.msg.assistant .bubble:not(.status-bubble)').count();
await page.locator('#input').fill('我还有多少点数？只查询，不要生成任何内容。');
await page.locator('#send-btn').click();
await page.waitForFunction((count) => {
  const bubbles = [...document.querySelectorAll('.msg.assistant .bubble:not(.status-bubble)')];
  if (bubbles.length <= count) return false;
  const text = (bubbles[bubbles.length - 1].textContent || '').trim();
  return text.length > 0 && !text.includes('出错了') && !text.includes('公共账号');
}, before, { timeout: 180000 });

const reply = await page.locator('.msg.assistant .bubble:not(.status-bubble)').last().innerText();
await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });
await page.waitForFunction((expected) => localStorage.getItem('hq-v4-session-id') === expected, sid,
  { timeout: 30000 });
await page.waitForFunction(() => (document.body.textContent || '').includes('我还有多少点数'), null,
  { timeout: 30000 });
await page.waitForFunction(() => {
  const badge = document.querySelector('#hq-badge');
  return badge && (badge.textContent || '').includes('能力随当前账号');
}, null, { timeout: 30000 });

const badge = await page.locator('#hq-badge').innerText();
const ok = /^[0-9a-f]{32}$/.test(sid) && reply.length > 0 &&
  badge.includes('能力随当前账号') && errors.length === 0;
console.log('STATS:', JSON.stringify({
  session_created: Boolean(sid),
  free_cli_reply: Boolean(reply),
  restored_same_session: true,
  customer_scoped_badge: badge.includes('能力随当前账号'),
}, null, 2));
console.log(ok ? 'PROD_P0_REGRESSION: PASS' : 'PROD_P0_REGRESSION: FAIL');
console.log('PAGE_ERRORS:', errors.length ? errors : 'none');
await page.screenshot({ path: '/tmp/hq-prod-p0-check.png', fullPage: false });
// 清掉本次测试会话和它的 CLI grant，再注销 120 秒网站测试会话。
await page.evaluate(async (sessionId) => {
  await fetch('api/v4/reset', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
  });
  await fetch('/api/auth/logout', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  });
}, sid).catch(() => {});
await browser.close();
process.exit(ok ? 0 : 1);
