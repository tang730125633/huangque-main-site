const test = require('node:test');
const assert = require('node:assert/strict');
const state = require('../site/workbench/digital-human-material-state.js');

test('upload in progress blocks analyze, start, add, and remove', () => {
  assert.equal(state.canAnalyze('input',true),false);
  assert.equal(state.canStart('planned',true,true),false);
  assert.equal(state.canChange('planned',true),false);
});

test('approved production locks the frozen material set', () => {
  assert.equal(state.canChange('approved',false),false);
  assert.equal(state.canAnalyze('approved',false),false);
  assert.equal(state.canStart('approved',false,true),true);
});

test('refresh restore exposes a clickable resume action for approved work', () => {
  const button={disabled:true,textContent:'生成进行中'};
  state.restoreStartButton(button,'approved');
  assert.equal(button.disabled,false);
  assert.equal(button.textContent,'继续上次未完成的生成');
  assert.equal(state.canStart('approved',false,true),true);
});

test('planned restore exposes the normal confirmation action', () => {
  const button={disabled:true,textContent:''};
  state.restoreStartButton(button,'planned');
  assert.equal(button.disabled,false);
  assert.equal(button.textContent,'确认方案并生成');
});

test('restore keeps only valid unexpired private uploads', () => {
  const valid={upload_id:'img_'+'a'.repeat(32),name:'有效素材.png',sha256:'digest',expires_at:2000};
  const expired={upload_id:'img_'+'b'.repeat(32),name:'过期素材.png',expires_at:999};
  const foreign={upload_id:'bad-id',name:'异常素材.png',expires_at:2000};
  assert.deepEqual(state.normalize([valid,expired,foreign],1000),[valid]);
});

test('input and planned restore may discard expired or invalid materials', () => {
  const valid={upload_id:'img_'+'a'.repeat(32),name:'有效素材.png',sha256:'digest',expires_at:2000};
  const expired={upload_id:'img_'+'b'.repeat(32),name:'过期素材.png',expires_at:1000};
  const invalid={upload_id:'foreign-id',name:'非法素材.png',expires_at:2000};

  for (const phase of ['input','planned']) {
    assert.deepEqual(state.restore([valid,expired,invalid],phase,1000),{
      items:[valid],
      valid:true,
      error:'',
    });
  }
});

test('approved restore accepts an intact frozen material set', () => {
  const first={upload_id:'img_'+'a'.repeat(32),name:'素材一.png',sha256:'first',expires_at:2001};
  const second={upload_id:'img_'+'b'.repeat(32),name:'素材二.png',sha256:'second',expires_at:3000};

  assert.deepEqual(state.restore([first,second],'approved',2000),{
    items:[first,second],
    valid:true,
    error:'',
  });
});

test('approved restore blocks when any frozen material expires or is invalid', () => {
  const valid={upload_id:'img_'+'a'.repeat(32),name:'有效素材.png',sha256:'',expires_at:2000};
  const expired={upload_id:'img_'+'b'.repeat(32),name:'过期素材.png',expires_at:1000};
  const invalid={upload_id:'bad-id',name:'非法素材.png',expires_at:2000};
  const expectedError='已确认的客户素材已过期或无效，不能自动改为 AI 补图；请放弃旧任务并重新设置';

  assert.deepEqual(state.restore([valid,expired],'approved',1000),{
    items:[valid],
    valid:false,
    error:expectedError,
  });
  assert.deepEqual(state.restore([valid,invalid],'approved',1000),{
    items:[valid],
    valid:false,
    error:expectedError,
  });
});

test('approved restore blocks oversized frozen sets instead of silently truncating to six', () => {
  const saved=Array.from({length:7},(_,index)=>({
    upload_id:'img_'+index.toString(16).repeat(32),
    name:'素材 '+(index+1)+'.png',
    expires_at:2000,
  }));

  const restored=state.restore(saved,'approved',1000);
  assert.equal(restored.items.length,6);
  assert.equal(restored.valid,false);
  assert.match(restored.error,/放弃旧任务并重新设置/);
});

test('approved restore allows an empty frozen material set', () => {
  assert.deepEqual(state.restore([],'approved',1000),{
    items:[],
    valid:true,
    error:'',
  });
});

test('approved restore fails closed when the frozen material payload is malformed', () => {
  const result=state.restore({upload_id:'img_'+('a'.repeat(32))},'approved',100);
  assert.equal(result.valid,false);
  assert.deepEqual(result.items,[]);
  assert.match(result.error,/不能自动改为 AI 补图/);
});
