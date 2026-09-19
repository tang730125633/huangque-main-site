/* 「立即完整测试」按钮：后端 start_test 早就支持 kind='full'，但界面一直没有入口。
   没有它，渠道的完整生成测试只能靠每日定时任务；一旦超过 24 小时，发布门槛
   （主渠道 require_ready）就会拒绝，用户看到的现象是「拖上去没反应」。 */
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../site/admin/channel-workspace.js'),'utf8');

function harness(healthSeq){
  const posts=[],toasts=[],notes={};
  let index=0;
  const ctx={
    fullTestBusy:new Set(),fullTestNote:notes,
    data:{items:[{id:'ch-a',health:undefined}]},
    refreshPriority(){},toast:m=>toasts.push(m),
    confirm:()=>true,
    api:async(url,opts)=>{posts.push([url,JSON.parse(opts.body)]);return{run_id:'run-abcdef123456',state:'queued'}},
    env:{refresh:async()=>{ctx.data.items[0].health=healthSeq[Math.min(index++,healthSeq.length-1)];return true}},
    setTimeout:(fn)=>fn(),   // 立刻推进轮询，避免测试等待
  };
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('    async function runFullTest('),source.indexOf('    function latencyControls(')),ctx);
  return {ctx,posts,toasts,notes};
}
test('完整测试触发 kind=full，并轮询到终态',async()=>{
  const h=harness(['检测中','检测中','成品核验通过']);
  await h.ctx.runFullTest('ch-a');
  assert.equal(h.posts.length,1);
  assert.equal(h.posts[0][0],'/api/admin/channel-manager/test');
  assert.deepEqual(h.posts[0][1],{id:'ch-a',kind:'full'});
  assert.equal(h.notes['ch-a'],'成品核验通过');
  assert.ok(h.toasts.some(m=>/通过/.test(m)),'通过时要给出可发布的提示');
  assert.equal(h.ctx.fullTestBusy.has('ch-a'),false,'结束后必须解除忙碌态');
});
test('失败与结果未知都如实上报，不谎报通过',async()=>{
  for(const state of ['异常','结果未知']){
    const h=harness([state]);
    await h.ctx.runFullTest('ch-b');
    assert.equal(h.notes['ch-b'],state);
    assert.ok(!h.toasts.some(m=>/通过/.test(m)),state+' 不能显示为通过');
  }
});
test('用户取消确认时不发请求',async()=>{
  const h=harness(['成品核验通过']);
  h.ctx.confirm=()=>false;
  await h.ctx.runFullTest('ch-c');
  assert.equal(h.posts.length,0);
  assert.equal(h.ctx.fullTestBusy.has('ch-c'),false);
});
test('同一渠道并发点击只提交一次',async()=>{
  const h=harness(['成品核验通过']);
  h.ctx.fullTestBusy.add('ch-d');
  await h.ctx.runFullTest('ch-d');
  assert.equal(h.posts.length,0,'忙碌中不得重复提交（每次都会真花钱）');
});
test('触发接口报错时不假装成功',async()=>{
  const h=harness(['成品核验通过']);
  h.ctx.api=async()=>{throw Error('预算或次数不足')};
  await h.ctx.runFullTest('ch-e');
  assert.match(h.notes['ch-e'],/预算或次数不足/);
  assert.equal(h.ctx.fullTestBusy.has('ch-e'),false);
});
test('按钮只在托管渠道上渲染，且不能重复点',()=>{
  assert.match(source,/data-cm-fulltest/);
  const m=source.match(/function fullTestControls\(id\)\{[\s\S]*?\n    \}/);
  assert.ok(m,'缺少 fullTestControls');
  assert.match(m[0],/indexOf\(':'\)>=0\).*return ''/,'带 : 的 uid 不是托管渠道 id，不应渲染按钮');
  assert.match(m[0],/立即完整测试/);
});
