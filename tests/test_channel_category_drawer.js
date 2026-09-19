/* 渠道管理「简洁视图」的布局契约：分类抽屉 + 模型横排 + 单模型渠道列表。
   对应验收表：
   - 分类与模型切换：渠道列表准确对应，不残留上个模型
   - 横排滚动：滚轮左右移动；离开横排后页面正常上下滚动
   - 精简入口：编辑 / 发布 / 错误反馈 / 回滚 / 测试 / 移除 仍能到达
*/
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const ROOT=path.join(__dirname,'..');
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

function build(){
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
    detail(){},mapping(){},refresh(){},task(){},journey(){}
  });
  workspace.render(workspaceData());
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

test('简洁视图保留了编辑 / 发布 / 错误反馈 / 回滚 / 测试 / 移除入口',()=>{
  // 这些元素必须真的渲染出来（否则后面「CSS 没隐藏」就没有意义）
  for(const token of ['cm-priority-example','cm-priority-notice','cm-priority-published','cm-priority-history',
    'data-cm-managed-edit','data-cm-priority-save','data-cm-priority-test','data-cm-priority-remove','data-cm-priority-rollback'])
    assert.ok(source.includes(token),'JS 未渲染 '+token);

  // 简洁视图只收起说明性文字与历史块，不得把上面这些操作入口 display:none 掉
  const hiddenRules=[...css.matchAll(/([^{}]+)\{display:none!important\}/g)].map(m=>m[1]).join(',');
  assert.ok(hiddenRules.length,'没有找到 display:none 规则，断言失效');
  for(const token of ['cm-priority-example','cm-priority-notice','cm-priority-published','cm-priority-history',
    'data-cm-managed-edit','data-cm-priority-save','data-cm-priority-test','data-cm-priority-remove','data-cm-priority-rollback'])
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

test('默认界面折叠辅助操作但保留渠道列表和发布状态',()=>{
  const {elements}=build();
  const html=elements.cmMatrix.innerHTML;
  assert.match(html,/<details class="cm-view-tools"><summary>更多<\/summary>/);
  assert.match(html,/<details class="cm-priority-tools"><summary>配置、检测与回滚<\/summary>/);
  assert.ok(html.indexOf('cm-priority-list')<html.indexOf('cm-priority-tools'));
  assert.ok(html.indexOf('cm-priority-published')<html.indexOf('cm-priority-tools'));
  assert.ok(html.indexOf('data-cm-priority-save')>html.indexOf('cm-priority-tools'));
  assert.doesNotMatch(html,/<details class="cm-priority-tools" open/);
});

test('渠道样式版本随内容变化而更新',()=>{
  const crypto=require('node:crypto');
  const index=fs.readFileSync(path.join(__dirname,'../site/admin/index.html'),'utf8');
  const hash=crypto.createHash('md5').update(css.replace(/\r\n/g,'\n')).digest('hex').slice(0,8);
  assert.ok(index.includes('/admin/channel-simple.css?v='+hash));
});
