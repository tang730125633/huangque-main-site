const fs = require('fs');
const path = require('path');
const vm = require('vm');

class ClassList { constructor(){this.names=new Set()} add(x){this.names.add(x)} remove(x){this.names.delete(x)} toggle(x,on){if(on)this.add(x);else this.remove(x)} }
function styleObject(){return {setProperty(k,v){this[k]=String(v)}}}
class Element {
  constructor(tag='div',id=''){this.tagName=tag.toUpperCase();this.id=id;this.value='';this.checked=false;this.disabled=false;this.children=[];this.style=styleObject();this.attributes={};this.listeners={};this.classList=new ClassList();this._text='';this.href='';this.src='';this.preload='';this.loadCount=0;this.pauseCount=0;this.onerror=null;this.onloadeddata=null;this.oncanplay=null;this._hqMediaRetryTimer=0}
  get textContent(){return this._text} set textContent(v){this._text=String(v);if(v==='')this.children=[]}
  set innerHTML(v){this._html=String(v)} get innerHTML(){return this._html||''}
  appendChild(x){this.children.push(x);return x} setAttribute(k,v){this.attributes[k]=String(v);if(k==='style'){this.style=styleObject();for(const part of String(v).split(';')){const index=part.indexOf(':');if(index>0)this.style.setProperty(part.slice(0,index).trim(),part.slice(index+1).trim())}}}
  getAttribute(k){return this.attributes[k]}
  addEventListener(k,fn){(this.listeners[k]||=[]).push(fn)}
  load(){this.loadCount++}
  pause(){this.pauseCount++}
}
function response(status,data){return {status,text:()=>Promise.resolve(JSON.stringify(data||{}))}}
async function flush(n=12){for(let i=0;i<n;i++)await new Promise(r=>setImmediate(r))}
function pendingCleared(storage){return ![...storage.keys()].some(key=>key.startsWith('hq-matrix-template-pending-v1')||key.startsWith('hq-matrix-template-pending-v2'))}
function actionState(runtime){const button=runtime.get('generateBtn');return {busy:button.getAttribute('aria-busy')==='true',enabled:!button.disabled,text:button.textContent,title:button.title||''}}

