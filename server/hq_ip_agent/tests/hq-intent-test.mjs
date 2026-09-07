// 意图门控专项：出片区是「出片那条消息」的附件，闲聊即卸载（不是收起）
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const turnQueue = [];
let chatSeq = 0;
let lastFilm = false; // 模拟后端 get_last_film：最近一轮是否出片

const AVATARS = [
  { id: '536', name: '本人形象', image_url: 'https://example.com/a.jpg', status: 'ready' },
  { id: '535', name: '形象 19', image_url: 'https://example.com/b.jpg', status: 'ready' },
];
const VOICE_MINE = [
  { id: '17', name: '音色 17', preview_url: 'https://example.com/v17.mp3', kind: 'clone', created_at: 1756600000 },
];
const SCRIPTS = [
  { id: 'A', title: '直接干脆版', summary: '一句话讲清卖点', body: '文案A全文。' },
  { id: 'B', title: '情感种草版', summary: '讲感受带情绪', body: '文案B全文。' },
  { id: 'C', title: '专业严谨版', summary: '摆数据讲逻辑', body: '文案C全文。' },
];
function filmWidgets() {
  return [
    { type: 'avatar_pick', id: 'avatar_pick:video-avatars', title: '数字人形象（点选使用）', gen: 1, items: AVATARS },
    { type: 'voice_pick', id: 'voice_pick:audio-slots', title: '声音克隆槽位', gen: 1, items: VOICE_MINE },
    // 线上真实 id 没有 script_pick: 前缀（script_10s）——卸载必须靠 film 标记而不是 id 前缀
    { type: 'script_pick', id: 'script_10s', title: '口播文案三版（点选）', gen: 1, items: SCRIPTS },
  ];
}
function turnPayload(seq, withFilm) {
  return { seq, state: 'done',
    reply: withFilm ? '好，10 秒的数字人口播。三版文案在下方，形象音色已默认选好，都定好点绿色「确认生成」。' : '第 ' + seq + ' 轮普通回复。',
    report: null,
    // 关键回归：真实后端 widgets 是会话级全量聚合——非出片轮也照样全量返回！
    // 前端必须只认 film 标志，绝不因 widgets 非空就把货架挂回来。
    widgets: filmWidgets(), routing: [],
    tool_log: [],
    film: withFilm, // 后端权威信号：本轮派发了 digital-human = 出片轮
    // 服务端出片流程挂起：digital-human 在等用户选文案（每轮都会带这个待办状态）
    delegations: withFilm ? { 'digital-human': { state: 'needs_user_input', question: '文案用哪一版？点上方文案卡选一版。', summary: '' } } : {},
    mode: { llm_mode: 'mock', llm_model: 'test' } };
}

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

await page.route('**/*', async (route) => {
  const req = route.request();
  const url = req.url();
  if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
  if (url.includes('/api/v4/start') && req.method() === 'POST') {
    return route.fulfill({ json: { session_id: 'intent-session-1', reply: '测试开始', mode: { llm_mode: 'mock', llm_model: 'test' } } });
  }
  if (url.includes('/api/v4/chat') && req.method() === 'POST') {
    chatSeq += 1;
    const withFilm = chatSeq === 2 || chatSeq === 4; // 第 2、4 条消息（出片）带卡
    turnQueue.push({ seq: 100 + chatSeq, withFilm });
    return route.fulfill({ json: { async: true, seq: 100 + chatSeq, reply: '' } });
  }
  if (url.includes('/api/v4/poll')) {
    if (turnQueue.length) { const t = turnQueue.shift(); lastFilm = t.withFilm; return route.fulfill({ json: turnPayload(t.seq, t.withFilm) }); }
    return route.fulfill({ json: { state: 'idle' } });
  }
  if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
  if (url.includes('/api/v4/selection')) return route.fulfill({ json: { ok: true } });
  if (url.includes('/api/v4/status')) {
    // 服务端一直保留出片流程的待办状态：状态轮询每 2 秒都会尝试渲染——意图门控必须拦得住
    return route.fulfill({ json: { turns: [], jobs: [], film: lastFilm, delegations: { 'digital-human': { state: 'needs_user_input', question: '文案用哪一版？点上方文案卡选一版。', summary: '' } }, tool: null, report: null } });
  }
  if (url.includes('/api/v4/stream')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
  if (url.includes('/api/v4/restore')) return route.fulfill({ json: { ok: false } });
  if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
  return route.continue();
});

const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }
async function filmCount() {
  return page.evaluate(() => ({
    boxes: document.querySelectorAll('.widget-box').length,
    summary: document.querySelectorAll('.summary-card').length,
    picked: document.querySelectorAll('.widget-card.picked, .widget-row.picked').length,
  }));
}

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(800);

