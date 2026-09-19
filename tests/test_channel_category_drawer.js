/* 渠道管理「简洁视图」的布局契约：分类抽屉 + 模型横排 + 单模型渠道列表。
   对应验收表：
   - 分类与模型切换：渠道列表准确对应，不残留上个模型
   - 横排滚动：滚轮左右移动；离开横排后页面正常上下滚动
   - 精简入口：编辑 / 发布 / 错误反馈 / 回滚 / 测试 / 移除 仍能到达
*/
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ROOT=path.join(__dirname,'..');
test('功能未开放时，原厂及托管主线路都不能宣称正在生产',()=>{
  for(const control of ['shadow','managed']){
    const data=workspaceData(),model=data.frontend_matrix.pages[0].products[0].models[0];
    model.admitted=false;model.routes[0].control_state=control;
    data.operation_mappings[0].state=control;
    const html=build({},data).elements.cmMatrix.innerHTML;
    assert.match(html,/已配置主线路/);
    assert.doesNotMatch(html,/当前生产主渠道/);
    assert.match(html,/功能未开放或就绪状态待核对/);
  }
});
test('影子候选不能冒充主渠道，生产原厂线路排在最前',()=>{
  const data=workspaceData(),route=data.frontend_matrix.pages[0].products[0].models[0].routes[0];
  route.control_state='shadow';route.primary={id:'legacy:gemini',name:'真实原厂线路',supplier:'Google',model:'gemini-3.1-flash-image',management:{kind:'server_env',uid:'legacy:gemini'}};
  data.operation_mappings[0].state='shadow';
  const {elements}=build({},data),html=elements.cmMatrix.innerHTML;
  assert.ok(html.indexOf('真实原厂线路')<html.indexOf('data-cm-managed-edit="ch-banana"'));
  assert.match(html,/候选 1 · 未接管/);
  assert.match(html,/data-cm-live-detail="legacy:gemini"/);
  const live=html.match(/<div class="cm-priority-channel cm-live-primary"[\s\S]*?<\/div><\/div>/)[0];
  assert.doesNotMatch(live,/draggable|data-cm-priority-channel/);
});

