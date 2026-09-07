// 滚动锚点专项（用户投诉：每说完一句话都被跳到最下面的「子 Agent 路由轨迹」面板）：
// ① 手机端长对话里发消息后，视口停在聊天区底部（最后一条消息可见），绝不滚进下方轨迹面板；
// ② 用户主动滑进轨迹面板看日志时，新消息到达不把他拽回来（不打扰）。
// 前置：长对话用注入的填充消息模拟（不依赖 LLM 回 30 次）。
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });

function fillChat(page, n) {
  return page.evaluate((n) => {
    const msgs = document.getElementById('messages');
    for (let i = 0; i < n; i++) {
      const w = document.createElement('div');
      w.className = 'msg ' + (i % 2 ? 'user' : 'assistant');
      const b = document.createElement('div');
      b.className = 'bubble';
      b.textContent = '填充消息 ' + i + '：' + '这是一段用于撑高页面的测试文本，模拟长对话。'.repeat(6);
      w.appendChild(b);
      msgs.appendChild(w);
    }
  }, n);
}

function fillTrajectory(page, n) {
  // 模拟真实忙碌的轨迹面板（截图投诉里那种满屏日志）
  return page.evaluate((n) => {
    const list = document.getElementById('tool-list');
    const empty = list.querySelector('.tool-empty');
    if (empty) empty.remove();
    for (let i = 0; i < n; i++) {
      const item = document.createElement('div');
      item.className = 'tool-item ok';
      item.innerHTML = '<span class="t-dot"></span><span class="t-name">hq-image</span>' +
        '<span class="t-detail">已提交任务生成并扣 35 点（job_id: 77' + String(i).padStart(2, '0') + '），正在生成中</span>';
      list.appendChild(item);
    }
  }, n);
}

// 等页面滚动完全停住（连续 stableMs 毫秒 scrollY 无变化）再测量，
// 替代固定 sleep——连跑负载下平滑滚动未停时测量会 flake。
async function settleScroll(page, stableMs = 500) {
  await page.evaluate((stableMs) => new Promise((resolve) => {
    let last = window.scrollY;
    let stable = 0;
    const t0 = Date.now();
    const tick = () => {
      if (Math.abs(window.scrollY - last) < 1) {
        stable += 100;
      } else { stable = 0; last = window.scrollY; }
      if (stable >= stableMs || Date.now() - t0 > 8000) resolve(true);
      else setTimeout(tick, 100);
    };
    tick();
  }), stableMs);
}

// ================= 场景①：长对话中发消息 → 视口停在聊天区底部 =================
{
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#composer', { timeout: 10000 });
  await page.waitForTimeout(800); // 等开场轮（若已开）
  await fillChat(page, 30);       // 撑高页面，制造真实滚动空间
  const scrollable = await page.evaluate(() => document.body.scrollHeight > window.innerHeight + 400);
  assert(scrollable, "页面已可滚动（填充消息生效）");

  await page.fill('#input', '我还有多少点数');
  await page.press('#input', 'Enter');
  // 等真实回复渲染（填充消息之后多出一条 assistant）
  await page.waitForFunction(
    () => document.querySelectorAll('.msg.assistant').length >= 16, null, { timeout: 60000 });
  await settleScroll(page);

  const m = await page.evaluate(() => {
    const chat = document.getElementById('chat-pane');
    const msgs = document.getElementById('messages');
    const last = msgs.lastElementChild;
    const lr = last ? last.getBoundingClientRect() : null;
    const pane = document.getElementById('tool-pane');
    const pr = pane ? pane.getBoundingClientRect() : null;
    return {
      scrollY: window.scrollY,
      innerH: window.innerHeight,
      bodyBottom: document.body.scrollHeight,
      chatBottom: chat.offsetTop + chat.offsetHeight,
      lastMsgBottom: lr ? lr.bottom : -1,
      toolTop: pr ? pr.top : -1,
      viewportBottom: window.scrollY + window.innerHeight,
    };
  });

  // ①视口底部不越过聊天区底部（旧实现滚到整页最底，越界量=轨迹面板高度）
  assert(m.viewportBottom <= m.chatBottom + 60,
    "视口底部停在聊天区（越界 " + (m.viewportBottom - m.chatBottom).toFixed(0) + "px ≤ 60）");
  // ②最后一条消息完整可见（悬浮输入框之上）
  assert(m.lastMsgBottom > 0 && m.lastMsgBottom <= m.innerH - 40,
    "最后一条消息完整可见（bottom=" + m.lastMsgBottom.toFixed(0) + "）");
  // ③轨迹面板不在视口内
  assert(m.toolTop >= m.innerH - 30,
    "轨迹面板不在视口内（top=" + m.toolTop.toFixed(0) + "）");
  // ④离整页最底还有轨迹面板的距离（旧实现=0）
  assert(m.bodyBottom - m.viewportBottom > 80,
    "未滚到整页最底（距底 " + (m.bodyBottom - m.viewportBottom).toFixed(0) + "px > 80）");
  await page.close();
}

// ================= 场景②：主动滑进轨迹面板 → 新消息到达不拽回 =================
{
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#composer', { timeout: 10000 });
  await page.waitForTimeout(800);
  await fillChat(page, 30);
  await fillTrajectory(page, 18); // 忙碌的轨迹面板（投诉场景）

  await page.fill('#input', '我还有多少点数');
  await page.press('#input', 'Enter');
  await page.waitForFunction(
    () => document.querySelectorAll('.msg.assistant').length >= 16, null, { timeout: 60000 });
  await settleScroll(page); // 自动滚动彻底停住，再开始「用户主动滑走」

  // 用户主动滚进轨迹面板（模拟翻看日志）
  await page.evaluate(() => {
    document.getElementById('tool-pane').scrollIntoView({ block: 'start' });
  });
  await settleScroll(page, 400); // scrollIntoView 自身的平滑滚动也等停稳
  await page.waitForTimeout(200);
  const inPane = await page.evaluate(() => ({
    scrollY: window.scrollY,
    toolTop: document.getElementById('tool-pane').getBoundingClientRect().top,
  }));
  assert(inPane.toolTop >= -10 && inPane.toolTop < 450, "用户已主动滚进轨迹面板（top=" + inPane.toolTop.toFixed(0) + "）");

  // 不经输入框直接发第二条（模拟后台到达的新消息：不触发 forceScrollBottom）
  await page.evaluate(() => {
    fetch('api/v4/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: localStorage.getItem('hq-v4-session-id') || '', message: '我还有多少点数', seq: 0 }),
    });
  });
  await page.waitForTimeout(3500);
  const after = await page.evaluate(() => window.scrollY);
  // 用户在轨迹面板翻看时，自动滚动不得把他拽回聊天区
  assert(Math.abs(after - inPane.scrollY) < 100,
    "翻看轨迹面板时不被打断（scrollY 变化 " + (after - inPane.scrollY).toFixed(0) + "px < 100）");
  await page.close();
}

await browser.close();
console.log(JSON.stringify(out, null, 2));
console.log('SCROLL_TEST: ' + (errors.length ? 'FAIL' : 'PASS'));
for (const e of errors) console.log('  ERROR:', e);
process.exit(errors.length ? 1 : 0);
