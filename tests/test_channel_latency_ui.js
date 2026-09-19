const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../site/admin/channel-workspace.js'),'utf8');
function harness(api){
 const ctx={latencyResults:{},latencyChoices:{},latencyBusy:new Set(),latencyEpoch:1,data:{items:[{id:'a',version:2}]},refreshPriority(){},api};
 vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('    async function detectLatency('),source.indexOf('    function livePrimaryRow(')),ctx);return ctx;
}
test('检测仅调用延迟接口，HTTP错误不会显示生成可用',async()=>{
 let input;const ctx=harness(async(url,opts)=>{input={url,body:JSON.parse(opts.body)};return {version:2,state:'http_error',http_status:401,latency_ms:238.2}});
 await ctx.detectLatency('managed:a');assert.equal(input.url,'/api/admin/channel-manager/latency');assert.deepEqual(input.body,{uid:'managed:a'});assert.equal(ctx.latencyResults['managed:a'],'238 ms · HTTP 401');
});
test('同渠道并发点击只发送一次，刷新后旧响应不污染新配置',async()=>{
 let done,calls=0;const ctx=harness(()=>{calls++;return new Promise(r=>done=r)});
 const run=ctx.detectLatency('managed:a');await ctx.detectLatency('managed:a');ctx.latencyEpoch++;done({version:2,state:'reachable',latency_ms:10});await run;
 assert.equal(calls,1);assert.equal(ctx.latencyResults['managed:a'],'');assert.equal(ctx.latencyBusy.size,0);
});
test('版本不符不展示测速，超时不冒充零延迟',async()=>{
 const ctx=harness(async()=>({version:1,state:'reachable',latency_ms:0}));await ctx.detectLatency('managed:a');assert.equal(ctx.latencyResults['managed:a'],'');
 ctx.api=async()=>({version:2,state:'timeout',latency_ms:10000});await ctx.detectLatency('managed:a');assert.match(ctx.latencyResults['managed:a'],/超时/);
});