function createRuntime(plan, storage){
  const page=fs.readFileSync(path.join(__dirname,'..','site','workbench','matrix-template.html'),'utf8');
  const source=[...page.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).filter(x=>x.trim()).pop();
  const elements=new Map();
  for(const m of page.matchAll(/<([a-z0-9-]+)[^>]*\sid="([^"]+)"[^>]*>/gi))elements.set(m[2],new Element(m[1],m[2]));
  const get=id=>elements.get(id)||(elements.set(id,new Element('div',id)),elements.get(id));
  const timers=[];const requests={auth:[],voices:[],post:[],poll:[],confirm:[]};let uuidCount=0,timerId=0,authUsername=plan.username||'alice';
  const sessionStorage={getItem:k=>storage.has(k)?storage.get(k):null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  const fetch=(url,options={})=>{
    if(url==='/api/auth/me'){const index=requests.auth.length;requests.auth.push({username:authUsername});return plan.auth?plan.auth(index,authUsername):Promise.resolve(response(200,{user:{username:authUsername}}))}
    if(url==='/api/gen/audio/voices'){const index=requests.voices.length;requests.voices.push({username:authUsername});return plan.voices?plan.voices(index,authUsername):Promise.resolve(response(200,{items:[{scope:'public',voice_key:'S_d21F8OR62',display_name:'温柔女声',preview_url:'/public.mp3',provider_voice:'longwan',status:'ready',ready:true},{scope:'personal',voice_key:'vip_alice',display_name:'我的复刻音色',preview_url:'/personal.mp3',provider_voice:'cosyvoice-v3.5-plus-alice',status:'ready',ready:true},{scope:'personal',voice_key:'vip_training',display_name:'训练中音色',preview_url:'/training.mp3',provider_voice:'cosyvoice-v3.5-plus-training',status:'training',ready:false},{scope:'personal',voice_key:'vip_failed',display_name:'失败音色',preview_url:'/failed.mp3',provider_voice:'cosyvoice-v3.5-plus-failed',status:'failed',ready:false}]}))}
    if(url==='/api/gen/matrix-template/templates')return Promise.resolve(response(200,{templates:[{id:'full-overlay-bold',name:'沉浸强标题',tags:['原生'],font_selectable:true},{id:'poster-split',name:'三段式活动海报',tags:['原生'],font_selectable:true},{id:'native-bold',name:'默认原生大字',tags:['默认'],font_selectable:true},{id:'minimal-headline',name:'极简标题',tags:['极简'],font_selectable:true},{id:'ref-01-fixture-01',name:'参考模板',description:'绿色粗描边手写标题',tags:['内置字体'],engine:'hyperframes',font_mode:'template_locked',font_selectable:false,variant:'v01'}],fonts:[{value:'',label:'自动搭配',source:'automatic'},{value:'Noto Sans SC',label:'思源黑体',source:'bundled'},{value:'AaHouDiHei',label:'Aa厚底黑',source:'private'}],default_template:'native-bold',default_font:'',max_batch_size:5,engine_concurrency:{ffmpeg:5,hyperframes:2},cost:5}));
    if(url==='/api/gen/matrix-template'){
      const index=requests.post.length;requests.post.push({url,options,account:authUsername});return plan.post(index,options);
    }
    if(url.startsWith('/api/gen/job/')){
      const index=requests.poll.length;requests.poll.push({url,options,account:authUsername});return plan.poll(index,options);
    }
    return Promise.resolve(response(200,{}));
  };
  const documentListeners={};const windowListeners={};
  const document={getElementById:get,createElement:t=>new Element(t),documentElement:{scrollWidth:390},hidden:false,addEventListener:(k,fn)=>{(documentListeners[k]||=[]).push(fn)}};
  const context={document,window:null,fetch,sessionStorage,location:{href:''},confirm:message=>{requests.confirm.push(message);return true},crypto:{randomUUID:()=>`uuid-${++uuidCount}`},Date,Math,JSON,Promise,Object,Array,String,Error,console,clearTimeout:id=>{const timer=timers.find(item=>item.id===id);if(timer)timer.active=false},setTimeout:(fn,delay=0)=>{const timer={id:++timerId,fn,delay,active:true};timers.push(timer);return timer.id},addEventListener:(k,fn)=>{(windowListeners[k]||=[]).push(fn)}};
  context.window=context;vm.createContext(context);vm.runInContext(source,context);
  return {get,has:id=>elements.has(id),requests,timers,storage,switchUsername:name=>{authUsername=name},runTimer:async()=>{while(timers.length){const timer=timers.shift();if(timer.active){timer.active=false;timer.fn();await flush();return}}},triggerWindow:async name=>{for(const fn of windowListeners[name]||[])fn();await flush()},triggerDocument:async name=>{for(const fn of documentListeners[name]||[])fn();await flush()},flush};
}

async function fillAndSubmit(runtime){
  await flush();runtime.get('topText').value='AI 工作流';runtime.get('bottomText').value='评论区留下关键词';runtime.get('topText').listeners.input[0]();runtime.get('bottomText').listeners.input[0]();runtime.get('generateBtn').onclick();await flush();
}

async function scenarioPostLoss(){
  const storage=new Map();
  const runtime=createRuntime({
    post:(i)=>i===0?Promise.reject(new Error('lost')):Promise.resolve(response(200,{job_id:7})),
    poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/video',duration:8}})),
  },storage);
  await fillAndSubmit(runtime);await runtime.runTimer();await flush();
  return {keys:runtime.requests.post.map(x=>x.options.headers['Idempotency-Key']),bodies:runtime.requests.post.map(x=>JSON.parse(x.options.body)),posts:runtime.requests.post.length,cleared:pendingCleared(storage)};
}
async function scenarioInProgress(){
  const storage=new Map();
  const runtime=createRuntime({
    post:(i)=>Promise.resolve(i===0?response(409,{code:'idempotency_in_progress'}):response(200,{job_id:8})),
    poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/video',duration:8}})),
  },storage);
  await fillAndSubmit(runtime);await runtime.runTimer();await flush();
  return {keys:runtime.requests.post.map(x=>x.options.headers['Idempotency-Key']),cleared:pendingCleared(storage)};
}
async function scenarioRefresh(){
  const storage=new Map();
  const first=createRuntime({post:()=>Promise.resolve(response(200,{job_id:9})),poll:()=>Promise.resolve(response(200,{status:'pending'}))},storage);
  await fillAndSubmit(first);
  const second=createRuntime({post:()=>Promise.reject(new Error('should not post')),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/video',duration:8}}))},storage);
  await flush();
  return {secondPosts:second.requests.post.length,secondPolls:second.requests.poll.length,cleared:pendingCleared(storage)};
}
async function scenarioPollFailure(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:10})),poll:i=>i===0?Promise.reject(new Error('temporary')):Promise.resolve(response(200,{status:'done',result:{video_url:'/video',duration:8}}))},storage);
  await fillAndSubmit(runtime);const afterFailure=actionState(runtime);await runtime.runTimer();await flush();
  return {polls:runtime.requests.poll.length,afterFailure,cleared:pendingCleared(storage)};
}
async function scenarioPollHttpFailure(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:11})),poll:i=>Promise.resolve(i===0?response(503,{detail:'poll unavailable'}):response(200,{status:'done',result:{video_url:'/http-poll-recovered-video',duration:8}}))},storage);
  await fillAndSubmit(runtime);const before={polls:runtime.requests.poll.length,action:actionState(runtime),cleared:pendingCleared(storage)};await runtime.runTimer();await flush(20);
  return {before,polls:runtime.requests.poll.length,src:runtime.get('video').src,cleared:pendingCleared(storage)};
}
async function scenarioPollRecoveryBeyondFive(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:10})),poll:i=>i<6?Promise.reject(new Error('poll unavailable')):Promise.resolve(response(200,{status:'done',result:{video_url:'/poll-recovered-video',duration:8}}))},storage);
  await fillAndSubmit(runtime);await flush(20);const before={polls:runtime.requests.poll.length,status:runtime.get('status').textContent,cleared:pendingCleared(storage)};
  for(let i=0;i<6;i++){await runtime.runTimer();await flush(20)}
  return {before,polls:runtime.requests.poll.length,status:runtime.get('status').textContent,src:runtime.get('video').src,cleared:pendingCleared(storage)};
}
async function scenarioInstantResult(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:13})),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/instant-video',duration:13}}))},storage);
  await fillAndSubmit(runtime);await flush(20);const video=runtime.get('video');
  return {src:video.src,display:video.style.display,loads:video.loadCount,pauses:video.pauseCount,preload:video.preload,live:runtime.get('livePreview').style.display,download:runtime.get('download').href,cleared:pendingCleared(storage)};
}
async function scenarioDelayedResultUrl(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:14})),poll:i=>Promise.resolve(response(200,{status:'done',result:i?{video_url:'/delayed-video',duration:8}:{duration:8}}))},storage);
  await fillAndSubmit(runtime);await flush(20);const before={polls:runtime.requests.poll.length,loads:runtime.get('video').loadCount,cleared:pendingCleared(storage),status:runtime.get('status').textContent};await runtime.runTimer();await flush(20);const video=runtime.get('video');
  return {before,polls:runtime.requests.poll.length,src:video.src,display:video.style.display,loads:video.loadCount,cleared:pendingCleared(storage)};
}

