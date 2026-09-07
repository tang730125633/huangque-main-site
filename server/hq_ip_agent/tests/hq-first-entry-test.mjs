// Regression: first-entry controls must queue until /start returns session_id.
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');
const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const errors = [];
const results = {};

for (const scenario of ['example-card', 'send-button', 'enter-key']) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const chats = [];
  let polls = 0;
  page.on('pageerror', (error) => errors.push(scenario + ': ' + error.message));
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = request.url();
    if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
    if (url.includes('/api/v4/start')) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      return route.fulfill({ json: { session_id: 'a'.repeat(32), async: true, seq: 1, mode: 'mock' } });
    }
    if (url.includes('/api/v4/chat')) {
      chats.push(request.postDataJSON());
      return route.fulfill({ json: { async: true, seq: 2 } });
    }
    if (url.includes('/api/v4/poll/')) {
      polls += 1;
      return route.fulfill({ json: polls === 1
        ? { state: 'done', seq: 1, reply: '开场完成' }
        : { state: 'done', seq: 2, reply: '首条消息已收到' } });
    }
    if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', hq_status: { ok: true } } });
    if (url.includes('/api/report/')) return route.fulfill({ status: 404, json: {} });
    if (url.includes('/api/v4/status/')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, report: null } });
    if (url.includes('/api/v4/stream/')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n\n' });
    return route.continue();
  });
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.locator('#send-btn').waitFor({ state: 'visible' });
  if (scenario === 'example-card') await page.locator('.example-card').first().click();
  if (scenario === 'send-button') {
    await page.locator('#input').fill('按钮首条');
    await page.locator('#send-btn').click();
  }
  if (scenario === 'enter-key') {
    await page.locator('#input').fill('回车首条');
    await page.locator('#input').press('Enter');
  }
  await page.waitForFunction(() => (document.body.textContent || '').includes('首条消息已收到'), null, { timeout: 8000 });
  results[scenario] = chats.length === 1 && chats[0].session_id === 'a'.repeat(32);
  await page.close();
}

for (const [name, pass] of Object.entries(results)) console.log(JSON.stringify({ [name]: pass ? 'PASS' : 'FAIL' }));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(Object.values(results).every(Boolean) && errors.length === 0 ? 0 : 1);
