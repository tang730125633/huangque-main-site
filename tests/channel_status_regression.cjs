/* 黄雀渠道管理 · 前端逻辑回归测试
   只跑纯逻辑与请求层（不触碰生产接口，不产生写操作）。 */
'use strict';
const vm=require('vm');
const fs=require('fs');
const path=require('path');

const ADMIN=path.join(__dirname,'..','site','admin');
const read=p=>fs.readFileSync(path.join(ADMIN,p),'utf8');

let passed=0,failed=0;
const failures=[];
function assert(cond,msg){if(cond){passed++;console.log('  ✓ '+msg)}else{failed++;failures.push(msg);console.error('  ✗ '+msg)}}
function section(name){console.log('\n== '+name+' ==')}

const GLOBALS={setTimeout,clearTimeout,setInterval,clearInterval,setImmediate,clearImmediate,console,Promise,Date,JSON,Math,Object,Array,String,Number,Boolean,Set,Map,Error};
function runIn(code,sandbox,filename){
  Object.assign(sandbox,GLOBALS);
  vm.createContext(sandbox);
  vm.runInContext(code,sandbox,{filename});
  return sandbox;
}

/* ---------------- 1. 纯目录逻辑 channel-catalog.js ---------------- */
section('channel-catalog.js 纯逻辑');
const cat=runIn(read('channel-catalog.js'),{}).ChannelCatalog;

const now=Date.now()/1000;
const mk=(over)=>({id:'ch1',name:'渠道A',supplier:'厂商A',adapter:'gemini_image',base_url:'https://api.x/v1',model:'gpt-x',version:3,enabled:true,checks:[],...over});

// 1) 启用但未验证
{
  const c=mk({checks:[]});
  const v=cat.verificationStatus(c,now);
  assert(v.overall.state!=='ok','启用但未验证 → 不判通过');
  assert(v.parts.auth.state==='missing','未验证的 auth 应为 missing');
}

// 2) 旧版本通过、当前版本未验证
{
  const c=mk({version:3,checks:[
    {kind:'auth',state:'passed',version:2,updated:now-100},
    {kind:'full',state:'passed',version:2,updated:now-100}
  ]});
  const v=cat.verificationStatus(c,now);
  assert(v.overall.state!=='ok','旧版本通过不能代表当前版本');
  assert(v.parts.auth.state==='stale-version','auth 应标 stale-version（仅有历史版本）');
}

// 3) 证据过期
{
  const c=mk({version:3,checks:[
    {kind:'auth',state:'passed',version:3,updated:now-90000},
    {kind:'full',state:'passed',version:3,updated:now-90000}
  ]});
  const v=cat.verificationStatus(c,now);
  assert(v.parts.auth.state==='expired','过期证据应标 expired');
  assert(v.overall.state!=='ok','过期证据不能判通过');
}

// 4) 较新异常覆盖旧成功
{
  const c=mk({version:3,checks:[
    {kind:'auth',state:'passed',version:3,updated:now-500},
    {kind:'full',state:'passed',version:3,updated:now-400},
    {kind:'full',state:'failed',version:3,updated:now-50}
  ]});
  const v=cat.verificationStatus(c,now);
  assert(v.parts.full.state==='failed','最新的 full 失败应覆盖旧的 full 通过');
  assert(v.overall.state!=='ok','存在较新异常时整体不能判通过');
}

// 5) checks 不携带 version
{
  const c=mk({version:3,checks:[
    {kind:'auth',state:'passed',updated:now-100},
    {kind:'full',state:'passed',updated:now-100}
  ]});
  const v=cat.verificationStatus(c,now);
  assert(v.parts.auth.state==='unattributed','checks 不带 version 时应标 unattributed');
  assert(v.overall.state!=='ok','无法归属当前版本的证据不能判通过');
}

// 6) 全部通过（当前版本、有效期内）
{
  const c=mk({version:3,checks:[
    {kind:'connection',state:'passed',version:3,updated:now-10},
    {kind:'auth',state:'passed',version:3,updated:now-10},
    {kind:'full',state:'passed',version:3,updated:now-10}
  ]});
  const v=cat.verificationStatus(c,now);
  assert(v.overall.state==='ok','三证据齐全且有效 → 验证通过');
}