async function scenarioLongDelayedResultUrl(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:17})),poll:i=>Promise.resolve(response(200,{status:'done',result:i>=8?{video_url:'/slow-video',duration:8}:{duration:8}}))},storage);
  await fillAndSubmit(runtime);await flush(20);for(let i=0;i<8;i++)await runtime.runTimer();await flush(20);const video=runtime.get('video');
  return {polls:runtime.requests.poll.length,src:video.src,loads:video.loadCount,status:runtime.get('status').textContent,cleared:pendingCleared(storage)};
}

async function scenarioForegroundResume(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:15})),poll:i=>Promise.resolve(response(200,i?{status:'done',result:{video_url:'/focus-video',duration:8}}:{status:'pending'}))},storage);
  await fillAndSubmit(runtime);await flush(20);const before={polls:runtime.requests.poll.length,loads:runtime.get('video').loadCount,cleared:pendingCleared(storage)};await runtime.triggerWindow('focus');await flush(20);const video=runtime.get('video');
  return {before,polls:runtime.requests.poll.length,src:video.src,display:video.style.display,loads:video.loadCount,cleared:pendingCleared(storage)};
}

async function scenarioMediaRetry(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:16})),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/retry-video',duration:8}}))},storage);
  await fillAndSubmit(runtime);await flush(20);const video=runtime.get('video'),before={src:video.src,loads:video.loadCount,preload:video.preload};video.onerror();await runtime.runTimer();await flush(20);const after={src:video.src,loads:video.loadCount};if(video.onloadeddata)video.onloadeddata();
  return {before,after,download:runtime.get('download').href,cleared:pendingCleared(storage)};
}

