// 恢复场景意图门控：会话出过片，但最后一句是闲聊 → 刷新后货架不许挂回来
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const FILM_WIDGETS = [
  { type: 'avatar_pick', id: 'avatar_pick:video-avatars', title: '数字人形象（点选使用）', gen: 1,
    items: [{ id: '536', name: '本人形象', image_url: 'https://example.com/a.jpg', status: 'ready' }] },
  { type: 'voice_pick', id: 'voice_pick:audio-slots', title: '声音克隆槽位', gen: 1,
    items: [{ id: '17', name: '音色 17', preview_url: 'https://example.com/v17.mp3', kind: 'clone', created_at: 1756600000 }] },
  { type: 'script_pick', id: 'script_pick:koubo', title: '口播文案三版（点选）', gen: 1,
    items: [{ id: 'A', title: '直接干脆版', summary: 's', body: 'b' }] },
];
let scenario = 'chat'; // chat=最后闲聊 / film=最后出片 / selected=刷新前已点选

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

await page.route('**/*', async (route) => {
  const req = route.request();
  const url = req.url();
  if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
  if (url.includes('/api/v4/restore')) {
    const history = scenario === 'chat'
      ? [{ role: 'user', content: '帮我做个十秒数字人口播' }, { role: 'assistant', content: '好，三版文案在下方…' }, { role: 'user', content: '刚才帮我出猪脚饭口播的就是你吧，现在想聊点别的' }, { role: 'assistant', content: '记得呀，你之前让我做口播。' }]
      : [{ role: 'user', content: '帮我做个十秒数字人口播' }, { role: 'assistant', content: '好，三版文案在下方…' }, { role: 'user', content: '做个十秒数字人口播，主题是面馆引流' }, { role: 'assistant', content: '三版文案好了，卡片在下方。' }];
    const selectedChoices = scenario === 'selected' ? {
      avatar: { id: '536', label: '本人形象', image_url: 'https://example.com/a.jpg', film: true, manual: true },
      voice: { id: '17', label: '我的克隆音色', preview_url: 'https://example.com/v17.mp3', film: true, manual: true },
      script: { id: 'A', label: '直接干脆版', film: true, manual: true },
    } : {};
    return route.fulfill({ json: { ok: true, history, widgets: FILM_WIDGETS, selected_choices: selectedChoices,
      delegations: {}, report: null, film: scenario !== 'chat', mode: { llm_mode: 'mock', llm_model: 'test' } } });
  }
  if (url.includes('/api/v4/start') && req.method() === 'POST') {
    return route.fulfill({ json: { session_id: 'restore-intent-1', reply: '测试开始', mode: { llm_mode: 'mock', llm_model: 'test' } } });
  }
  if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, film: scenario !== 'chat', tool: null, report: null } });
  if (url.includes('/api/v4/selection')) return route.fulfill({ json: { ok: true } });
  if (url.includes('/api/v4/stream')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
  if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 999, reply: '' } });
  if (url.includes('/api/v4/poll')) return route.fulfill({ json: { state: 'idle' } });
  if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
  return route.continue();
});

const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }
async function filmCount() {
  return page.evaluate(() => ({
    boxes: document.querySelectorAll('.widget-box').length,
    summary: document.querySelectorAll('.summary-card').length,
  }));
}

// 场景 A：最后一句是闲聊 → 恢复后零出片卡（历史里出过片也不行）
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(() => localStorage.setItem('hq-v4-session-id', 'restore-intent-1'));
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2000);
let st = await filmCount();
assert(st.boxes === 0 && st.summary === 0, '最后闲聊：恢复后零出片卡（box=' + st.boxes + ', summary=' + st.summary + '）');
const msgsA = await page.$$eval('.msg.assistant .bubble:not(.status-bubble)', (ns) => ns.length);
assert(msgsA === 2, '闲聊场景历史消息正常恢复（' + msgsA + ' 条）');
await page.screenshot({ path: '/tmp/hq-restore-chat-clean.png', fullPage: true });

// 场景 B：最后一句是出片意图 → 恢复后出片卡挂上
scenario = 'film';
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2000);
st = await filmCount();
assert(st.boxes >= 3 && st.summary === 1, '最后出片：恢复后出片卡挂上（box=' + st.boxes + ', summary=' + st.summary + '）');
const go = await page.$eval('.summary-card .sum-go', (n) => ({ disabled: n.disabled, text: n.textContent }));
assert(go.disabled === true, '恢复后文案未选仍禁点确认生成');
await page.screenshot({ path: '/tmp/hq-restore-film-on.png', fullPage: true });

// 场景 C：点选后尚未发送就刷新 → 三项选择与高亮原样恢复
scenario = 'selected';
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2000);
const picked = await page.locator('.widget-card.picked, .widget-row.picked').count();
const selectedGo = await page.$eval('.summary-card .sum-go', (n) => ({ disabled: n.disabled, text: n.textContent }));
assert(picked >= 3, '点选后立即刷新：形象/音色/文案高亮恢复（picked=' + picked + '）');
assert(selectedGo.disabled === false, '点选后立即刷新：确认生成仍可点');

console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