// 7) 密钥不泄露明文
{
  const c=mk({secret:'sk-SUPER-SECRET-12345',secret_configured:true});
  const cfg=cat.configStatus(c);
  assert(cfg.key==='configured','密钥已配置标记');
  const dump=JSON.stringify(cfg);
  assert(!dump.includes('sk-SUPER-SECRET-12345'),'configStatus 输出不得包含明文密钥');
}

// 8) 影子模式不误报生产生效
{
  const data={operation_mappings:[
    {operation_id:'op1',label:'生图',state:'shadow',revision:4,channels:['ch1','ch2']},
    {operation_id:'op2',label:'生视频',state:'managed',revision:2,channels:['ch1']}
  ],mappings:[]};
  const prod=cat.productionRoles({id:'ch1'},data);
  const op1=prod.entries.find(e=>e.operationId==='op1');
  assert(op1&&op1.role==='影子候选（不改生产路由）','影子映射应显示影子候选');
  assert(prod.managed.length===1&&prod.managed[0].operationId==='op2','只有 managed 映射算托管');
  assert(prod.shadow.length===1,'影子映射单独归类');
}

// 9) 生产角色：主渠道/备用/候补
{
  const data={operation_mappings:[{operation_id:'op1',label:'生图',state:'managed',revision:7,channels:['a','b','c']}],mappings:[]};
  assert(cat.productionRoles({id:'b'},data).entries[0].role==='备用渠道','第2位是备用渠道');
  assert(cat.productionRoles({id:'c'},data).entries[0].role==='生产候补 #3','第3位是生产候补 #3');
  assert(cat.productionRoles({id:'a'},data).entries[0].role==='生产主渠道','第1位是生产主渠道');
}

// 10) 未接入
{
  const prod=cat.productionRoles({id:'ch1'},{operation_mappings:[],mappings:[]});
  assert(prod.integrated===false,'未接入任何映射');
}

// 11) 替换流程的既有契约由 tests/test_channel_catalog.js 覆盖（applyReplacement
//     只依赖 post / routeMappings / mappingChannels，并返回已暂停 operation 数组）。

/* ---------------- 2. 请求层 channel-request.js ---------------- */
section('channel-request.js 请求层');
const req=runIn(read('channel-request.js'),{}).ChannelRequest;

async function reqTests(){
  // 非 JSON
  {
    const client=req.createClient((path,opt)=>Promise.resolve({detail:'响应不是 JSON'}),{});
    await client.get('/x').then(()=>assert(false,'非JSON不应成功')).catch(e=>assert(e.name==='NonJsonError','非 JSON 响应识别为 NonJsonError'));
  }
  // 超时（写请求）
  {
    const client=req.createClient(()=>new Promise(()=>{}),{writeTimeout:30});
    await client.post('/x',{}).then(()=>assert(false,'超时不应成功')).catch(e=>assert(e.timedOut===true&&e.name==='TimeoutError','写请求超时应带 timedOut 标记（供“结果待核对”）'));
  }
  // 业务 ok:false 原样返回（由调用方判定，不误报为非JSON）
  {
    const client=req.createClient((path,opt)=>Promise.resolve({ok:false,detail:'业务失败'}),{});
    await client.get('/x').then(d=>assert(d.ok===false&&d.detail==='业务失败','ok:false 原样返回，不误报非JSON')).catch(()=>assert(false,'ok:false 不应抛异常'));
  }
  // 响应乱序 / 重复点击：latest guard
  {
    const g=req.createLatestGuard();
    const a=g.begin(),b=g.begin();
    assert(g.isLatest(a)===false,'较早请求不是最新');
    assert(g.isLatest(b)===true,'最新请求有效');
  }
}

/* ---------------- 3. 刷新失败过期 + 响应乱序（channel-manager load） ---------------- */
section('channel-manager.js 刷新/过期/乱序');
function makeEl(id){
  const attrs={};
  return {id,innerHTML:'',textContent:'',hidden:false,value:'',disabled:false,
    classList:{_s:new Set(),add(c){this._s.add(c)},remove(c){this._s.delete(c)},contains(c){return this._s.has(c)}},
    setAttribute(k,v){attrs[k]=v},removeAttribute(k){delete attrs[k]},_attrs:attrs,
    querySelector(){return makeEl(id+'::child')},querySelectorAll(){return []},
    scrollIntoView(){},focus(){},addEventListener(){}};
}
function deferred(){let resolve,reject;const promise=new Promise((res,rej)=>{resolve=res;reject=rej});return{promise,resolve,reject}}

