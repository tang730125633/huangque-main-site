const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const context={};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../site/admin/channel-catalog.js'),'utf8'),context);
const C=context.ChannelCatalog;
const data={adapters:{image:{kind:'image'},video:{kind:'xiaole_video'}},items:[{id:'a',name:'自定义渠道',supplier:'中转 A',model:'Grok',adapter:'video',enabled:true,health:'未验证',connection_type:'relay'},{id:'b',name:'旧图片',adapter:'image',enabled:false,health:'成品核验通过'}],mappings:[{channel:'a',label:'黄雀 1'}]};
const rows=C.catalog(data,[{key:'openai',name:'OpenAI',category:'图片生成 / 视频生成',features:[],evidence:{state:'ok',label:'鉴权通过'}},{key:'old',name:'历史',category:'图片生成',accepts_new_jobs:false}]);
const filters={category:'all',q:'',supplier:'',transport:'',status:'',history:false};
test('lifecycle confirmation preserves input on failure, prevents duplicate requests and refreshes after success',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../site/admin/channel-manager.js'),'utf8');
  const buttons=[{disabled:false},{disabled:false}],error={textContent:''},reason={value:'',focus(){}};
  const form={elements:{reason},querySelectorAll:()=>buttons};
  const dialog={innerHTML:'',open:false,querySelector:s=>s==='form'?form:s==='[role=alert]'?error:buttons[0],addEventListener(){},showModal(){this.open=true},close(){this.open=false;this.onclose()},remove(){}};
  const requests=[];let refreshes=0;
  const ctx={data:{mappings:[]},document:{createElement:()=>dialog,body:{append(){}}},esc:String,workspace:{close(){}},toast(){},load(){refreshes++},post:(action,body)=>new Promise((resolve,reject)=>requests.push({action,body,resolve,reject}))};
  vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('    function lifecycle('),source.indexOf('    function render(){')),ctx);
  ctx.lifecycle({id:'channel',version:3,name:'test',source:'managed'},'disable');
  const submit=()=>form.onsubmit({preventDefault(){}});
  await submit();assert.equal(requests.length,0);
  reason.value='维护测试';const first=submit();await submit();assert.equal(requests.length,1);
  assert.equal(requests[0].body.version,3);assert.equal(requests[0].action,'lifecycle');
  requests[0].reject(Error('版本已变化'));await first;
  assert.equal(reason.value,'维护测试');assert.equal(dialog.open,true);assert.match(error.textContent,/版本已变化/);
  const second=submit();requests[1].resolve({});await second;assert.equal(dialog.open,false);assert.equal(refreshes,1);
});
test('recycle bin is separate from history and excluded from mapping choices',()=>{
  const changed={...data,items:[...data.items,{id:'deleted',adapter:'image',enabled:false,_lifecycle:{deleted:true}}]};
  const catalog=C.catalog(changed,[]);
  assert.equal(C.filter(catalog,{...filters,history:true}).length,2);
  assert.equal(C.filter(catalog,{...filters,status:'deleted'}).length,1);
  assert.equal(C.compatible(changed,'image').length,1);
});
test('legacy lifecycle states override admission display without fabricating health',()=>{
  const changed={...data,legacy_controls:{openai:{revision:2,enabled:false}},legacy_scopes:{openai:'图片任务'}};
  const row=C.catalog(changed,[{key:'openai',name:'OpenAI',category:'图片生成'}])[0];
  assert.equal(row.enabled,false);assert.equal(row.version,2);assert.equal(row.health,'未验证');
  assert.equal(C.filter([row],filters).length,0);
  assert.equal(C.filter([row],{...filters,status:'disabled'}).length,1);
});
test('multi-capability supplier is one configuration in multiple categories',()=>{
  assert.equal(rows.filter(r=>r.uid==='legacy:openai').length,1);
  assert.equal(C.filter(rows,{...filters,category:'image'})[0].uid,'legacy:openai');
  assert.equal(C.filter(rows,{...filters,category:'video'}).length,2);
});
test('history is hidden by default and disabled filter makes it discoverable',()=>{
  assert.equal(C.filter(rows,filters).length,2);
  assert.equal(C.filter(rows,{...filters,history:true}).length,4);
  assert.equal(C.filter(rows,{...filters,status:'disabled'}).length,2);
});
test('search, supplier, transport and category combine without changing rows',()=>{
  assert.equal(C.filter(rows,{...filters,q:'黄雀',supplier:'中转 A',transport:'relay',category:'video'}).length,1);
  assert.equal(C.filter(rows,{...filters,q:'grok',category:'image'}).length,0);
  assert.equal(rows.length,4);
});
test('missing and stale results never become green',()=>{
  assert.equal(C.checkLabel(null),'未验证');
  assert.equal(C.checkLabel({state:'passed',updated:1},90000),'证据已过期');
  assert.equal(C.checkLabel({state:'unknown',updated:100},101),'结果未知');
  assert.equal(C.checkLabel({state:'passed',updated:100},101),'通过');
  assert.equal(C.filter(rows,{...filters,status:'attention'}).length,1);
});
test('mapping options only contain compatible protocol kinds',()=>{
  assert.equal(C.compatible(data,'image').length,1);
  assert.equal(C.compatible(data,'image')[0].id,'b');
  assert.equal(C.compatible(data,'audio').length,0);
});
test('unknown provider category stays explicit',()=>{
  assert.deepEqual(Array.from(C.classify('未登记服务')),['other']);
  assert.equal(rows.find(r=>r.id==='b').supplier,'未标注供应商');
});
test('admin scripts parse together and channel entry is unique',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../site/admin/index.html'),'utf8');
  for(const m of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))if(m[1].trim())new vm.Script(m[1]);
  for(const file of ['channel-workspace.js','channel-manager.js'])new vm.Script(fs.readFileSync(path.join(__dirname,'../site/admin',file),'utf8'));
  assert.equal((html.match(/data-module-tab="managedChannels"/g)||[]).length,1);
  assert.equal((html.match(/data-module-tab="channels"/g)||[]).length,0);
});