async function scenarioLivePreview(){
  const runtime=createRuntime({post:()=>Promise.reject(new Error('unused')),poll:()=>Promise.reject(new Error('unused'))},new Map());
  await flush();runtime.get('topText').value='实时标题';runtime.get('bottomText').value='实时行动文案';runtime.get('topText').listeners.input[0]();runtime.get('bottomText').listeners.input[0]();runtime.get('templateGrid').children[1].onclick();
  const style=runtime.get('livePreview').style;return {top:runtime.get('liveTop').textContent,bottom:runtime.get('liveBottom').textContent,template:runtime.get('livePreview').attributes['data-template'],liveBg:style['--live-bg'],liveFg:style['--live-fg'],liveAccent:style['--live-accent'],videoDisplay:runtime.get('video').style.display};
}
async function scenarioActionPrerequisites(){
  const runtime=createRuntime({post:()=>Promise.reject(new Error('unused')),poll:()=>Promise.reject(new Error('unused'))},new Map());
  await flush();const empty=actionState(runtime),emptyBefore={auth:runtime.requests.auth.length,post:runtime.requests.post.length,poll:runtime.requests.poll.length,confirm:runtime.requests.confirm.length};
  runtime.get('generateBtn').onclick();await flush();const emptyReminder={status:runtime.get('status').textContent,toast:runtime.get('toast').textContent,auth:runtime.requests.auth.length-emptyBefore.auth,post:runtime.requests.post.length-emptyBefore.post,poll:runtime.requests.poll.length-emptyBefore.poll,confirm:runtime.requests.confirm.length-emptyBefore.confirm};
  runtime.get('topText').value='有效标题';runtime.get('topText').listeners.input[0]();const topOnly=actionState(runtime),topOnlyBefore={auth:runtime.requests.auth.length,post:runtime.requests.post.length,poll:runtime.requests.poll.length,confirm:runtime.requests.confirm.length};
  runtime.get('generateBtn').onclick();await flush();const topOnlyReminder={status:runtime.get('status').textContent,toast:runtime.get('toast').textContent,auth:runtime.requests.auth.length-topOnlyBefore.auth,post:runtime.requests.post.length-topOnlyBefore.post,poll:runtime.requests.poll.length-topOnlyBefore.poll,confirm:runtime.requests.confirm.length-topOnlyBefore.confirm};
  runtime.get('topText').value='';runtime.get('topText').listeners.input[0]();runtime.get('bottomText').value='有效行动文案';runtime.get('bottomText').listeners.input[0]();const bottomOnly=actionState(runtime),bottomOnlyBefore={auth:runtime.requests.auth.length,post:runtime.requests.post.length,poll:runtime.requests.poll.length,confirm:runtime.requests.confirm.length};
  runtime.get('generateBtn').onclick();await flush();const bottomOnlyReminder={status:runtime.get('status').textContent,toast:runtime.get('toast').textContent,auth:runtime.requests.auth.length-bottomOnlyBefore.auth,post:runtime.requests.post.length-bottomOnlyBefore.post,poll:runtime.requests.poll.length-bottomOnlyBefore.poll,confirm:runtime.requests.confirm.length-bottomOnlyBefore.confirm};
  runtime.get('topText').value='有效标题';runtime.get('topText').listeners.input[0]();const complete=actionState(runtime);
  return {empty,emptyReminder,topOnly,topOnlyReminder,bottomOnly,bottomOnlyReminder,complete};
}
async function scenarioTemplateVisibility(){
  const runtime=createRuntime({post:()=>Promise.reject(new Error('unused')),poll:()=>Promise.reject(new Error('unused'))},new Map());
  await flush();const cards=runtime.get('templateGrid').children;
  return {count:cards.length,html:cards.map(card=>card.innerHTML).join('\n'),selectedName:runtime.get('templateName').textContent,active:runtime.get('livePreview').attributes['data-template']};
}
async function scenarioHiddenTemplatePendingRecovery(){
  const storage=new Map([['hq-matrix-template-pending-v2:alice',JSON.stringify({owner:'alice',started_at:Date.now(),items:[{key:'hidden-template-key',body:{top_text:'旧标题',bottom_text:'旧行动文案',template_id:'full-overlay-bold',bgm:true},job_id:'',status:'uncertain',result:null,error:'',refund_status:''}]})]]);
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:17})),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/hidden-template-video',duration:8}}))},storage);
  await flush(30);
  return {body:JSON.parse(runtime.requests.post[0].options.body),src:runtime.get('video').src,active:runtime.get('livePreview').attributes['data-template'],cleared:pendingCleared(storage)};
}
async function scenarioVoiceoverSubmission(){
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:18})),poll:()=>Promise.resolve(response(200,{status:'pending'}))},new Map());
  await flush(30);runtime.get('topText').value='配音标题';runtime.get('bottomText').value='评论区扣关键词';runtime.get('topText').listeners.input[0]();runtime.get('bottomText').listeners.input[0]();runtime.get('voiceoverEnabled').checked=true;runtime.get('voiceoverEnabled').listeners.change[0]();runtime.get('voiceoverText').value='这是一段完整口播文案';runtime.get('voiceoverText').listeners.input[0]();const publicOptions=runtime.get('voiceoverVoice').children.map(item=>item.value);runtime.get('personalVoiceTab').onclick();runtime.get('voiceoverVoice').value='vip_alice';runtime.get('voiceoverVoice').listeners.change[0].call(runtime.get('voiceoverVoice'));runtime.get('generateBtn').onclick();await flush(30);
  return {body:JSON.parse(runtime.requests.post[0].options.body),panelHidden:runtime.get('voiceoverPanel').hidden,scope:runtime.get('voiceScopeLabel').textContent,publicOptions,options:runtime.get('voiceoverVoice').children.map(item=>item.value),count:runtime.get('voiceoverCount').textContent};
}
async function scenarioVoiceoverValidation(){
  const runtime=createRuntime({post:()=>Promise.reject(new Error('unused')),poll:()=>Promise.reject(new Error('unused'))},new Map());
  await flush(30);runtime.get('topText').value='配音标题';runtime.get('bottomText').value='评论区扣关键词';runtime.get('topText').listeners.input[0]();runtime.get('bottomText').listeners.input[0]();runtime.get('voiceoverEnabled').checked=true;runtime.get('voiceoverEnabled').listeners.change[0]();const before={auth:runtime.requests.auth.length,post:runtime.requests.post.length,confirm:runtime.requests.confirm.length};runtime.get('generateBtn').onclick();await flush(20);
  return {status:runtime.get('status').textContent,toast:runtime.get('toast').textContent,auth:runtime.requests.auth.length-before.auth,posts:runtime.requests.post.length-before.post,confirms:runtime.requests.confirm.length-before.confirm};
}
async function scenarioVoiceoverRestore(){
  const storage=new Map([['hq-matrix-template-pending-v2:alice',JSON.stringify({owner:'alice',started_at:Date.now(),items:[{key:'voiceover-restore-key',body:{top_text:'恢复标题',bottom_text:'恢复行动文案',template_id:'native-bold',bgm:false,voiceover:{text:'恢复后的完整口播',voice:'vip_alice',speed:1,pitch:0,volume:0,delivery:'natural',voice_scope:'personal'}},job_id:19,status:'pending',result:null,error:'',refund_status:''}]})]]);
  const runtime=createRuntime({post:()=>Promise.reject(new Error('should not post')),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/voiceover-restored-video',duration:9,voiceover:{enabled:true}}}))},storage);
  await flush(40);return {enabled:runtime.get('voiceoverEnabled').checked,panelHidden:runtime.get('voiceoverPanel').hidden,text:runtime.get('voiceoverText').value,scope:runtime.get('voiceScopeLabel').textContent,voice:runtime.get('voiceoverVoice').value,posts:runtime.requests.post.length,src:runtime.get('video').src,meta:runtime.get('meta').textContent,cleared:pendingCleared(storage)};
}
async function scenarioAutomaticFont(){
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:11})),poll:()=>Promise.resolve(response(200,{status:'pending'}))},new Map());
  await flush();runtime.get('topText').value='自动字体标题';runtime.get('bottomText').value='自动字体行动文案';runtime.get('topText').listeners.input[0]();runtime.get('bottomText').listeners.input[0]();runtime.get('generateBtn').onclick();await flush();
  return {body:JSON.parse(runtime.requests.post[0].options.body),fontControl:runtime.has('fontFamily'),fontSource:runtime.has('fontSource')};
}
async function scenarioLockedTemplateBatch(){
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:12})),poll:()=>Promise.resolve(response(200,{status:'pending'}))},new Map());
  await flush();runtime.get('templateGrid').children[2].onclick();runtime.get('batchCount').value='5';runtime.get('topText').value='固定字体标题';runtime.get('bottomText').value='固定字体行动文案';runtime.get('topText').listeners.input[0]();runtime.get('bottomText').listeners.input[0]();runtime.get('generateBtn').onclick();await flush();
  return {body:JSON.parse(runtime.requests.post[0].options.body),bodies:runtime.requests.post.map(x=>JSON.parse(x.options.body)),posts:runtime.requests.post.length,batchDisabled:runtime.get('batchCount').disabled,batchValue:runtime.get('batchCount').value,batchHint:runtime.get('batchHint').textContent};
}
async function scenarioBatchFive(){
  const storage=new Map();
  const runtime=createRuntime({
    post:(i)=>Promise.resolve(response(200,{job_id:100+i})),
    poll:(i)=>Promise.resolve(response(200,{status:'done',result:{video_url:'/video-'+i,duration:8+i/10}})),
  },storage);
  await flush();runtime.get('batchCount').value='5';await fillAndSubmit(runtime);await flush(30);
  const cards=runtime.get('batchResults').children;return {posts:runtime.requests.post.length,polls:runtime.requests.poll.length,keys:runtime.requests.post.map(x=>x.options.headers['Idempotency-Key']),bodies:runtime.requests.post.map(x=>JSON.parse(x.options.body)),batchHint:runtime.get('batchHint').textContent,batchLabels:runtime.get('batchCount').children.map(option=>option.textContent),cards:cards.length,preloads:cards.map(card=>card.children.find(child=>child.tagName==='VIDEO').preload),loads:cards.map(card=>card.children.find(child=>child.tagName==='VIDEO').loadCount),cleared:pendingCleared(storage)};
}
async function scenarioLegacyPending(){
  const storage=new Map([['hq-matrix-template-pending-v1:alice',JSON.stringify({owner:'alice',key:'legacy-key',body:{top_text:'旧标题',bottom_text:'旧行动文案',template_id:'native-bold',bgm:true},job_id:88,started_at:1})]]);
  const runtime=createRuntime({post:()=>Promise.reject(new Error('should not post')),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/legacy-video',duration:8}}))},storage);
  await flush(30);return {posts:runtime.requests.post.length,polls:runtime.requests.poll.length,cleared:pendingCleared(storage)};
}
async function scenarioMixedFailureReload(){
  const storage=new Map();
  const first=createRuntime({
    post:(i)=>Promise.resolve(i===0?response(429,{detail:'任务队列已满'}):response(200,{job_id:200+i})),
    poll:(i)=>Promise.resolve(response(200,{status:'done',result:{video_url:'/mixed-'+i,duration:8}})),
  },storage);
  await flush();first.get('batchCount').value='5';await fillAndSubmit(first);await flush(30);
  const beforeCards=first.get('batchResults').children;
  const failed=beforeCards.find(card=>String(card.className).indexOf('failed')>=0);
  const second=createRuntime({post:()=>Promise.reject(new Error('failed item must not repost')),poll:()=>Promise.reject(new Error('terminal batch must not repoll'))},storage);
  await flush(30);
  return {beforePosts:first.requests.post.length,afterPosts:second.requests.post.length,afterPolls:second.requests.poll.length,beforeCards:beforeCards.length,afterCards:second.get('batchResults').children.length,videos:beforeCards.filter(card=>card.children.some(child=>child.tagName==='VIDEO')).length,error:failed&&failed.children[1].textContent,refund:failed&&failed.children[2].textContent,failedKeyAttempts:first.requests.post.filter(call=>call.options.headers['Idempotency-Key']==='matrix-template-uuid-1').length,pendingCleared:pendingCleared(storage)};
}
async function scenarioJobFailureRefund(){
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:300})),poll:()=>Promise.resolve(response(200,{status:'failed',error:'渲染失败',refunded:true}))},new Map());
  await fillAndSubmit(runtime);await flush(20);const card=runtime.get('batchResults').children[0];return {cards:runtime.get('batchResults').children.length,error:card.children[1].textContent,refund:card.children[2].textContent,action:actionState(runtime)};
}
async function scenarioRefundPendingThenConfirmed(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(202,{job_id:301,refund_state:'pending'})),poll:i=>Promise.resolve(response(200,{status:'failed',error:'任务队列已满',refunded:i>0}))},storage);
  await fillAndSubmit(runtime);await flush(20);var card=runtime.get('batchResults').children[0],before=card.children[2].textContent,beforeAction=actionState(runtime);await runtime.runTimer();await flush(20);card=runtime.get('batchResults').children[0];return {polls:runtime.requests.poll.length,before,beforeAction,after:card.children[2].textContent,afterAction:actionState(runtime),title:card.children[0].textContent,cards:runtime.get('batchResults').children.length,cleared:pendingCleared(storage)};
}

