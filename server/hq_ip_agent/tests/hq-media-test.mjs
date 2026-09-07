// 后台成果交付专项：历史恢复带图 + SSE 实时贴图/普通任务终态
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }

let deliveryPushed = false;
const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

await page.route('**/*', async (route) => {
  const req = route.request();
  const url = req.url();
  if (url.includes('/api/v4/restore')) {
    return route.fulfill({ json: {
      ok: true,
      history: [
        { role: 'user', content: '帮我扒这篇小红书笔记' },
        { role: 'assistant', content: '📥 《漂亮是一种感觉》扒好了：图片在下面 👇（已存到本地，链接不过期）。',
          images: ['api/v4/media/s/7681/img_01.jpg', 'api/v4/media/s/7681/img_02.jpg'] },
      ],
      widgets: [], delegations: {}, report: null, film: false,
      mode: { llm_mode: 'mock', llm_model: 'test' },
    } });
  }
  if (url.includes('/api/v4/media/')) {
    // 1x1 png
    const png = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082', 'hex');
    return route.fulfill({ status: 200, contentType: 'image/png', body: png });
  }
  if (url.includes('/api/v4/stream')) {
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body:
      'event: delivery\ndata: {"reply":"📥 后台采集完成：图片已贴出 👇","images":["api/v4/media/s/9999/img_01.jpg"]}\n\n' +
      'event: delivery\ndata: {"reply":"任务 7622 ✅ 已完成。\\nhttps://example.test/result.mp4","images":[]}\n\n:\n' }).catch(() => {});
  }
  if (url.includes('/api/v4/start') && req.method() === 'POST') {
    return route.fulfill({ json: { session_id: 'media-test-1', reply: '测试开始', mode: { llm_mode: 'mock', llm_model: 'test' } } });
  }
  if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, film: false, tool: null, report: null } });
  if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 900, reply: '' } });
  if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
  if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
  if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
  return route.continue();
});

// ===== 场景 A：恢复历史里带 images 的助手消息 → 图片贴条渲染 =====
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(() => localStorage.setItem('hq-v4-session-id', 'media-test-1'));
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => document.querySelectorAll('.msg-images').length > 0, null, { timeout: 8000 });
const imgs = await page.$$eval('.msg-images img', (ns) => ns.map((n) => n.getAttribute('src')));
assert(imgs.length >= 2 && imgs.every((s) => s.includes('api/v4/media/')), '恢复历史：带图消息渲染本地图（' + JSON.stringify(imgs) + '）');
// 图片真实加载成功（后端返回 200 的 1x1 png）
await page.waitForFunction(() => {
  const i = document.querySelector('.msg-images img');
  return i && i.complete && i.naturalWidth > 0;
}, null, { timeout: 5000 });
const loaded = await page.$eval('.msg-images img', (n) => n.naturalWidth > 0);
assert(loaded, '图片内容真实加载成功（不是裂图）');

// ===== 场景 B：SSE delivery 事件 → 实时贴出新气泡 + 图片 =====
await page.waitForFunction(() => document.querySelectorAll('.msg-images').length >= 2, null, { timeout: 8000 });
const bubbles = await page.$$eval('.msg.assistant .bubble:not(.status-bubble)', (ns) => ns.map((n) => n.textContent));
assert(bubbles.some((t) => t.includes('后台采集完成')), 'SSE delivery：新带图气泡实时出现');
const allImgs = await page.$$eval('.msg-images img', (ns) => ns.length);
assert(allImgs >= 3, 'SSE delivery：贴条图片总数 >= 3（实际 ' + allImgs + '）');
assert(bubbles.some((t) => t.includes('任务 7622') && t.includes('已完成')),
  'SSE delivery：普通后台任务终态实时进入对话');
assert(await page.locator('.msg.assistant video').count() >= 1,
  'SSE delivery：成品视频链接直接渲染为播放器');
await page.screenshot({ path: '/tmp/hq-media-delivery.png', fullPage: false });

console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
