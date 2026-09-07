// 附件专项（#5/#29）：+号选文件→预览暂存→随文字发送；断网明示拦截、恢复不自动补跑
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

let chatBodies = [];
let uploadCount = 0;
await page.route('**/*', async (route) => {
  const req = route.request();
  const url = req.url();
  if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
  if (url.includes('/api/v4/start') && req.method() === 'POST') {
    return route.fulfill({ json: { session_id: 'attach-test-1', reply: '测试开始', mode: { llm_mode: 'mock', llm_model: 'test' } } });
  }
  if (url.includes('/api/v4/restore')) return route.fulfill({ json: { ok: false } });
  if (url.includes('/api/v4/upload')) {
    uploadCount++;
    return route.fulfill({ json: { ok: true, file_id: 'att-' + uploadCount, name: '测试图.png', url: 'api/v4/media/att-1.png', kind: 'image' } });
  }
  if (url.includes('/api/v4/chat')) {
    chatBodies.push(req.postDataJSON());
    return route.fulfill({ json: { async: true, seq: 901, reply: '' } });
  }
  if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, film: false, tool: null, report: null } });
  if (url.includes('/api/v4/stream')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
  if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
  if (url.includes('/api/v4/media/')) {
    const png = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082', 'hex');
    return route.fulfill({ status: 200, contentType: 'image/png', body: png });
  }
  if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
  return route.continue();
});

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(() => localStorage.setItem('hq-v4-session-id', 'attach-test-1'));
await page.reload({ waitUntil: 'domcontentloaded' });

// Node 侧等待（闭包变量只能在 Node 里读，page.waitForFunction 拿不到）
async function waitFor(cond, timeout = 8000) {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > timeout) return false;
    await new Promise((r) => setTimeout(r, 100));
  }
  return true;
}

// ===== 场景 A：+号选图片 → 预览暂存出现（带 ✕ 可移除），文字+附件一起发送 =====
await page.setInputFiles('#file-input', { name: '测试图.png', mimeType: 'image/png', buffer: Buffer.from('89504e47', 'hex') });
await page.waitForSelector('.upload-chip', { timeout: 8000 });
assert(await page.$('.upload-chip img') !== null, '预览暂存：图片缩略图出现');
await page.fill('#input', '帮我看看这张图');
await page.click('#send-btn');
assert(await waitFor(() => chatBodies.length >= 1), '发送请求已到达后端');
const b1 = chatBodies[0];
assert(b1 && b1.message.includes('帮我看看这张图') && b1.message.includes('附件：图片：测试图.png'),
  '发送：消息带附件标注（' + JSON.stringify(b1 && b1.message) + '）');
assert(b1 && Array.isArray(b1.attachments) && b1.attachments[0] === 'att-1',
  '发送：attachments 里带 file_id 进后端 payload');
// 发送后预览卸空
await page.waitForFunction(() => document.querySelectorAll('.upload-chip').length === 0, null, { timeout: 5000 });
assert(true, '发送后预览暂存区清空');
await page.waitForFunction(() => document.body.innerText.includes('附件：图片：测试图.png'), null, { timeout: 5000 });
assert(true, '用户消息气泡里可见附件标注');

// ===== 场景 B：不支持格式当场提示，不发请求 =====
const beforeChat = chatBodies.length;
await page.setInputFiles('#file-input', { name: '说明.txt', mimeType: 'text/plain', buffer: Buffer.from('x') });
await page.waitForFunction(() => document.body.innerText.includes('格式不支持'), null, { timeout: 5000 });
assert(chatBodies.length === beforeChat, 'txt 被客户端预检拦下（不发后端）');

// ===== 场景 C（#29）：断网时发送被拦，明确提示且不写气泡、不补跑 =====
const beforeChat2 = chatBodies.length;
await context.setOffline(true);
await page.waitForFunction(() => document.getElementById('net-banner') && !document.getElementById('net-banner').hidden, null, { timeout: 5000 });
assert(true, '断网：输入区上方出现提示条');
const bannerText = await page.$eval('#net-banner', (n) => n.textContent);
assert(bannerText.includes('不会自动补发'), '断网提示明说「不会自动补发」');
await page.fill('#input', '断网时这条');
await page.click('#send-btn');
await page.waitForFunction(() => document.body.innerText.includes('还没有发出去'), null, { timeout: 5000 });
assert(true, '断网发送：被拦下并明确告知「还没有发出去」');
await new Promise((r) => setTimeout(r, 1500));
assert(chatBodies.length === beforeChat2, '断网发送：没有请求发出（0 次）');

// ===== 场景 D（#29）：恢复网络 → 提示条提醒手动重发，不自动补跑 =====
await context.setOffline(false);
await page.waitForFunction(() => document.getElementById('net-banner') && document.getElementById('net-banner').textContent.includes('网络已恢复'), null, { timeout: 5000 });
assert(true, '恢复：提示条出现「网络已恢复」提醒手动重发');
await new Promise((r) => setTimeout(r, 2000));
assert(chatBodies.length === beforeChat2, '恢复后：没有悄悄自动补跑（请求数不变）');

console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
