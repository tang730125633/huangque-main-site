const test = require('node:test');
const assert = require('node:assert/strict');
const setup = require('../site/workbench/digital-human-setup-state.js');
const recovery = require('../site/workbench/digital-human-recovery.js');
const material = require('../site/workbench/digital-human-material-state.js');
const voice = require('../site/workbench/digital-human-voice-state.js');

function nodes(){return {photo:{disabled:false},voiceSource:{disabled:false},voiceSample:{disabled:false},restart:{hidden:true}};}

test('initial setup keeps restart hidden and both voice controls enabled', () => {
  const dom=nodes();
  setup.applyControls(dom,'input');
  assert.deepEqual(dom,{photo:{disabled:false},voiceSource:{disabled:false},voiceSample:{disabled:false},restart:{hidden:true}});
});

test('entering approved immediately locks the portrait and both voice controls and shows restart', () => {
  const dom=nodes();
  setup.applyControls(dom,'approved');
  assert.deepEqual(dom,{photo:{disabled:true},voiceSource:{disabled:true},voiceSample:{disabled:true},restart:{hidden:false}});
});

test('applyControls remains compatible with callers that do not provide a portrait control', () => {
  const dom={voiceSource:{disabled:false},voiceSample:{disabled:false},restart:{hidden:true}};
  setup.applyControls(dom,'approved');
  assert.deepEqual(dom,{voiceSource:{disabled:true},voiceSample:{disabled:true},restart:{hidden:false}});
});

test('cancelling restart preserves approved state and locked DOM', () => {
  const current={phase:'approved',voiceKey:'vip_keep',jobs:{video:[9]}};
  const result=setup.restart(current,false),dom=nodes();
  setup.applyControls(dom,result.state.phase);
  assert.equal(result.changed,false);
  assert.deepEqual(result.state,current);
  assert.equal(dom.photo.disabled,true);
  assert.equal(dom.restart.hidden,false);
});

test('confirming restart returns input and re-enables both voice controls', () => {
  const result=setup.restart({phase:'approved',voiceKey:'vip_old'},true),dom=nodes();
  setup.applyControls(dom,result.state.phase);
  assert.equal(result.changed,true);
  assert.deepEqual(result.state,{phase:'input'});
  assert.equal(dom.photo.disabled,false);
  assert.equal(dom.voiceSource.disabled,false);
  assert.equal(dom.voiceSample.disabled,false);
  assert.equal(dom.restart.hidden,true);
});

test('restart transition never submits a task or charges points', () => {
  const runEpoch=4,result=setup.restart({phase:'approved'},true),currentEpoch=result.cancelRun?runEpoch+1:runEpoch;
  assert.equal(result.submit,false);
  assert.equal(result.charge,false);
  assert.equal(result.cancelRun,true);
  assert.equal(setup.canContinue(runEpoch,currentEpoch),false);
  let submitCount=0;
  if(setup.canContinue(runEpoch,currentEpoch))submitCount++;
  assert.equal(submitCount,0);
});

test('completed state hides restart and leaves controls ready for another video', () => {
  const dom=nodes();
  setup.applyControls(dom,'complete');
  assert.deepEqual(dom,{photo:{disabled:false},voiceSource:{disabled:false},voiceSample:{disabled:false},restart:{hidden:true}});
});

test('approved recovery requests the original portrait attachment when the local file is lost', () => {
  const state={phase:'approved',jobs:{gesture:[101,102]},failed:{gesture:[false,false]}};
  assert.deepEqual(setup.resolvePhotoRecovery(state,false),{
    locked:true,valid:false,requiresRestart:false,requiresAttachment:true,gesturesComplete:false,
    error:'请重新附加本次授权使用的原人物照片后继续；如需更换人物，请先放弃上次任务并重新设置',
  });
});

test('approved recovery cannot treat invalid or failed gesture jobs as complete', () => {
  const invalid={phase:'approved',jobs:{gesture:[101,0,103]},failed:{gesture:[false,false,false]}};
  const failed={phase:'approved',jobs:{gesture:[101,102,103]},failed:{gesture:[false,true,false]}};
  assert.equal(setup.gesturesComplete(invalid),false);
  assert.equal(setup.resolvePhotoRecovery(invalid,false).requiresAttachment,true);
  assert.equal(setup.gesturesComplete(failed),false);
  assert.equal(setup.resolvePhotoRecovery(failed,false).requiresAttachment,true);
});

