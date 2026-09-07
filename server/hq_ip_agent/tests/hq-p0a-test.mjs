// A 批专项：① 双通道乱序按 seq 排序 ② SSE 断线固定降级 HTTPS 轮询 ③ 交付断线补偿 ④ 长历史批量渲染
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });

// ================= 场景①：连发两条，后发先回 → 渲染仍按序 =================
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  const png = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082', 'hex');
  let releaseEvents = null;
  const eventsGate = new Promise((r) => { releaseEvents = r; });
  await page.route('**/*', async (route) => {
    const req = route.request();
    const url = req.url();
    if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
    if (url.includes('/api/v4/chat')) {
      const body = JSON.parse(req.postData() || '{}');
      if (body.message.includes('第一条')) return route.fulfill({ json: { async: true, seq: 1001, reply: '' } });
      return route.fulfill({ json: { async: true, seq: 1002, reply: '' } });
    }
    if (url.includes('/api/v4/stream/')) {
      await eventsGate; // 等两条消息都发出后再放 turn 事件（模拟真实完成时序）
      // 后发的 1002 先完成（SSE 完成序），1001 随后
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body:
        'event: turn\ndata: {"state":"done","seq":1002,"reply":"第二条的回复"}\n\n' +
        'event: turn\ndata: {"state":"done","seq":1001,"reply":"第一条的回复"}\n\n:\n' }).catch(() => {});
    }
    if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, report: null, film: false, deliveries: [] } });
    if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
    if (url.includes('/api/v4/start') && req.method() === 'POST') return route.fulfill({ json: { session_id: 'p0a-1', reply: '开始', mode: { llm_mode: 'mock' } } });
    if (url.includes('/api/v4/restore')) return route.fulfill({ json: { ok: false, error: 'x' } });
    if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
    if (url.includes('/api/v4/media/')) return route.fulfill({ status: 200, contentType: 'image/png', body: png });
    if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
    return route.continue();
  });
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelector('#composer') && !document.getElementById('input').disabled, null, { timeout: 8000 });
  // 连发两条（此时两个 seq 都已登记在途）
  await page.fill('#input', '第一条');
  await page.press('#input', 'Enter');
  await page.waitForTimeout(200);
  await page.fill('#input', '第二条');
  await page.press('#input', 'Enter');
  await page.waitForTimeout(400);
  releaseEvents(); // 放开 SSE 事件：1002 先到、1001 后到
  // 等两条回复都渲染
  await page.waitForFunction(() => {
    const t = document.body.textContent || '';
    return t.includes('第一条的回复') && t.includes('第二条的回复');
  }, null, { timeout: 10000 });
  const order = await page.evaluate(() => {
    const bubbles = [...document.querySelectorAll('.msg.assistant .bubble:not(.status-bubble)')].map((n) => n.textContent);
    const i1 = bubbles.findIndex((b) => b === '第一条的回复');
    const i2 = bubbles.findIndex((b) => b === '第二条的回复');
    return { i1: i1, i2: i2 };
  });
  assert(order.i1 >= 0 && order.i2 >= 0 && order.i1 < order.i2,
    '后发先回仍按 seq 排序渲染（第一条 index=' + order.i1 + ' < 第二条 index=' + order.i2 + '）');
  await page.close();
}

// ================= 场景②：SSE 断线 → 固定降级 HTTPS 轮询，不再重连 =================
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  const streamLog = [];
  let statusPolls = 0;
  await page.route('**/*', async (route) => {
    const req = route.request();
    const url = req.url();
    if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
    if (url.includes('/api/v4/stream/')) {
      streamLog.push(Date.now());
      return route.fulfill({ status: 500, body: 'fail' });
    }
    if (url.includes('/api/v4/status')) {
      statusPolls += 1;
      return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, report: null, film: false, deliveries: [] } });
    }
    if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
    if (url.includes('/api/v4/start') && req.method() === 'POST') return route.fulfill({ json: { session_id: 'p0a-2', reply: '开始', mode: { llm_mode: 'mock' } } });
    if (url.includes('/api/v4/restore')) return route.fulfill({ json: { ok: false, error: 'x' } });
    if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 1, reply: '' } });
    if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
    if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
    return route.continue();
  });
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(7000);
  assert(streamLog.length === 1, 'SSE 断线后不再重连（连接次数 ' + streamLog.length + '）');
  assert(statusPolls >= 1, 'SSE 断线后改走 HTTPS 状态轮询（请求次数 ' + statusPolls + ' ≥ 1）');
  await page.close();
}

