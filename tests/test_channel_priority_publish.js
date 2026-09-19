const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../site/admin/channel-workspace.js'),'utf8');
function harness(mode='managed'){
  let mapping={state:mode,revision:4,channels:['a','b']};
  const calls=[],messages=[],status={};
  const ctx={priorityRequest:null,priorityBusy:false,priorityUncertain:false,priorityDrafts:{op:{state:mode,revision:4,channels:['b','a']}},
    mappingForOperation:()=>mapping,mappingChannels:m=>[...m.channels],refreshPriority(){},toast:m=>messages.push(m),
    root:{querySelector:()=>status},api:async(url,opts)=>{calls.push(JSON.parse(opts.body));mapping={state:mode,revision:5,channels:['b','a']}},env:{refresh:async()=>{}}};
  vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('    async function publishPriority('),source.indexOf('    function movePriority(')),ctx);
  return {ctx,calls,messages,status};
}
test('same-set managed reorder uses CAS and confirms readback',async()=>{
  const h=harness();await h.ctx.publishPriority('op',true);
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].expected_revision,4);
  assert.deepEqual(h.calls[0].channels,['b','a']);assert.match(h.messages[0],/顺序已生效/);
});
test('shadow order never automatically takes over production',async()=>{
  const h=harness('shadow');await h.ctx.publishPriority('op',true);assert.equal(h.calls.length,0);assert.match(h.messages[0],/草稿/);
});
test('adding candidate requires explicit publish, not incidental drag',async()=>{
  const h=harness();h.ctx.priorityDrafts.op.channels.push('c');await h.ctx.publishPriority('op',true);assert.equal(h.calls.length,0);
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
});