async function scenarioBusyActionChecksWithoutDuplicate(){
  let finishPoll;
  const pollResponse=new Promise(resolve=>{finishPoll=resolve});
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:306})),poll:()=>pollResponse},new Map());
  await fillAndSubmit(runtime);await flush(20);const before=actionState(runtime);
  runtime.get('generateBtn').onclick();await flush(20);
  const during={action:actionState(runtime),posts:runtime.requests.post.length,polls:runtime.requests.poll.length};
  finishPoll(response(200,{status:'failed',error:'渲染失败',refunded:true}));await flush(20);
  return {before,during,after:actionState(runtime),posts:runtime.requests.post.length,polls:runtime.requests.poll.length,cleared:pendingCleared(runtime.storage)};
}

async function scenarioDelayedOuterCheckAuthCannotCreateNewJob(){
  let finishPoll,releaseAuth;
  const firstPoll=new Promise(resolve=>{finishPoll=resolve});
  const delayedAuth=new Promise(resolve=>{releaseAuth=resolve});
  const runtime=createRuntime({
    auth:(index,username)=>index===5?delayedAuth:Promise.resolve(response(200,{user:{username}})),
    post:index=>Promise.resolve(response(200,{job_id:index===0?311:312})),
    poll:index=>index===0?firstPoll:Promise.resolve(response(200,{status:'done',result:{video_url:'/unexpected-second-video',duration:8}})),
  },new Map());
  await fillAndSubmit(runtime);await flush(20);
  runtime.get('generateBtn').onclick();await flush(20);
  const beforeTerminal={posts:runtime.requests.post.length,polls:runtime.requests.poll.length,confirms:runtime.requests.confirm.length,action:actionState(runtime)};
  finishPoll(response(200,{status:'done',result:{video_url:'/first-video',duration:8}}));await flush(20);
  const terminal={src:runtime.get('video').src,cleared:pendingCleared(runtime.storage),action:actionState(runtime)};
  releaseAuth(response(200,{user:{username:'alice'}}));await flush(20);
  return {beforeTerminal,terminal,posts:runtime.requests.post.length,polls:runtime.requests.poll.length,confirms:runtime.requests.confirm.length,keys:runtime.requests.post.map(call=>call.options.headers['Idempotency-Key']),src:runtime.get('video').src,action:actionState(runtime),cleared:pendingCleared(runtime.storage)};
}

