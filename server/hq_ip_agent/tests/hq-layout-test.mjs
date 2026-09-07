// 布局专项验证：IP 定位报告左侧固定栏
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const turnQueue = [];
let chatSeq = 0;

function reportPayload() {
  return {
    status: 'final', phase: '报告定稿',
    files: { pdf: 'api/download/顾言_IP人设定位_定稿.pdf', md: null, json: null },
    m5: { status: 'ready', phase: '选题方案已生成',
      files: { pdf: 'api/download/顾言_选题生成_模块5.pdf', md: 'api/download/顾言_选题生成_模块5.md' } },
    m6: { status: 'ready', phase: '文案已生成', topic: 'x',
      files: { pdf: 'api/download/顾言_文案生成_模块6.pdf', md: 'api/download/顾言_文案生成_模块6.md' } },
  };
}

function turnPayload(seq) {
  return { seq, state: 'done', reply: '报告好了，看左边。', report: reportPayload(), widgets: [], routing: [], tool_log: [], delegations: {}, film: false, mode: { llm_mode: 'mock', llm_model: 'test' } };
}

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

await page.route('**/*', async (route) => {
  const req = route.request();
  const url = req.url();
  if (url.includes('/api/auth/me')) return route.fulfill({ json: { user: { username: 'test-user' } } });
  if (url.includes('/api/v4/start') && req.method() === 'POST') {
    return route.fulfill({ json: { session_id: 'test-session-1', reply: '测试开始', mode: { llm_mode: 'mock', llm_model: 'test' } } });
  }
  if (url.includes('/api/v4/chat') && req.method() === 'POST') {
    chatSeq += 1;
    turnQueue.push(100 + chatSeq);
    return route.fulfill({ json: { async: true, seq: 100 + chatSeq, reply: '' } });
  }
  if (url.includes('/api/v4/poll')) {
    if (turnQueue.length) return route.fulfill({ json: turnPayload(turnQueue.shift()) });
    return route.fulfill({ json: { state: 'idle' } });
  }
  if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
  if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, tool: null, report: null } });
  if (url.includes('/api/v4/stream')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
  if (url.includes('/api/v4/restore')) return route.fulfill({ json: { ok: false } });
  if (url.includes('/api/report/')) return route.fulfill({ json: reportPayload() });
  if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
  if (url.includes('/api/download/')) return route.fulfill({ status: 200, contentType: 'text/plain; charset=utf-8', body: '# 内容\n正文' });
  return route.continue();
});

const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(800);

// 1. 初始无报告：左栏隐藏，聊天占满全宽
const initPaneHidden = await page.$eval('#report-pane', (n) => n.hidden);
assert(initPaneHidden === true, '初始无报告：左报告栏隐藏');
const initShell = await page.evaluate(() => {
  const s = document.querySelector('.shell');
  const m = document.querySelector('.main-col');
  return { flex: getComputedStyle(s).display, shellW: s.getBoundingClientRect().width, mainW: m.getBoundingClientRect().width };
});
assert(initShell.flex === 'flex', 'shell 是 flex 布局');
assert(initShell.mainW / initShell.shellW > 0.95, '无报告时聊天列占满全宽（' + (initShell.mainW / initShell.shellW).toFixed(2) + '）');

// 2. 发消息 → 报告到达 → 左栏出现且在聊天左边、粘顶
await page.fill('#input', '出个 IP 定位报告');
await page.press('#input', 'Enter');
await page.waitForFunction(() => !document.querySelector('#report-pane').hidden, null, { timeout: 8000 });
await page.waitForTimeout(400);
const lay = await page.evaluate(() => {
  const p = document.querySelector('#report-pane');
  const m = document.querySelector('.main-col');
  const b = document.querySelector('#report-box');
  return {
    pos: getComputedStyle(p).position,
    paneLeft: p.getBoundingClientRect().left,
    paneRight: p.getBoundingClientRect().right,
    mainLeft: m.getBoundingClientRect().left,
    boxInPane: !!p.contains(b),
    paneW: p.getBoundingClientRect().width,
  };
});
assert(lay.pos === 'sticky', '报告栏 position:sticky（滚动时固定可见），实际=' + lay.pos);
assert(lay.boxInPane, '报告卡在左栏内');
assert(lay.paneLeft < lay.mainLeft && lay.paneRight <= lay.mainLeft, '报告栏在聊天左边（paneRight=' + lay.paneRight.toFixed(0) + ' <= mainLeft=' + lay.mainLeft.toFixed(0) + '）');
assert(lay.paneW >= 290 && lay.paneW <= 310, '左栏宽约 300px，实际=' + lay.paneW.toFixed(0));

// 3. 滚动后左栏仍贴顶可见（粘顶生效）
await page.evaluate(() => window.scrollTo(0, 2000));
await page.waitForTimeout(300);
const stickyTop = await page.$eval('#report-pane', (n) => n.getBoundingClientRect().top);
assert(stickyTop >= 60 && stickyTop <= 100, '滚动后报告栏仍粘在视口顶部（top=' + stickyTop.toFixed(0) + '）');

// 4. 窄屏：恢复单列
await page.setViewportSize({ width: 700, height: 1000 });
await page.waitForTimeout(300);
const narrow = await page.evaluate(() => {
  const p = document.querySelector('#report-pane');
  const m = document.querySelector('.main-col');
  return { dir: getComputedStyle(document.querySelector('.shell')).flexDirection, pos: getComputedStyle(p).position, paneLeft: p.getBoundingClientRect().left, mainLeft: m.getBoundingClientRect().left };
});
assert(narrow.dir === 'column', '窄屏 shell 单列');
assert(narrow.pos === 'static', '窄屏报告栏不再粘顶');
assert(Math.abs(narrow.paneLeft - narrow.mainLeft) < 8, '窄屏报告与聊天同列对齐（左对齐）');

// 5. 重置：左栏再次隐藏
await page.setViewportSize({ width: 1440, height: 1000 });
await page.waitForTimeout(300);
await page.click('#reset-btn');
await page.waitForTimeout(600);
assert(await page.$eval('#report-pane', (n) => n.hidden) === true, '重置后左报告栏隐藏');

await page.screenshot({ path: '/tmp/hq-layout-left.png', fullPage: true });
console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
