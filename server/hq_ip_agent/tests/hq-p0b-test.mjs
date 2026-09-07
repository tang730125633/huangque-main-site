// P0b 专项：① 出错轮不动报告栏与出片货架 ② 恢复失败给重试/开新选择，不覆盖 sid ③ 卡死任务超时收工
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });

// ================= 场景①：出错轮不动报告栏与出片卡 =================
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.route('**/*', async (route) => {
    const req = route.request();
    const url = req.url();
    if (url.includes('/api/v4/restore')) {
      return route.fulfill({ json: {
        ok: true,
        history: [{ role: 'user', content: '做我的数字人口播' }, { role: 'assistant', content: '好的，形象和音色卡片在下面' }],
        widgets: [{ type: 'avatar_pick', id: 'avatar_pick:video-avatars', title: '数字人形象（点选使用）',
          items: [{ id: 'a1', name: '本人形象', image_url: 'https://x.com/a.jpg' }] }],
        delegations: {},
        report: { status: 'final', files: { pdf: 'api/download/report.pdf' }, m5: {}, m6: {} },
        film: true,
        mode: { llm_mode: 'mock' },
      } });
    }
    if (url.includes('/api/v4/stream/')) {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body:
        'event: turn\ndata: {"state":"error","seq":901,"reply":"这轮处理出错了（KeyError）。你可以再发一次试试。"}\n\n:\n' }).catch(() => {});
    }
    if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, report: null, film: false } });
    if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
    if (url.includes('/api/v4/start') && req.method() === 'POST') return route.fulfill({ json: { session_id: 'p0b-1', reply: '开始', mode: { llm_mode: 'mock' } } });
    if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 1, reply: '' } });
    if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
    if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
    return route.continue();
  });
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => localStorage.setItem('hq-v4-session-id', 'p0b-1'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.widget-card').length > 0 && !document.getElementById('report-pane').hidden, null, { timeout: 8000 });
  await page.waitForFunction(() => (document.body.textContent || '').includes('这轮处理出错了'), null, { timeout: 8000 });
  const r1 = await page.evaluate(() => ({
    reportHidden: document.getElementById('report-pane').hidden,
    widgetCards: document.querySelectorAll('.widget-card').length,
  }));
  assert(!r1.reportHidden, '出错后报告栏仍然可见（reportHidden=' + r1.reportHidden + '）');
  assert(r1.widgetCards > 0, '出错后出片卡仍在（widgetCards=' + r1.widgetCards + '）');
  await page.close();
}

// ================= 场景②：恢复失败 → 重试 UI，不覆盖 sid =================
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  let restoreN = 0;
  await page.route('**/*', async (route) => {
    const req = route.request();
    const url = req.url();
    if (url.includes('/api/v4/restore')) {
      restoreN += 1;
      if (restoreN === 1) return route.fulfill({ status: 500, body: 'boom' });
      return route.fulfill({ json: {
        ok: true,
        history: [{ role: 'user', content: '你好' }, { role: 'assistant', content: '欢迎回来，你的记录都在。' }],
        widgets: [], delegations: {}, report: null, film: false, mode: { llm_mode: 'mock' },
      } });
    }
    if (url.includes('/api/v4/stream/')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
    if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, report: null, film: false } });
    if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
    if (url.includes('/api/v4/start') && req.method() === 'POST') return route.fulfill({ json: { session_id: 'p0b-2', reply: '开始', mode: { llm_mode: 'mock' } } });
    if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 1, reply: '' } });
    if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
    if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
    return route.continue();
  });
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => localStorage.setItem('hq-v4-session-id', 'p0b-2'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.restore-retry-msg').length > 0, null, { timeout: 8000 });
  const sidKept = await page.evaluate(() => localStorage.getItem('hq-v4-session-id'));
  assert(sidKept === 'p0b-2', '恢复失败后 sid 未被覆盖（实际 ' + sidKept + '）');
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('.restore-retry-btn')];
    btns.find((b) => b.textContent.includes('重试')).click();
  });
  await page.waitForFunction(() => (document.body.textContent || '').includes('欢迎回来，你的记录都在。'), null, { timeout: 8000 });
  const retryGone = await page.evaluate(() => document.querySelectorAll('.restore-retry-msg').length === 0);
  assert(retryGone, '重试成功后重试气泡消失');
  await page.close();
}

// ================= 场景③：卡死任务超时收工（阈值 3s） =================
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.addInitScript(() => { window.HQ_JOB_TIMEOUT_MS = 3000; });
  await page.route('**/*', async (route) => {
    const req = route.request();
    const url = req.url();
    if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: ['m5_topics'], delegations: {}, report: null, film: false } });
    if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
    if (url.includes('/api/v4/stream/')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
    if (url.includes('/api/v4/start') && req.method() === 'POST') return route.fulfill({ json: { session_id: 'p0b-3', reply: '开始', mode: { llm_mode: 'mock' } } });
    if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 1, reply: '' } });
    if (url.includes('/api/v4/restore')) return route.fulfill({ json: { ok: false, error: 'x' } });
    if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
    if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
    return route.continue();
  });
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.getElementById('status-bubble'), null, { timeout: 10000 });
  await page.waitForFunction(() => !document.getElementById('status-bubble'), null, { timeout: 10000 });
  const msg = await page.evaluate(() => (document.body.textContent || '').includes('超过 15 分钟还没结束'));
  assert(msg, '卡死任务超时后气泡撤除并提示（不再无限转圈）');
  await page.close();
}

console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