// ===== 1. 闲聊「还记得我吗」：页面上不许有任何出片卡 =====
await page.fill('#input', '你还记得我吗');
await page.press('#input', 'Enter');
await page.waitForFunction(() => {
  const bs = Array.from(document.querySelectorAll('.msg.assistant .bubble:not(.status-bubble)'));
  return bs.some((b) => b.textContent.includes('第 101 轮'));
}, null, { timeout: 8000 });
await page.waitForTimeout(400);
let st = await filmCount();
assert(st.boxes === 0 && st.summary === 0, '闲聊后：零出片卡、零配置卡（box=' + st.boxes + ', summary=' + st.summary + '）');

// ===== 2. 「做十秒数字人口播」：出片卡才出现 =====
await page.fill('#input', '做十秒数字人口播');
await page.press('#input', 'Enter');
await page.waitForFunction(() => Array.from(document.querySelectorAll('.widget-box')).some((n) => n.textContent.includes('口播文案三版')), null, { timeout: 8000 });
await page.waitForTimeout(500);
st = await filmCount();
assert(st.boxes >= 4, '出片请求后：形象/音色/文案卡 + 数字人待办卡出现（box=' + st.boxes + '）');
const boxTitles = await page.$$eval('.widget-box .widget-title', (ns) => ns.map((n) => n.textContent));
assert(boxTitles.some((t) => t.includes('数字人 等你回复')), '出片语境下数字人待办卡在');
assert(st.summary === 1, '出片配置卡出现');
const go = await page.$eval('.summary-card .sum-go', (n) => ({ disabled: n.disabled, text: n.textContent }));
assert(go.disabled === true && go.text.includes('文案'), '文案未选：确认生成禁用（先选一版文案）');
const names2 = await page.$$eval('.summary-card .sum-name', (ns) => ns.map((n) => n.textContent));
assert(names2.includes('本人形象') && names2.some((t) => t.includes('我的克隆音色')), '默认形象/音色已自动勾选进配置卡: ' + JSON.stringify(names2));

// ===== 3. 聊下一句普通话：发送瞬间出片区立即卸载（不等回复）=====
// 关键回归：这条消息里含「口播」二字但其实是闲聊——关键词猜意图会把货架留住；
// 权威信号 = 后端 film=false，发送瞬间就必须卸载。
await page.fill('#input', '刚才那条猪脚饭口播就是我做的，现在想聊点别的');
await page.press('#input', 'Enter');
await page.waitForTimeout(150); // 不等后端回复，只看发送瞬间
st = await filmCount();
assert(st.boxes === 0 && st.summary === 0 && st.picked === 0, '普通话发送瞬间：出片区全部卸载（box=' + st.boxes + ', summary=' + st.summary + ', picked=' + st.picked + '）');
// 回复到达后仍然干净；再等 3 秒越过状态轮询周期——服务端仍挂着数字人待办状态，门控必须拦得住
await page.waitForFunction(() => {
  const bs = Array.from(document.querySelectorAll('.msg.assistant .bubble:not(.status-bubble)'));
  return bs.some((b) => b.textContent.includes('第 103 轮'));
}, null, { timeout: 8000 });
await page.waitForTimeout(3500);
st = await filmCount();
assert(st.boxes === 0 && st.summary === 0, '普通话回复后仍零出片组件（状态轮询不反弹，box=' + st.boxes + '）');

// ===== 4. 再次出片：卡重新挂上（默认勾选重新生效）=====
await page.fill('#input', '帮我做十条口播视频');
await page.press('#input', 'Enter');
await page.waitForFunction(() => Array.from(document.querySelectorAll('.widget-box')).some((n) => n.textContent.includes('口播文案三版')), null, { timeout: 8000 });
await page.waitForTimeout(400);
st = await filmCount();
const names4 = await page.$$eval('.summary-card .sum-name', (ns) => ns.map((n) => n.textContent));
assert(st.summary === 1 && names4.includes('本人形象') && names4.some((t) => t.includes('我的克隆音色')), '再次出片：出片区重新挂上，默认重新勾选: ' + JSON.stringify(names4));
await page.screenshot({ path: '/tmp/hq-intent-04-film-again.png', fullPage: true });

console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
