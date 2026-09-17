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

test('shadow observations have explicit run type and state labels',()=>{
  const source=fs.readFileSync(path.join(__dirname,'../site/admin/channel-manager.js'),'utf8');
  assert.match(source,/captured:'已记录'/);
  assert.match(source,/shadow:'影子观察'/);
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
test('ordered mapping candidates remain visible in channel relationships',()=>{
  const changed={...data,operation_mappings:[{operation_id:'image.test',label:'图片候选',channels:['b']} ]};
  const candidate=C.catalog(changed,[]).find(row=>row.id==='b');
  assert.deepEqual(Array.from(C.mappingChannels(changed.operation_mappings[0])),['b']);
  assert.deepEqual(Array.from(candidate.features),['图片候选']);
});
test('unknown provider category stays explicit',()=>{
  assert.deepEqual(Array.from(C.classify('未登记服务')),['other']);
  assert.equal(rows.find(r=>r.id==='b').supplier,'未标注供应商');
});
test('admin scripts parse together and channel entry is unique',()=>{
  const html=fs.readFileSync(path.join(__dirname,'../site/admin/index.html'),'utf8');
  assert.match(html,/\.cm-business-tabs\{[^}]*overflow-x:auto;overflow-y:hidden[^}]*\}/);
  for(const m of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))if(m[1].trim())new vm.Script(m[1]);
  for(const file of ['channel-workspace.js','channel-manager.js'])new vm.Script(fs.readFileSync(path.join(__dirname,'../site/admin',file),'utf8'));
  assert.equal((html.match(/data-module-tab="managedChannels"/g)||[]).length,1);
  assert.equal((html.match(/data-module-tab="channels"/g)||[]).length,0);
});

