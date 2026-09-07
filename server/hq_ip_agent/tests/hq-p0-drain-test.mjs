// P0 专项 C：restore 排空——回放轮次按内容去重不重复贴 + 「刷新窗口内刚完成」的孤儿轮次兜回渲染
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'http://127.0.0.1:8000';
const errors = [];
const out = {};
function assert(cond, name) { out[name] = cond ? 'PASS' : 'FAIL'; if (!cond) errors.push('assert: ' + name); }

const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

let pollN = 0;
await page.route('**/*', async (route) => {
  const req = route.request();
  const url = req.url();
  if (url.includes('/api/v4/restore')) {
    return route.fulfill({ json: {
      ok: true,
      history: [
        { role: 'user', content: '帮我查一下点数' },
        { role: 'assistant', content: '你的账号剩余 1200 点。' },           // 已完成的旧轮次（回放源）
        { role: 'user', content: '再帮我扒一篇小红书' },
        { role: 'assistant', content: '好的，正在采集，稍等。' },
      ],
      widgets: [], delegations: {}, report: null, film: false,
      mode: { llm_mode: 'mock' },
    } });
  }
  if (url.includes('/api/v4/poll')) {
    pollN += 1;
    if (pollN === 1) {
      // 后端还留着两个 done 轮次：一个是历史里已有的（回放），一个是恢复快照后才完成的（孤儿）
      return route.fulfill({ json: { state: 'done', seq: 1001, reply: '你的账号剩余 1200 点。' } });
    }
    if (pollN === 2) {
      return route.fulfill({ json: { state: 'done', seq: 1002, reply: '采集完成！6 张图已经贴给你了。' } });
    }
    return route.fulfill({ json: { state: 'idle' } });
  }
  if (url.includes('/api/v4/stream/')) {
    return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ':\n' }).catch(() => {});
  }
  if (url.includes('/api/v4/status')) return route.fulfill({ json: { turns: [], jobs: [], delegations: {}, report: null, film: false } });
  if (url.includes('/api/v4/start') && req.method() === 'POST') {
    return route.fulfill({ json: { session_id: 'p0-drain-1', reply: '开始', mode: { llm_mode: 'mock' } } });
  }
  if (url.includes('/api/v4/chat')) return route.fulfill({ json: { async: true, seq: 1, reply: '' } });
  if (url.includes('/api/v4/reset')) return route.fulfill({ json: { ok: true } });
  if (url.includes('/api/health')) return route.fulfill({ json: { llm_mode: 'mock', llm_model: 'test', hq_status: { ok: false } } });
  return route.continue();
});

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.evaluate(() => localStorage.setItem('hq-v4-session-id', 'p0-drain-1'));
await page.reload({ waitUntil: 'domcontentloaded' });
await page.waitForFunction(() => (document.body.textContent || '').includes('好的，正在采集'), null, { timeout: 8000 });

// 等排空轮询跑完三拍（2.5s 间隔）：回放跳过、孤儿渲染、idle 收工
await page.waitForFunction(() => (document.body.textContent || '').includes('采集完成！6 张图已经贴给你了。'), null, { timeout: 10000 });
await page.waitForTimeout(1500);

const counts = await page.evaluate(() => {
  const t = document.body.textContent;
  return {
    replay: (t.match(/你的账号剩余 1200 点/g) || []).length,
    orphan: (t.match(/采集完成！6 张图已经贴给你了/g) || []).length,
  };
});
assert(counts.replay === 1, '回放轮次按内容去重：历史回复只出现 1 次（实际 ' + counts.replay + '）');
assert(counts.orphan === 1, '刷新窗口内完成的孤儿轮次被兜回渲染（实际 ' + counts.orphan + '）');
assert(pollN <= 4, '排空后轮询及时收工（poll 次数 ' + pollN + '，应 <=4）');

console.log(JSON.stringify(out, null, 2));
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(errors.length ? 1 : 0);
