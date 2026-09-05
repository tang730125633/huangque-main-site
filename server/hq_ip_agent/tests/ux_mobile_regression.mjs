import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
const {chromium} = await import(process.env.HQ_PW_MODULE || 'playwright');
const root = process.cwd();
const ui = path.resolve(root, '../../site/workbench/hq-ip-agent');
const out = path.join(root, '.test-output');
fs.mkdirSync(out, {recursive:true});
const browser = await chromium.launch({...(process.env.HQ_BROWSER_CHANNEL ? {channel:process.env.HQ_BROWSER_CHANNEL} : {}), headless:true});
const results = [];
try {
  for (const width of [360,390,412]) {
    const page = await browser.newPage({viewport:{width,height:844}, deviceScaleFactor:1});
    const errors=[]; page.on('pageerror', e=>errors.push(e.message));
    let submitted=0, firstPoll=true, completed=false;
    const quoteId='a'.repeat(64);
    const delegation=()=> completed ? {collect:{state:'completed',summary:'采集结果已就绪',quote_id:''}} :
      {collect:{state:'needs_approval',summary:'已按小红书关键词取得采集报价，确认后提交采集任务并返回账号名单。'.repeat(3),
       quote_id:quoteId,quote:{cost:1,points:1939,expires_in:300}}};
    await page.route('**/*', async route=>{
      const req=route.request(), url=new URL(req.url()), p=url.pathname;
      if(p==='/v4') return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(ui,'v4.html'),'utf8')});
      if(p.startsWith('/static/')) return route.fulfill({contentType:p.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync(path.join(ui,p),'utf8')});
      if(p==='/api/health') return route.fulfill({json:{llm_mode:'live',hq_status:{ok:true}}});
      if(p==='/api/v4/start') return route.fulfill({json:{session_id:'mobile-test',async:true,seq:1,mode:'live'}});
      if(p.includes('/api/v4/stream/')) return route.fulfill({status:200,contentType:'text/event-stream',body:': heartbeat\n\n'});
      if(p.includes('/api/v4/status/')) return route.fulfill({json:{turns:[{seq:1,state:'working'}],jobs:[],delegations:delegation(),film:false}});
      if(p.includes('/api/v4/state/')) return route.fulfill({json:delegation()});
      if(p.includes('/api/v4/poll/')) {
        if(submitted && firstPoll) {firstPoll=false; return route.fulfill({json:{state:'done',seq:2,reply:'本轮采集结果已送达',delegations:delegation(),film:false}});}
        return route.fulfill({json:{state:'working'}});
      }
      if(p==='/api/v4/chat') {
        const body=req.postDataJSON();
        assert.deepEqual(body.approval,{domain:'collect',quote_id:quoteId,decision:'confirm'});
        submitted++; return route.fulfill({json:{async:true,seq:2}});
      }
      return route.fulfill({json:{}});
    });
    await page.goto('http://hq.test/v4');
    await page.locator('.approval-box').waitFor();
    await page.evaluate(()=>{
      document.querySelector('.brand-name').textContent='黄雀 · 主 Agent（v4 业务结果路由）';
      document.querySelector('#mode-badge').textContent='主 Agent 在线';
      document.querySelector('#hq-badge').textContent='已授权（9-05 13:51 到期）';
    });
    const layout=await page.evaluate(()=>{
      const rect=s=>document.querySelector(s).getBoundingClientRect();
      const nav=rect('.nav'), right=rect('.nav-right'), main=rect('.approval-box .wr-main'), actions=rect('.approval-box .wr-actions');
      return {overflow:document.documentElement.scrollWidth>innerWidth,navFits:right.bottom<=nav.bottom,
              buttonsBelow:actions.top>=main.bottom,textWidth:main.width};
    });
    assert.equal(layout.overflow,false);
    assert.equal(layout.navFits,true);
    assert.equal(layout.buttonsBelow,true);
    assert.ok(layout.textWidth>width*0.65);
    await page.screenshot({path:path.join(out,`mobile-${width}.png`),fullPage:true});
    await page.locator('.approval-box button.pick').evaluate(el=>{el.click();el.click();el.click();});
    await page.getByText('本轮采集结果已送达',{exact:true}).waitFor({timeout:15000});
    assert.equal(submitted,1,'triple click must only send one confirmation');
    assert.equal(await page.getByText('本轮采集结果已送达',{exact:true}).count(),1);
    completed=true;
    await page.locator('.approval-box').waitFor({state:'detached',timeout:6000});
    assert.deepEqual(errors,[]);
    results.push({width,...layout,confirmRequests:submitted,heldReplyDelivered:true,staleCardRemoved:true});
    await page.close();
  }
  fs.writeFileSync(path.join(out,'mobile-results.json'),JSON.stringify(results,null,2));
  console.log(JSON.stringify(results));
} finally {await browser.close();}
