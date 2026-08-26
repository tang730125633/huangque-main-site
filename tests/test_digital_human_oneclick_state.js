const test=require('node:test');
const assert=require('node:assert/strict');
const pageState=require('../site/workbench/digital-human-oneclick-state.js');
const material=require('../site/workbench/digital-human-material-state.js');

test('approved material failure survives unrelated saves and a second refresh',()=>{
  const valid={upload_id:'img_'+('a'.repeat(32)),name:'有效素材.png',sha256:'a',expires_at:3000};
  const expired={upload_id:'img_'+('b'.repeat(32)),name:'过期素材.png',sha256:'b',expires_at:1000};
  let saved={phase:'approved',customerUploads:[valid,expired]};

  const first=material.restore(saved.customerUploads,saved.phase,2000);
  assert.equal(first.valid,false);
  assert.deepEqual(first.items,[valid]);

  saved=Object.assign({},saved,{
    voiceKey:'vip_ready_slot',
    customerUploads:pageState.persistableMaterials(saved,first.items,first.valid),
  });
  assert.deepEqual(saved.customerUploads,[valid,expired]);

  const second=material.restore(saved.customerUploads,saved.phase,2000);
  assert.equal(second.valid,false);
  assert.deepEqual(second.items,[valid]);
});

test('valid material state persists the normalized frozen set',()=>{
  const first={upload_id:'img_'+('a'.repeat(32)),name:'一.png',sha256:'a',expires_at:3000};
  const second={upload_id:'img_'+('b'.repeat(32)),name:'二.png',sha256:'b',expires_at:4000};
  const saved={phase:'planned',customerUploads:[first]};
  assert.deepEqual(pageState.persistableMaterials(saved,[first,second],true),[first,second]);
});

test('newly launched terminal job is marked during the first failed attempt',async()=>{
  const terminal=Object.assign(new Error('terminal'),{terminalJob:true});
  let launched=0,marked=0;
  await assert.rejects(pageState.resumeJob({
    jobId:0,
    retryApproved:false,
    launch:async retry=>{launched++;assert.equal(retry,false);throw terminal;},
    poll:async()=>assert.fail('new job must launch'),
    markTerminal:()=>{marked++;},
  }),/terminal/);
  assert.equal(launched,1);
  assert.equal(marked,1);
});

test('terminal retry launch is marked once and a completed job only polls',async()=>{
  const terminal=Object.assign(new Error('retry terminal'),{terminalJob:true});
  let launched=0,polled=0,marked=0;
  await assert.rejects(pageState.resumeJob({
    jobId:91,
    retryApproved:true,
    launch:async retry=>{launched++;assert.equal(retry,true);throw terminal;},
    poll:async()=>{polled++;},
    markTerminal:()=>{marked++;},
  }),/retry terminal/);
  assert.deepEqual({launched,polled,marked},{launched:1,polled:0,marked:1});

  const result=await pageState.resumeJob({
    jobId:92,
    retryApproved:false,
    launch:async()=>assert.fail('completed job must not relaunch'),
    poll:async id=>{polled++;return {jobId:id,status:'done'};},
    markTerminal:()=>{marked++;},
  });
  assert.deepEqual(result,{jobId:92,status:'done'});
  assert.deepEqual({launched,polled,marked},{launched:1,polled:1,marked:1});
});

test('restored active job that becomes terminal is marked without relaunching',async()=>{
  const terminal=Object.assign(new Error('restored terminal'),{terminalJob:true});
  let launched=0,polled=0,marked=0;
  await assert.rejects(pageState.resumeJob({
    jobId:93,
    retryApproved:false,
    launch:async()=>{launched++;},
    poll:async id=>{polled++;assert.equal(id,93);throw terminal;},
    markTerminal:()=>{marked++;},
  }),/restored terminal/);
  assert.deepEqual({launched,polled,marked},{launched:0,polled:1,marked:1});
});

