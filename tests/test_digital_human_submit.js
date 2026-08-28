const test = require('node:test');
const assert = require('node:assert/strict');
const submit = require('../site/workbench/digital-human-submit.js');

test('retries a transient security 503 twice and then succeeds', async () => {
  let calls=0,retries=[];
  const result=await submit.withSecurityRetry(()=>{
    calls++;
    if(calls<3){const error=new Error('图片安全检测暂时不可用');error.status=503;error.code='content_security_image_unavailable';throw error;}
    return 42;
  },{delays:[0,0],onRetry:(attempt)=>retries.push(attempt)});
  assert.equal(result,42);
  assert.equal(calls,3);
  assert.deepEqual(retries,[1,2]);
});

test('does not retry policy rejection or unrelated failures', async () => {
  for(const source of [
    {status:400,code:'content_rejected'},
    {status:503,code:'upstream_unavailable'},
    {status:503,code:'content_security_configuration_unavailable'},
  ]){
    let calls=0;
    await assert.rejects(submit.withSecurityRetry(()=>{
      calls++;const error=new Error('terminal');Object.assign(error,source);throw error;
    },{delays:[0,0]}),/terminal/);
    assert.equal(calls,1);
  }
});

test('visible error includes no-charge statement and request id', () => {
  const error=new Error('图片安全检测暂时不可用，请稍后重试');
  Object.assign(error,{status:503,code:'content_security_image_unavailable',requestId:'hq_test_123'});
  assert.match(submit.describe(error),/尚未创建任务、未扣点/);
  assert.match(submit.describe(error),/hq_test_123/);
});

test('retries only the active-job capacity response and preserves the operation', async () => {
  let calls=0,retries=[];
  const result=await submit.withCapacityRetry(()=>{
    calls++;
    if(calls<3){
      const error=new Error('您有 5 个任务正在排队/生成，完成后再提交');
      Object.assign(error,{status:429,code:'active_job_cap',hqCode:'HQ-RATE-001'});
      throw error;
    }
    return 88;
  },{delays:[0,0],onRetry:attempt=>retries.push(attempt)});
  assert.equal(result,88);
  assert.equal(calls,3);
  assert.deepEqual(retries,[1,2]);
});

test('does not retry unrelated rate limits', async () => {
  for(const source of [
    {status:429,hqCode:'HQ-RATE-001',message:'请求过于频繁，请稍后再试'},
    {status:429,hqCode:'HQ-POINTS-001',message:'完成后再提交'},
    {status:503,hqCode:'HQ-RATE-001',message:'您有 5 个任务正在排队/生成，完成后再提交'},
  ]){
    let calls=0;
    await assert.rejects(submit.withCapacityRetry(()=>{
      calls++;const error=new Error(source.message);Object.assign(error,source);throw error;
    },{delays:[0,0]}));
    assert.equal(calls,1);
  }
});
