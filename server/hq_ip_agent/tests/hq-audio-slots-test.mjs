// 声音克隆替换槽位卡专项（#30）：audio 域注册的 voice_pick（film=false）在非出片轮也要渲染、
// 用户点选后随下一条消息以「【点选】」开头发回后端（id=slot_id），且不弹「出片配置卡」。
// 回归：出片轮只渲染 film 卡（出片货架意图门控不破）、非出片轮不渲染出片卡。
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const out = {};
function assert(cond, name, extra) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name + (extra ? ' ' + extra : '')); }

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

const SLOT_A = 'slot_b3bb9b800b6c4cf99b467352cf2b4089';
const SLOT_B = 'slot_5d5b8e9f63c6484592eaeb216c38f0e3';

const widgets = [
  { // audio 域声音克隆槽位卡：film=false
    type: 'voice_pick', id: 'voice_pick:audio-slots', title: '声音克隆槽位（点选使用，▶ 可试听）',
    hint: '点击卡片即可选中，无需打字。', gen: 1, film: false,
    items: [
      { id: SLOT_A, slot_id: SLOT_A, numeric_id: '17', name: '我的克隆音色', kind: 'clone',
        preview_url: 'https://example.com/a.mp3', created_at: 1788112455 },
      { id: SLOT_B, slot_id: SLOT_B, numeric_id: '9', name: '我的克隆音色', kind: 'clone',
        preview_url: 'https://example.com/b.mp3', created_at: 1788453993 },
    ],
  },
  { // 出片流程音色卡：film=true（非出片轮不应渲染）
    type: 'voice_pick', id: 'voice_pick:voices', title: '音色（点选使用，▶ 可试听）',
    hint: '点击卡片即可选中，无需打字。', gen: 1, film: true,
    items: [
      { id: 'voice-101', name: '晓晓', kind: 'voice', preview_url: 'https://example.com/v.mp3', created_at: 1700000000 },
    ],
  },
];

const chatBodies = [];
let restoreFilm = false; // 本轮 restore 的 film 标志（模拟后端轮次意图）
await page.route('**/*', async (route) => {
  const req = route.request();
  const url = req.url();
  if (url.includes('/api/v4/restore/') && req.method() === 'GET') {
    return route.fulfill({ json: {
      ok: true,
      history: [
        { role: 'assistant', content: '好的，我先看一下你现有的克隆槽位，卡片在下方，点选要替换的那个。' },
        { role: 'user', content: '我想替换我的克隆音色' },
      ],
      delegations: {},
      report: {},
      widgets: widgets,
      film: restoreFilm,
      mode: { llm_mode: 'mock', llm_model: 'test' },
    } });
  }
  if (url.includes('/api/v4/start') && req.method() === 'POST') {
    return route.fulfill({ json: { session_id: 'audio-slot-test-1', reply: '', mode: { llm_mode: 'mock', llm_model: 'test' } } });
  }
  if (url.includes('/api/v4/chat')) {
    chatBodies.push(req.postDataJSON());
    return route.fulfill({ json: { async: true, seq: 901, reply: '' } });
  }
  if (url.includes('/api/v4/selection')) return route.fulfill({ json: { ok: true } });
  if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, film: restoreFilm, tool: null, report: null } });
  if (url.includes('/api/v4/stream')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
  if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
  if (url.includes('/api/v4/media/')) {
    const png = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082', 'hex');
    return route.fulfill({ status: 200, contentType: 'image/png', body: png });
  }
  if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
  return route.continue();
});

// Node 侧等待
async function waitFor(cond, timeout = 8000) {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > timeout) return false;
    await new Promise((r) => setTimeout(r, 100));
  }
  return true;
}