test('纳米香蕉三行：官方在前，乐创候选在后，保留拖动测速且无发布管理栏',()=>{
  const data=workspaceData(),route=data.frontend_matrix.pages[0].products[0].models[0].routes[0];
  route.control_state='shadow';
  route.primary={id:'legacy:gemini',name:'Google Gemini API',supplier:'Google',model:'gemini-3.1-flash-image',management:{kind:'server_env',uid:'legacy:gemini'}};
  data.items.push({id:'xlw-image-2',name:'GPT Image 2 生图（乐创）',supplier:'乐创',adapter:'openai_image',model:'gpt-image-2',enabled:true});
  data.items.push({id:'xlw-image-25',name:'GPT Image 2.5 生图（乐创）',supplier:'乐创',adapter:'openai_image',model:'gpt-image-2.5-flare',enabled:true});
  data.operation_mappings[0]={operation_id:'image.banana.text',state:'shadow',revision:1,channels:['xlw-image-2','xlw-image-25']};
  const html=build({},data).elements.cmMatrix.innerHTML;
  const positions=['Google Gemini API','GPT Image 2 生图（乐创）','GPT Image 2.5 生图（乐创）'].map(name=>html.indexOf(name));
  assert.ok(positions.every(pos=>pos>=0)&&positions[0]<positions[1]&&positions[1]<positions[2]);
  assert.equal((html.match(/class="cm-priority-channel/g)||[]).length,3);
  assert.match(html,/class="cm-priority-drag"/);
  assert.match(html,/检测延迟/);
  assert.doesNotMatch(html,/服务端已发布|cm-priority-published|cm-priority-state|选择兼容渠道/);
});

test('无托管操作的文本音频等仍显示原生产线路，暂停时不假装接单',()=>{
  const data=workspaceData(),model=data.frontend_matrix.pages[0].products[0].models[0];
  data.operations=[];model.routes[0].primary.name='原线路';
  let html=build({},data).elements.cmMatrix.innerHTML;
  assert.match(html,/data-cm-live-primary/);assert.match(html,/原线路/);
  model.routes[0].control_state='paused';
  html=build({},data).elements.cmMatrix.innerHTML;
  assert.match(html,/当前功能已暂停/);assert.doesNotMatch(html,/data-cm-live-primary/);
});

test('已发布托管主渠道不重复插入，新增草稿不冒充已发布候补',()=>{
  const data=workspaceData(),built=build({},data);
  assert.doesNotMatch(built.elements.cmMatrix.innerHTML,/data-cm-live-primary/);
  data.items.push({...data.items[0],id:'draft-new'});
  built.workspace.render(data);
  built.workspace.addCreatedChannel({id:'draft-new'},{operationId:'image.banana.text'});
  assert.match(built.elements.cmMatrix.innerHTML,/候选 2 · 未发布/);
});
function submitHarness(post){
  const manager=fs.readFileSync(path.join(ROOT,'site/admin/channel-manager.js'),'utf8');
  let handler;const button={disabled:false},form={id:'cmForm',dataset:{compact:'true'},querySelector:()=>button};
  const calls={closed:0,added:[],messages:[]};
  const ctx={document:{querySelector:()=>({addEventListener:(_,fn)=>handler=fn})},
    editing:{id:undefined,model:'gpt-image-2',_modelCreate:{operationId:'A'}},
    createUncertain:false,createPending:false,values:()=>({supplier:'mock',base_url:'https://example.com',secret:'mock-only'}),
    compactPayload:(channel,v)=>({...v,model:channel.model}),routeMappings:()=>[],
    mappingChannels:()=>[],confirm:()=>true,post,load:async()=>true,
    el:id=>id==='cmForm'?ctx.currentForm:null,currentForm:form,
    workspace:{close:()=>calls.closed++,addCreatedChannel:(saved,creation)=>calls.added.push({saved,creation})},
    toast:m=>calls.messages.push(m)};
  vm.createContext(ctx);
  const start=manager.indexOf("    document.querySelector('[data-module=\"managedChannels\"]').addEventListener('submit'");
  vm.runInContext(manager.slice(start,manager.indexOf('    setInterval(',start)),ctx);
  return {ctx,form,button,calls,submit:()=>handler({target:form,preventDefault(){}})};
}

test('延迟保存使用提交时模型，不关闭后来打开的编辑器',async()=>{
  let finish,started;
  const ready=new Promise(r=>started=r);
  const h=submitHarness(()=>{started();return new Promise(r=>finish=r)});
  const pending=h.submit();await ready;
  h.ctx.editing={id:'other',model:'another-model',_modelCreate:{operationId:'B'}};
  h.ctx.currentForm={id:'different-form'};
  finish({id:'new-A'});await pending;
  assert.equal(h.calls.closed,0);
  assert.equal(h.calls.added[0].creation.operationId,'A');
  assert.equal(h.calls.added[0].saved.id,'new-A');
});

test('新增响应丢失或非 JSON 时锁定重试，不重复创建',async()=>{
  for(const error of [new TypeError('Failed to fetch'),Object.assign(new Error('NonJSON'),{nonJson:true}),Object.assign(new Error('timeout'),{timedOut:true})]){
    let count=0;const h=submitHarness(async()=>{count++;throw error});
    await h.submit();await h.submit();
    assert.equal(count,1);assert.equal(h.button.disabled,true);
    assert.equal(h.ctx.createUncertain,true);assert.equal(h.calls.added.length,0);
    assert.ok(h.calls.messages.some(m=>m.includes('结果待核对')));
  }
});

test('跨表单新增在前一请求执行期间也被阻止',async()=>{
  const h=submitHarness(async()=>{throw Error('不应请求')});
  h.ctx.createPending=true;
  await h.submit();
  assert.equal(h.ctx.createPending,true);
  assert.equal(h.calls.added.length,0);
  assert.ok(h.calls.messages.some(m=>m.includes('尚未确认')));
});

test('Gemini 环境变量线路的 NB2 与 Pro 按各自实际模型新增，不复制凭据',async()=>{
  for(const modelName of ['gemini-3.1-flash-image-preview','gemini-3-pro-image-preview']){
    const data=workspaceData(),model=data.frontend_matrix.pages[0].products[0].models[0];
    model.actual_model=modelName;
    model.routes[0].primary.management={kind:'server_env',uid:'legacy:gemini'};
    data.adapters.gemini_image={kind:'image',name:'Gemini'};
    let template;
    const {root}=build({legacy:()=>[{key:'gemini',name:'Google Gemini',category:'生图',configured:true,base_url:'https://secret.example',secret:'NEVER_COPY'}],newChannel:t=>template=t},data);
    await click(root,'[data-cm-model-add]',modelBtn('image','banana','nb2'));
    assert.equal(template.adapter,'gemini_image');
    assert.equal(template.model,modelName);
    assert.equal(template.id,undefined);assert.equal(template.secret,undefined);
    assert.equal(template.base_url,'');
  }
});
test('新增入口绑定当前模型，不继承现有 ID 或密钥，不提交生产映射',async()=>{
  let template;const data=workspaceData();let writes=0;
  const {elements,root,workspace}=build({newChannel:t=>template=t,api:async()=>{writes++;return{}}},data);
  assert.match(elements.cmMatrix.innerHTML,/data-cm-model-add[^>]+>＋新增渠道/);
  assert.match(elements.cmMatrix.innerHTML,/data-cm-managed-edit/);
  await click(root,'[data-cm-model-key]',modelBtn('image','banana','engine2'));
  await click(root,'[data-cm-model-add]',modelBtn('image','banana','engine2'));
  assert.equal(template.model,'gpt-image-2');assert.equal(template.adapter,'openai_image');
  assert.equal(template.id,undefined);assert.equal(template.secret,undefined);
  assert.equal(template.base_url,'');assert.equal(template.daily_test,false);
  assert.equal(template._modelCreate.operationId,'image.engine2.text');
  data.items.push({id:'new',...template});
  workspace.render(data);workspace.addCreatedChannel({id:'new'},template._modelCreate);
  assert.match(elements.cmMatrix.innerHTML,/data-cm-managed-edit="new"/);
  assert.equal(writes,0);
  assert.deepEqual(data.operation_mappings[1].channels,['ch-engine2']);
});

test('无法确认同模型协议时明确拒绝，不借用候选或其他模型',async()=>{
  let called=0,message='';const data=workspaceData();
  const {root}=build({newChannel:()=>called++,toast:m=>message=m},data);
  await click(root,'[data-cm-model-add]',modelBtn('image','banana','nb2'));
  assert.equal(called,0);assert.match(message,/不支持新增兼容供应商/);
});

test('新增必填 Key，编辑仍可留空；新建载荷不携带旧 ID',()=>{
  const manager=fs.readFileSync(path.join(ROOT,'site/admin/channel-manager.js'),'utf8');
  const ctx={window:{}};vm.createContext(ctx);
  vm.runInContext(manager.slice(manager.indexOf('    function compactPayload('),manager.indexOf('    function edit(c=')),ctx);
  const template={_modelCreate:{},id:'old',version:9,adapter:'openai_image',model:'gpt-image-2',enabled:true,monitor:false,daily_test:false};
  assert.throws(()=>ctx.compactPayload(template,{supplier:'测试',base_url:'https://example.com',secret:''}),/必须填写/);
  const payload=ctx.compactPayload(template,{supplier:'测试',base_url:'https://example.com',secret:'dummy-test-key'});
  assert.equal(payload.id,undefined);assert.equal(payload.version,undefined);
  assert.equal(payload.model,'gpt-image-2');assert.equal(payload.name,'测试 · gpt-image-2');
  assert.equal(payload.daily_test,false);assert.equal(payload._modelCreate,undefined);
  assert.match(manager,/f.dataset.saveUnknown='true'/);
  assert.match(manager,/workspace.addCreatedChannel\(saved,creation\)/);
});
const source=fs.readFileSync(path.join(ROOT,'site/admin/channel-workspace.js'),'utf8');
const css=fs.readFileSync(path.join(ROOT,'site/admin/channel-simple.css'),'utf8');
const catalogue={};vm.createContext(catalogue);vm.runInContext(fs.readFileSync(path.join(ROOT,'site/admin/channel-catalog.js'),'utf8'),catalogue);
const C=catalogue.ChannelCatalog;

function element(id){
  return {id,hidden:false,innerHTML:'',textContent:'',value:'',checked:false,open:false,isConnected:true,
    listeners:{},scrollLeft:0,scrollWidth:0,clientWidth:0,
    focus(){},setAttribute(){},addEventListener(type,fn){this.listeners[type]=fn},
    classList:{toggle(){},add(){},remove(){}},querySelector(){return null},querySelectorAll(){return[]}};
}

function route(operationId,channelId,name){
  return {capability:'文生图',control_state:'managed',admitted:true,operation_id:operationId,
    primary:{id:channelId,name,supplier:'供应商',connection_type:'official',base_host:'x.example.com',
      credential_source:'渠道密钥库',configured:true,enabled:true,
      auth:{state:'ok',label:'鉴权通过'},full:{state:'ok',label:'成品验证通过'},
      management:{kind:'managed_channel',uid:'managed:'+channelId}}};
}

const items=[
  {id:'ch-banana',name:'纳米香蕉渠道',supplier:'供应商 A',adapter:'openai_image',model:'gemini-3',base_url:'https://a.example/v1',connection_type:'official',enabled:true,configured:true,health:'成品核验通过'},
  {id:'ch-engine2',name:'引擎 2 渠道',supplier:'供应商 B',adapter:'openai_image',model:'gpt-image-2',base_url:'https://b.example/v1',connection_type:'relay',enabled:true,configured:true,health:'未验证'},
  {id:'ch-video',name:'视频渠道',supplier:'供应商 C',adapter:'xiaole_video',model:'v1',base_url:'https://c.example/v1',connection_type:'relay',enabled:true,configured:true,health:'未验证'}
];
const operations=[
  {operation_id:'image.banana.text',channel_kind:'image',name:'纳米香蕉 文生图',mapping:{operation_id:'image.banana.text',state:'managed',revision:1,channels:['ch-banana'],channel:'ch-banana'}},
  {operation_id:'image.engine2.text',channel_kind:'image',name:'引擎 2 文生图',mapping:{operation_id:'image.engine2.text',state:'managed',revision:1,channels:['ch-engine2'],channel:'ch-engine2'}},
  {operation_id:'video.banana.text',channel_kind:'video',name:'视频 文生视频',mapping:{operation_id:'video.banana.text',state:'managed',revision:1,channels:['ch-video'],channel:'ch-video'}}
];

function workspaceData(){
  return {
    items:items.map(item=>Object.assign({},item)),
    adapters:{openai_image:{kind:'image',name:'图片生成'},xiaole_video:{kind:'video',name:'视频生成'}},
    operations:operations.map(op=>Object.assign({},op)),
    operation_mappings:operations.map(op=>Object.assign({},op.mapping)),
    mappings:[],legacy_controls:{},runs:[],
    frontend_matrix:{pages:[
      {page:'image',label:'生图',summary:{products:1,models:2,admitted_models:2,attention_models:0},products:[
        {key:'banana',label:'纳米香蕉',description:'前台产品',visible:true,admitted:true,models:[
          {key:'nb2',label:'纳米香蕉 2',actual_model:'gemini-3.1-flash-image',capabilities:['文生图'],visible:true,admitted:true,routes:[route('image.banana.text','ch-banana','纳米香蕉渠道')]},
          {key:'engine2',label:'黄雀引擎 2',actual_model:'gpt-image-2',capabilities:['文生图'],visible:true,admitted:true,routes:[route('image.engine2.text','ch-engine2','引擎 2 渠道')]}
        ]}
      ]},
      {page:'video',label:'生视频',summary:{products:1,models:1,admitted_models:1,attention_models:0},products:[
        {key:'clip',label:'视频生成',description:'前台产品',visible:true,admitted:true,models:[
          {key:'v1',label:'视频模型',actual_model:'xiaole-video-1',capabilities:['文生视频'],visible:true,admitted:true,routes:[route('video.banana.text','ch-video','视频渠道')]}
        ]}
      ]}
    ]}
  };
}

function build(overrides={},data=workspaceData()){
  const ids=['cmMatrix','cmSearch','cmSupplier','cmTransport','cmState','cmHistory','cmDrawer','cmDrawerTitle','cmDrawerClose','cmDetail','cmEditor','cmMappingEditor','cmCount','cmCategories','cmList','cmHealth','cmLayout','cmLayoutStatus','cmModelPriority'];
  const elements=Object.fromEntries(ids.map(id=>[id,element(id)]));
  elements.cmDrawer.hidden=true;
  const root=element('root');
  const panels=['matrix','catalog','mapping','health','audit','layout'].map(cmPanel=>({dataset:{cmPanel},hidden:cmPanel!=='matrix'}));
  const context={window:null,ChannelCatalog:C,confirm:()=>true,
    document:{activeElement:element('active'),querySelector:()=>root,
      querySelectorAll:selector=>selector==='[data-cm-panel]'?panels:[],
      body:{classList:{add(){},remove(){}}}}};
  context.window=context;
  vm.createContext(context);vm.runInContext(source,context);
  const workspace=context.initChannelWorkspace({
    el:id=>elements[id],esc:String,toast(){},api:async()=>({}),legacy:()=>[],
    closeLegacy(){return true},editChannel(){},newChannel(){},lifecycle(){},
    detail(){},mapping(){},refresh(){},task(){},journey(){},...overrides
  });
  workspace.render(data);
  return {elements,root,workspace};
}

/* 点击派发：closest 只在指定选择器上返回目标，避免误命中其它分支。
   注意处理器里有一段 `const b=e.target.closest('button')`，分类相关的分支都走它，
   所以 'button' 也要命中（模型 / 配置那两支在此之前已经 return）。 */
function click(root,selector,node){
  return root.listeners.click({target:{closest:sel=>sel===selector||sel==='button'?node:null}});
}
function modelBtn(page,product,model){return {dataset:{cmModelPage:page,cmModelProduct:product,cmModelKey:model}};}

test('分类抽屉列出全部分类，且有且只有当前分类是选中态',()=>{
  const {elements}=build();
  const html=elements.cmMatrix.innerHTML;
  assert.match(html,/class="cm-category-picker"/);
  assert.match(html,/class="cm-category-sheet"/);
  assert.match(html,/data-cm-category-close/);
  for(const label of ['生图','生视频','数字人','音频与配音','文本与助手'])
    assert.ok(html.includes(label),'分类缺少 '+label);
  assert.equal((html.match(/data-cm-matrix-page="image" aria-pressed="true"/g)||[]).length,1);
  // 分类选中态只在抽屉内统计（模型横排也有 aria-pressed，不要混进来）
  const sheet=html.match(/<div class="cm-category-sheet">[\s\S]*?<\/div><\/details>/)[0];
  assert.equal((sheet.match(/aria-pressed="true"/g)||[]).length,1,'分类抽屉里只能有一个分类是选中态');
  assert.equal((sheet.match(/data-cm-matrix-page=/g)||[]).length,8,'抽屉应列出全部 8 个分类');
});

test('模型横排只显示当前分类的模型，切换分类后不残留上一个分类',async()=>{
  const {elements,root}=build();
  const strip=()=>elements.cmMatrix.innerHTML.match(/<nav class="cm-model-strip"[\s\S]*?<\/nav>/)[0];
  assert.match(strip(),/纳米香蕉/);
  assert.match(strip(),/黄雀引擎 2/);
  assert.doesNotMatch(strip(),/视频模型/);

  await click(root,'[data-cm-matrix-page]',{dataset:{cmMatrixPage:'video'}});
  const afterStrip=elements.cmMatrix.innerHTML.match(/<nav class="cm-model-strip"[\s\S]*?<\/nav>/)[0];
  assert.match(afterStrip,/视频模型/);
  assert.doesNotMatch(afterStrip,/纳米香蕉/,'切换分类后不应残留上一个分类的模型');
  assert.doesNotMatch(afterStrip,/黄雀引擎 2/);
});

test('模型横排展示模型名与英文标识，且英文标识来自实际模型',()=>{
  const {elements}=build();
  const strip=elements.cmMatrix.innerHTML.match(/<nav class="cm-model-strip"[\s\S]*?<\/nav>/)[0];
  assert.match(strip,/<strong>[^<]*纳米香蕉 2<\/strong>/);
  assert.match(strip,/<small>gemini-3\.1-flash-image<\/small>/);
  assert.match(strip,/data-cm-model-product="banana"/);
  assert.match(strip,/data-cm-model-key="nb2"/);
});

test('切换模型后渠道列表对应该模型，不残留上一个模型',async()=>{
  const {elements,root,workspace}=build();
  // 默认选中的是首个可见模型（纳米香蕉 2）→ 渠道列表应指向它的 operation
  assert.match(elements.cmMatrix.innerHTML,/data-cm-priority-operation="image.banana.text"/);
  assert.doesNotMatch(elements.cmMatrix.innerHTML,/data-cm-priority-operation="image.engine2.text"/);

  await click(root,'[data-cm-model-key]',modelBtn('image','banana','engine2'));
  const html=elements.cmMatrix.innerHTML;
  assert.match(html,/data-cm-priority-operation="image\.engine2\.text"/);
  assert.doesNotMatch(html,/data-cm-priority-operation="image\.banana\.text"/,'不应残留上一个模型的渠道列表');
  assert.match(html,/引擎 2 渠道/);
  assert.doesNotMatch(html,/纳米香蕉渠道/);
  assert.equal(elements.cmDrawer.hidden,true,'切换模型不应自行打开抽屉');
});

test('模型横排支持滚轮左右滚动，不可滚动时不抢页面纵向滚动',()=>{
  const {root}=build();
  const strip={scrollLeft:0,scrollWidth:900,clientWidth:300};
  const wheel=deltaY=>({target:{closest:sel=>sel==='.cm-model-strip'?strip:null},
    deltaX:0,deltaY,deltaMode:0,ctrlKey:false,prevented:false,preventDefault(){this.prevented=true}});

  const down=wheel(120);
  root.listeners.wheel(down);
  assert.equal(down.prevented,true,'在横排上应拦截滚轮并左右滚动');
  assert.equal(strip.scrollLeft,120);

  strip.scrollLeft=850;
  root.listeners.wheel(wheel(120));
  assert.equal(strip.scrollLeft,600,'应夹在可滚动范围内，不越界');

  const flat={scrollLeft:0,scrollWidth:300,clientWidth:300};
  const over={target:{closest:sel=>sel==='.cm-model-strip'?flat:null},
    deltaX:0,deltaY:120,deltaMode:0,ctrlKey:false,prevented:false,preventDefault(){this.prevented=true}};
  root.listeners.wheel(over);
  assert.equal(over.prevented,false,'没有可滚动的横排时不能拦截，页面要能正常上下滚动');
  assert.equal(flat.scrollLeft,0);

  const outside={target:{closest:()=>null},deltaX:0,deltaY:120,deltaMode:0,ctrlKey:false,
    prevented:false,preventDefault(){this.prevented=true}};
  root.listeners.wheel(outside);
  assert.equal(outside.prevented,false);
});

test('分类抽屉可以关闭',async()=>{
  const {root}=build();
  let open=true;
  const closeBtn={dataset:{cmCategoryClose:''},closest:sel=>sel==='details'?{set open(v){open=v}}:null};
  await click(root,'[data-cm-category-close]',closeBtn);
  assert.equal(open,false);
});

test('简洁视图保留每行编辑检测和发布状态，不隐藏错误反馈',()=>{
  // 这些元素必须真的渲染出来（否则后面「CSS 没隐藏」就没有意义）
  for(const token of ['cm-priority-published','data-cm-managed-edit','data-cm-latency','data-cm-priority-status'])
    assert.ok(source.includes(token),'JS 未渲染 '+token);

  // 简洁视图只收起说明性文字与历史块，不得把上面这些操作入口 display:none 掉
  const hiddenRules=[...css.matchAll(/([^{}]+)\{display:none!important\}/g)].map(m=>m[1]).join(',');
  assert.ok(hiddenRules.length,'没有找到 display:none 规则，断言失效');
  for(const token of ['cm-priority-published','data-cm-managed-edit','data-cm-latency','data-cm-priority-status'])
    assert.ok(!hiddenRules.includes(token),'简洁视图把 '+token+' 隐藏了，排障与恢复入口会不可达');

  // 高级视图按钮必须保留，作为回到完整矩阵的出口
  assert.ok(!hiddenRules.includes('data-cm-simple-toggle'),'高级视图入口被隐藏，完整矩阵将无法到达');
});

test('模型横排不会把模型名截断（CSS 契约）',()=>{
  const rule=css.match(/#cmMatrix \.cm-model-strip>button strong[^{]*\{([^}]*)\}/);
  assert.ok(rule,'缺少模型横排按钮的排版规则');
  assert.match(rule[1],/white-space:nowrap/);
  assert.ok(!/text-overflow:\s*ellipsis/.test(rule[1]),'模型名不应使用省略号截断');
});

test('简洁渠道页保留列表，隐藏发布管理栏与底部配置区域',()=>{
  const {elements}=build();
  const html=elements.cmMatrix.innerHTML;
  assert.match(html,/<details class="cm-view-tools"><summary>更多<\/summary>/);
  assert.match(html,/cm-priority-list/);
  assert.doesNotMatch(html,/cm-priority-published|服务端已发布|cm-priority-state|选择兼容渠道/);
  assert.doesNotMatch(html,/cm-priority-tools|配置、检测与回滚|data-cm-priority-save|data-cm-priority-rollback|data-cm-priority-test/);
});

test('渠道样式版本随内容变化而更新',()=>{
  const crypto=require('node:crypto');
  const index=fs.readFileSync(path.join(__dirname,'../site/admin/index.html'),'utf8');
  const hash=crypto.createHash('md5').update(css.replace(/\r\n/g,'\n')).digest('hex').slice(0,8);
  assert.ok(index.includes('/admin/channel-simple.css?v='+hash));
});

test('精简编辑只更改供应商 URL Key，保留其他生产参数',()=>{
  const manager=fs.readFileSync(path.join(__dirname,'../site/admin/channel-manager.js'),'utf8');
  const ctx={window:{}};vm.createContext(ctx);
  vm.runInContext(manager.slice(manager.indexOf('    function compactPayload('),manager.indexOf('    function edit(c=')),ctx);
  const old={id:'a',version:7,name:'渠道 A',adapter:'openai_image',model:'gpt-image-2',enabled:false,monitor:true,daily_test:true,proxy:'https://proxy.example',timeout:77,concurrency:3,queue_limit:0,rpm:21,poll_seconds:999,daily_hour:0,daily_limit:2,test_cost:1,daily_budget:3,fixture:{prompt:'原提示词',ratio:'9:16',custom:'保留'},secret:'NEVER_COPY',history:[{}]};
  const result=ctx.compactPayload(old,{supplier:'新供应商',base_url:'https://new.example/v1',secret:'',model:'恶意改变',enabled:true});
  for(const key of Object.keys(old).filter(key=>!['secret','history'].includes(key)))assert.deepEqual(result[key],old[key],key);
  assert.equal(result.secret,'');assert.equal(result.supplier,'新供应商');assert.equal(result.base_url,'https://new.example/v1');assert.equal(result.history,undefined);
  const example=ctx.invocationExample({...old,base_url:'https://old.example/v1'});
  assert.match(example,/YOUR_API_KEY/);assert.doesNotMatch(example,/NEVER_COPY/);assert.match(example,/images\/generations/);
  assert.match(ctx.invocationExample({...old,adapter:'minimax_h3',model:'MiniMax-H3'}),/v2\/video_generation/);
  const compactForm=manager.slice(manager.indexOf('      if((c.id||c._modelCreate)&&!validation){'),manager.indexOf("      el('cmEditor').innerHTML='<form id=\"cmForm\" class=\"cm-form\"><h3>"));
  assert.match(compactForm,/供应商名称/);assert.match(compactForm,/Base URL/);assert.match(compactForm,/secretField/);assert.match(compactForm,/调用示例/);
  assert.doesNotMatch(compactForm,/field\('实际模型|data-edit-pane/);
  assert.match(source,/data-cm-channel-history/);
  assert.match(source,/env\.channelHistory\?\./);
  assert.match(manager,/channelHistory:showChannelHistory/);
  const history=manager.slice(manager.indexOf('    function showChannelHistory('),manager.indexOf('    function edit(c='));
  assert.match(history,/data-rollback/);
});
