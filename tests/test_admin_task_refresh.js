const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../site/admin/index.html'), 'utf8');
test('termination dialog requires reason, blocks double submit and preserves failed input',async()=>{
  const ids=['terminateTaskDialog','terminateTaskTitle','terminateTaskInfo','terminateTaskReason','terminateTaskError','terminateTaskSubmit','terminateTaskForm','terminateTaskCancel'];
  const nodes=Object.fromEntries(ids.map(id=>[id,{value:'',textContent:'',disabled:false,focus(){},showModal(){this.open=true},close(){this.open=false}}]));
  let resolveRequest,rejectRequest,requests=[],reloads=0;
  const c={el:id=>nodes[id],fmtDuration:String,toast:()=>{},loadReqLogs:()=>reloads++,api:(path,options)=>{requests.push({path,body:JSON.parse(options.body)});return new Promise((resolve,reject)=>{resolveRequest=resolve;rejectRequest=reject})}};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  function openTaskTermination('),source.indexOf('  function taskTimingMetric(')),c);
  c.openTaskTermination({task_id:99,user:'demo',func:'模板成片',cost:5});
  assert.equal(nodes.terminateTaskTitle.textContent,'终止此任务？','普通任务保持原话术');
  const submit=()=>nodes.terminateTaskForm.onsubmit({preventDefault(){}});
  submit();assert.equal(requests.length,0);
  nodes.terminateTaskReason.value='用户要求取消';submit();submit();assert.equal(requests.length,1);
  assert.deepEqual(requests[0],{path:'/api/admin/tasks/terminate',body:{job_id:99,reason:'用户要求取消'}});
  rejectRequest(new Error('暂未确认'));await new Promise(setImmediate);
  assert.equal(nodes.terminateTaskDialog.open,true);assert.equal(nodes.terminateTaskReason.value,'用户要求取消');
  assert.equal(nodes.terminateTaskError.textContent,'暂未确认');assert.equal(nodes.terminateTaskSubmit.disabled,false);
  submit();resolveRequest({});await new Promise(setImmediate);
  assert.equal(nodes.terminateTaskDialog.open,false);assert.equal(reloads,1);
});
test('termination action follows backend capability and preserves unknown remote status',()=>{
  const c={esc:s=>String(s??'').replace(/</g,'&lt;'),fmtDuration:s=>s+'秒',fmtWait:s=>s+'秒等待',fmtTime:String,taskStageClass:s=>s,taskRawField:()=>''};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  function taskTimingMetric('),source.indexOf('  function renderActivity(')),c);
  const base={source:'job',task_id:9,cat:'running',stages:[]};
  assert.match(c.renderTaskCard({...base,can_terminate:true}),/data-terminate-task="9"/);
  assert.doesNotMatch(c.renderTaskCard(base),/data-terminate-task=/);
  const html=c.renderTaskCard({...base,termination:{platform_state:'stopping',remote_state:'unconfirmed',refund_state:'pending',actor:'admin',reason:'<script>',requested_at:1}});
  assert.match(html,/终止中 · 等待执行退出/);assert.match(html,/远端停止未确认/);
  assert.match(html,/退款待确认/);assert.match(html,/&lt;script>/);
  assert.doesNotMatch(html,/data-terminate-task=/);
});
test('running tasks show waited time and unconfirmed submissions ask for reconciliation',()=>{
  const c={esc:s=>String(s??'').replace(/</g,'&lt;'),fmtDuration:s=>s+'秒',fmtWait:s=>s+'秒等待',fmtTime:String,taskStageClass:s=>s,taskRawField:()=>''};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  function taskTimingMetric('),source.indexOf('  function renderActivity(')),c);
  const base={source:'job',task_id:9,cat:'running',stages:[]};
  // 运行中：显示「已等待」（now − created_at），不再显示冻结的耗时
  const running=c.renderTaskCard({...base,duration_sec:0,waited_sec:6720,can_terminate:true});
  assert.match(running,/已等待/);assert.match(running,/6720秒等待/);
  assert.doesNotMatch(running,/处理耗时/);
  // 提交未确认：状态如实、动作变成「对账 / 退款」
  const unconfirmed=c.renderTaskCard({...base,duration_sec:0,waited_sec:6720,can_terminate:true,
    unconfirmed:true,evidence_tone:'warn',evidence_label:'提交未确认 · 待对账'});
  assert.match(unconfirmed,/提交未确认 · 待对账/);assert.match(unconfirmed,/class="pill warn"/);
  assert.match(unconfirmed,/对账 \/ 退款/);assert.match(unconfirmed,/提交结果未确认/);
  // 已完成任务仍是「处理耗时」
  const done=c.renderTaskCard({source:'job',task_id:9,cat:'ok',duration_sec:58,stages:[]});
  assert.match(done,/处理耗时/);assert.match(done,/58秒/);assert.doesNotMatch(done,/已等待/);
});

test('reconciliation dialog states the unconfirmed risk and relabels the action',()=>{
  const ids=['terminateTaskDialog','terminateTaskTitle','terminateTaskInfo','terminateTaskReason','terminateTaskError','terminateTaskSubmit','terminateTaskForm','terminateTaskCancel'];
  const nodes=Object.fromEntries(ids.map(id=>[id,{value:'',textContent:'',disabled:false,focus(){},showModal(){this.open=true},close(){this.open=false}}]));
  const c={el:id=>nodes[id],fmtDuration:String,fmtWait:s=>s+'秒等待',toast:()=>{},loadReqLogs:()=>{},api:()=>new Promise(()=>{})};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  function openTaskTermination('),source.indexOf('  function taskTimingMetric(')),c);
  c.openTaskTermination({task_id:8447,user:'tang1',func:'作图',cost:20,cat:'running',waited_sec:6720,
    evidence_label:'提交未确认 · 待对账',unconfirmed:true});
  assert.equal(nodes.terminateTaskTitle.textContent,'提交未确认 · 对账 / 退款');
  assert.equal(nodes.terminateTaskSubmit.textContent,'确认终止并退款');
  assert.match(nodes.terminateTaskInfo.textContent,/提交未确认/);
  assert.match(nodes.terminateTaskInfo.textContent,/已等待：6720秒等待/);
  assert.match(nodes.terminateTaskInfo.textContent,/按未交付退款/);
  assert.doesNotMatch(nodes.terminateTaskInfo.textContent,/耗时：/);
});

function setup() {
  const elements = Object.fromEntries(['reqSource','reqStatus','reqUser','reqSearch','reqAttributed','reqNoise','reqUpdatedAt'].map(id => [id, {value:'',checked:false,textContent:''}]));
  const pending=[], rendered=[];
  const context={state:{reqPage:1,reqPageSize:20,days:7},el:id=>elements[id],encodeURIComponent,Promise,toast:()=>{},renderActivity:d=>rendered.push(d),api:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),pollNote:()=>{}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function loadReqLogs('),source.indexOf('  function ',source.indexOf('  function loadReqLogs(')+12)),context);
  return {context,elements,pending,rendered};
}
test('new user filter starts immediately and stale responses cannot replace it', async()=>{
  const {context:c,elements:e,pending:p,rendered:r}=setup();
  const first=c.loadReqLogs(true);
  e.reqUser.value='qilin';const second=c.loadReqLogs(false);
  assert.equal(p.length,2);assert.match(p[1].url,/user=qilin/);
  p[1].resolve({user:'qilin'});await second;
  p[0].resolve({user:'other'});await first;
  assert.deepEqual(r,[{user:'qilin'}]);
});
test('identical refresh shares request and stale failure cannot overwrite new success', async()=>{
  const {context:c,elements:e,pending:p,rendered:r}=setup();
  const first=c.loadReqLogs(true);assert.equal(c.loadReqLogs(true),first);
  e.reqSearch.value='new';const next=c.loadReqLogs();
  p[1].resolve({total:1});await next;e.reqUpdatedAt.textContent='fresh';
  p[0].reject(new Error('old failure'));await first;
  assert.equal(e.reqUpdatedAt.textContent,'fresh');assert.equal(r.length,1);
});
test('empty filter results render normally and request failure releases refresh lock', async()=>{
  const {context:c,pending:p,rendered:r,elements:e}=setup();
  const first=c.loadReqLogs();p[0].resolve({total:0,items:[]});await first;
  assert.equal(r[0].total,0);
  const failed=c.loadReqLogs();p[1].reject(new Error('offline'));await failed;
  assert.equal(c.state.reqLoading,false);
  assert.match(e.reqUpdatedAt.textContent,/刷新失败/);
  const retry=c.loadReqLogs();p[2].resolve({total:1});await retry;
  assert.equal(r.length,2);
});
test('shrinking page count fetches last valid page without rendering empty old page', async()=>{
  const {context:c,pending:p,rendered:r}=setup();c.state.reqPage=3;
  const request=c.loadReqLogs();p[0].resolve({total:21,items:[]});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(c.state.reqPage,2);assert.match(p[1].url,/offset=20/);assert.equal(r.length,0);
  p[1].resolve({total:21,items:[{task_id:'last'}]});await request;
  assert.equal(r.length,1);assert.equal(r[0].items[0].task_id,'last');
});
test('journey summary states missing evidence and blocked E2E rather than success',()=>{
  const c={registryE2ERun:()=>null,registryE2EPassed:()=>false,registryActiveDependencies:()=>[],esc:String};
  vm.createContext(c);
  const start=source.indexOf('  function operationEvidenceSummary(');
  vm.runInContext(source.slice(start,source.indexOf('  function renderOperations(',start)),c);
  const html=c.operationEvidenceSummary({name:'图片页'},{feature:{name:'作图'},mode:{name:'模式一',validation:{supported:false,blocked_reason:'素材未准备'}}},{});
  assert.match(html,/渠道未采集/);assert.match(html,/模型未采集/);assert.match(html,/未采集任务级代理证据/);
  assert.match(html,/素材未准备/);assert.match(html,/没有八段全部通过/);
});
test('horizontal task cards keep five evidence nodes without inventing success',()=>{
  const c={esc:s=>String(s??'').replace(/</g,'&lt;'),fmtDuration:s=>s+'秒',fmtWait:s=>s+'秒等待',fmtTime:String,taskStageClass:s=>s,taskRawField:()=>''};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  function taskTimingMetric('),source.indexOf('  function renderActivity(')),c);
  const html=c.renderTaskCard({source:'job',task_id:1,cat:'running',func:'<test>',stages:[]});
  assert.match(html,/task-row-main/);assert.match(html,/task-stages/);assert.match(html,/task-metrics/);
  assert.match(html,/业务受理/);assert.match(html,/成品交付/);assert.match(html,/&lt;test>/);
  assert.equal((html.match(/class="task-stage unknown"/g)||[]).length,5);
  assert.doesNotMatch(html,/task-card-v2|task-timeline|class="task-stage passed"/);
});

test('task evidence distinguishes unknown submission from failure and folds raw records',()=>{
  const c={esc:String,fmtDuration:s=>s+'秒',fmtWait:s=>s+'秒等待',taskStageClass:s=>s,taskRawField:()=>''};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  function taskTimingMetric('),source.indexOf('  function renderActivity(')),c);
  const item={source:'job',task_id:1,stages:[],runtime_trace:[{stage:'provider_submit',state:'unknown',error_type:'TimeoutError',duration_sec:3}]};
  const html=c.renderTaskCard(item);
  assert.match(html,/提交供应商超时；供应商是否接单尚未确认/);
  assert.match(html,/<details class="task-technical"><summary>/);
  assert.equal((html.match(/尚未采集步骤证据：/g)||[]).length,1);
  assert.doesNotMatch(html,/task-evidence-card|该步骤已记录失败/);
  assert.match(html,/时间未采集/);
  item.runtime_trace[0].state='failed';
  assert.match(c.renderTaskCard(item),/该步骤已记录失败/);
});

// ===== P0 刷新节奏：纯决策逻辑 =====
function pollContext(){
  const c={window:{}};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  var POLL_POLICY='),source.indexOf('  var pollTimer=null')),c);
  return c.window.HqAdminPoll;
}

test('refresh policy grades modules and backs off on failures',()=>{
  const poll=pollContext();
  // 分模块基准：实时任务 8s 探活、服务器 5s、运营看板空闲 60s
  assert.equal(poll.delay('logs',true,0),8000);
  assert.equal(poll.delay('logs',false,0),30000);
  assert.equal(poll.delay('servers',true,0),5000);
  assert.equal(poll.delay('servers',false,0),10000);
  assert.equal(poll.delay('operations',true,0),30000);
  assert.equal(poll.delay('operations',false,0),60000);
  assert.equal(poll.delay('dashboard',true,0),10000);
  assert.equal(poll.delay('dashboard',false,0),30000);
  // 未知模块回落通用档，不返回 undefined
  assert.equal(poll.delay('whatever',true,0),15000);
  // 失败退避 2^n，封顶 60 秒
  assert.deepEqual([1,2,3,4,9].map(n=>poll.delay('servers',true,n)),[10000,20000,40000,40000,40000]);
  assert.equal(poll.delay('operations',true,3),60000);
  assert.equal(poll.delay('logs',true,-5),8000);
});

test('refresh policy only speeds up when work is actually in flight',()=>{
  const poll=pollContext();
  assert.equal(poll.isActive('servers',{}),true);
  // 实时任务：有 running/pending 才算有活在飞
  assert.equal(poll.isActive('logs',{items:[{cat:'done'},{cat:'failed'}]}),false);
  assert.equal(poll.isActive('logs',{items:[{cat:'running'}]}),true);
  assert.equal(poll.isActive('logs',{items:[{status:'pending'}]}),true);
  // 提交未确认的任务不会自己推进（等人工对账）→ 不该让页面保持高频刷新
  assert.equal(poll.isActive('logs',{items:[{cat:'running',unconfirmed:true}]}),false);
  assert.equal(poll.isActive('logs',{items:[{cat:'running',unconfirmed:true},{cat:'running'}]}),true);
  assert.equal(poll.isActive('logs',{items:[]}),false);
  assert.equal(poll.isActive('logs',{}),false);
  // 运营/看板：跑批或待退款都算
  assert.equal(poll.isActive('operations',{stats:{live:{running:0,refund_pending:0}}}),false);
  assert.equal(poll.isActive('operations',{stats:{live:{running:2}}}),true);
  assert.equal(poll.isActive('dashboard',{stats:{live:{refund_pending:1}}}),true);
  assert.equal(poll.isActive('dashboard',{}),false);
});

test('fingerprint probe only refetches the heavy timeline when it changed',()=>{
  const poll=pollContext();
  // 首次没有基线 → 必须拉
  assert.equal(poll.shouldFullFetch(1000,0,'','sig-a'),true);
  // 指纹变了 → 拉
  assert.equal(poll.shouldFullFetch(1000,900,'sig-a','sig-b'),true);
  // 指纹没变且刚拉过 → 省掉重接口
  assert.equal(poll.shouldFullFetch(1000,900,'sig-a','sig-a'),false);
  // 指纹没变但超过 60s 没全量拉 → 兜底拉一次
  assert.equal(poll.shouldFullFetch(200000,1000,'sig-a','sig-a'),true);
  assert.equal(poll.shouldFullFetch(90000,0,'sig-a','sig-a'),true);
  assert.equal(poll.shouldFullFetch(59999,0,'sig-a','sig-a'),false);
});

test('admin refresh runs one scheduler instead of per-module timers',()=>{
  // 只能有一处统一定时器
  assert.match(source,/function scheduleNextPoll\(/);
  assert.match(source,/pollTimer=setTimeout\(function\(\)/);
  assert.match(source,/\},hqAdminPoll\.delay\(state\.module,active,pollFailures\)\)/);
  // 活跃/空闲判定接到指纹探测与整点兜底
  assert.match(source,/function pollNote\(ok\)\{pollFailures=ok\?0:Math\.min\(pollFailures\+1,3\)\}/);
  assert.match(source,/shouldFullFetch\(now,pollLastFullAt,pollFingerprint,fingerprint\)/);
  // 轻量探测走 activity probe=1，且只在有活在飞时启用
  assert.match(source,/policy\.probe&&active/);
  assert.match(source,/\/api\/admin\/activity\?probe=1&days=/);
  // 回到前台立刻刷一次，不干等定时器
  assert.match(source,/document\.addEventListener\('visibilitychange'/);
  assert.match(source,/function restartPolling\(\)\{pollFailures=0;scheduleNextPoll\(\)\}/);
  // 页面隐藏或正在做敏感操作时不打接口，只顺延
  assert.match(source,/if\(document\.hidden\|\|state\.poolActions\)\{scheduleNextPoll\(\);return\}/);
  // 旧的每模块定时轮询必须删干净
  assert.doesNotMatch(source,/setInterval\(loadServers,5000\)/);
  assert.doesNotMatch(source,/setInterval\(loadRealtimeTasks,5000\)/);
  assert.doesNotMatch(source,/refreshTimer\s*=\s*setInterval\(/);
  assert.doesNotMatch(source,/serverTimer\s*=\s*setInterval\(/);
});

test('locking the admin console stops the scheduler and switching modules restarts it',()=>{
  const lock=source.slice(source.indexOf('  function lockAdmin('),source.indexOf('  function switchModule('));
  assert.match(lock,/clearTimeout\(pollTimer\)/);
  const switcher=source.slice(source.indexOf('  function switchModule('));
  assert.match(switcher.slice(0,4000),/restartPolling\(\)/);
  // 刷新按钮仍可手动拉最新（用户不必等下一期）
  assert.match(source,/el\('operationsRefresh'\)\.onclick=function\(\)\{load\(false\)/);
  assert.match(source,/el\('reqRefresh'\)\.onclick=function\(\)\{loadReqLogs\(false\)\}/);
});
