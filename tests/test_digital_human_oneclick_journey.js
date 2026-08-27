const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const material=require('../site/workbench/digital-human-material-state.js');
const setup=require('../site/workbench/digital-human-setup-state.js');
const voice=require('../site/workbench/digital-human-voice-state.js');
const pageState=require('../site/workbench/digital-human-oneclick-state.js');

const stages=['material','video'];

function createHarness(saved){
  let persisted=JSON.parse(JSON.stringify(saved));
  const submissions={material:0,video:0,compose:0,voiceClone:0};
  const next={material:200,video:300,compose:400};
  function restore(){
    const state=JSON.parse(JSON.stringify(persisted));
    const restored=material.restore(state.customerUploads,state.phase,2000);
    return {state,restored};
  }
  function save(state,normalized,valid){
    state.customerUploads=pageState.persistableMaterials(state,normalized,valid);
    persisted=JSON.parse(JSON.stringify(state));
  }
  async function runBucket(context,bucket,count,pollOverride){
    const state=context.state;
    const result=await setup.runJobs({
      items:Array.from({length:count},(_,index)=>({index})),
      ids:state.jobs[bucket],keys:state.keys[bucket],failed:state.failed[bucket],
      epoch:1,currentEpoch:()=>1,maxConcurrency:bucket==='material'?3:1,
      key:index=>`${bucket}-${index}-stable-${submissions[bucket]+1}`,
      submit:async()=>{submissions[bucket]++;return ++next[bucket];},
      poll:pollOverride||((id)=>Promise.resolve({jobId:id,status:'done'})),
      resume:pageState.resumeJob,
      commit:batch=>{
        state.jobs[bucket]=batch.ids;
        state.keys[bucket]=batch.keys;
        state.failed[bucket]=batch.failed;
        save(state,context.restored.items,context.restored.valid);
      },
    });
    return result;
  }
  async function runCompose(context,pollOverride){
    const state=context.state;
    function launch(retry){
      if(retry)state.keys.compose=`compose-retry-${submissions.compose+1}`;
      if(!state.keys.compose)state.keys.compose='compose-stable-1';
      submissions.compose++;
      state.compose_job=++next.compose;
      state.compose_failed=false;
      save(state,context.restored.items,context.restored.valid);
      return (pollOverride||((id)=>Promise.resolve({jobId:id,status:'done'})))(state.compose_job);
    }
    const result=await pageState.resumeJob({
      jobId:Number(state.compose_job)||0,
      retryApproved:!!state.compose_failed,
      poll:pollOverride||((id)=>Promise.resolve({jobId:id,status:'done'})),
      launch,
      markTerminal:()=>{state.compose_failed=true;save(state,context.restored.items,context.restored.valid);},
    });
    return result;
  }
  return {restore,save,runBucket,runCompose,submissions,get persisted(){return persisted;}};
}

function approvedState(){return {
  version:8,phase:'approved',voiceMode:'existing',voiceKey:'vip_ready_slot',
  voiceCloneSubmitted:false,voiceCloneAccepted:false,voiceCloneProgress:false,
  customerUploads:[],photoSha256:'a'.repeat(64),voiceSha256:'',consent:{consent_token:'token'},
  jobs:{material:[],video:[]},keys:{material:[],video:[],compose:''},
  failed:{material:[],video:[]},compose_job:0,compose_failed:false,
};}

test('full restored one-click journey keeps 6/3/1 submissions across real serialized refreshes',async()=>{
  const harness=createHarness(approvedState());
  let context=harness.restore();
  assert.equal(context.restored.valid,true);
  assert.equal(voice.resolveLoaded(
    {phase:'approved',voiceMode:'existing',voiceKey:'vip_ready_slot'},
    [{slot_id:'ready_slot',status:'ready',voice_id:'provider-id'}],
  ).valid,true);

  await harness.runBucket(context,'material',6);
  context=harness.restore();
  await harness.runBucket(context,'video',3);
  context=harness.restore();
  await harness.runCompose(context);
  assert.deepEqual(harness.submissions,{material:6,video:3,compose:1,voiceClone:0});
  context=harness.restore();
  for(const bucket of stages){
    assert.equal(context.state.jobs[bucket].length,bucket==='material'?6:3);
    assert.equal(context.state.jobs[bucket].every(id=>Number(id)>0),true);
    assert.equal(context.state.failed[bucket].some(Boolean),false);
  }
  assert.equal(Number(context.state.compose_job)>0,true);
  assert.equal(context.state.compose_failed,false);

  await harness.runBucket(context,'material',6);
  await harness.runBucket(context,'video',3);
  await harness.runCompose(context);
  assert.deepEqual(harness.submissions,{material:6,video:3,compose:1,voiceClone:0});
});

