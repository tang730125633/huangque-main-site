const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../site/admin/index.html'), 'utf8');
function setup() {
  const elements = Object.fromEntries(['reqSource','reqStatus','reqUser','reqSearch','reqAttributed','reqNoise','reqUpdatedAt'].map(id => [id, {value:'',checked:false,textContent:''}]));
  const pending=[], rendered=[];
  const context={state:{reqPage:1,reqPageSize:20,days:7},el:id=>elements[id],encodeURIComponent,Promise,toast:()=>{},renderActivity:d=>rendered.push(d),api:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject}))};
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
  const c={esc:s=>String(s??'').replace(/</g,'&lt;'),fmtDuration:s=>s+'秒',fmtTime:String,taskStageClass:s=>s,taskRawField:()=>''};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  function renderTaskCard('),source.indexOf('  function renderActivity(')),c);
  const html=c.renderTaskCard({source:'job',task_id:1,cat:'running',func:'<test>',stages:[]});
  assert.match(html,/task-row-main/);assert.match(html,/task-stages/);assert.match(html,/task-metrics/);
  assert.match(html,/业务受理/);assert.match(html,/成品交付/);assert.match(html,/&lt;test>/);
  assert.equal((html.match(/class="task-stage unknown"/g)||[]).length,5);
  assert.doesNotMatch(html,/task-card-v2|task-timeline|class="task-stage passed"/);
});

test('task evidence distinguishes unknown submission from failure and folds raw records',()=>{
  const c={esc:String,fmtDuration:s=>s+'秒',taskStageClass:s=>s,taskRawField:()=>''};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  function renderTaskCard('),source.indexOf('  function renderActivity(')),c);
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
