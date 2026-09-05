// Isolated visual/interaction fixture, NOT a live model, auth or production test.
// Uses the actual script.html and Agent JS; other app scripts are deliberately
// disabled. All requests stay on a random loopback port. No real accounts/keys.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const out = path.resolve(process.env.DIRECTOR_EVIDENCE_DIR || path.join(root, 'docs/evidence/director-natural-chat'));
const site = path.join(root, 'site');
const before = execFileSync('git', ['show', '36d2693f72359cb3c8252f98958a604261a1bb48:site/workbench/script-agent.js'], {cwd:root});
let enabled = true, beforeMode = false, posts = 0;
const jobs = new Map();
const reply = (res, body) => {res.writeHead(200, {'Content-Type':'application/json'});res.end(JSON.stringify(body));};
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if(url.pathname === '/api/gen/health') return reply(res, {director_agent_enabled:enabled});
  if(url.pathname === '/api/auth/me') return reply(res, {user:{username:'director-local-qa'}});
  if(req.method === 'POST') {
    assert.equal(url.pathname, '/api/gen/director_agent');
    let bytes = '';
    req.on('data', part => bytes += part);
    req.on('end', () => {
      const body = JSON.parse(bytes);
      const content = body.prompt === '你好' ? '你好，想做什么内容？' : '这次买饮料，记住这个活动：买三送一！想了解活动详情，评论区留言。';
      const id = String(++posts);
      jobs.set(id, {type:'director_agent',content,plan:{plan_id:'plan_fixture',page_revision:body.page_revision,
        stage:'understand',content,actions:[],warnings:[],requires_confirmation:false}});
      reply(res, {job_id:id});
    });
    return;
  }
  if(url.pathname.startsWith('/api/gen/job/')) return reply(res, {status:'done',result:jobs.get(url.pathname.split('/').pop())});
  const file = path.resolve(site, '.' + decodeURIComponent(url.pathname));
  if(!file.startsWith(site + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    res.writeHead(404);return res.end();
  }
  let data = fs.readFileSync(file);
  if(file.endsWith('script.html')) {
    data = Buffer.from(data.toString().replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
      .replace('</body>', '<script src="script-agent.js"></script></body>'));
  } else if(file.endsWith('script-agent.js') && beforeMode) data = before;
  res.writeHead(200, {'Content-Type':({'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8',
    '.css':'text/css','.png':'image/png','.woff2':'font/woff2'})[path.extname(file)] || 'application/octet-stream'});
  res.end(data);
});

(async () => {
  fs.mkdirSync(out, {recursive:true});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({channel:process.env.DIRECTOR_BROWSER_CHANNEL || 'msedge',headless:true});
  const errors = [], remote = [];
  let context;
  try {
    context = await browser.newContext({viewport:{width:1366,height:900}});
    await context.route('**/*', route => {
      if(!route.request().url().startsWith(origin + '/')) {remote.push(route.request().url());return route.abort();}
      return route.continue();
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    beforeMode = true;
    await page.goto(origin + '/workbench/script.html');
    await page.locator('#hqDirectorAgent').click();
    await page.screenshot({path:path.join(out, 'before-desktop.png')});
    beforeMode = false;
    await page.evaluate(() => sessionStorage.clear());
    await page.reload();
    await page.locator('#hqDirectorAgent').click();
    assert.equal(await page.locator('.hq-da-msg.assistant').innerText(), '你好，想做什么内容？有主题或文案直接发我。');
    await page.screenshot({path:path.join(out, 'after-desktop.png')});
    await page.locator('.hq-da-input').fill('你好');
    await page.locator('.hq-da-send').click();
    await page.getByText('你好，想做什么内容？', {exact:true}).waitFor();
    await page.locator('.hq-da-input').fill('饮料买三送一，给我一条口播');
    await page.locator('.hq-da-send').click();
    await page.getByText('这次买饮料，记住这个活动：买三送一！想了解活动详情，评论区留言。', {exact:true}).waitFor();
    assert.equal(await page.locator('.hq-da-confirm').count(), 0);
    assert.equal(posts, 2);
    await page.reload();
    await page.getByText('这次买饮料，记住这个活动：买三送一！想了解活动详情，评论区留言。', {exact:true}).waitFor();
    assert.equal(posts, 2, 'refresh must not create a new request');
    await page.screenshot({path:path.join(out, 'result-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    const panel = await page.locator('.hq-da-panel').boundingBox();
    assert.ok(panel.x >= 0 && panel.x + panel.width <= 390 && panel.y >= 0);
    await page.screenshot({path:path.join(out, 'after-mobile.png')});
    await page.locator('.hq-da-close').click();
    assert.equal(await page.locator('.hq-da-panel').isVisible(), false);
    enabled = false;
    await page.reload();
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('#hqDirectorAgent').count(), 0);
    assert.deepEqual(errors, []);
    assert.deepEqual(remote, []);
    console.log('PASS: before/after, greeting, draft, no price card, refresh, mobile bounds, close, flag off; 0 page errors; 0 external requests');
  } finally {
    if(context) await context.close();
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {console.error(error); server.close(); process.exitCode=1;});
