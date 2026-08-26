const test = require('node:test');
const assert = require('node:assert/strict');
const recovery = require('../site/workbench/digital-human-recovery.js');

test('ambiguous poll failure preserves job and does not POST', async () => {
  const state = {jobId:17, key:'stable-key', failed:false};
  const posts = [];
  let terminalMarks = 0;
  const network = Object.assign(new Error('503'), {status:503});
  await assert.rejects(recovery.resume({
    jobId:state.jobId, retryApproved:state.failed,
    poll:async id => {assert.equal(id,state.jobId);throw network;},
    launch:async () => {posts.push(state.key);},
    markTerminal:() => {terminalMarks++;state.failed=true;},
  }), /503/);
  assert.deepEqual(posts,[]);
  assert.equal(state.jobId,17);
  assert.equal(state.key,'stable-key');
  assert.equal(state.failed,false);
  assert.equal(terminalMarks,0);
});

test('terminal failure pauses first and relaunches only after user retry', async () => {
  const state = {jobId:18, key:'old-key', failed:false};
  const posts = [];
  let terminalMarks = 0;
  const terminal = Object.assign(new Error('failed'), {terminalJob:true});
  await assert.rejects(recovery.resume({
    jobId:state.jobId, retryApproved:state.failed,
    poll:async id => {assert.equal(id,state.jobId);throw terminal;},
    launch:async () => {posts.push(state.key);},
    markTerminal:() => {terminalMarks++;state.failed=true;},
  }), /failed/);
  assert.deepEqual(posts,[]);
  assert.equal(terminalMarks,1);
  assert.equal(state.jobId,18);
  assert.equal(state.key,'old-key');
  assert.equal(state.failed,true);

  const result = await recovery.resume({
    jobId:state.jobId, retryApproved:state.failed,
    poll:async () => {throw new Error('must not poll');},
    launch:async retry => {
      assert.equal(retry,true);
      state.key='new-key';
      posts.push(state.key);
      state.jobId=19;
      state.failed=false;
      return {job_id:state.jobId};
    },
    markTerminal:() => {terminalMarks++;},
  });
  assert.deepEqual(result,{job_id:19});
  assert.deepEqual(posts,['new-key']);
  assert.equal(state.jobId,19);
  assert.equal(state.key,'new-key');
  assert.equal(state.failed,false);
});

test('ambiguous initial POST is retried with the same idempotency key', async () => {
  const state = {jobId:0, key:'submit-key'};
  const posts = [];
  const launch = async retry => {
    assert.equal(retry,false);
    posts.push(state.key);
    throw Object.assign(new Error('gateway timeout'), {status:504});
  };
  await assert.rejects(recovery.resume({jobId:state.jobId, retryApproved:false, launch}), /gateway timeout/);
  await assert.rejects(recovery.resume({jobId:state.jobId, retryApproved:false, launch}), /gateway timeout/);
  assert.deepEqual(posts,['submit-key','submit-key']);
  assert.equal(state.jobId,0);
  assert.equal(state.key,'submit-key');
});