// ===== 场景 A：非出片轮（film=false）也能渲染声音克隆槽位卡 =====
restoreFilm = false;
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(() => localStorage.setItem('hq-v4-session-id', 'audio-slot-test-1'));
await page.reload({ waitUntil: 'domcontentloaded' });
await waitFor(() => page.locator('.voice-panel').count() > 0, 8000);
assert(await page.locator('.voice-panel').count() >= 1, 'A① 非出片轮渲染了音色面板（槽位卡可见）');
assert(await page.locator('.voice-panel .widget-row').count() >= 2, 'A② 两个克隆槽位都渲染成可点选行');
await page.locator('.voice-panel .widget-head').click(); // 收起态点标题展开再读文本
await page.waitForFunction(() => {
  var b = document.querySelector('.voice-panel');
  return b && !b.classList.contains('collapsed');
}, null, { timeout: 5000 });
// 出片音色卡（film=true）在非出片轮不渲染
const panelText = await page.locator('.voice-panel').innerText();
assert(!panelText.includes('晓晓'), 'A③ 非出片轮不渲染出片音色卡（意图门控不破）');
// 槽位同名用创建日期区分
assert(panelText.includes('我的克隆音色'), 'A④ 槽位显示「我的克隆音色」可读名');
assert(panelText.includes('创建于'), 'A⑤ 同名槽位带创建日期区分');
// 不自动勾选：替换哪个槽位必须用户自己点
assert((await page.locator('.voice-panel .widget-row.picked').count()) === 0, 'A⑥ 槽位卡不自动勾选默认项');

// ===== 场景 B：点选槽位 → 不弹出片配置卡；下一条消息带【点选】+ slot_id =====
await page.locator('.voice-panel .widget-row').nth(0).locator('button.pick').click();
await page.waitForFunction(() => document.querySelectorAll('.voice-panel .widget-row.picked').length === 1, null, { timeout: 5000 });
assert(true, 'B① 点选后槽位行高亮「已选」');
await page.waitForTimeout(300);
assert((await page.locator('.summary-card').count()) === 0, 'B② 非出片点选不弹出「出片配置卡」');

await page.fill('#input', '就替换这个槽位，用新录音');
await page.press('#input', 'Enter');
assert(await waitFor(() => chatBodies.length >= 1), 'B③ 消息已发到后端');
const b1 = chatBodies[0];
assert(b1 && typeof b1.message === 'string' && b1.message.startsWith('【点选】'), 'B④ 消息以【点选】开头（' + (b1 && b1.message).slice(0, 60) + '）');
assert(b1 && b1.message.includes('id=' + SLOT_A), 'B⑤ 点选 id 就是 slot_id（可直接用于 voice-clone-*）');
assert(b1 && b1.message.includes('就替换这个槽位'), 'B⑥ 用户原话完整保留在消息里');
// 点选已消费：再发一条不带【点选】（等上一条发送收尾，避免 Enter 落在 streaming 窗口）
await page.waitForFunction(() => {
  var b = document.getElementById('send-btn');
  return b && !b.disabled;
}, null, { timeout: 8000 });
await page.fill('#input', '对了，那个录音要 mp3 格式吗？');
await page.press('#input', 'Enter');
assert(await waitFor(() => chatBodies.length >= 2), 'B⑦ 第二条消息已发到后端');
const b2 = chatBodies[1];
assert(!(b2 && b2.message.startsWith('【点选】')), 'B⑧ 点选只携带一次，不重复注入');

// ===== 场景 C：出片轮（film=true）渲染出片卡，非出片槽位卡不混入出片面板 =====
restoreFilm = true;
await page.reload({ waitUntil: 'domcontentloaded' });
await waitFor(() => page.locator('.voice-panel').count() > 0, 8000);
await page.locator('.voice-panel .widget-head').click(); // 展开再读文本
await page.waitForFunction(() => {
  var b = document.querySelector('.voice-panel');
  return b && !b.classList.contains('collapsed');
}, null, { timeout: 5000 });
const filmPanelText = await page.locator('.voice-panel').innerText();
assert(filmPanelText.includes('晓晓'), 'C① 出片轮渲染出片音色卡');
assert(!filmPanelText.includes('我的克隆音色'), 'C② 出片轮不把非出片槽位卡混进出片面板');

// ===== 场景 D：非出片轮转出片轮再转非出片，槽位卡不丢（会话级恢复） =====
restoreFilm = false;
await page.reload({ waitUntil: 'domcontentloaded' });
await waitFor(() => page.locator('.voice-panel').count() > 0, 8000);
await page.locator('.voice-panel .widget-head').click(); // 展开再读文本
await page.waitForFunction(() => {
  var b = document.querySelector('.voice-panel');
  return b && !b.classList.contains('collapsed');
}, null, { timeout: 5000 });
assert((await page.locator('.voice-panel').innerText()).includes('我的克隆音色'), 'D① 转回非出片轮后槽位卡恢复渲染');

await browser.close();
console.log(JSON.stringify(out, null, 2));
console.log('AUDIO_SLOTS: ' + (errors.length ? 'FAIL' : 'PASS'));
for (const e of errors) console.log('  ERROR:', e);
process.exit(errors.length ? 1 : 0);
