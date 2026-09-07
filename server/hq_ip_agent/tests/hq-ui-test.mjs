// 黄雀 v4 出片引导四句 + 默认自动勾选 + 文案未选禁出片 Playwright 验证（含回归：编号清除/报告区/配置卡）
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const turnQueue = [];
let chatSeq = 0;
let lastChatBody = null;
let lastFilm = false; // 模拟后端 get_last_film：最近一轮是否出片

const AVATARS = [
  { id: '536', name: '本人形象', image_url: 'https://example.com/a.jpg', status: 'ready' },
  { id: '535', name: '形象 19', image_url: 'https://example.com/b.jpg', status: 'ready' },
  { id: '534', name: '形象 18', image_url: 'https://example.com/c.jpg', status: 'ready' },
];
const VOICE_MINE = [
  { id: '17', name: '音色 17', preview_url: 'https://example.com/v17.mp3', kind: 'clone', created_at: 1756600000 },
  { id: '9', name: '音色 9', preview_url: 'https://example.com/v9.mp3', kind: 'clone', created_at: 1756513600 },
];
const VOICE_SYS = [
  { id: '301', name: '温柔女声（情感种草）', preview_url: 'https://example.com/s1.mp3', kind: 'voice', scope: 'public' },
];
const SCRIPTS = [
  { id: 'A', title: '直接干脆版', summary: '一句话讲清卖点', body: '文案A全文：直接干脆。' },
  { id: 'B', title: '情感种草版', summary: '讲感受带情绪', body: '文案B全文：情感种草。' },
  { id: 'C', title: '专业严谨版', summary: '摆数据讲逻辑', body: '文案C全文：专业严谨。' },
];

const GUIDANCE_1 = '好，10 条、每条 10 秒的数字人口播。\n\n文案还没有确认的，我先出三版给你点。\n\n形象和音色我已帮你选好默认的，不满意再点卡片换。\n\n都改好后，点下方绿色「确认生成」。';
const GUIDANCE_2 = '先确认文案用哪一版：三版在下方，点一版就行。';

function widgetsPayload(withScript) {
  const ws = [
    { type: 'avatar_pick', id: 'avatar_pick:video-avatars', title: '数字人形象（点选使用）', gen: 1,
      hint: '点击卡片即可选中，无需打字。', items: AVATARS },
    { type: 'voice_pick', id: 'voice_pick:audio-slots', title: '声音克隆槽位', gen: 1, items: VOICE_MINE },
    { type: 'voice_pick', id: 'voice_pick:voices', title: '音色（点选使用）', gen: 1, items: VOICE_SYS },
  ];
  if (withScript) {
    ws.push({ type: 'script_pick', id: 'script_pick:koubo', title: '口播文案三版（点选）', gen: 1, items: SCRIPTS });
  }
  return ws;
}

function reportPayload() {
  return {
    status: 'final', phase: '报告定稿',
    files: { pdf: 'api/download/顾言_IP人设定位_定稿.pdf', md: null, json: null },
    m5: { status: 'ready', phase: '选题方案已生成并通过模板校验',
      files: { pdf: 'api/download/顾言_选题生成_模块5.pdf', md: 'api/download/顾言_选题生成_模块5.md' } },
    m6: { status: 'ready', phase: '文案已生成并通过模板校验', topic: '你的Agent生成了方案但没回写结果',
      files: { pdf: 'api/download/顾言_文案生成_模块6.pdf', md: 'api/download/顾言_文案生成_模块6.md' } },
  };
}

function turnPayload(seq, kind) {
  return {
    seq, state: 'done',
    reply: seq === 101 ? GUIDANCE_1 : GUIDANCE_2,
    report: reportPayload(),
    widgets: kind === 'none' ? [] : widgetsPayload(kind === 'script'),
    routing: [{ domain: 'digital-human', state: seq === 101 ? 'needs_user_input' : 'completed' }],
    tool_log: [],
    delegations: {},
    film: kind !== 'none', // 后端权威信号：本轮派发 digital-human = 出片轮
    mode: { llm_mode: 'mock', llm_model: 'test' },
  };
}

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('console', (m) => {
  if (m.type() === 'error') errors.push('console: ' + m.text() + (m.location().url ? ' @ ' + m.location().url : ''));
});
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