test('three valid non-failed gesture jobs can resume without a local portrait', () => {
  const state={phase:'approved',jobs:{gesture:[101,'102',103]},failed:{gesture:[false,false,false]}};
  assert.equal(setup.gesturesComplete(state),true);
  assert.deepEqual(setup.resolvePhotoRecovery(state,false),{
    locked:true,valid:true,requiresRestart:false,requiresAttachment:false,gesturesComplete:true,error:'',
  });
});

test('an approved in-memory run can finish gestures with its already authorized portrait', () => {
  const state={phase:'approved',jobs:{gesture:[]},failed:{gesture:[]}};
  assert.deepEqual(setup.resolvePhotoRecovery(state,true),{
    locked:true,valid:true,requiresRestart:false,requiresAttachment:false,gesturesComplete:false,error:'',
  });
});

test('input phase still permits selecting a portrait when none is loaded', () => {
  assert.deepEqual(setup.resolvePhotoRecovery({phase:'input'},false),{
    locked:false,valid:true,requiresRestart:false,requiresAttachment:false,gesturesComplete:false,error:'',
  });
});

test('approved recovery accepts only the exact authorized portrait digest', () => {
  const state={phase:'approved',photoSha256:'abc123'};
  assert.deepEqual(setup.validatePhotoAttachment(state,'ABC123'),{accepted:true,error:''});
  assert.equal(setup.validatePhotoAttachment(state,'different').accepted,false);
  assert.match(setup.validatePhotoAttachment(state,'different').error,/不一致/);
  assert.equal(setup.validatePhotoAttachment({phase:'approved'},'abc123').accepted,false);
  assert.equal(setup.validatePhotoAttachment({phase:'input'},'different').accepted,true);
});

test('approved controls permit reattaching only files still needed by the frozen run', () => {
  const state={phase:'approved',voiceMode:'clone',jobs:{gesture:[1,2]},failed:{gesture:[false,false]}};
  const dom=nodes();
  setup.applyControls(dom,'approved',state);
  assert.equal(dom.photo.disabled,false);
  assert.equal(dom.voiceSample.disabled,false);
  assert.equal(dom.voiceSource.disabled,true);
});

function deferred(){
  let resolve,reject;
  const promise=new Promise((ok,bad)=>{resolve=ok;reject=bad;});
  return {promise,resolve,reject};
}

function resume(options){
  if(!options.jobId)return options.launch(false);
  if(options.retryApproved)return options.launch(true);
  return options.poll(options.jobId).catch(error=>{
    if(error&&error.terminalJob)options.markTerminal();
    throw error;
  });
}

test('resolving a suspended submit after confirmed restart cannot repopulate state or launch polling', async () => {
  const pending=deferred(),commits=[],progress=[];
  let currentEpoch=1,pollCount=0,nextPaidStageCount=0;
  let pageState={phase:'approved',jobs:[],keys:[]},localStorageValue=JSON.stringify(pageState),ui='old-running';
  const running=setup.runJobs({
    items:[{name:'old'}],ids:[],keys:[],failed:[],epoch:1,
    currentEpoch:()=>currentEpoch,key:()=> 'old-key',
    submit:()=>pending.promise,poll:()=>{pollCount++;return Promise.resolve({old:true});},
    resume,commit:value=>{commits.push(value);pageState={phase:'approved',jobs:value.ids,keys:value.keys};localStorageValue=JSON.stringify(pageState);},
    onCount:value=>{progress.push(value);ui='old-progress';},
  }).then(()=>{nextPaidStageCount++;});
  assert.equal(commits.length,1);
  currentEpoch++;
  commits.length=0;
  pageState={phase:'input',jobs:[],keys:[]};
  localStorageValue=JSON.stringify(pageState);
  ui='new-input';
  pending.resolve(91);
  await assert.rejects(running,error=>error.generationCancelled===true);
  assert.deepEqual(pageState,{phase:'input',jobs:[],keys:[]});
  assert.deepEqual(JSON.parse(localStorageValue),{phase:'input',jobs:[],keys:[]});
  assert.equal(ui,'new-input');
  assert.deepEqual(commits,[]);
  assert.deepEqual(progress,[]);
  assert.equal(pollCount,0);
  assert.equal(nextPaidStageCount,0);
});