async function scenarioDelayedCheckAuthCannotDuplicateAcceptedPost(){
  let releaseAuth;
  const delayedAuth=new Promise(resolve=>{releaseAuth=resolve});
  const runtime=createRuntime({
    auth:(index,username)=>index===3?delayedAuth:Promise.resolve(response(200,{user:{username}})),
    post:()=>Promise.resolve(response(200,{job_id:307})),
    poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/delayed-auth-post-video',duration:8}})),
  },new Map());
  await fillAndSubmit(runtime);await flush(20);
  const before={posts:runtime.requests.post.length,polls:runtime.requests.poll.length,auth:runtime.requests.auth.length,action:actionState(runtime)};
  runtime.get('generateBtn').onclick();await flush(20);
  const afterClick={posts:runtime.requests.post.length,polls:runtime.requests.poll.length,auth:runtime.requests.auth.length,action:actionState(runtime)};
  releaseAuth(response(200,{user:{username:'alice'}}));await flush(20);
  return {before,afterClick,after:actionState(runtime),posts:runtime.requests.post.length,polls:runtime.requests.poll.length,auth:runtime.requests.auth.length,src:runtime.get('video').src,cleared:pendingCleared(runtime.storage)};
}

async function scenarioDelayedCheckAuthCannotReviveTerminalPoll(){
  let releaseAuth;
  const delayedAuth=new Promise(resolve=>{releaseAuth=resolve});
  const runtime=createRuntime({
    auth:(index,username)=>index===4?delayedAuth:Promise.resolve(response(200,{user:{username}})),
    post:()=>Promise.resolve(response(200,{job_id:308})),
    poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/delayed-auth-poll-video',duration:8}})),
  },new Map());
  await fillAndSubmit(runtime);await flush(20);
  const before={posts:runtime.requests.post.length,polls:runtime.requests.poll.length,auth:runtime.requests.auth.length,action:actionState(runtime)};
  runtime.get('generateBtn').onclick();await flush(20);
  const afterClick={posts:runtime.requests.post.length,polls:runtime.requests.poll.length,auth:runtime.requests.auth.length,action:actionState(runtime)};
  releaseAuth(response(200,{user:{username:'alice'}}));await flush(20);
  return {before,afterClick,posts:runtime.requests.post.length,polls:runtime.requests.poll.length,auth:runtime.requests.auth.length,action:actionState(runtime),src:runtime.get('video').src,cleared:pendingCleared(runtime.storage)};
}

async function scenarioDelayedSubmitAuthHonorsLinkedJob(){
  let releaseAuth;
  const delayedAuth=new Promise(resolve=>{releaseAuth=resolve});
  const storage=new Map();
  const runtime=createRuntime({
    auth:(index,username)=>index===3?delayedAuth:Promise.resolve(response(200,{user:{username}})),
    post:()=>Promise.reject(new Error('stale auth must not submit')),
    poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/linked-job-video',duration:8}})),
  },storage);
  await fillAndSubmit(runtime);await flush(20);
  const key='hq-matrix-template-pending-v2:alice',current=JSON.parse(storage.get(key));
  current.items[0].job_id=309;current.items[0].status='pending';storage.set(key,JSON.stringify(current));
  releaseAuth(response(200,{user:{username:'alice'}}));await flush(20);
  const afterAuth={posts:runtime.requests.post.length,polls:runtime.requests.poll.length,action:actionState(runtime)};
  await runtime.triggerWindow('focus');await flush(20);
  return {afterAuth,posts:runtime.requests.post.length,polls:runtime.requests.poll.length,action:actionState(runtime),src:runtime.get('video').src,cleared:pendingCleared(storage)};
}

async function scenarioDelayedPollAuthHonorsClearedPending(){
  let releaseAuth;
  const delayedAuth=new Promise(resolve=>{releaseAuth=resolve});
  const storage=new Map();
  const runtime=createRuntime({
    auth:(index,username)=>index===4?delayedAuth:Promise.resolve(response(200,{user:{username}})),
    post:()=>Promise.resolve(response(200,{job_id:310})),
    poll:()=>Promise.resolve(response(200,{status:'pending'})),
  },storage);
  await fillAndSubmit(runtime);await flush(20);
  storage.delete('hq-matrix-template-pending-v2:alice');
  releaseAuth(response(200,{user:{username:'alice'}}));await flush(20);
  const afterAuth={polls:runtime.requests.poll.length,action:actionState(runtime),cleared:pendingCleared(storage)};
  await runtime.triggerWindow('focus');await flush(20);
  return {afterAuth,polls:runtime.requests.poll.length,action:actionState(runtime),cleared:pendingCleared(storage)};
}

async function scenarioUncertainRecoversAutomatically(){
  const key='matrix-template-stable-retry-key';
  const storage=new Map([['hq-matrix-template-pending-v2:alice',JSON.stringify({owner:'alice',started_at:Date.now()-867000,items:[{key,body:{top_text:'待确认标题',bottom_text:'待确认行动文案',template_id:'native-bold',bgm:true},job_id:'',status:'uncertain',result:null,error:'提交响应丢失',refund_status:''}]})]]);
  const runtime=createRuntime({post:i=>i<4?Promise.reject(new Error('response lost')):Promise.resolve(response(200,{job_id:401})),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/auto-recovered-video',duration:8}}))},storage);
  await flush(30);const afterLoad={posts:runtime.requests.post.length,status:runtime.get('status').textContent,action:actionState(runtime)};
  for(let i=0;i<4;i++){await runtime.runTimer();await flush(20)}
  return {afterLoad,posts:runtime.requests.post.length,keys:runtime.requests.post.map(x=>x.options.headers['Idempotency-Key']),status:runtime.get('status').textContent,src:runtime.get('video').src,cleared:pendingCleared(storage)};
}

async function scenarioStaleSubmittingRecoversAutomatically(){
  const key='matrix-template-stale-retry-key';
  const storage=new Map([['hq-matrix-template-pending-v2:alice',JSON.stringify({owner:'alice',started_at:Date.now()-120000,items:[{key,body:{top_text:'旧提交标题',bottom_text:'旧提交行动文案',template_id:'native-bold',bgm:true},job_id:'',status:'submitting',result:null,error:'',refund_status:''}]})]]);
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:402})),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/stale-recovered-video',duration:8}}))},storage);
  await flush(30);
  return {posts:runtime.requests.post.length,key:runtime.requests.post[0]&&runtime.requests.post[0].options.headers['Idempotency-Key'],status:runtime.get('status').textContent,src:runtime.get('video').src,cleared:pendingCleared(storage)};
}