test('frontend function center uses a model list and keeps technical details in the drawer',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../site/admin/channel-workspace.js'),'utf8');
  const element=id=>({id,hidden:false,innerHTML:'',textContent:'',value:'',checked:false,
    isConnected:true,listeners:{},focus(){},classList:{toggle(){}},addEventListener(type,fn){this.listeners[type]=fn},setAttribute(){},querySelector(){return null},querySelectorAll(){return[]}});
  const ids=['cmMatrix','cmSearch','cmSupplier','cmTransport','cmState','cmHistory','cmDrawer','cmDrawerTitle','cmDrawerClose','cmDetail','cmEditor','cmMappingEditor','cmCount','cmCategories','cmList','cmHealth'];
  const elements=Object.fromEntries(ids.map(id=>[id,element(id)])),root=element('root');
  elements.cmDrawer.hidden=true;
  const panels=['matrix','catalog','mapping','health','audit','layout'].map(cmPanel=>({dataset:{cmPanel},hidden:cmPanel!=='matrix'}));
  const context={window:null,ChannelCatalog:C,confirm:()=>true,document:{activeElement:element('active'),querySelector:()=>root,querySelectorAll:selector=>selector==='[data-cm-panel]'?panels:[],body:{classList:{add(){},remove(){}}}}};context.window=context;
  vm.createContext(context);vm.runInContext(source,context);
  const legacyChannels=[
    {key:'gemini',name:'Google Gemini API',category:'图片生成',features:['纳米香蕉'],configured:true,accepts_new_jobs:true,env_base_url:'https://generativelanguage.googleapis.com',evidence:{state:'ok',label:'鉴权通过'}},
    {key:'cosyvoice',name:'阿里百炼 API',category:'音频生成',features:['AI 配音 → 公共音色'],configured:true,accepts_new_jobs:true,env_base_url:'https://dashscope.aliyuncs.com',evidence:{state:'ok',label:'鉴权通过'}}
  ];
  const detailCalls=[],editCalls=[],newCalls=[],requests=[];
  let allowLegacyClose=true;
  const workspace=context.initChannelWorkspace({el:id=>elements[id],esc:String,toast(){},api:async(path,options)=>{requests.push([path,options]);return{}},legacy:()=>legacyChannels,closeLegacy(){return allowLegacyClose},editChannel:id=>editCalls.push(id),newChannel:template=>newCalls.push(template),lifecycle(){},detail(channel,options){detailCalls.push([channel.key,options.managementKind])},mapping(){},refresh(){},task(){},journey(){}});
  const workspaceData={items:[],mappings:[],legacy_controls:{},adapters:{},frontend_matrix:{page:'image',summary:{products:1,models:1,admitted_models:1,attention_models:0},products:[{
    key:'banana',label:'纳米香蕉',description:'前台产品',visible:true,admitted:true,models:[{key:'nb2',label:'纳米香蕉 2',actual_model:'gemini-3.1-flash-image',capabilities:['文生图','图生图'],admitted:true,routes:[{capability:'文生图',control_state:'legacy',admitted:true,primary:{id:'gemini-primary',name:'Google Gemini API',supplier:'Google Gemini API',connection_type:'official',base_host:'generativelanguage.googleapis.com',credential_source:'服务器环境变量 · GEMINI_API_KEY',configured:true,enabled:true,auth:{state:'unverified',label:'未验证'},full:{state:'unverified',label:'未建立模型级成品证据'}},backup:{id:'gemini-backup',name:'Google Gemini API · 兜底',supplier:'Google Gemini API',connection_type:'relay',base_host:'relay.example.com',credential_source:'服务器环境变量 · GEMINI_API_KEY',configured:true,enabled:true,auth:{state:'ok',label:'鉴权通过'},full:{state:'unverified',label:'未建立模型级成品证据'}},candidate:{id:'gemini-shadow',name:'候选线路',supplier:'候选供应商',connection_type:'relay',base_host:'shadow.example.com',credential_source:'渠道密钥库',configured:true,enabled:false,auth:{state:'stale',label:'证据已过期'},full:{state:'unverified',label:'未建立模型级成品证据'}}},{capability:'图生图',control_state:'legacy',admitted:true,primary:{id:'gemini-primary',name:'Google Gemini API',supplier:'Google Gemini API',connection_type:'official',base_host:'generativelanguage.googleapis.com',credential_source:'服务器环境变量 · GEMINI_API_KEY',configured:true,enabled:true,auth:{state:'unverified',label:'未验证'},full:{state:'unverified',label:'未建立模型级成品证据'}}}]}]
  },{key:'xiaole',label:'果肉生图',description:'前台未开放',visible:false,admitted:false,models:[{key:'default',label:'GPT Image 2',actual_model:'gpt-image-2',visible:false,admitted:false,routes:[]}]}]}};
  const bananaModel=workspaceData.frontend_matrix.products[0].models[0];
  bananaModel.routes[0].primary.management={kind:'server_env',uid:'legacy:gemini'};
  bananaModel.routes[0].backup.management={kind:'provider_pool',uid:'legacy:gemini'};
  bananaModel.routes[0].candidate.management={kind:'managed_channel',uid:'managed:shadow-channel'};
  bananaModel.routes[1].primary.management={kind:'server_env',uid:'legacy:gemini'};
  workspace.render(workspaceData);
  assert.match(elements.cmMatrix.innerHTML,/cm-function-workspace/);
  for(const group of ['内容创作','人物与声音','智能工具','基础服务'])assert.match(elements.cmMatrix.innerHTML,new RegExp(group));
  for(const label of ['生图','生视频'])assert.match(elements.cmMatrix.innerHTML,new RegExp(label));
  const businessHtml=elements.cmMatrix.innerHTML.match(/<nav class="cm-business-tabs"[^>]*>([\s\S]*?)<\/nav>/)[1];
  assert.equal((businessHtml.match(/data-cm-matrix-group=/g)||[]).length,4);
  assert.doesNotMatch(businessHtml,/生图|生视频|数字人|音频与配音/);
  assert.doesNotMatch(businessHtml,/项需处理/);
  assert.match(elements.cmMatrix.innerHTML,/cm-subfunction-tabs/);
  assert.match(elements.cmMatrix.innerHTML,/cm-matrix-status neutral">待验证/);
  assert.match(elements.cmMatrix.innerHTML,/待验证 <b>1<\/b>/);
  assert.match(elements.cmMatrix.innerHTML,/需要处理 <b>0<\/b>/);
  const primary=workspaceData.frontend_matrix.products[0].models[0].routes[0].primary;
  primary.auth={state:'pending',label:'检测中'};primary.full={state:'ok',label:'通过'};
  workspace.render(workspaceData);
  assert.match(elements.cmMatrix.innerHTML,/cm-matrix-status neutral">检测中/);
  assert.doesNotMatch(elements.cmMatrix.innerHTML,/项需处理/);
  assert.match(elements.cmMatrix.innerHTML,/需要处理 <b>0<\/b>/);
  primary.auth={state:'attention',label:'凭据被拒绝'};
  workspace.render(workspaceData);
  assert.match(elements.cmMatrix.innerHTML,/cm-matrix-status warn">凭据被拒绝/);
  assert.match(elements.cmMatrix.innerHTML,/1 项需处理/);
  assert.match(elements.cmMatrix.innerHTML,/需要处理 <b>1<\/b>/);
  primary.auth={state:'ok',label:'鉴权通过'};primary.full={state:'ok',label:'成品验证通过'};
  const imageRoute=workspaceData.frontend_matrix.products[0].models[0].routes[1];
  imageRoute.admitted=false;imageRoute.reason='图生图线路不可用';
  workspace.render(workspaceData);
  assert.match(elements.cmMatrix.innerHTML,/cm-matrix-status warn">部分能力不可接单/);
  assert.match(elements.cmMatrix.innerHTML,/1 项需处理/);
  assert.match(elements.cmMatrix.innerHTML,/需要处理 <b>1<\/b>/);
  imageRoute.admitted=true;imageRoute.reason='';
  primary.auth={state:'stale',label:'证据已过期'};
  workspace.render(workspaceData);
  assert.match(elements.cmMatrix.innerHTML,/cm-matrix-status warn">证据已过期/);
  assert.match(elements.cmMatrix.innerHTML,/1 项需处理/);
  assert.match(elements.cmMatrix.innerHTML,/需要处理 <b>1<\/b>/);
  primary.auth={state:'unverified',label:'未验证'};primary.full={state:'unverified',label:'未建立模型级成品证据'};
  workspace.render(workspaceData);
  assert.match(elements.cmMatrix.innerHTML,/cm-model-list/);
  assert.match(elements.cmMatrix.innerHTML,/cm-model-list-link/);
  assert.match(elements.cmMatrix.innerHTML,/当前主渠道/);
  assert.match(elements.cmMatrix.innerHTML,/纳米香蕉 2/);
  assert.match(elements.cmMatrix.innerHTML,/Google Gemini API/);
  assert.match(elements.cmMatrix.innerHTML,/官方直连/);
  assert.match(elements.cmMatrix.innerHTML,/配置渠道/);
  assert.doesNotMatch(elements.cmMatrix.innerHTML,/generativelanguage\.googleapis\.com/);
  assert.doesNotMatch(elements.cmMatrix.innerHTML,/服务器环境变量/);
  assert.doesNotMatch(elements.cmMatrix.innerHTML,/证据已过期/);
  assert.match(elements.cmMatrix.innerHTML,/cm-hidden-products/);
  assert.match(elements.cmMatrix.innerHTML,/果肉生图/);
  assert.doesNotMatch(elements.cmMatrix.innerHTML,/cm-hidden-products" open/);
  const hiddenButton={dataset:{cmMatrixHidden:''}};
  root.listeners.click({target:{closest:()=>hiddenButton}});
  assert.match(elements.cmMatrix.innerHTML,/cm-hidden-products" open/);
  assert.match(elements.cmMatrix.innerHTML,/前台隐藏/);
  root.listeners.click({target:{closest:()=>hiddenButton}});
  const peopleButton={dataset:{cmMatrixGroup:'people'}};
  root.listeners.click({target:{closest:()=>peopleButton}});
  assert.match(elements.cmMatrix.innerHTML,/<h3>数字人<\/h3>/);
  const audioButton={dataset:{cmMatrixPage:'audio'}};
  root.listeners.click({target:{closest:()=>audioButton}});
  assert.match(elements.cmMatrix.innerHTML,/阿里百炼 API/);
  assert.match(elements.cmMatrix.innerHTML,/服务配置/);
  assert.match(elements.cmMatrix.innerHTML,/已登记的真实前端功能与依赖服务/);
  const cosyvoice=legacyChannels.find(item=>item.key==='cosyvoice');
  cosyvoice.evidence={state:'warn',verification_state:'pending',label:'已配置 · 需人工检测'};
  workspace.render(workspaceData);
  assert.match(elements.cmMatrix.innerHTML,/cm-matrix-status neutral">已配置 · 需人工检测/);
  assert.doesNotMatch(elements.cmMatrix.innerHTML,/项需处理/);
  cosyvoice.evidence={state:'fail',label:'凭据已失效'};
  workspace.render(workspaceData);
  assert.match(elements.cmMatrix.innerHTML,/cm-matrix-status warn">凭据已失效/);
  assert.match(elements.cmMatrix.innerHTML,/1 项需处理/);
  root.listeners.click({target:{closest:()=>({dataset:{cmMatrixGroup:'creation'}})}});
  assert.match(elements.cmMatrix.innerHTML,/<h3>生图<\/h3>/);
  root.listeners.click({target:{closest:()=>peopleButton}});
  assert.match(elements.cmMatrix.innerHTML,/<h3>音频与配音<\/h3>/);
  root.listeners.click({target:{closest:()=>({dataset:{cmMatrixGroup:'creation'}})}});
  assert.match(elements.cmMatrix.innerHTML,/纳米香蕉 2/);
  root.listeners.click({target:{closest:()=>({dataset:{cmView:'health'}})}});
  assert.equal(panels.find(panel=>panel.dataset.cmPanel==='health').hidden,false);
  assert.equal(panels.find(panel=>panel.dataset.cmPanel==='matrix').hidden,true);
  root.listeners.click({target:{closest:()=>({dataset:{cmView:'matrix'}})}});
  assert.equal(panels.find(panel=>panel.dataset.cmPanel==='matrix').hidden,false);
  workspaceData.items=[
    {id:'managed-primary',name:'托管主渠道',supplier:'供应商 A',adapter:'openai_image',model:'gemini-3.1-flash-image',base_url:'https://primary.example/v1',connection_type:'official',enabled:true,configured:true,health:'成品核验通过'},
    {id:'managed-backup',name:'托管备用渠道',supplier:'供应商 B',adapter:'openai_image',model:'gemini-3.1-flash-image',base_url:'https://backup.example/v1',connection_type:'relay',enabled:true,configured:true,health:'未验证'}
  ];
  workspaceData.adapters={openai_image:{kind:'image',name:'图片生成'},gemini_image:{kind:'image',name:'Google Gemini 官方生图'}};
  workspaceData.operations=[{operation_id:'image.banana.nb2.text',channel_kind:'image',name:'纳米香蕉 2 文生图',mapping:{operation_id:'image.banana.nb2.text',state:'shadow',revision:4,channels:['managed-primary','managed-backup'],channel:'managed-primary',backup:'managed-backup'}}];
  workspaceData.operation_mappings=[workspaceData.operations[0].mapping];
  workspaceData.runs=[{id:81,job_id:501,operation_id:'image.banana.nb2.text',mapping_revision:4,channel:'managed-backup',state:'passed',execution_snapshot:{route_attempt:2,attempts:[{attempt:1,channel:'managed-primary',version:1,state:'failed',detail:'供应商明确拒绝提交'}]}}];
  bananaModel.routes[0].operation_id='image.banana.nb2.text';
  workspace.render(workspaceData);
  const modelButton={dataset:{cmModelPage:'image',cmModelProduct:'banana',cmModelKey:'nb2'}};
  root.listeners.click({target:{closest:selector=>selector==='[data-cm-model-key]'?modelButton:null}});
  assert.equal(elements.cmDrawer.hidden,true);
  assert.match(elements.cmMatrix.innerHTML,/cm-priority-editor/);
  assert.match(elements.cmMatrix.innerHTML,/渠道优先级/);
  assert.match(elements.cmMatrix.innerHTML,/draggable="true"/);
  assert.match(elements.cmMatrix.innerHTML,/托管主渠道/);
  assert.match(elements.cmMatrix.innerHTML,/托管备用渠道/);
  assert.match(elements.cmMatrix.innerHTML,/data-cm-managed-edit="managed-primary"/);
  assert.match(elements.cmMatrix.innerHTML,/修改 Key \/ URL/);
  assert.match(elements.cmMatrix.innerHTML,/最近安全切换/);
  assert.match(elements.cmMatrix.innerHTML,/未受理，已安全切换/);
  assert.match(elements.cmMatrix.innerHTML,/生成成功/);
  assert.match(elements.cmMatrix.innerHTML,/结果未知或已受理后失败均不会切换/);
  assert.match(elements.cmMatrix.innerHTML,/data-cm-priority-save=/);
  const moveButton={dataset:{cmPriorityMove:'1',operation:'image.banana.nb2.text',channel:'managed-primary'},disabled:false};
  await root.listeners.click({target:{closest:selector=>selector==='button'?moveButton:null}});
  const priorityHtml=elements.cmMatrix.innerHTML.match(/<div class="cm-priority-list">[\s\S]*?<div class="cm-priority-add">/)[0];
  assert.ok(priorityHtml.indexOf('托管备用渠道')<priorityHtml.indexOf('托管主渠道'));
  const saveButton={dataset:{cmPrioritySave:'image.banana.nb2.text'},disabled:false};
  await root.listeners.click({target:{closest:selector=>selector==='button'?saveButton:null}});
  const publish=JSON.parse(requests.find(([path])=>path.endsWith('/operation-mapping'))[1].body);
  assert.deepEqual(Array.from(publish.channels),['managed-backup','managed-primary']);
  assert.equal(publish.expected_revision,4);
  const configButton={dataset:{cmModelConfig:'',cmModelPage:'image',cmModelProduct:'banana',cmModelKey:'nb2'}};
  await root.listeners.click({target:{closest:selector=>selector==='[data-cm-model-config]'?configButton:null}});
  assert.equal(elements.cmDrawer.hidden,false);
  assert.match(elements.cmDrawerTitle.textContent,/纳米香蕉 · 纳米香蕉 2/);
  assert.match(elements.cmDetail.innerHTML,/generativelanguage\.googleapis\.com/);
  assert.match(elements.cmDetail.innerHTML,/服务器环境变量 · GEMINI_API_KEY/);
  assert.match(elements.cmDetail.innerHTML,/影子候选：候选线路/);
  assert.match(elements.cmDetail.innerHTML,/证据已过期/);
  assert.match(elements.cmDetail.innerHTML,/未建立模型级成品证据/);
  assert.match(elements.cmDetail.innerHTML,/cm-current-route/);
  assert.match(elements.cmDetail.innerHTML,/当前主渠道/);
  assert.match(elements.cmDetail.innerHTML,/cm-model-inline-config/);
  assert.match(elements.cmDetail.innerHTML,/凭据与连接配置/);
  assert.match(elements.cmDetail.innerHTML,/cm-model-advanced/);
  assert.match(elements.cmDetail.innerHTML,/id="cmLegacyEditorHost"/);
  assert.match(elements.cmDetail.innerHTML,/id="cmLegacyKeys"/);
  assert.match(elements.cmDetail.innerHTML,/data-cm-managed-edit="shadow-channel"/);
  assert.match(elements.cmDetail.innerHTML,/data-cm-inline-route="legacy:gemini" data-cm-inline-kind="provider_pool"/);
  assert.match(elements.cmDetail.innerHTML,/服务器托管 · 安全迁移/);
  assert.match(elements.cmDetail.innerHTML,/data-cm-server-replace/);
  assert.deepEqual(detailCalls,[['gemini','server_env']]);
  root.listeners.click({target:{closest:()=>({dataset:{cmServerReplace:'image.banana.nb2'}})}});
  assert.equal(newCalls.length,1);
  assert.equal(newCalls[0].adapter,'gemini_image');
  assert.equal(newCalls[0].model,'gemini-3.1-flash-image');
  assert.equal(newCalls[0].base_url,'https://generativelanguage.googleapis.com');
  assert.equal(newCalls[0].enabled,true);
  assert.deepEqual(Array.from(newCalls[0]._replacement.operations),['image.banana.nb2.text']);
  const poolButton={dataset:{cmInlineRoute:'legacy:gemini',cmInlineKind:'provider_pool'},classList:{toggle(){}},setAttribute(){}};
  allowLegacyClose=false;
  root.listeners.click({target:{closest:()=>poolButton}});
  assert.deepEqual(detailCalls,[['gemini','server_env']]);
  allowLegacyClose=true;
  root.listeners.click({target:{closest:()=>poolButton}});
  assert.deepEqual(detailCalls,[['gemini','server_env'],['gemini','provider_pool']]);
  allowLegacyClose=false;
  root.listeners.click({target:{closest:()=>({dataset:{cmManagedEdit:'shadow-channel'}})}});
  assert.deepEqual(editCalls,[]);
  allowLegacyClose=true;
  root.listeners.click({target:{closest:()=>({dataset:{cmManagedEdit:'shadow-channel'}})}});
  assert.deepEqual(editCalls,['shadow-channel']);
  assert.match(source,/data-cm-matrix-page/);
  assert.match(source,/matrixPageMeta/);
  assert.match(source,/matrixPageGroups/);
  assert.match(source,/cm-business-tabs/);
  assert.match(source,/data-cm-matrix-hidden/);
  assert.match(source,/data-cm-inline-route/);
  assert.match(source,/data-cm-managed-edit/);
  assert.match(source,/data-cm-server-replace/);
  assert.match(source,/data-cm-view="layout">调整前台展示/);
  const html=fs.readFileSync(path.join(__dirname,'../site/admin/index.html'),'utf8');
  assert.doesNotMatch(html,/module-subnav[^>]*aria-label="渠道管理页面"/);
  assert.doesNotMatch(html,/data-cm-tab=/);
  assert.doesNotMatch(html,/<button data-cm-view="catalog">渠道与密钥<\/button>/);
  assert.match(html,/<details class="cm-admin-menu">[\s\S]*data-cm-view="catalog">底层渠道库/);
  assert.match(html,/<h3>底层渠道库<\/h3>/);
  assert.match(html,/data-cm-view="health">运行检查/);
  assert.match(html,/data-cm-view="matrix">← 返回模型与渠道/);
});

test('layout loader accepts wrapped and legacy contracts and exposes empty and retry states',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../site/admin/channel-workspace.js'),'utf8');
  const element=id=>({id,hidden:false,innerHTML:'',textContent:'',value:'',checked:false,
    classList:{toggle(){}},addEventListener(){},setAttribute(){},querySelectorAll(){return[]},
    querySelector(selector){
      if(selector==='#cmLayoutSave')return this.saveButton||(this.saveButton={disabled:false});
      if(selector==='#cmLayoutRetry')return this.retryButton||(this.retryButton={});
      return null;
    }});
  const ids=['cmLayout','cmSearch','cmSupplier','cmTransport','cmState','cmHistory','cmDrawer'];
  const elements=Object.fromEntries(ids.map(id=>[id,element(id)])),root=element('root');
  let failure=null,response={
    layout:{video:{order:['grok'],default:'grok'},image:{order:['gpt','lechuang','xiaole'],default:'gpt'}},
    effective_layout:{video:{order:['grok'],default:'grok'},image:{order:['gpt','lechuang','xiaole'],default:'gpt'}},
    entries:{video:[{key:'grok',label:'果肉视频生成',visible:true,defaultable:true,status:'visible',reason:'主站显示'}],image:[
      {key:'gpt',label:'黄雀引擎 2',visible:true,defaultable:true,status:'visible',reason:'主站显示'},
      {key:'lechuang',label:'乐创 · 生图',visible:true,defaultable:true,status:'visible',reason:'主站显示',models:['GPT Image 2','GPT Image 2.5']},
      {key:'xiaole',label:'果肉生图',visible:false,defaultable:false,status:'feature_off',reason:'功能开关未开启'}
    ]}
  };
  const context={window:null,document:{querySelector:()=>root,querySelectorAll:()=>[]}};context.window=context;
  vm.createContext(context);vm.runInContext(source,context);
  const workspace=context.initChannelWorkspace({
    el:id=>elements[id],esc:String,api:async()=>{if(failure)throw failure;return response},legacy:()=>[],closeLegacy(){},
    lifecycle(){},detail(){},mapping(){},refresh(){},task(){},journey(){}
  });
  const settle=()=>new Promise(resolve=>setImmediate(resolve));
  workspace.showTab('layout');
  assert.match(elements.cmLayout.innerHTML,/正在读取前台布局/);
  await settle();
  assert.match(elements.cmLayout.innerHTML,/data-layout-row="video:grok"/);
  assert.match(elements.cmLayout.innerHTML,/乐创 · 生图/);
  assert.match(elements.cmLayout.innerHTML,/GPT Image 2 · GPT Image 2.5/);
  assert.match(elements.cmLayout.innerHTML,/功能开关未开启/);
  assert.match(elements.cmLayout.innerHTML,/当前用户页预览/);

  response={video:{order:['talking'],default:'talking'},image:{order:['banana'],default:'banana'}};
  workspace.showTab('layout');await settle();
  assert.match(elements.cmLayout.innerHTML,/data-layout-row="video:talking"/);

  response={layout:{video:{order:[],default:''},image:{order:[],default:''}}};
  workspace.showTab('layout');await settle();
  assert.equal((elements.cmLayout.innerHTML.match(/cm-layout-empty/g)||[]).length,2);
  assert.equal(elements.cmLayout.saveButton.disabled,true);

  failure=Error('network down');workspace.showTab('layout');await settle();
  assert.match(elements.cmLayout.innerHTML,/cm-layout-error/);
  assert.equal(typeof elements.cmLayout.retryButton.onclick,'function');
  failure=null;response={layout:{video:{order:['minimax'],default:'minimax'},image:{order:['seedream'],default:'seedream'}}};
  elements.cmLayout.retryButton.onclick();
  assert.match(elements.cmLayout.innerHTML,/正在读取前台布局/);
  await settle();
  assert.match(elements.cmLayout.innerHTML,/data-layout-row="video:minimax"/);
});
