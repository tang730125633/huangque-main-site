const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../site/admin/channel-workspace.js'),'utf8');
function harness(mode='managed'){
  let mapping={state:mode,revision:4,channels:['a','b']};
  const calls=[],messages=[],status={};
  // 每条渠道各自持有自己的实际模型 ID：不再靠「同名」通过比较。
  const item=id=>({id,enabled:true,adapter:'ad-image',model:'model-'+id});
  const ctx={priorityErrors:{},priorityRequest:null,priorityBusy:false,priorityUncertain:false,priorityDrafts:{op:{state:mode,revision:4,channels:['b','a']}},
    data:{items:['a','b','c'].map(item),
      operations:[{operation_id:'op',channel_kind:'image'}],
      adapters:{'ad-image':{kind:'image'},'ad-video':{kind:'video'}}},
    matrixPages:()=>[{products:[{models:[{routes:[{operation_id:'op',primary:{model:'model-a'}}]}]}]}],
    mappingForOperation:()=>mapping,mappingChannels:m=>[...m.channels],refreshPriority(){},toast:m=>messages.push(m),
    root:{querySelector:()=>status},api:async(url,opts)=>{const body=JSON.parse(opts.body);calls.push(body);mapping={state:body.state,revision:5,channels:body.channels}},env:{refresh:async()=>{}}};
  vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('    async function publishPriority('),source.indexOf('    function movePriority(')),ctx);
  return {ctx,calls,messages,status};
}
test('拖到第一位按 CAS 提交并读回确认，跨模型渠道也允许切换',async()=>{
  const h=harness();await h.ctx.publishPriority('op',true);
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].expected_revision,4);
  assert.deepEqual(h.calls[0].channels,['b','a']);assert.match(h.messages[0],/已切换/);
  // b 的模型是 model-b，主渠道 a 是 model-a：不同名不再拦截，各自保留模型 ID
  assert.notEqual(h.ctx.data.items[1].model,h.ctx.data.items[0].model);
});
test('显式拖动到首位走托管发布并读回确认',async()=>{
  const h=harness('shadow');await h.ctx.publishPriority('op',true);assert.equal(h.calls.length,1);assert.equal(h.calls[0].state,'managed');assert.match(h.messages[0],/已切换/);
});
test('已停用、已删除、能力不匹配的渠道不能被拖上主位',async()=>{
  for(const change of [{enabled:false},{_lifecycle:{deleted:true}},{adapter:'ad-video'}]){
    const h=harness();Object.assign(h.ctx.data.items[0],change);await h.ctx.publishPriority('op',true);
    assert.equal(h.calls.length,0,'应当被拦截：'+JSON.stringify(change));
    assert.match(h.messages[0],/未应用/);
  }
});
test('通道为空或首位渠道已不存在时给出明确原因',async()=>{
  const empty=harness();empty.ctx.priorityDrafts.op.channels=[];
  await empty.ctx.publishPriority('op',true);
  assert.equal(empty.calls.length,0);assert.match(empty.messages[0],/未应用/);
  const ghost=harness();ghost.ctx.priorityDrafts.op.channels=['ghost'];
  await ghost.ctx.publishPriority('op',true);
  assert.equal(ghost.calls.length,0);assert.match(ghost.messages[0],/未应用/);
});
test('concurrent drag cannot submit twice',async()=>{
  const h=harness();let done;h.ctx.api=()=>new Promise(resolve=>done=resolve);
  const first=h.ctx.publishPriority('op',true);await h.ctx.publishPriority('op',true);done();await first;
  assert.equal(h.ctx.priorityBusy,false);assert.ok(h.messages.every(m=>!m.includes('已切换')));
});
test('unknown publication and failed readback blocks further writes',async()=>{
  const h=harness();let writes=0;h.ctx.api=async()=>{writes++;throw Error('network')};h.ctx.env.refresh=async()=>{throw Error('offline')};
  await h.ctx.publishPriority('op',true);assert.equal(h.ctx.priorityUncertain,true);
  h.ctx.priorityDrafts.op={state:'managed',revision:4,channels:['a','b']};await h.ctx.publishPriority('op',true);
  assert.equal(writes,1);assert.ok(h.messages.every(m=>!m.startsWith('已切换')));
});
test('real refresh failure contract is false, and still locks writes',async()=>{
  const h=harness();h.ctx.api=async()=>{throw Error('network')};h.ctx.env.refresh=async()=>false;
  await h.ctx.publishPriority('op',true);assert.equal(h.ctx.priorityUncertain,true);
});
test('lost response after commit is recovered by readback without resubmission',async()=>{
  const h=harness(),save=h.ctx.api;
  h.ctx.api=async(...args)=>{await save(...args);throw Error('response lost')};
  await h.ctx.publishPriority('op',true);
  assert.equal(h.calls.length,1);assert.equal(h.ctx.priorityUncertain,false);
  assert.match(h.messages.at(-1),/已读回确认主渠道已切换/);
  assert.equal(h.ctx.priorityErrors.op,undefined);
});
test('未发布的停用渠道不阻塞已发布顺序的重排',async()=>{
 const h=harness();h.ctx.data.items.push({id:'disabled',enabled:false,adapter:'ad-image',model:'model-a'});h.ctx.priorityDrafts.op.channels.push('disabled');await h.ctx.publishPriority('op',true);
 assert.deepEqual(h.calls[0].channels,['b','a']);
});