function buildManagerEnv(apiMock){
  const elements={};
  const el=id=>elements[id]||(elements[id]=makeEl(id));
  const section=el('managedChannels');
  let rendered=null;
  const workspace={render:d=>{rendered=d},close(){},editor(){},open(){},showTab(){},setCloseGuard(){}};
  const sandbox={
    window:null,setInterval:()=>0,clearInterval:()=>{},
    document:{querySelector(sel){return sel==='[data-module="managedChannels"]'?section:null},querySelectorAll(){return[]}},
    ChannelCatalog:cat,ChannelRequest:req,
    initChannelWorkspace:()=>workspace,initChannelParameterEditor:()=>({open(){}}),
    navigator:{clipboard:null},confirm:()=>true,alert:()=>{},FileReader:function(){}
  };
  sandbox.window=sandbox;
  runIn(read('channel-manager.js'),sandbox,'channel-manager.js');
  const env={api:apiMock,esc:s=>String(s),el,toast(){},active:()=>false,legacy:()=>[],detail(){},closeLegacy:()=>true,task(){},journey(){}};
  const load=sandbox.initChannelManager(env);
  return {load,el,elements,getRendered:()=>rendered};
}
function validData(){
  return {items:[],mappings:[],operation_mappings:[],operations:[],runs:[],adapters:{},legacy_controls:{},legacy_scopes:{},legacy_audit_error:null,events:[],legacy_events:[],notifications:{delivery:{sent:0,pending:0,failed:0}},frontend_matrix:{}};
}

async function managerTests(){
  // 3a) 成功 → 失败（标过期）→ 成功恢复
  {
    const pending=[];
    const apiMock=(path,opt)=>path==='/api/admin/channel-manager'?pending.shift().promise:Promise.reject(new Error('unexpected '+path));
    const m=buildManagerEnv(apiMock);
    const d1=deferred();pending.push(d1);const p1=m.load();d1.resolve(validData());await p1;
    assert(m.el('managedChannels').classList.contains('cm-data-stale')===false,'首次成功不标过期');
    assert(m.el('cmStatus')._attrs.role!=='alert','成功后无 alert role');
    const d2=deferred();pending.push(d2);const p2=m.load();d2.reject(new Error('网络断开'));await p2;
    assert(m.el('managedChannels').classList.contains('cm-data-stale')===true,'刷新失败标过期');
    assert(m.el('cmStatus')._attrs.role==='alert','刷新失败提示为 alert');
    assert(/读取失败/.test(m.el('cmStatus').textContent),'失败提示含读取失败');
    const d3=deferred();pending.push(d3);const p3=m.load();d3.resolve(validData());await p3;
    assert(m.el('managedChannels').classList.contains('cm-data-stale')===false,'再次成功清除过期');
    assert(m.el('cmStatus')._attrs.role!=='alert','恢复后移除 alert');
  }
  // 3b) 响应乱序：旧响应不覆盖新响应
  {
    const pending=[];
    const apiMock=(path,opt)=>path==='/api/admin/channel-manager'?pending.shift().promise:Promise.reject(new Error('unexpected'));
    const m=buildManagerEnv(apiMock);
    const d1=deferred(),d2=deferred();pending.push(d1,d2);
    const p1=m.load();const p2=m.load();
    d2.resolve({...validData(),items:[{id:'B',name:'新数据B',adapter:'gemini_image',checks:[],supplier:'s'}]});await p2;
    d1.resolve({...validData(),items:[{id:'A',name:'旧数据A',adapter:'gemini_image',checks:[],supplier:'s'}]});await p1;
    assert(m.getRendered()&&m.getRendered().items[0].id==='B','旧响应被丢弃，最新响应生效');
  }
}

/* ---------------- 运行 ---------------- */
(async()=>{
  await reqTests();
  await managerTests();
  console.log('\n======== 结果 ========');
  console.log('通过 '+passed+' · 失败 '+failed);
  if(failures.length){console.log('失败项：');failures.forEach(f=>console.log('  - '+f))}
  process.exit(failed?1:0);
})();