async function scenarioCrossAccountPendingIsolation(){
  const aliceKey='hq-matrix-template-pending-v2:alice';
  const storage=new Map([
    [aliceKey,JSON.stringify({owner:'alice',started_at:Date.now(),items:[{key:'alice-private-key',body:{top_text:'Alice 私密标题',bottom_text:'Alice 私密文案',template_id:'native-bold',bgm:true},job_id:'',status:'uncertain',result:null,error:'',refund_status:''}]})],
    ['hq-matrix-template-pending-v2',JSON.stringify({started_at:Date.now(),items:[{key:'ownerless-key',body:{top_text:'旧状态',bottom_text:'不可恢复',template_id:'native-bold',bgm:true},job_id:'',status:'uncertain'}]})],
  ]);
  const runtime=createRuntime({username:'bob',post:()=>Promise.reject(new Error('Bob must not submit Alice state')),poll:()=>Promise.reject(new Error('Bob must not poll Alice state'))},storage);
  await flush(30);
  return {posts:runtime.requests.post.length,polls:runtime.requests.poll.length,aliceRetained:storage.has(aliceKey),ownerlessRemoved:!storage.has('hq-matrix-template-pending-v2'),top:runtime.get('topText').value};
}

async function scenarioDynamicAccountSwitchFailsClosed(){
  const storage=new Map();
  const runtime=createRuntime({post:i=>i===0?Promise.reject(new Error('response lost')):Promise.resolve(response(200,{job_id:500+i})),poll:()=>Promise.resolve(response(200,{status:'pending'}))},storage);
  await flush(30);runtime.get('batchCount').value='2';await fillAndSubmit(runtime);await flush(30);
  const before={postAccounts:runtime.requests.post.map(call=>call.account),pollAccounts:runtime.requests.poll.map(call=>call.account),alicePending:storage.has('hq-matrix-template-pending-v2:alice')};
  runtime.switchUsername('bob');await runtime.runTimer();await flush(30);await runtime.runTimer();await flush(30);
  return {before,postAccounts:runtime.requests.post.map(call=>call.account),pollAccounts:runtime.requests.poll.map(call=>call.account),bobPosts:runtime.requests.post.filter(call=>call.account==='bob').length,bobPolls:runtime.requests.poll.filter(call=>call.account==='bob').length,alicePending:storage.has('hq-matrix-template-pending-v2:alice'),bobPending:storage.has('hq-matrix-template-pending-v2:bob'),top:runtime.get('topText').value,bottom:runtime.get('bottomText').value,status:runtime.get('status').textContent};
}

async function scenarioRetryAuthFailureFailsClosed(){
  const storage=new Map();
  const runtime=createRuntime({auth:(i,username)=>Promise.resolve(i===4?response(503,{detail:'auth unavailable'}):response(200,{user:{username}})),post:()=>Promise.reject(new Error('response lost')),poll:()=>Promise.reject(new Error('poll must not run'))},storage);
  await fillAndSubmit(runtime);await flush(30);const before={posts:runtime.requests.post.length,pending:storage.has('hq-matrix-template-pending-v2:alice')};await runtime.runTimer();await flush(30);
  return {before,posts:runtime.requests.post.length,polls:runtime.requests.poll.length,pending:storage.has('hq-matrix-template-pending-v2:alice'),top:runtime.get('topText').value,status:runtime.get('status').textContent};
}

