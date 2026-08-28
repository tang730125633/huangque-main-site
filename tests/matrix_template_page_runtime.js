const fs = require('fs');
const path = require('path');
const vm = require('vm');

class ClassList { constructor(){this.names=new Set()} add(x){this.names.add(x)} remove(x){this.names.delete(x)} toggle(x,on){if(on)this.add(x);else this.remove(x)} }
class Element {
  constructor(tag='div',id=''){this.tagName=tag.toUpperCase();this.id=id;this.value='';this.checked=false;this.disabled=false;this.children=[];this.style={};this.attributes={};this.listeners={};this.classList=new ClassList();this._text='';this.href='';this.src='';this.preload='';this.loadCount=0;this.pauseCount=0}
  get textContent(){return this._text} set textContent(v){this._text=String(v);if(v==='')this.children=[]}
  set innerHTML(v){this._html=String(v)} get innerHTML(){return this._html||''}
  appendChild(x){this.children.push(x);return x} setAttribute(k,v){this.attributes[k]=String(v)}
  getAttribute(k){return this.attributes[k]}
  addEventListener(k,fn){(this.listeners[k]||=[]).push(fn)}
  load(){this.loadCount++}
  pause(){this.pauseCount++}
}
function response(status,data){return {status,text:()=>Promise.resolve(JSON.stringify(data||{}))}}
async function flush(n=12){for(let i=0;i<n;i++)await new Promise(r=>setImmediate(r))}
function pendingCleared(storage){return !storage.has('hq-matrix-template-pending-v1')&&!storage.has('hq-matrix-template-pending-v2')}

