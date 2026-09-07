// 线上验证 v3：先滚动到图片（lazy 图只有进视口才加载），再断言
const { chromium } = await import(process.env.HQ_PW_MODULE || 'file:///Users/xlzj/openclaw/node_modules/playwright/index.mjs');

const BASE = process.env.HQ_BASE || 'https://huangquechuanmei.com/workbench/ip12/';
const SID = process.env.HQ_SID || 'fedea76e2d8e40778b64bbaf47ba5098';
const errors = [];
const failedReqs = [];
const browser = await chromium.launch({ executablePath: process.env.HQ_CHROMIUM || '/Users/xlzj/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
page.on('requestfailed', (r) => failedReqs.push(r.url() + ' :: ' + (r.failure()?.errorText || '')));
page.on('response', (r) => { if (r.url().includes('/api/v4/media/') && r.status() !== 200) failedReqs.push(r.url() + ' :: HTTP ' + r.status()); });

await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 30000 });
// 先等页面自身的 start() 走完（异步拿到 sid 写入 localStorage），再覆盖成目标会话：
// 否则 start() 的异步回调会晚一步把目标 sid 覆盖掉（时序竞态，历史恢复被跳过）
await page.waitForFunction(() => !!localStorage.getItem('hq-v4-session-id'), null, { timeout: 15000 });
await page.evaluate((sid) => localStorage.setItem('hq-v4-session-id', sid), SID);
await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });

await page.waitForFunction(() => document.querySelectorAll('.msg-images img').length >= 6, null, { timeout: 30000 });

// 滚动到带图气泡，触发 lazy 加载
const imgBox = page.locator('.msg-images').last();
await imgBox.scrollIntoViewIfNeeded();
await page.waitForTimeout(800);

await page.waitForFunction(() => {
  const imgs = [...document.querySelectorAll('.msg-images img')];
  return imgs.every((n) => n.complete && n.naturalWidth > 0);
}, null, { timeout: 20000 }).catch(() => {});

const gridInfo = await page.evaluate(() => {
  const g = [...document.querySelectorAll('.msg-images')].pop();
  return {
    count: g.querySelectorAll('img').length,
    loaded: [...g.querySelectorAll('img')].map((n) => n.complete && n.naturalWidth > 0),
    naturalSizes: [...g.querySelectorAll('img')].map((n) => n.naturalWidth + 'x' + n.naturalHeight),
    hrefs: [...g.querySelectorAll('a')].map((n) => n.href).slice(0, 2),
  };
});
console.log('GRID:', JSON.stringify(gridInfo, null, 2));
const mediaReqs = failedReqs.filter((f) => f.includes('/api/v4/media/'));
console.log('MEDIA_FAILED_REQS:', mediaReqs.length ? mediaReqs : 'none');

await imgBox.scrollIntoViewIfNeeded();
await page.waitForTimeout(1000);
await page.screenshot({ path: '/tmp/hq-prod-media-delivery.png', fullPage: false });

const allLoaded = gridInfo.count >= 6 && gridInfo.loaded.every(Boolean);
console.log(allLoaded ? 'PROD_MEDIA_RENDER: PASS' : 'PROD_MEDIA_RENDER: FAIL');
console.log('ERRORS:', errors.length ? errors : 'none');
await browser.close();
process.exit(allLoaded && !errors.length && !mediaReqs.length ? 0 : 1);