test('terminal talking and compose failures pause once, then retry only the failed stage',async()=>{
  const harness=createHarness(approvedState());
  let context=harness.restore();
  await harness.runBucket(context,'material',6);
  context=harness.restore();
  const terminal=Object.assign(new Error('talking terminal'),{terminalJob:true});
  await assert.rejects(harness.runBucket(context,'video',3,id=>id===303?Promise.reject(terminal):Promise.resolve({jobId:id,status:'done'})),/terminal/);
  assert.deepEqual(harness.submissions,{material:6,video:3,compose:0,voiceClone:0});
  context=harness.restore();
  assert.deepEqual(context.state.jobs.video,[301,302,303]);
  assert.deepEqual(context.state.failed.video,[false,false,true]);
  const completedTalkingIds=context.state.jobs.video.slice(0,2);
  await harness.runBucket(context,'video',3);
  assert.deepEqual(harness.submissions,{material:6,video:4,compose:0,voiceClone:0});
  context=harness.restore();
  assert.deepEqual(context.state.jobs.video.slice(0,2),completedTalkingIds);
  assert.equal(context.state.jobs.video[2],304);
  assert.deepEqual(context.state.failed.video,[false,false,false]);

  const terminalCompose=Object.assign(new Error('compose terminal'),{terminalJob:true});
  await assert.rejects(harness.runCompose(context,()=>Promise.reject(terminalCompose)),/compose terminal/);
  assert.deepEqual(harness.submissions,{material:6,video:4,compose:1,voiceClone:0});
  context=harness.restore();
  assert.equal(context.state.compose_job,401);
  assert.equal(context.state.compose_failed,true);
  await harness.runCompose(context);
  assert.deepEqual(harness.submissions,{material:6,video:4,compose:2,voiceClone:0});
  context=harness.restore();
  assert.equal(context.state.compose_job,402);
  assert.equal(context.state.compose_failed,false);
});

test('approved recovery gates fail before any paid child submission',async()=>{
  const expired={upload_id:'img_'+('b'.repeat(32)),name:'expired.png',expires_at:1000};
  const materialHarness=createHarness(Object.assign(approvedState(),{customerUploads:[expired]}));
  let context=materialHarness.restore();
  assert.equal(context.restored.valid,false);
  materialHarness.save(context.state,context.restored.items,context.restored.valid);
  context=materialHarness.restore();
  assert.equal(context.restored.valid,false);
  assert.deepEqual(materialHarness.submissions,{material:0,video:0,compose:0,voiceClone:0});

  const photo=setup.resolvePhotoRecovery(approvedState(),false);
  assert.equal(photo.valid,false);
  assert.equal(setup.validatePhotoAttachment(approvedState(),'b'.repeat(64)).accepted,false);

  const uncertain=voice.restoredCloneDecision({result:{status:'ready'}},{accepted:false,progress:true});
  assert.equal(uncertain.action,'reattach');
  assert.deepEqual(materialHarness.submissions,{material:0,video:0,compose:0,voiceClone:0});
});

test('expired uploads do not block six durable materials while one failed talking job alone retries',async()=>{
  const expired={upload_id:'img_'+('b'.repeat(32)),name:'expired.png',expires_at:1000};
  const saved=Object.assign(approvedState(),{
    customerUploads:[expired],
    jobs:{material:[201,202,203,204,205,206],video:[301,302,303]},
    failed:{material:[false,false,false,false,false,false],video:[false,false,true]},
  });
  const harness=createHarness(saved);
  let context=harness.restore();
  assert.equal(context.restored.valid,false);
  assert.equal(pageState.canRecoverMaterials(context.state),true);
  await harness.runBucket(context,'material',6);
  context=harness.restore();
  await harness.runBucket(context,'video',3);
  assert.deepEqual(harness.submissions,{material:0,video:1,compose:0,voiceClone:0});
});

test('page wires recovery gates before paid generation and uses page terminal recovery',()=>{
  const page=fs.readFileSync(path.join(__dirname,'../site/workbench/digital-human-oneclick.html'),'utf8');
  const start=page.slice(page.indexOf('function start(){'),page.indexOf("$('photo').onchange"));
  const ordered=[
    'DigitalHumanMaterialState.restore(state.customerUploads,state.phase)',
    'DigitalHumanSetupState.resolvePhotoRecovery(state,!!photoData||!!photo)',
    "state.phase==='approved'&&clone&&!state.voiceCloneAccepted&&!voice",
    "!state.consent&&!$('consent').checked",
    'validatePhotoRecovery(photo,epoch)',
    'validateMaterialRecovery(epoch)',
    'validateVideoRecovery(epoch)',
    'heygenPreflight(epoch)',
    'prepareConsent(photo,voice,clone,epoch)',
    'generateMaterials(epoch)',
    'generateTalking(voiceKey,epoch)',
    'compose(epoch)',
  ];
  let last=-1;
  for(const marker of ordered){
    const index=start.indexOf(marker);
    assert.ok(index>last,`${marker} must follow the previous journey stage`);
    last=index;
  }
  assert.match(page,/resume:DigitalHumanOneClickState\.resumeJob/);
  assert.match(page,/return DigitalHumanOneClickState\.resumeJob\(\{jobId:Number\(state\.compose_job\)\|\|0/);
  assert.doesNotMatch(page,/DigitalHumanRecovery\.resume\(/);
  assert.match(page,/restoreFailedSteps\(photoRecovery,restoredMaterials\.valid\|\|materialJobsRecoverable\)/);
  assert.doesNotMatch(page,/invalidateGestureRecovery/);
});