test('resolving a suspended poll after confirmed restart cannot write old job progress', async () => {
  const pendingPoll=deferred(),pollStarted=deferred(),commits=[],progress=[];
  let currentEpoch=7,nextPaidStageCount=0;
  let pageState={phase:'approved',jobs:[],keys:[]},localStorageValue=JSON.stringify(pageState),ui='old-running';
  const running=setup.runJobs({
    items:[{name:'old'}],ids:[],keys:[],failed:[],epoch:7,
    currentEpoch:()=>currentEpoch,key:()=> 'old-key',
    submit:()=>Promise.resolve(92),
    poll:()=>{pollStarted.resolve();return pendingPoll.promise;},resume,
    commit:value=>{commits.push(value);pageState={phase:'approved',jobs:value.ids,keys:value.keys};localStorageValue=JSON.stringify(pageState);},
    onCount:value=>{progress.push(value);ui='old-progress';},
  }).then(()=>{nextPaidStageCount++;});
  await pollStarted.promise;
  currentEpoch++;
  commits.length=0;
  pageState={phase:'input',jobs:[],keys:[]};
  localStorageValue=JSON.stringify(pageState);
  ui='new-input';
  pendingPoll.resolve({old:true});
  await assert.rejects(running,error=>error.generationCancelled===true);
  assert.deepEqual(pageState,{phase:'input',jobs:[],keys:[]});
  assert.deepEqual(JSON.parse(localStorageValue),{phase:'input',jobs:[],keys:[]});
  assert.equal(ui,'new-input');
  assert.deepEqual(commits,[]);
  assert.deepEqual(progress,[]);
  assert.equal(nextPaidStageCount,0);
});

test('a stale rejected request is converted to cancellation and cannot revive old UI', async () => {
  const pending=deferred(),commits=[];
  let currentEpoch=11,ui='old-running';
  const running=setup.runJobs({
    items:[{name:'old'}],ids:[],keys:[],failed:[],epoch:11,
    currentEpoch:()=>currentEpoch,key:()=> 'old-key',
    submit:()=>pending.promise,poll:()=>Promise.resolve({}),resume,
    commit:value=>commits.push(value),onCount:()=>{ui='old-progress';},
  });
  currentEpoch++;
  commits.length=0;
  ui='new-input';
  pending.reject(new Error('late network error'));
  await assert.rejects(running,error=>error.generationCancelled===true);
  assert.deepEqual(commits,[]);
  assert.equal(ui,'new-input');
});

test('cancelling the restart confirmation preserves the epoch and lets the current job recover', async () => {
  const transition=setup.restart({phase:'approved'},false),commits=[];
  let currentEpoch=3;
  if(transition.cancelRun)currentEpoch++;
  const result=await setup.runJobs({
    items:[{name:'keep'}],ids:[93],keys:['keep-key'],failed:[false],epoch:3,
    currentEpoch:()=>currentEpoch,key:()=> 'unused',submit:()=>Promise.reject(new Error('must not submit')),
    poll:id=>Promise.resolve({jobId:id}),resume,commit:value=>commits.push(value),
  });
  assert.equal(transition.changed,false);
  assert.deepEqual(result.results,[{jobId:93}]);
  assert.equal(commits.at(-1).ids[0],93);
});

test('bounded workers never exceed material concurrency and preserve item order', async () => {
  let active=0,maxActive=0,nextJobId=100,currentEpoch=21;
  const gates=[],commits=[],progress=[];
  const resultPromise=setup.runJobs({
    items:[0,1,2,3,4,5],ids:[],keys:[],failed:[],epoch:21,maxConcurrency:3,
    currentEpoch:()=>currentEpoch,key:index=>'material-'+index,resume,
    submit:(item)=>{
      active++;maxActive=Math.max(maxActive,active);
      const gate=deferred();gates.push({item,gate});
      nextJobId++;
      return gate.promise.then(()=>100+item);
    },
    poll:id=>{active--;return Promise.resolve({jobId:id});},
    commit:value=>commits.push(value),onCount:value=>progress.push(value),
  });
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(gates.length,3);
  assert.equal(maxActive,3);
  gates.slice(0,3).forEach(entry=>entry.gate.resolve());
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(gates.length,6);
  assert.equal(maxActive,3);
  gates.slice(3).forEach(entry=>entry.gate.resolve());
  const result=await resultPromise;
  assert.equal(maxActive,3);
  assert.deepEqual(result.results.map(item=>item.jobId),[100,101,102,103,104,105]);
  assert.deepEqual(progress,[1,2,3,4,5,6]);
  assert.deepEqual(commits.at(-1).ids,[100,101,102,103,104,105]);
});