await page.route('**/*', async (route) => {
  const req = route.request();
  const url = req.url();
  const method = req.method();
  if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
  if (url.startsWith('https://example.com/')) {
    const png = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082', 'hex');
    return route.fulfill({ status: 200, contentType: 'image/png', body: png });
  }
  if (url.includes('/api/v4/start') && method === 'POST') {
    return route.fulfill({ json: { session_id: 'test-session-1', reply: '测试开始', mode: { llm_mode: 'mock', llm_model: 'test' } } });
  }
  if (url.includes('/api/v4/chat') && method === 'POST') {
    chatSeq += 1;
    lastChatBody = req.postDataJSON();
    const msg = (lastChatBody || {}).message || '';
    // 模拟真实后端：【已选汇总】/【点选】是出片流程自身的续消息 → 仍是出片轮（film=true）；
    // 普通闲聊轮不带出片卡。权威信号 = film 字段，前端绝不拿关键词猜意图。
    const kind = /^【(已选汇总|点选)】/.test(msg) ? 'basic' : chatSeq === 1 ? 'basic' : chatSeq === 2 ? 'script' : 'none';
    turnQueue.push({ seq: 100 + chatSeq, kind });
    return route.fulfill({ json: { async: true, seq: 100 + chatSeq, reply: '' } });
  }
  if (url.includes('/api/v4/poll')) {
    if (turnQueue.length) { const t = turnQueue.shift(); lastFilm = t.kind !== 'none'; return route.fulfill({ json: turnPayload(t.seq, t.kind) }); }
    return route.fulfill({ json: { state: 'idle' } });
  }
  if (url.includes('/api/v4/reset')) {
    return route.fulfill({ json: { ok: true } });
  }
  if (url.includes('/api/v4/selection')) {
    return route.fulfill({ json: { ok: true } });
  }
  if (url.includes('/api/v4/status')) {
    return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, film: lastFilm, tool: null, report: reportPayload() } });
  }
  if (url.includes('/api/v4/tasks/')) return route.fulfill({ json: { ok: true, tasks: [] } });
  if (url.includes('/api/v4/stream')) {
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
  }
  if (url.includes('/api/v4/restore')) {
    return route.fulfill({ json: { ok: false } });
  }
  if (url.includes('/api/report/')) {
    return route.fulfill({ json: reportPayload() });
  }
  if (url.includes('/api/health')) {
    return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
  }
  if (url.includes('/api/download/')) {
    const dec = decodeURIComponent(url);
    const body = dec.includes('模块5') ? '# 选题\n\n1. 选题一\n2. 选题二' : '# 口播文案\n\n### 版本一\n正文内容……';
    return route.fulfill({ status: 200, contentType: 'text/plain; charset=utf-8', body });
  }
  return route.continue();
});

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(800);

const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }

// ===== 第一轮：出片需求 → 引导四句 + 形象/音色卡（无文案卡） =====
await page.fill('#input', '帮我做个十秒数字人口播');
await page.press('#input', 'Enter');
await page.waitForSelector('.widget-box', { timeout: 6000 });
await page.waitForTimeout(600);

// 1. 引导气泡在消息流里，且是四句结构（复述/原料/默认/下一步）
const msgs = await page.$$eval('.msg.assistant .bubble', (ns) => ns.map((n) => n.textContent));
const guide = msgs[msgs.length - 1];
assert(!!guide && guide.includes('10 秒'), '引导第一句：复述任务（条数/时长/形式）');
assert(guide.includes('文案') && guide.includes('先出三版') || guide.includes('点'), '引导第二句：指定原料（没确认就先出三版）');
assert(guide.includes('形象') && guide.includes('音色') && guide.includes('默认'), '引导第三句：给默认（形象/音色已选）');
assert(guide.includes('确认生成'), '引导第四句：下一步点确认生成');

// 2. 锚定：引导气泡在视口内（引导在上，卡片不许把话顶出屏幕）
const guideBox = await page.evaluate(() => {
  const bs = document.querySelectorAll('.msg.assistant .bubble');
  const b = bs[bs.length - 1];
  const r = b.getBoundingClientRect();
  return { top: r.top, bottom: r.bottom, vh: window.innerHeight, scrollY: window.scrollY, docH: document.body.scrollHeight };
});
assert(guideBox.top >= -2 && guideBox.bottom <= guideBox.vh + 2, '引导气泡完整在视口内（top=' + guideBox.top.toFixed(1) + '）');
assert(guideBox.scrollY > 0 && guideBox.top <= 60, '视口锚定在引导气泡顶部（卡片在下方，不把话顶出屏幕；scrollY=' + guideBox.scrollY + ', top=' + guideBox.top.toFixed(1) + '）');

// 3. 默认自动勾选：没点任何卡片，出片配置卡已出现且形象+音色已选
const autoCard = await page.$('.summary-card');
assert(!!autoCard, '默认勾选后出片配置卡自动出现（无需点击）');
const autoNames = await page.$$eval('.summary-card .sum-name', (ns) => ns.map((n) => n.textContent));
assert(autoNames.includes('本人形象'), '默认形象=本人形象: ' + JSON.stringify(autoNames));
assert(autoNames.some((t) => t.includes('我的克隆音色')), '默认音色=我的克隆音色: ' + JSON.stringify(autoNames));
const autoGo = await page.$('.summary-card .sum-go');
assert(!!autoGo && !(await autoGo.isDisabled()), '本轮无文案卡：确认按钮可用（还没到文案必选）');
const goBg = await autoGo.evaluate((n) => getComputedStyle(n).backgroundColor);
assert(goBg === 'rgb(26, 127, 55)', '确认按钮是绿色 #1a7f37，实际=' + goBg);

