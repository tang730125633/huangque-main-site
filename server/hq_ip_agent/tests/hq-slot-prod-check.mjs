// #30 线上真实流程验证：全新会话 → 要求替换克隆音色 → 等 hq-audio 查 audio-slots
// → 槽位卡在非出片轮真实渲染 → 点选一个槽位 → 下一条消息以【点选】开头且带 slot_id。
// 全程只做免费操作（audio-slots 免费只读 + 点选），不触发任何付费生成/克隆提交。
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'https://huangquechuanmei.com/workbench/ip12/';
const errors = [];
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

let sentMessages = [];
page.on('request', (req) => {
  if (req.url().includes('/api/v4/chat') && req.method() === 'POST') {
    try { sentMessages.push(req.postDataJSON().message || ''); } catch (e) {}
  }
});

async function texts() {
  return page.evaluate(() => {
    const real = (sel) => [...document.querySelectorAll(sel)].map((n) => n.textContent);
    const asst = real('.msg.assistant .bubble:not(.status-bubble)');
    const users = real('.msg.user .bubble:not(.status-bubble)');
    return {
      all: [...users, ...asst].join('\n'),
      tail: [...users, ...asst].slice(-6).join('\n'),
      userCount: document.querySelectorAll('.msg.user').length,
      asstRealCount: asst.length,
    };
  });
}

async function send(msg, label) {
  log('→', label, msg.slice(0, 50));
  const before = await texts();
  await page.fill('#input', msg);
  await page.press('#input', 'Enter');
  const t0 = Date.now();
  while (true) {
    const t = await texts();
    if (t.asstRealCount > before.asstRealCount) {
      log('  回复出现', ((Date.now() - t0) / 1000).toFixed(0) + 's');
      return t;
    }
    if (Date.now() - t0 > 150000) { log('  ⚠ 等回复超时'); return null; }
    await page.waitForTimeout(3000);
  }
}

const stage = { ok: {} };
try {
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => !!localStorage.getItem('hq-v4-session-id'), null, { timeout: 30000 });
  const sid = await page.evaluate(() => localStorage.getItem('hq-v4-session-id'));
  log('新会话 sid:', sid);
  stage.sid = sid;

  // 1. 提需求（hq-audio 域 → 非出片轮）
  await send('我想替换我的克隆音色，重新录一段我的声音', '要求替换克隆音色');

  // 2. 等槽位卡渲染（film=false 的 voice_pick 在非出片轮可见——#30 核心修复点）
  let panelAppeared = false;
  const t0 = Date.now();
  while (Date.now() - t0 < 180000) {
    const n = await page.locator('.voice-panel').count();
    if (n > 0) { panelAppeared = true; break; }
    await page.waitForTimeout(3000);
  }
  stage.ok.slotCards = panelAppeared;
  log(panelAppeared ? '✔ 槽位卡渲染出现' : '✘ 槽位卡未出现（180s）');
  if (!panelAppeared) throw new Error('槽位卡未渲染');

  // 展开面板（若是收起态；带重试——新回复可能重渲染）
  for (let i = 0; i < 3; i++) {
    const collapsed = await page.evaluate(() => {
      var b = document.querySelector('.voice-panel');
      return b ? b.classList.contains('collapsed') : false;
    });
    if (!collapsed) break;
    await page.locator('.voice-panel .widget-head').click().catch(() => {});
    await page.waitForTimeout(800);
  }
  const panelText = await page.locator('.voice-panel').innerText();
  log('面板内容:', panelText.replace(/\n/g, ' | ').slice(0, 220));
  stage.ok.cloneLabel = panelText.includes('我的克隆音色');

  // 3. 点选第一个槽位
  await page.locator('.voice-panel .widget-row').nth(0).locator('button.pick').click();
  await page.waitForFunction(() => document.querySelectorAll('.voice-panel .widget-row.picked').length === 1, null, { timeout: 8000 });
  stage.ok.picked = true;
  log('✔ 已点选槽位');

  // 4. 发消息 → 【点选】自动携带（listen 请求 body 验证）
  const beforeCount = sentMessages.length;
  await page.fill('#input', '就替换这个槽位');
  await page.press('#input', 'Enter');
  await page.waitForFunction(() => {
    var b = document.getElementById('send-btn');
    return b && !b.disabled;
  }, null, { timeout: 8000 });
  const sent = sentMessages.slice(beforeCount).join('\n---\n');
  stage.sentRaw = sent;
  stage.ok.pickPrefix = sent.startsWith('【点选】');
  stage.ok.slotId = /id=slot_[a-f0-9]{8,}/.test(sent);
  log('发送的消息:', sent.slice(0, 150));
  log(stage.ok.pickPrefix ? '✔ 【点选】前缀' : '✘ 无【点选】前缀');
  log(stage.ok.slotId ? '✔ 带 slot_id' : '✘ 无 slot_id');

  // 5. 等 Agent 回复（预期：让用户上传样音，不会自动提交任何克隆）
  const t1 = Date.now();
  let reply = '';
  while (Date.now() - t1 < 180000) {
    const t = await texts();
    if (t.asstRealCount > 2) { reply = t.tail; break; }
    await page.waitForTimeout(3000);
  }
  stage.replyTail = reply.slice(0, 400);
  log('Agent 回复:', reply.replace(/\n/g, ' | ').slice(0, 200));

  await page.screenshot({ path: '/tmp/hq-slot-prod.png', fullPage: false });
  console.log(JSON.stringify(stage, null, 2));
} catch (e) {
  log('脚本异常:', e.message);
  await page.screenshot({ path: '/tmp/hq-slot-prod-err.png', fullPage: false });
  process.exitCode = 1;
}

const pass = Object.values(stage.ok).filter(Boolean).length;
console.log('SLOT_PROD_STAGES:', pass, '/', Object.keys(stage.ok).length);
console.log('PAGE_ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(Object.values(stage.ok).every(Boolean) && errors.length === 0 ? 0 : 1);