test('pipeline controller resumes finished child jobs and resubmits only a terminal child job', async () => {
  const portrait={name:'portrait.jpg'},script='这是一段已经准备完成并通过方案分析的数字人口播文案。';
  const selectedVoice=voice.resolveLoaded(
    {phase:'planned',voiceMode:'existing',voiceKey:'vip_ready_slot'},
    [{slot_id:'ready_slot',status:'ready',voice_id:'provider-voice-id',voice_name:'我的声音'}],
  );
  assert.equal(!!portrait,true);
  assert.ok(script.length>=12);
  assert.equal(selectedVoice.valid,true);
  assert.equal(selectedVoice.selection.voiceMode,'existing');
  assert.equal(material.canStart('planned',false,true),true);

  const stages={plan:'done',voice:'done',gesture:'waiting',material:'waiting',talking:'waiting',compose:'waiting'};
  const items={
    gesture:['hook','explain','cta'],
    material:['shot-1','shot-2','shot-3','shot-4','shot-5','shot-6'],
    talking:['part-1','part-2','part-3'],
    compose:['final-video'],
  };
  const jobs={};
  Object.keys(items).forEach(name=>{jobs[name]={ids:[],keys:[],failed:[]};});
  const submissions={gesture:0,material:0,talking:0,compose:0,voiceClone:0};
  const nextId={gesture:100,material:200,talking:300,compose:400};
  const keySerial={gesture:0,material:0,talking:0,compose:0};
  const progress={};
  let currentEpoch=31;

  async function runStage(name,pollOverride){
    const bucket=jobs[name];
    progress[name]=[];
    stages[name]='running';
    try{
      const result=await setup.runJobs({
        items:items[name],ids:bucket.ids,keys:bucket.keys,failed:bucket.failed,
        epoch:31,currentEpoch:()=>currentEpoch,maxConcurrency:1,resume:recovery.resume,
        key:index=>`${name}-${index}-key-${++keySerial[name]}`,
        submit:async (item,index,stableKey)=>{
          assert.ok(stableKey.startsWith(`${name}-${index}-key-`));
          submissions[name]++;
          return ++nextId[name];
        },
        poll:pollOverride||((id)=>Promise.resolve({jobId:id,status:'done'})),
        commit:batch=>{
          bucket.ids=batch.ids;
          bucket.keys=batch.keys;
          bucket.failed=batch.failed;
        },
        onCount:value=>progress[name].push(value),
      });
      stages[name]='done';
      return result;
    }catch(error){
      stages[name]='failed';
      throw error;
    }
  }

  await runStage('gesture');
  await runStage('material');
  await runStage('talking');
  await runStage('compose');
  assert.deepEqual(submissions,{gesture:3,material:6,talking:3,compose:1,voiceClone:0});
  assert.deepEqual(stages,{plan:'done',voice:'done',gesture:'done',material:'done',talking:'done',compose:'done'});
  assert.deepEqual(progress.gesture,[1,2,3]);
  assert.deepEqual(progress.material,[1,2,3,4,5,6]);
  assert.deepEqual(progress.talking,[1,2,3]);
  assert.deepEqual(progress.compose,[1]);

  const completedSnapshot=JSON.parse(JSON.stringify(jobs));
  for(const name of ['gesture','material','talking','compose'])await runStage(name);
  assert.deepEqual(submissions,{gesture:3,material:6,talking:3,compose:1,voiceClone:0});
  assert.deepEqual(jobs,completedSnapshot);
  assert.deepEqual(stages,{plan:'done',voice:'done',gesture:'done',material:'done',talking:'done',compose:'done'});

  const terminalJob=jobs.talking.ids[1];
  const terminal=Object.assign(new Error('talking provider terminal failure'),{terminalJob:true});
  await assert.rejects(
    runStage('talking',id=>id===terminalJob?Promise.reject(terminal):Promise.resolve({jobId:id,status:'done'})),
    /terminal failure/,
  );
  assert.equal(stages.talking,'failed');
  assert.equal(jobs.talking.failed[1],true);
  assert.deepEqual(submissions,{gesture:3,material:6,talking:3,compose:1,voiceClone:0});

  const beforeRetry=JSON.parse(JSON.stringify(jobs.talking));
  await runStage('talking');
  assert.equal(stages.talking,'done');
  assert.deepEqual(submissions,{gesture:3,material:6,talking:4,compose:1,voiceClone:0});
  assert.equal(jobs.talking.ids[0],beforeRetry.ids[0]);
  assert.notEqual(jobs.talking.ids[1],beforeRetry.ids[1]);
  assert.equal(jobs.talking.ids[2],beforeRetry.ids[2]);
  assert.equal(jobs.talking.keys[0],beforeRetry.keys[0]);
  assert.notEqual(jobs.talking.keys[1],beforeRetry.keys[1]);
  assert.equal(jobs.talking.keys[2],beforeRetry.keys[2]);
  assert.deepEqual(jobs.talking.failed,[false,false,false]);
});