// 4. 配置卡在引导气泡之后（引导在上、选择在下）
const order = await page.evaluate(() => {
  const bs = document.querySelectorAll('.msg.assistant .bubble');
  const b = bs[bs.length - 1];
  const c = document.querySelector('.summary-card');
  if (!b || !c || !b.closest('.msg') || b.closest('.msg').parentNode !== c.parentNode) return -1;
  const wrap = b.closest('.msg');
  return Array.prototype.indexOf.call(c.parentNode.children, c) - Array.prototype.indexOf.call(c.parentNode.children, wrap);
});
assert(order > 0, '配置卡 DOM 顺序在引导气泡之后（引导在上，卡片是附件）');

// 5. 回归：编号清除
const avatarNames = await page.$$eval('.widget-card .wc-name', (ns) => ns.map((n) => n.textContent));
assert(JSON.stringify(avatarNames) === JSON.stringify(['本人形象']), '编号形象只留脸: ' + JSON.stringify(avatarNames));
const voiceNames = await page.$$eval('.widget-row.voice-row .wr-name', (ns) => ns.map((n) => n.textContent));
assert(voiceNames.length >= 2 && voiceNames.every((t) => !/\d/.test(t)) && voiceNames.some((t) => t.includes('我的克隆音色')), '音色名无编号、显示我的克隆音色');

// ===== 第二轮：文案三版卡到达 → 未选文案禁止出片 =====
await page.fill('#input', '文案帮我定一下');
await page.press('#input', 'Enter');
await page.waitForSelector('.widget-box', { timeout: 6000 });
await page.waitForFunction(() => Array.from(document.querySelectorAll('.widget-box')).some((n) => n.textContent.includes('口播文案三版')), { timeout: 6000 });
await page.waitForTimeout(600);
const scriptBox = page.locator('.widget-box', { hasText: '口播文案三版' });

// 6. 文案未选：确认生成不可点 + 按钮换文案必选文字 + 文案格亮必选提示
const needGo = await page.$('.summary-card .sum-go');
assert(!!needGo && (await needGo.isDisabled()), '文案未选：确认生成按钮 disabled');
const goText = await needGo.textContent();
assert(goText.includes('先选一版文案'), '按钮文字=先选一版文案，实际=' + goText);
const reqSlot = await page.$('.summary-card .sum-slot-required');
assert(!!reqSlot && (await reqSlot.textContent()).includes('必选'), '文案格显示必选提示');
const reqBorder = await reqSlot.evaluate((n) => getComputedStyle(n).borderStyle);
assert(reqBorder === 'dashed', '必选格虚线边框');

// 7. 第二轮引导追问文案也在视口内
const guide2Box = await page.evaluate(() => {
  const bs = document.querySelectorAll('.msg.assistant .bubble');
  const b = bs[bs.length - 1];
  const r = b.getBoundingClientRect();
  return { top: r.top, bottom: r.bottom, vh: window.innerHeight };
});
assert(guide2Box.top >= -2 && guide2Box.bottom <= guide2Box.vh + 2, '追问气泡在视口内（top=' + guide2Box.top.toFixed(1) + '）');

// 8. 点选一版文案 → 按钮恢复可点、绿色、文案格有标题
await scriptBox.locator('button.pick').first().click();
await page.waitForTimeout(500);
const okGo = await page.$('.summary-card .sum-go');
assert(!!okGo && !(await okGo.isDisabled()), '选完文案后确认按钮恢复可点');
assert((await okGo.textContent()).includes('确认生成'), '按钮文字恢复确认生成');
const sumNames2 = await page.$$eval('.summary-card .sum-name', (ns) => ns.map((n) => n.textContent));
assert(sumNames2.some((t) => t.includes('直接干脆版')), '配置卡文案格显示所选版本: ' + JSON.stringify(sumNames2));
assert((await page.$('.summary-card .sum-slot-required')) === null, '必选提示消失');

// 9. 点确认生成 → 提交，且提交内容带三格选择（文案 id=A）
await okGo.click();
await page.waitForTimeout(400);
assert((await page.$eval('.summary-card .sum-go', (n) => n.textContent)) === '已提交，生成中…', '确认后按钮变已提交');
assert(!!lastChatBody && typeof lastChatBody.message === 'string', '确认生成发起了消息');
assert(lastChatBody.message.includes('文案：直接干脆版') && lastChatBody.message.includes('id=A'), '提交内容含所选文案: ' + (lastChatBody && lastChatBody.message));
assert(lastChatBody.message.includes('形象：本人形象') && lastChatBody.message.includes('音色：我的克隆音色'), '提交内容含默认形象+音色');

// 10. 重置：全部状态清空（配置卡、卡片、必选标记）
await page.click('#reset-btn');
await page.waitForTimeout(600);
assert((await page.$('.summary-card')) === null, '重置后配置卡消失');
assert((await page.$('.widget-box')) === null, '重置后素材卡清空');
assert((await page.$$eval('.msg.assistant .bubble', (ns) => ns.length)) <= 1, '消息流回到初始');

await page.screenshot({ path: '/tmp/hq-ui-03-guide.png', fullPage: true });
console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
