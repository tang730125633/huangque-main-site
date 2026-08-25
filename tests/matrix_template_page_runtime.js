const fs = require('fs');
const path = require('path');
const vm = require('vm');

class ClassList { constructor(){this.names=new Set()} add(x){this.names.add(x)} remove(x){this.names.delete(x)} toggle(x,on){if(on)this.add(x);else this.remove(x)} }
class Element {
  constructor(tag='div',id=''){this.tagName=tag.toUpperCase();this.id=id;this.value='';this.checked=false;this.disabled=false;this.children=[];this.style={};this.attributes={};this.listeners={};this.classList=new ClassList();this._text='';this.href='';this.src=''}
  get textContent(){return this._text} set textContent(v){this._text=String(v);if(v==='')this.children=[]}
  set innerHTML(v){this._html=String(v)} get innerHTML(){return this._html||''}
  appendChild(x){this.children.push(x);return x} setAttribute(k,v){this.attributes[k]=String(v)}
  addEventListener(k,fn){(this.listeners[k]||=[]).push(fn)}
}
function response(status,data){return {status,text:()=>Promise.resolve(JSON.stringify(data||{}))}}
async function flush(n=12){for(let i=0;i<n;i++)await new Promise(r=>setImmediate(r))}

function createRuntime(plan, storage){
  const page=fs.readFileSync(path.join(__dirname,'..','site','workbench','matrix-template.html'),'utf8');
  const source=[...page.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(x=>x[1]).filter(x=>x.trim()).pop();
  const elements=new Map();
  for(const m of page.matchAll(/<([a-z0-9-]+)[^>]*\sid="([^"]+)"[^>]*>/gi))elements.set(m[2],new Element(m[1],m[2]));
  const get=id=>elements.get(id)||(elements.set(id,new Element('div',id)),elements.get(id));
  get('bgm').checked=true;
  const timers=[];const requests={post:[],poll:[]};let uuidCount=0;
  const sessionStorage={getItem:k=>storage.has(k)?storage.get(k):null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  const fetch=(url,options={})=>{
    if(url==='/api/gen/matrix-template/templates')return Promise.resolve(response(200,{templates:[{id:'native-bold',name:'默认原生大字',tags:['默认']}],default_template:'native-bold',cost:5}));
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
  return {keys:runtime.requests.post.map(x=>x.options.headers['Idempotency-Key']),posts:runtime.requests.post.length,cleared:storage.size===0};
}
async function scenarioInProgress(){
  const storage=new Map();
  const runtime=createRuntime({
    post:(i)=>Promise.resolve(i===0?response(409,{code:'idempotency_in_progress'}):response(200,{job_id:8})),
    poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/video',duration:8}})),
  },storage);
  await fillAndSubmit(runtime);await runtime.runTimer();await flush();
  return {keys:runtime.requests.post.map(x=>x.options.headers['Idempotency-Key']),cleared:storage.size===0};
}
async function scenarioRefresh(){
  const storage=new Map();
  const first=createRuntime({post:()=>Promise.resolve(response(200,{job_id:9})),poll:()=>Promise.resolve(response(200,{status:'pending'}))},storage);
  await fillAndSubmit(first);
  const second=createRuntime({post:()=>Promise.reject(new Error('should not post')),poll:()=>Promise.resolve(response(200,{status:'done',result:{video_url:'/video',duration:8}}))},storage);
  await flush();
  return {secondPosts:second.requests.post.length,secondPolls:second.requests.poll.length,cleared:storage.size===0};
}
async function scenarioPollFailure(){
  const storage=new Map();
  const runtime=createRuntime({post:()=>Promise.resolve(response(200,{job_id:10})),poll:i=>i===0?Promise.reject(new Error('temporary')):Promise.resolve(response(200,{status:'done',result:{video_url:'/video',duration:8}}))},storage);
  await fillAndSubmit(runtime);const busyAfterFailure=runtime.get('generateBtn').disabled;await runtime.runTimer();await flush();
  return {polls:runtime.requests.poll.length,busyAfterFailure,cleared:storage.size===0};
}

async function main(){const name=process.argv[2];const handlers={postLoss:scenarioPostLoss,inProgress:scenarioInProgress,refresh:scenarioRefresh,pollFailure:scenarioPollFailure};if(!handlers[name])throw new Error('unknown scenario');process.stdout.write(JSON.stringify(await handlers[name]()))}
main().catch(e=>{console.error(e.stack||e);process.exitCode=1});