test('server-invalid gesture recovery opens only the frozen portrait reattachment path',()=>{
  const saved={jobs:{gesture:[11,12,13]},failed:{gesture:[false,false,false]}};
  assert.equal(pageState.invalidateGestureRecovery(saved,{code:'network_error'}),false);
  assert.deepEqual(saved.failed.gesture,[false,false,false]);
  assert.equal(pageState.invalidateGestureRecovery(saved,{code:'gesture_recovery_invalid'}),true);
  assert.deepEqual(saved.jobs.gesture,[11,12,13]);
  assert.deepEqual(saved.failed.gesture,[true,true,true]);
});

test('gesture recovery marks only the invalid server job when the server identifies it',()=>{
  const saved={jobs:{gesture:[11,12,13]},failed:{gesture:[false,false,false]}};
  assert.equal(pageState.invalidateGestureRecovery(saved,{code:'gesture_recovery_invalid',invalidJobIds:[12]}),true);
  assert.deepEqual(saved.failed.gesture,[false,true,false]);
});

test('recovery selects every reusable child and marks only the server-invalid index',()=>{
  const saved={phase:'approved',consent:{consent_token:'token'},jobs:{material:[11,12,13,14,15,16],video:[21,22,23]},failed:{material:[false,false,false,false,false,false],video:[false,false,false]}};
  assert.deepEqual(pageState.recoveryJobIds(saved,'material',6),[
    {index:0,job_id:11},{index:1,job_id:12},{index:2,job_id:13},
    {index:3,job_id:14},{index:4,job_id:15},{index:5,job_id:16},
  ]);
  assert.equal(pageState.canRecoverMaterials(saved),true);
  assert.equal(pageState.invalidateMaterialRecovery(saved,{code:'material_recovery_invalid',invalidJobIds:[13]}),true);
  assert.deepEqual(saved.failed.material,[false,false,true,false,false,false]);
  assert.deepEqual(pageState.recoveryJobIds(saved,'material',6),[
    {index:0,job_id:11},{index:1,job_id:12},{index:3,job_id:14},
    {index:4,job_id:15},{index:5,job_id:16},
  ]);
  assert.equal(pageState.canRecoverMaterials(saved),false);
  assert.equal(pageState.invalidateVideoRecovery(saved,{code:'video_recovery_invalid',invalidJobIds:[22]}),true);
  assert.deepEqual(saved.failed.video,[false,true,false]);
  assert.equal(pageState.retryLabel({code:'video_recovery_invalid',invalidJobIds:[21,22,23]},'talking'),'重新生成失败的 3 段口播');
  assert.equal(pageState.retryLabel({code:'other'},'talking'),'从失败步骤再试一次');
});

test('sparse recovery preserves original indexes for every paid child bucket',()=>{
  const saved={phase:'approved',consent:{consent_token:'token'},jobs:{
    gesture:[101,0,103],material:[201,0,203,204,205,206],video:[301,0,303],
  },failed:{
    gesture:[false,true,false],material:[false,true,false,false,false,false],
    video:[false,true,false],
  }};
  assert.deepEqual(pageState.recoveryJobIds(saved,'gesture',3),[
    {index:0,job_id:101},{index:2,job_id:103},
  ]);
  assert.deepEqual(pageState.recoveryJobIds(saved,'material',6),[
    {index:0,job_id:201},{index:2,job_id:203},{index:3,job_id:204},
    {index:4,job_id:205},{index:5,job_id:206},
  ]);
  assert.deepEqual(pageState.recoveryJobIds(saved,'video',3),[
    {index:0,job_id:301},{index:2,job_id:303},
  ]);
});

test('charged queue failures poll until refund is confirmed',()=>{
  assert.deepEqual(pageState.jobFailureDecision({status:'error',cost:5,refund_state:'pending'}),{
    action:'poll',refundConfirmed:false,
  });
  assert.deepEqual(pageState.jobFailureDecision({status:'error',cost:5,refund_state:'refunded'}),{
    action:'terminal',refundConfirmed:true,
  });
  assert.deepEqual(pageState.jobFailureDecision({status:'error',cost:0,refund_state:'none'}),{
    action:'terminal',refundConfirmed:true,
  });
});