// ================= 场景③：SSE 掉线窗口内完成的交付，status 帧补贴图 =================
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  const png = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082', 'hex');
  await page.route('**/*', async (route) => {
    const req = route.request();
    const url = req.url();
    if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
    if (url.includes('/api/v4/stream/')) return route.fulfill({ status: 500, body: 'fail' }); // SSE 全程断线
    if (url.includes('/api/v4/status')) {
      return route.fulfill({ json: {
        turns: [], jobs: [], delegations: {}, report: null, film: false,
        deliveries: [{ reply: '📥 采集完成：图片在下面 👇', images: ['api/v4/media/s/9/img_01.png'], job: 9 }],
      } });
    }
    if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
    if (url.includes('/api/v4/start') && req.method() === 'POST') return route.fulfill({ json: { session_id: 'p0a-3', reply: '开始', mode: { llm_mode: 'mock' } } });
    if (url.includes('/api/v4/restore')) return route.fulfill({ json: { ok: false, error: 'x' } });
    if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 1, reply: '' } });
    if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
    if (url.includes('/api/v4/media/')) return route.fulfill({ status: 200, contentType: 'image/png', body: png });
    if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
    return route.continue();
  });
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  // SSE 断了（500）→ statusTimer 轮询（2s）→ 第一帧就应补贴交付消息
  await page.waitForFunction(() => (document.body.textContent || '').includes('采集完成：图片在下面'), null, { timeout: 12000 });
  await page.waitForFunction(() => document.querySelectorAll('.msg-images').length > 0, null, { timeout: 8000 });
  // 再等几帧，确认不重复贴
  await page.waitForTimeout(6000);
  const cnt = await page.evaluate(() => (document.body.textContent.match(/采集完成：图片在下面/g) || []).length);
  assert(cnt === 1, '断线窗口交付由 status 帧补出且只贴一次（出现 ' + cnt + ' 次）');
  await page.close();
}

// ================= 场景④：长历史（800 条）批量渲染，无逐条滚动卡顿 =================
{
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  const history = [];
  for (let i = 0; i < 400; i++) {
    history.push({ role: 'user', content: '问题 ' + i });
    history.push({ role: 'assistant', content: '回答 ' + i + '：这是一段比较长的回答内容，用于验证批量渲染。' });
  }
  await page.route('**/*', async (route) => {
    const req = route.request();
    const url = req.url();
    if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
    if (url.includes('/api/v4/restore')) return route.fulfill({ json: {
      ok: true, history: history, widgets: [], delegations: {}, report: null, film: false, mode: { llm_mode: 'mock' },
    } });
    if (url.includes('/api/v4/stream/')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
    if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, report: null, film: false, deliveries: [] } });
    if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
    if (url.includes('/api/v4/start') && req.method() === 'POST') return route.fulfill({ json: { session_id: 'p0a-4', reply: '开始', mode: { llm_mode: 'mock' } } });
    if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 1, reply: '' } });
    if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
    if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
    return route.continue();
  });
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => localStorage.setItem('hq-v4-session-id', 'p0a-4'));
  const t0 = Date.now();
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('.msg').length >= 800, null, { timeout: 15000 });
  const renderMs = Date.now() - t0;
  const lastVisible = await page.evaluate(() => {
    const bubbles = [...document.querySelectorAll('.msg.assistant .bubble:not(.status-bubble)')];
    return bubbles[bubbles.length - 1].textContent;
  });
  assert(renderMs < 6000, '800 条历史批量渲染不卡顿（' + renderMs + 'ms < 6s）');
  assert(lastVisible.includes('回答 399'), '末尾消息渲染正确（' + lastVisible.slice(0, 20) + '）');
  await page.close();
}

console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
