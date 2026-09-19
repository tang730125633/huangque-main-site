const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../site/admin/channel-workspace.js'),'utf8');
function harness(mode='managed'){
  let mapping={state:mode,revision:4,channels:['a','b']};
  const calls=[],messages=[],status={};
  const ctx={priorityErrors:{},priorityRequest:null,priorityBusy:false,priorityUncertain:false,priorityDrafts:{op:{state:mode,revision:4,channels:['b','a']}},
    data:{items:['a','b','c'].map(id=>({id,enabled:true,model:'same-model'}))},
    matrixPages:()=>[{products:[{models:[{routes:[{operation_id:'op',primary:{model:'same-model'}}]}]}]}],
    mappingForOperation:()=>mapping,mappingChannels:m=>[...m.channels],refreshPriority(){},toast:m=>messages.push(m),
    root:{querySelector:()=>status},api:async(url,opts)=>{const body=JSON.parse(opts.body);calls.push(body);mapping={state:body.state,revision:5,channels:body.channels}},env:{refresh:async()=>{}}};
  vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('    async function publishPriority('),source.indexOf('    function movePriority(')),ctx);
  return {ctx,calls,messages,status};
}
test('same-set managed reorder uses CAS and confirms readback',async()=>{
  const h=harness();await h.ctx.publishPriority('op',true);
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].expected_revision,4);
  assert.deepEqual(h.calls[0].channels,['b','a']);assert.match(h.messages[0],/顺序已生效/);
});
test('explicit drag promotes same-model candidates through server managed gate',async()=>{
  const h=harness('shadow');await h.ctx.publishPriority('op',true);assert.equal(h.calls.length,1);assert.equal(h.calls[0].state,'managed');assert.match(h.messages[0],/生效/);
});
test('cross-model and disabled candidates cannot take over by dragging',async()=>{
  for(const change of [{model:'different'},{enabled:false},{_lifecycle:{deleted:true}}]){
    const h=harness();Object.assign(h.ctx.data.items[1],change);await h.ctx.publishPriority('op',true);assert.equal(h.calls.length,0);assert.match(h.messages[0],/未应用/);
  }
});
test('concurrent drag cannot submit twice',async()=>{
  const h=harness();let done;h.ctx.api=()=>new Promise(resolve=>done=resolve);
  const first=h.ctx.publishPriority('op',true);await h.ctx.publishPriority('op',true);done();await first;
  assert.equal(h.ctx.priorityBusy,false);assert.ok(h.messages.every(m=>!m.includes('顺序已生效')));
});
test('unknown publication and failed readback blocks further writes',async()=>{
  const h=harness();let writes=0;h.ctx.api=async()=>{writes++;throw Error('network')};h.ctx.env.refresh=async()=>{throw Error('offline')};
  await h.ctx.publishPriority('op',true);assert.equal(h.ctx.priorityUncertain,true);
  h.ctx.priorityDrafts.op={state:'managed',revision:4,channels:['a','b']};await h.ctx.publishPriority('op',true);
  assert.equal(writes,1);assert.ok(h.messages.every(m=>!m.startsWith('顺序已生效')));
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
  assert.match(h.messages.at(-1),/已读回确认顺序生效/);
  assert.equal(h.ctx.priorityErrors.op,undefined);
});
test('unassigned disabled same-model channels do not block a published reorder',async()=>{
 const h=harness();h.ctx.data.items.push({id:'disabled',enabled:false,model:'same-model'});h.ctx.priorityDrafts.op.channels.push('disabled');await h.ctx.publishPriority('op',true);
 assert.deepEqual(h.calls[0].channels,['b','a']);
});
