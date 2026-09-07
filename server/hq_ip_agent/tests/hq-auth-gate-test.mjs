import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const {chromium} = await import(process.env.HQ_PW_MODULE || 'playwright');
const testDir = path.dirname(fileURLToPath(import.meta.url));
const uiDir = path.resolve(testDir, '../../../site/workbench/hq-ip-agent');
const outputDir = path.join(testDir, '.test-output');
fs.mkdirSync(outputDir, {recursive: true});
const browser = await chromium.launch({
  ...(process.env.HQ_BROWSER_CHANNEL ? {channel:process.env.HQ_BROWSER_CHANNEL} : {}),
  headless: true,
});

const page = await browser.newPage({viewport: {width: 390, height: 844}});
const pageErrors = [];
let authenticated = false;
let startRequests = 0;
let chatRequests = 0;
page.on('pageerror', error => pageErrors.push(error.message));

await page.route('**/*', async route => {
  const request = route.request();
  const url = new URL(request.url());
  const pathname = url.pathname;

  if (pathname === '/workbench/ip12/') {
    return route.fulfill({contentType: 'text/html; charset=utf-8', body: fs.readFileSync(path.join(uiDir, 'v4.html'), 'utf8')});
  }
  if (pathname.startsWith('/workbench/ip12/static/')) {
    const filename = path.basename(pathname);
    return route.fulfill({
      contentType: filename.endsWith('.css') ? 'text/css; charset=utf-8' : 'application/javascript; charset=utf-8',
      body: fs.readFileSync(path.join(uiDir, 'static', filename), 'utf8'),
    });
  }
  if (pathname === '/api/auth/me') {
    return route.fulfill(authenticated
      ? {status: 200, json: {user: {username: 'ip12-test'}}}
      : {status: 401, json: {detail: '请先登录黄雀账号'}});
  }
  if (pathname === '/api/auth/login') {
    const body = request.postDataJSON();
    assert.deepEqual(body, {username: 'ip12-test', password: 'secret123'});
    authenticated = true;
    return route.fulfill({status: 200, json: {user: {username: 'ip12-test'}}});
  }
  if (pathname.endsWith('/api/health')) {
    return route.fulfill({json: {llm_mode: 'mock', hq_status: {ok: true}}});
  }
  if (pathname.endsWith('/api/v4/start')) {
    startRequests += 1;
    return route.fulfill({json: {session_id: 'auth-gate-session', reply: '登录后正常启动', mode: 'mock'}});
  }
  if (pathname.endsWith('/api/v4/chat')) {
    chatRequests += 1;
    authenticated = false;
    return route.fulfill({status: 401, json: {error: '请先登录黄雀账号', code: 'unauthorized'}});
  }
  if (pathname.includes('/api/v4/status/')) return route.fulfill({json: {turns: [], jobs: [], delegations: {}}});
  if (pathname.includes('/api/v4/stream/')) return route.fulfill({status: 200, contentType: 'text/event-stream', body: ':\n\n'});
  if (pathname.includes('/api/report/')) return route.fulfill({status: 404, json: {}});
  return route.fulfill({json: {}});
});

try {
  await page.goto('http://hq.test/workbench/ip12/', {waitUntil: 'domcontentloaded'});
  const gate = page.locator('#auth-gate');
  await gate.waitFor({state: 'visible', timeout: 2500});
  assert.equal(startRequests, 0, 'anonymous entry must not start an Agent session');
  assert.equal(await page.locator('.shell').getAttribute('inert'), '', 'application background must be inert while login is required');
  assert.equal(await page.locator('body').evaluate(el => el.classList.contains('auth-required')), true);
  assert.equal((await gate.getByRole('link', {name: /其他登录方式|注册账号/}).getAttribute('href')).includes('next=%2Fworkbench%2Fip12%2F'), true);
  const mobileLayout = await page.evaluate(() => {
    const card = document.querySelector('.auth-card').getBoundingClientRect();
    const submit = document.querySelector('.auth-submit').getBoundingClientRect();
    return {overflow: document.documentElement.scrollWidth > innerWidth, cardWidth: card.width, cardBottom: card.bottom, submitHeight: submit.height};
  });
  assert.equal(mobileLayout.overflow, false);
  assert.ok(mobileLayout.cardWidth >= 350 && mobileLayout.cardWidth <= 370);
  assert.ok(Math.abs(mobileLayout.cardBottom - 832) <= 2);
  assert.ok(mobileLayout.submitHeight >= 50);
  await page.screenshot({path: path.join(outputDir, 'auth-gate-mobile.png'), fullPage: true});

  await page.setViewportSize({width: 1280, height: 800});
  const desktopLayout = await page.evaluate(() => {
    const card = document.querySelector('.auth-card').getBoundingClientRect();
    return {width: card.width, centerX: card.left + card.width / 2, centerY: card.top + card.height / 2};
  });
  assert.ok(desktopLayout.width <= 440);
  assert.ok(Math.abs(desktopLayout.centerX - 640) <= 2);
  assert.ok(Math.abs(desktopLayout.centerY - 400) <= 2);
  await page.screenshot({path: path.join(outputDir, 'auth-gate-desktop.png'), fullPage: true});
  await page.setViewportSize({width: 390, height: 844});

  await page.locator('#auth-username').fill('ip12-test');
  await page.locator('#auth-password').fill('secret123');
  await page.locator('#auth-login-form').getByRole('button', {name: '登录并继续'}).click();
  await page.getByText('登录后正常启动', {exact: true}).waitFor({timeout: 5000});
  assert.equal(startRequests, 1, 'successful login must initialize the Agent exactly once');
  assert.equal(await gate.isHidden(), true);

  await page.locator('#input').fill('测试会话失效');
  await page.locator('#input').press('Enter');
  await gate.waitFor({state: 'visible', timeout: 3000});
  assert.equal(chatRequests, 1);
  assert.equal(await page.locator('.shell').getAttribute('inert'), '');
  assert.deepEqual(pageErrors, []);
  console.log(JSON.stringify({anonymousGate: 'PASS', noAnonymousStart: 'PASS', loginResume: 'PASS', midSession401: 'PASS'}));
} finally {
  await browser.close();
}
