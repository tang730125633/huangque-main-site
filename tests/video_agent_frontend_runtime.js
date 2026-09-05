'use strict';
const assert=require('node:assert/strict');
const canvas=require('../site/workbench/video-agent-canvas.js');
const session=require('../site/workbench/video-agent-session.js');
const routing=require('../site/workbench/video-agent-routing.js');

assert.deepEqual(canvas.clampPosition(-1,2),{x:0,y:1});
const card={x:.5,y:.5,width:300,height:240};
assert.equal(canvas.move(card,'ArrowLeft',false),true);
assert.equal(card.x,.49);
assert.equal(canvas.resize(card,'ArrowUp',true,{width:300,height:240}),true);
assert.equal(card.height,220);
assert.equal(session.isFresh(1000,1001),true);
assert.equal(session.isFresh(1001,1000),false);
assert.equal(session.isFresh(1,session.TTL_MS+2),false);
const routes={talking:{function:'talking'},story:{function:'cinematic',cineMode:'open'},motion:{function:'cinematic',cineMode:'motion'},create:{function:'grok'},tryon:{function:'tryon'},compose:{href:'one-click-video.html'}};
assert.deepEqual(Object.keys(routes).map(key=>routing.handoffTarget(routes[key])),['talking','open','motion','create','tryon','link']);
assert.equal(routing.effectiveRoute({function:'minimax'},true,false,routes.create),routes.create);
assert.equal(routing.cinematicMode(routes.story,true),'open');

const source=require('node:fs').readFileSync(require('node:path').join(__dirname,'../site/workbench/video.html'),'utf8');
assert.match(source,/story:\{function:'cinematic',cineMode:'open'/);
assert.match(source,/var effectiveRoute=openVideoWorkbench\(route\)/);
assert.match(source,/agentIdentityVerified=false/);
assert.doesNotMatch(source,/initializeAgentChatScroll\(\);\s*restoreAgentSession\(\);/);
assert.match(source,/id="agentPendingUpdateList"/);
(async()=>{
  let calls=0;
  const result=await session.reconcile(async(url,options)=>{calls++;assert.equal(url,'/api/gen/video/agent/actions/vpa_abc/reconcile');assert.equal(options.method,'POST');assert.equal(options.body,'{}');return {ok:true,json:async()=>({pending_action:{status:'submitted'}})};},'vpa_abc',{},()=>new Error('unexpected'));
  assert.equal(calls,1);assert.equal(result.pending_action.status,'submitted');
  await assert.rejects(session.reconcile(async()=>({ok:false,status:409,json:async()=>({code:'pending_reconcile_in_flight'})}),'vpa_abc',{},(response,data)=>Object.assign(new Error(data.code),{status:response.status,code:data.code})),error=>error.status===409&&error.code==='pending_reconcile_in_flight');
  console.log('video agent frontend runtime: ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
