// P0 专项 A：reset 关旧 SSE（旧会话事件不串流进新会话 + 新会话建起自己的 SSE）
// P0 专项 B：reportLinks 的 files.pdf 转义（注入不生效）
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }

let startCount = 0;
const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

await page.route('**/*', async (route) => {
  const req = route.request();
  const url = req.url();
  if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
  if (url.includes('/api/v4/stream/')) {
    const sid = decodeURIComponent(url.split('/api/v4/stream/')[1]);
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body:
      'event: turn\ndata: {"state":"done","seq":' + (900 + startCount) + ',"reply":"turn-from-' + sid + '"}\n\n:\n' });
  }
  if (url.includes('/api/v4/start') && req.method() === 'POST') {
    startCount += 1;
    return route.fulfill({ json: { session_id: 'p0-sid-' + startCount, reply: '会话 ' + startCount + ' 开始', mode: { llm_mode: 'mock' } } });
  }
  if (url.includes('/api/v4/restore')) {
    const pdf = '"><img src=x onerror="window.__xss=1">.pdf';
    return route.fulfill({ json: {
      ok: true,
      history: [{ role: 'user', content: '你好' }, { role: 'assistant', content: '你好呀' }],
      widgets: [], delegations: {}, report: { status: 'final', files: { pdf: pdf }, m5: {}, m6: {} }, film: false,
      mode: { llm_mode: 'mock' },
    } });
  }
  if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
  if (url.includes('/api/v4/export/')) return route.fulfill({
    status: 200,
    contentType: 'application/x-ndjson',
    headers: { 'Content-Disposition': 'attachment; filename="ip12-test.jsonl"' },
    body: '{"type":"session"}\n',
  });
  if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, report: null, film: false } });
  if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
  if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 1, reply: '' } });
  if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
  return route.continue();
});

// ===== A：初始会话 s1 的 SSE turn 事件 =====
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => {
  const t = document.body.textContent || '';
  return t.includes('turn-from-p0-sid-1');
}, null, { timeout: 10000 });

// ===== A：reset 后新会话 s2 应建起自己的 SSE；旧 s1 事件不得再进（含 EventSource 自动重连的重播）=====
await page.click('#reset-btn');
await page.waitForFunction(() => {
  const t = document.body.textContent || '';
  return t.includes('会话 2 开始') && t.includes('turn-from-p0-sid-2');
}, null, { timeout: 10000 });
await page.waitForTimeout(4500); // 等过 EventSource 默认重连周期（~3s），旧连接若未关会重播 s1 事件
const s1Count = await page.evaluate(() => (document.body.textContent.match(/turn-from-p0-sid-1/g) || []).length);
assert(s1Count <= 1, 'reset 后旧会话事件不重播（turn-from-p0-sid-1 出现 ' + s1Count + ' 次，应 <=1）');
assert((await page.evaluate(() => document.body.textContent)).includes('turn-from-p0-sid-2'),
  'reset 后新会话自己的 SSE 事件正常到达');

// ===== B：注入文件名渲染进报告栏，XSS 不得执行 =====
await page.evaluate(() => localStorage.setItem('hq-v4-session-id', 'xss-test-1'));
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => document.querySelector('.report-link'), null, { timeout: 8000 });
const injected = await page.evaluate(() => window.__xss);
assert(injected === undefined, '报告 PDF 链接注入不执行（window.__xss 未定义）');
const outer = await page.evaluate(() => document.querySelector('.report-link').outerHTML);
const imgInjected = await page.evaluate(() => document.querySelectorAll('#report-pane img').length);
assert(outer.indexOf('&quot;') >= 0 && imgInjected === 0,
  '报告链接已转义且无注入标签（outerHTML=' + outer + ', imgs=' + imgInjected + '）');

// ===== C：导出按钮下载 JSONL，不需要用户去服务器找文件 =====
const downloadPromise = page.waitForEvent('download');
await page.click('#export-btn');
const download = await downloadPromise;
assert(download.suggestedFilename() === 'ip12-test.jsonl', '导出按钮下载 JSONL 文件');

console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