async function scenarioConcurrentStaleAuthRestoresNewOwnerOnce(){
  let resolveBobAuth;
  const bobAuth=new Promise(resolve=>{resolveBobAuth=resolve});
  const storage=new Map([['hq-matrix-template-pending-v2:bob',JSON.stringify({owner:'bob',started_at:Date.now(),items:[{key:'bob-own-key',body:{top_text:'Bob 标题',bottom_text:'Bob 文案',template_id:'native-bold',bgm:true},job_id:'',status:'uncertain',result:null,error:'',refund_status:''}]})]]);
  const runtime=createRuntime({auth:(i,username)=>username==='bob'?bobAuth:Promise.resolve(response(200,{user:{username}})),post:(i,options)=>options.headers['Idempotency-Key']==='bob-own-key'?Promise.resolve(response(200,{job_id:601})):Promise.reject(new Error('alice response lost')),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/bob-own-video',duration:8}}))},storage);
  await fillAndSubmit(runtime);await flush(30);runtime.switchUsername('bob');await runtime.runTimer();await runtime.triggerWindow('focus');resolveBobAuth(response(200,{user:{username:'bob'}}));await flush(50);
  return {postAccounts:runtime.requests.post.map(call=>call.account),postKeys:runtime.requests.post.map(call=>call.options.headers['Idempotency-Key']),bobPosts:runtime.requests.post.filter(call=>call.account==='bob').length,bobPolls:runtime.requests.poll.filter(call=>call.account==='bob').length,alicePending:storage.has('hq-matrix-template-pending-v2:alice'),bobPending:storage.has('hq-matrix-template-pending-v2:bob'),top:runtime.get('topText').value,src:runtime.get('video').src};
}

async function scenarioForegroundDoesNotDuplicateInflightRequests(){
  let acceptPost,finishPoll;
  const postResponse=new Promise(resolve=>{acceptPost=resolve});
  const pollResponse=new Promise(resolve=>{finishPoll=resolve});
  const runtime=createRuntime({post:()=>postResponse,poll:()=>pollResponse},new Map());
  await fillAndSubmit(runtime);await flush(20);
  await runtime.triggerWindow('focus');await runtime.triggerDocument('visibilitychange');await flush(20);
  const postsWhileInflight=runtime.requests.post.length;
  acceptPost(response(200,{job_id:403}));await flush(20);
  await runtime.triggerWindow('focus');await runtime.triggerDocument('visibilitychange');await flush(20);
  const pollsWhileInflight=runtime.requests.poll.length;
  finishPoll(response(200,{status:'done',result:{video_url:'/single-flight-video',duration:8}}));await flush(20);
  return {postsWhileInflight,pollsWhileInflight,src:runtime.get('video').src,cleared:pendingCleared(runtime.storage)};
}

async function scenarioHungSubmissionTimesOutAndRecovers(){
  let finishFirst;
  const firstResponse=new Promise(resolve=>{finishFirst=resolve});
  const runtime=createRuntime({post:i=>i===0?firstResponse:Promise.resolve(response(200,{job_id:404})),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/timeout-recovered-video',duration:8}}))},new Map());
  await fillAndSubmit(runtime);await flush(20);
  const before={posts:runtime.requests.post.length,status:runtime.get('status').textContent};
  await runtime.runTimer();await flush(20);
  const afterTimeout={posts:runtime.requests.post.length,status:runtime.get('status').textContent,cleared:pendingCleared(runtime.storage)};
  await runtime.runTimer();await flush(30);
  const afterRecovery={posts:runtime.requests.post.length,keys:runtime.requests.post.map(call=>call.options.headers['Idempotency-Key']),src:runtime.get('video').src,cleared:pendingCleared(runtime.storage)};
  finishFirst(response(200,{job_id:999}));await flush(30);
  return {before,afterTimeout,afterRecovery,afterLateResponse:{posts:runtime.requests.post.length,polls:runtime.requests.poll.length,src:runtime.get('video').src,cleared:pendingCleared(runtime.storage)}};
}

async function scenarioHungPollTimesOutAndRecovers(){
  let finishFirst;
  const firstResponse=new Promise(resolve=>{finishFirst=resolve});
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:405})),poll:i=>i===0?firstResponse:Promise.resolve(response(200,{status:'done',result:{video_url:'/timeout-poll-recovered-video',duration:8}}))},new Map());
  await fillAndSubmit(runtime);await flush(20);
  const before={polls:runtime.requests.poll.length,action:actionState(runtime),cleared:pendingCleared(runtime.storage)};
  await runtime.runTimer();await flush(20);
  const afterTimeout={polls:runtime.requests.poll.length,action:actionState(runtime),cleared:pendingCleared(runtime.storage)};
  await runtime.runTimer();await flush(30);
  const afterRecovery={polls:runtime.requests.poll.length,src:runtime.get('video').src,cleared:pendingCleared(runtime.storage)};
  finishFirst(response(200,{status:'failed',error:'late stale failure',refunded:true}));await flush(30);
  return {before,afterTimeout,afterRecovery,afterLateResponse:{polls:runtime.requests.poll.length,src:runtime.get('video').src,cleared:pendingCleared(runtime.storage)}};
}

async function main(){const name=process.argv[2];const handlers={postLoss:scenarioPostLoss,inProgress:scenarioInProgress,refresh:scenarioRefresh,pollFailure:scenarioPollFailure,pollHttpFailure:scenarioPollHttpFailure,pollRecoveryBeyondFive:scenarioPollRecoveryBeyondFive,instantResult:scenarioInstantResult,delayedResultUrl:scenarioDelayedResultUrl,longDelayedResultUrl:scenarioLongDelayedResultUrl,foregroundResume:scenarioForegroundResume,mediaRetry:scenarioMediaRetry,livePreview:scenarioLivePreview,actionPrerequisites:scenarioActionPrerequisites,templateVisibility:scenarioTemplateVisibility,hiddenTemplatePendingRecovery:scenarioHiddenTemplatePendingRecovery,voiceoverSubmission:scenarioVoiceoverSubmission,voiceoverValidation:scenarioVoiceoverValidation,voiceoverRestore:scenarioVoiceoverRestore,automaticFont:scenarioAutomaticFont,lockedTemplateBatch:scenarioLockedTemplateBatch,batchFive:scenarioBatchFive,legacyPending:scenarioLegacyPending,mixedFailureReload:scenarioMixedFailureReload,jobFailureRefund:scenarioJobFailureRefund,refundPendingThenConfirmed:scenarioRefundPendingThenConfirmed,busyActionCheck:scenarioBusyActionChecksWithoutDuplicate,delayedOuterCheckAuth:scenarioDelayedOuterCheckAuthCannotCreateNewJob,delayedPostAuth:scenarioDelayedCheckAuthCannotDuplicateAcceptedPost,delayedPollAuth:scenarioDelayedCheckAuthCannotReviveTerminalPoll,linkedJobDuringAuth:scenarioDelayedSubmitAuthHonorsLinkedJob,clearedPendingDuringAuth:scenarioDelayedPollAuthHonorsClearedPending,uncertainAutoRecovery:scenarioUncertainRecoversAutomatically,staleSubmittingAutoRecovery:scenarioStaleSubmittingRecoversAutomatically,crossAccountPending:scenarioCrossAccountPendingIsolation,dynamicAccountSwitch:scenarioDynamicAccountSwitchFailsClosed,retryAuthFailure:scenarioRetryAuthFailureFailsClosed,concurrentStaleAuth:scenarioConcurrentStaleAuthRestoresNewOwnerOnce,foregroundSingleFlight:scenarioForegroundDoesNotDuplicateInflightRequests,hungSubmissionTimeout:scenarioHungSubmissionTimesOutAndRecovers,hungPollTimeout:scenarioHungPollTimesOutAndRecovers};if(!handlers[name])throw new Error('unknown scenario');process.stdout.write(JSON.stringify(await handlers[name]()))}
main().catch(e=>{console.error(e.stack||e);process.exitCode=1});
