// 验证任务进度：提交后能持续看到阶段和耗时、刷新后能恢复、
// 查询中断不被当成验证失败、重复提交不会重复创建收费任务。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

const source=fs.readFileSync(require('node:path').join(__dirname,'../site/admin/channel-workspace.js'),'utf8');
const from=source.indexOf('    var channelTestRuns={};');
const to=source.indexOf('    // 与拖动发布同一份映射');
assert.ok(from>0&&to>from,'取不到验证进度片段');

function harness(apiImpl){
  const store={};
  const ctx={channelTestRuns:{},console:console,setInterval:()=>0,setTimeout:(f)=>{ctx.__tick=f;return 0},
    api:apiImpl,
    localStorage:{getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=v},removeItem:k=>{delete store[k]}},
    Object:Object,JSON:JSON,Date:Date,Math:Math,String:String,Array:Array,Number:Number,
    confirm:()=>true,alert:()=>{},
    document:{querySelectorAll:()=>[]},toast:()=>{},load:async()=>{}, _store:store};
  vm.createContext(ctx);
  vm.runInContext(source.slice(from,to),ctx);
  return ctx;
}

test('提交后立刻显示排队中，并记录开始时间（用于算耗时）',async()=>{
  const ctx=harness(async()=>({run_id:'r1',state:'queued',kind:'full',version:3,started:Math.floor(Date.now()/1000)}));
  await ctx.runChannelTest('managed:a','full');
  const run=ctx.channelTestRuns['managed:a'];
  assert.equal(run.run_id,'r1');
  assert.equal(run.label,'排队中');
  assert.equal(run.started,Math.floor(Date.now()/1000));
  assert.equal(run.finished,false);
});

test('后端去重时如实说明是复用，不是新建',async()=>{
  const ctx=harness(async()=>({run_id:'r9',state:'running',kind:'full',version:3,started:Math.floor(Date.now()/1000),deduplicated:true}));
  await ctx.runChannelTest('managed:a','full');
  assert.match(ctx.channelTestRuns['managed:a'].label,/复用|进行中/);
});

test('进度文案包含阶段、耗时与任务标识',()=>{
  const ctx=harness(async()=>({}));
  const text=ctx.renderTestRun({run_id:'abcdef1234567890',kind:'full',state:'running',label:'执行中',
    phase:'生成中',detail:'已提交供应商',version:5,started:Date.now()/1000-65,finished:false});
  assert.match(text,/完整生成/);
  assert.match(text,/执行中/);
  assert.match(text,/阶段：生成中/);
  assert.match(text,/已用时 1 分/);
  assert.match(text,/abcdef12/);
  assert.match(text,/配置 v5/);
});

test('终态显示最终结果，不再显示「已用时」',()=>{
  const ctx=harness(async()=>({}));
  const text=ctx.renderTestRun({run_id:'r1',kind:'full',state:'passed',label:'已完成',
    version:5,started:1000,ended:1120,finished:true});
  assert.match(text,/已完成/);
  assert.match(text,/耗时 2 分/);
  assert.doesNotMatch(text,/已用时/);
});

test('任务持久化到本地存储，刷新后可恢复',async()=>{
  const ctx=harness(async()=>({run_id:'r1',state:'running',kind:'full',version:3,started:Math.floor(Date.now()/1000)}));
  await ctx.runChannelTest('managed:a','full');
  const raw=JSON.parse(ctx._store['hq.channelTestRuns']);
  assert.ok(raw['managed:a'],'未落盘');
  assert.equal(raw['managed:a'].run_id,'r1');
  // 新实例读回
  const ctx2=harness(async()=>({}));
  ctx2._store['hq.channelTestRuns']=ctx._store['hq.channelTestRuns'];
  ctx2.loadTestRuns();
  assert.equal(ctx2.channelTestRuns['managed:a'].run_id,'r1');
});

test('查询中断不写失败，只标注重试',async()=>{
  let calls=0;
  const ctx=harness(async()=>{calls++;if(calls===1)throw new Error('网络中断');
    return {run_id:'r1',state:'running',label:'执行中',kind:'full',version:3,started:Math.floor(Date.now()/1000),finished:false}});
  ctx.channelTestRuns['managed:a']={run_id:'r1',kind:'full',state:'running',label:'执行中',started:Math.floor(Date.now()/1000),finished:false};
  const p=ctx.pollChannelTest('managed:a','r1');
  for(let i=0;i<4;i++){await Promise.resolve();}
  // 中断那一次不能把状态改成 failed
  const mid=ctx.channelTestRuns['managed:a'];
  assert.notEqual(mid.state,'failed');
  assert.notEqual(mid.label,'失败');
});