function createRuntime(plan, storage){
  const page=fs.readFileSync(path.join(__dirname,'..','site','workbench','matrix-template.html'),'utf8');
  const source=[...page.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).filter(x=>x.trim()).pop();
  const elements=new Map();
  for(const m of page.matchAll(/<([a-z0-9-]+)[^>]*\sid="([^"]+)"[^>]*>/gi))elements.set(m[2],new Element(m[1],m[2]));
  const get=id=>elements.get(id)||(elements.set(id,new Element('div',id)),elements.get(id));
  const timers=[];const requests={post:[],poll:[]};let uuidCount=0;
  const sessionStorage={getItem:k=>storage.has(k)?storage.get(k):null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  const fetch=(url,options={})=>{
    if(url==='/api/gen/matrix-template/templates')return Promise.resolve(response(200,{templates:[{id:'native-bold',name:'默认原生大字',tags:['默认'],font_selectable:true},{id:'minimal-headline',name:'极简标题',tags:['极简'],font_selectable:true},{id:'ref-01-fixture-01',name:'参考模板',description:'绿色粗描边手写标题',tags:['内置字体'],engine:'hyperframes',font_mode:'template_locked',font_selectable:false,variant:'v01'}],fonts:[{value:'',label:'自动搭配',source:'automatic'},{value:'Noto Sans SC',label:'思源黑体',source:'bundled'},{value:'AaHouDiHei',label:'Aa厚底黑',source:'private'}],default_template:'native-bold',default_font:'',cost:5}));
    if(url==='/api/gen/matrix-template'){
      const index=requests.post.length;requests.post.push({url,options});return plan.post(index,options);
    }
    if(url.startsWith('/api/gen/job/')){
      const index=requests.poll.length;requests.poll.push({url,options});return plan.poll(index,options);
    }
    return Promise.resolve(response(200,{}));
  };
  const document={getElementById:get,createElement:t=>new Element(t),documentElement:{scrollWidth:390}};
  const context={document,window:null,fetch,sessionStorage,location:{href:''},confirm:()=>true,crypto:{randomUUID:()=>`uuid-${++uuidCount}`},Date,Math,JSON,Promise,Object,Array,String,Error,console,clearTimeout:()=>{},setTimeout:fn=>(timers.push(fn),timers.length)};
  context.window=context;vm.createContext(context);vm.runInContext(source,context);
  return {get,requests,timers,storage,runTimer:async()=>{const fn=timers.shift();if(fn){fn();await flush()}},flush};
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
  await fillAndSubmit(runtime);const busyAfterFailure=runtime.get('generateBtn').disabled;await runtime.runTimer();await flush();
  return {polls:runtime.requests.poll.length,busyAfterFailure,cleared:pendingCleared(storage)};
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

async function scenarioLivePreview(){
  const runtime=createRuntime({post:()=>Promise.reject(new Error('unused')),poll:()=>Promise.reject(new Error('unused'))},new Map());
  await flush();runtime.get('topText').value='实时标题';runtime.get('bottomText').value='实时行动文案';runtime.get('topText').listeners.input[0]();runtime.get('bottomText').listeners.input[0]();runtime.get('templateGrid').children[1].onclick();
  return {top:runtime.get('liveTop').textContent,bottom:runtime.get('liveBottom').textContent,template:runtime.get('livePreview').attributes['data-template'],style:runtime.get('livePreview').attributes.style,videoDisplay:runtime.get('video').style.display};
}
async function scenarioFontSelect(){
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:11})),poll:()=>Promise.resolve(response(200,{status:'pending'}))},new Map());
  await flush();runtime.get('topText').value='指定字体标题';runtime.get('bottomText').value='指定字体行动文案';runtime.get('fontFamily').value='AaHouDiHei';runtime.get('fontFamily').listeners.change[0].call(runtime.get('fontFamily'));runtime.get('generateBtn').onclick();await flush();
  return {body:JSON.parse(runtime.requests.post[0].options.body),source:runtime.get('fontSource').textContent,options:runtime.get('fontFamily').children.map(x=>x.value)};
}
async function scenarioLockedFont(){
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:12})),poll:()=>Promise.resolve(response(200,{status:'pending'}))},new Map());
  await flush();runtime.get('fontFamily').value='AaHouDiHei';runtime.get('fontFamily').listeners.change[0].call(runtime.get('fontFamily'));runtime.get('templateGrid').children[2].onclick();runtime.get('topText').value='固定字体标题';runtime.get('bottomText').value='固定字体行动文案';runtime.get('topText').listeners.input[0]();runtime.get('bottomText').listeners.input[0]();runtime.get('generateBtn').onclick();await flush();
  return {body:JSON.parse(runtime.requests.post[0].options.body),disabled:runtime.get('fontFamily').disabled,value:runtime.get('fontFamily').value,source:runtime.get('fontSource').textContent,batchDisabled:runtime.get('batchCount').disabled,batchValue:runtime.get('batchCount').value,batchHint:runtime.get('batchHint').textContent};
}
async function scenarioBatchFive(){
  const storage=new Map();
  const runtime=createRuntime({
    post:(i)=>Promise.resolve(response(200,{job_id:100+i})),
    poll:(i)=>Promise.resolve(response(200,{status:'done',result:{video_url:'/video-'+i,duration:8+i/10}})),
  },storage);
  await flush();runtime.get('batchCount').value='5';await fillAndSubmit(runtime);await flush(30);
  return {posts:runtime.requests.post.length,polls:runtime.requests.poll.length,keys:runtime.requests.post.map(x=>x.options.headers['Idempotency-Key']),bodies:runtime.requests.post.map(x=>JSON.parse(x.options.body)),cards:runtime.get('batchResults').children.length,cleared:pendingCleared(storage)};
}
async function scenarioLegacyPending(){
  const storage=new Map([['hq-matrix-template-pending-v1',JSON.stringify({key:'legacy-key',body:{top_text:'旧标题',bottom_text:'旧行动文案',template_id:'native-bold',bgm:true},job_id:88,started_at:1})]]);
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
  await fillAndSubmit(runtime);await flush(20);const card=runtime.get('batchResults').children[0];return {cards:runtime.get('batchResults').children.length,error:card.children[1].textContent,refund:card.children[2].textContent};
}
async function scenarioRefundPendingThenConfirmed(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(202,{job_id:301,refund_state:'pending'})),poll:i=>Promise.resolve(response(200,{status:'failed',error:'任务队列已满',refunded:i>0}))},storage);
  await fillAndSubmit(runtime);await flush(20);var card=runtime.get('batchResults').children[0],before=card.children[2].textContent;await runtime.runTimer();await flush(20);card=runtime.get('batchResults').children[0];return {polls:runtime.requests.poll.length,before,after:card.children[2].textContent,title:card.children[0].textContent,cards:runtime.get('batchResults').children.length,cleared:pendingCleared(storage)};
}

async function main(){const name=process.argv[2];const handlers={postLoss:scenarioPostLoss,inProgress:scenarioInProgress,refresh:scenarioRefresh,pollFailure:scenarioPollFailure,instantResult:scenarioInstantResult,delayedResultUrl:scenarioDelayedResultUrl,livePreview:scenarioLivePreview,fontSelect:scenarioFontSelect,lockedFont:scenarioLockedFont,batchFive:scenarioBatchFive,legacyPending:scenarioLegacyPending,mixedFailureReload:scenarioMixedFailureReload,jobFailureRefund:scenarioJobFailureRefund,refundPendingThenConfirmed:scenarioRefundPendingThenConfirmed};if(!handlers[name])throw new Error('unknown scenario');process.stdout.write(JSON.stringify(await handlers[name]()))}
main().catch(e=>{console.error(e.stack||e);process.exitCode=1});
