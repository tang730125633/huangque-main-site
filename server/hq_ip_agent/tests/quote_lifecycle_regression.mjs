// Mock every request: no production, credentials, model, collection or point use.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
const {chromium} = await import(process.env.HQ_PW_MODULE || 'playwright');
const ui = path.resolve('../../site/workbench/hq-ip-agent');
const out = path.resolve('.test-output');
fs.mkdirSync(out, {recursive:true});
const browser = await chromium.launch({headless:true,
  ...(process.env.HQ_BROWSER_CHANNEL ? {channel:process.env.HQ_BROWSER_CHANNEL} : {})});
const results = [];
const quote = (id, cost=1) => ({collect:{state:'needs_approval',summary:'本次采集报价',
  quote_id:id,quote:{cost,points:1937,expires_in:300}}});
const running = {collect:{state:'running',summary:'原任务已提交，正在处理',quote_id:''}};
async function harness(initial, latest=initial) {
  const page = await browser.newPage({viewport:{width:390,height:844}});
  const h = {page, shown:initial, latest, reads:0, posts:[], errors:[], failState:false, loseSubmit:false};
  page.on('pageerror', e=>h.errors.push(e.message));
  await page.route('**/*', async route=>{
    const req=route.request(), p=new URL(req.url()).pathname;
    if(p==='/v4') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(ui,'v4.html'),'utf8')});
    if(p.startsWith('/static/')) {
      const body = process.env.HQ_BASELINE_REF && p.endsWith('/v4.js') ? execFileSync('git',
        ['show',`${process.env.HQ_BASELINE_REF}:site/workbench/hq-ip-agent/static/v4.js`],{encoding:'utf8'}) :
        fs.readFileSync(path.join(ui,p),'utf8');
      return route.fulfill({contentType:p.endsWith('.css')?'text/css':'application/javascript',body});
    }
    if(p==='/api/health') return route.fulfill({json:{llm_mode:'live',hq_status:{ok:true}}});
    if(p==='/api/v4/start') return route.fulfill({json:{session_id:'quote-test',async:true,seq:1,mode:'live'}});
    if(p.includes('/api/v4/stream/')) return route.fulfill({contentType:'text/event-stream',body:': heartbeat\n\n'});
    if(p.includes('/api/v4/status/')) return route.fulfill({json:{turns:[{seq:1,state:'working'}],jobs:[],delegations:h.shown,film:false}});
    if(p.includes('/api/v4/poll/')) return route.fulfill({json:{state:'working'}});
    if(p.includes('/api/v4/state/')) {
      assert.equal(req.method(),'GET'); h.reads++;
      // Keep the preflight pending long enough to exercise triple clicks.
      await new Promise(resolve=>setTimeout(resolve,150));
      return route.fulfill(h.failState ? {status:503,json:{error:'test unavailable'}} : {json:h.latest});
    }
    if(p==='/api/v4/chat') {
      h.posts.push(req.postDataJSON());
      h.latest=running;
      if(h.loseSubmit) return route.abort('failed');
      h.shown=running;
      return route.fulfill({json:{async:true,seq:2}});
    }
    return route.fulfill({json:{}});
  });
  await page.goto('http://hq.test/v4');
  return h;
}
const triple = async h => h.page.locator('.approval-box button.pick').evaluate(el=>{el.click();el.click();el.click();});
async function check(h,name) {
  assert.deepEqual(h.errors,[]);
  assert.equal(await h.page.getByText('这是一张旧报价卡，请刷新页面获取当前任务状态后再操作。',{exact:true}).count(),0);
  await h.page.screenshot({path:path.join(out,`quote-${name}.png`),fullPage:true});
  results.push({name,readOnlyRefreshes:h.reads,confirmationPosts:h.posts.length});
  await h.page.close();
}
try {
  // Same intermediate snapshot as the production screenshot: pending token is gone.
  let h=await harness(quote(''),running);
  await h.page.waitForFunction(()=>!document.querySelector('.approval-box') && document.body.innerText.includes('原任务已提交'),null,{timeout:6000});
  assert.equal(h.posts.length,0);
  assert.ok(h.reads>=1);
  await check(h,'consumed-without-id');

  h=await harness(quote(''));
  await h.page.getByRole('button',{name:'重新获取任务状态',exact:true}).waitFor();
  assert.equal(await h.page.locator('.approval-box .wr-sub').count(),0,'legacy quote must not advertise expired pricing');
  const reads=h.reads;
  await h.page.waitForTimeout(3300); // repeated status snapshot must not start a refresh loop
  assert.equal(h.reads,reads);
  await triple(h);
  await h.page.getByRole('button',{name:'重新获取任务状态',exact:true}).waitFor();
  assert.equal(h.reads,reads+1);
  assert.equal(h.posts.length,0);
  await check(h,'legacy-refresh-no-spam');

  const a='a'.repeat(64), b='b'.repeat(64);
  h=await harness(quote(a),quote(''));
  await h.page.locator('.approval-box button.pick').waitFor();
  await triple(h);
  await h.page.getByRole('button',{name:'重新获取任务状态',exact:true}).waitFor();
  assert.equal(await h.page.locator(`.approval-box[data-quote-id="${a}"]`).count(),0);
  assert.equal(h.posts.length,0);
  await check(h,'valid-to-legacy');

  h=await harness(quote(a),quote(b,3));
  await h.page.locator('.approval-box button.pick').waitFor();
  await triple(h);
  await h.page.locator(`.approval-box[data-quote-id="${b}"]`).waitFor();
  assert.equal(h.posts.length,0,'click on one-point quote cannot approve replacement three-point quote');
  assert.match(await h.page.locator('.approval-box .wr-sub').innerText(),/本次 3 点/);
  await h.page.waitForTimeout(3300); // delayed old status must not resurrect quote A
  assert.equal(await h.page.locator(`.approval-box[data-quote-id="${a}"]`).count(),0);
  await triple(h);
  await h.page.waitForFunction(()=>document.body.innerText.includes('已收到确认'));
  assert.equal(h.posts.length,1);
  assert.deepEqual(h.posts[0].approval,{domain:'collect',quote_id:b,decision:'confirm'});
  await h.page.locator('.approval-box').waitFor({state:'detached',timeout:6000});
  await check(h,'old-to-new-price');

  h=await harness(quote(a)); h.failState=true;
  await h.page.locator('.approval-box button.pick').waitFor();
  await triple(h);
  await h.page.getByText('暂时无法获取最新任务状态。本次未提交，请点击重试。',{exact:true}).waitFor();
  assert.equal(h.posts.length,0);
  assert.equal(h.reads,1);
  h.failState=false; h.loseSubmit=true;
  await triple(h);
  await h.page.getByRole('button',{name:'重试当前报价确认',exact:true}).waitFor();
  assert.equal(h.posts.length,1);
  await triple(h); // submit succeeded upstream but response lost: consult receipt, don't re-submit
  await h.page.locator('.approval-box').waitFor({state:'detached',timeout:6000});
  assert.equal(h.posts.length,1);
  await check(h,'refresh-failure-and-lost-ack');
  fs.writeFileSync(path.join(out,'quote-lifecycle-results.json'),JSON.stringify(results,null,2));
  console.log(JSON.stringify(results));
} finally {await browser.close();}
