// 线路 URL／Key 编辑弹窗的隔离测试：验证/发布/回滚的交互契约。
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');

const SOURCE=fs.readFileSync(path.join(__dirname,'..','site','admin','channel-provider-config.js'),'utf8');

function stubEl(){
  const o={innerHTML:'',textContent:'',dataset:{},open:false,disabled:false,
    listeners:{},_q:{},_form:null,
    querySelector(sel){if(sel==='form')return o._form;return o._q[sel]||(o._q[sel]=stubEl())},
    querySelectorAll(){return []},
    addEventListener(t,f){o.listeners[t]=f},
    showModal(){o.open=true},close(){o.open=false},
    appendChild(){},removeChild(){},setAttribute(){},getAttribute(){return null},focus(){}};
  o.parentNode={removeChild(){}};
  return o;
}

function formStub(){
  return {elements:{url:{value:'',oninput:null},secret:{value:'',oninput:null}},
    dataset:{},querySelectorAll(){return[]},querySelector(){return null}};
}

function harness(items,apiImpl){
  const nodes={};
  const el=id=>nodes[id]||(nodes[id]=stubEl());
  const document={_dlg:null,listeners:{},
    createElement(tag){const e=stubEl();if(tag==='dialog'){e._form=formStub();document._dlg=e}return e},
    body:{appendChild(){}},
    addEventListener(t,f){document.listeners[t]=f}};
  const requests=[];
  const api=async(p,opt)=>{
    if(apiImpl)return apiImpl(p,opt,requests);
    if(p==='/api/admin/provider-config')return {items};
    requests.push({path:p,body:opt&&opt.body?JSON.parse(opt.body):null});
    if(p.endsWith('/draft'))return {seq:7};
    if(p.endsWith('/validate'))return {ok:true,checks:{connection:{ok:true},auth:{ok:true}}};
    return {ok:true};
  };
  const sandbox={window:null,document,console,JSON,Math,Date,String,Number,Array,Object,Error,setTimeout,clearTimeout};
  sandbox.window=sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE,sandbox);
  const mod=sandbox.initChannelProviderConfig({api,esc:String,el,toast(){}});
  return {mod,el,nodes,document,requests};
}

const ITEMS=[
  {target_id:'image.seedream',provider:'seedance',features:['图片生成 → 黄雀引擎 1（Seedream）'],
   url:'https://ark.cn-beijing.volces.com/api/v3',url_default:'https://ark.cn-beijing.volces.com/api/v3',
   key_present:true,key_last4:'2222',source:'backend',version:3,editable:true,pool_shared:true,
   effective:{state:'publishing',label:'正在生效',not_loaded_instances:['svc-2']}},
  {target_id:'xiaolevideo',provider:'xiaolevideo',features:['图片生成 → 果肉生图（已下架）'],
   url:'https://api.xiaolevideo.cn',key_present:false,key_last4:'',source:'env',version:null,
   editable:false,deprecated:true,deprecated_reason:'该生图 API 已下架',pool_shared:false,
   effective:{state:'env',label:'使用环境变量'}}
];

test('模型详情入口可先加载再打开；接口不可用时绝不开放编辑',async()=>{
  const h=harness(ITEMS);
  await h.mod.open('image.seedream');
  assert.ok(h.document._dlg&&h.document._dlg.open);
  const blocked=harness([{...ITEMS[0],available:false,reason:'数据库不可用'}]);
  await blocked.mod.open('image.seedream');
  assert.equal(blocked.document._dlg,null);
  assert.doesNotMatch(blocked.el('cmProviderConfig').innerHTML,/data-pc-edit/);
  assert.match(blocked.el('cmProviderConfig').innerHTML,/数据库不可用/);
});

test('页面初始化调用模块的 load 方法，不把控制器当函数',()=>{
  const html=fs.readFileSync(path.join(__dirname,'..','site','admin','index.html'),'utf8');
  assert.match(html,/if\(loadProviderConfig\)loadProviderConfig\.load\(\)/);
  assert.doesNotMatch(html,/if\(loadProviderConfig\)loadProviderConfig\(\)/);
});

test('异步验证期间输入锁定，过期验证响应不得恢复发布按钮',async()=>{
  let resolveValidation;
  const validation=new Promise(resolve=>{resolveValidation=resolve});
  const h=harness(ITEMS,async(p,opt)=>{
    if(p==='/api/admin/provider-config')return {items:ITEMS};
    if(p.endsWith('/draft'))return {seq:8};
    if(p.endsWith('/validate'))return validation;
    throw Error('不应发布');
  });
  await h.mod.open('image.seedream');
  const dlg=h.document._dlg,form=dlg.querySelector('form');
  const pending=dlg.querySelector('[data-pc-validate]').onclick();
  await Promise.resolve();await Promise.resolve();
  assert.equal(form.elements.url.disabled,true);
  form.elements.url.value='https://changed.invalid';form.elements.url.oninput();
  resolveValidation({ok:true,checks:{}});await pending;
  assert.equal(form.dataset.ok,'');
  assert.equal(dlg.querySelector('[data-pc-publish]').disabled,true);
  assert.equal(form.elements.url.disabled,false);
});

test('列表：显示来源/版本/生效状态，pool_shared 提示，已下架线路不可修改',async()=>{
  const h=harness(ITEMS);
  await h.mod.load();
  const html=h.el('cmProviderConfig').innerHTML;
  assert.match(html,/后台配置 v3/);
  assert.match(html,/正在生效/);
  assert.match(html,/未刷新实例 1 个/);
  assert.match(html,/该环境变量同时被号池使用/);
  assert.match(html,/data-pc-edit="image.seedream"/);
  assert.match(html,/不可修改/);
  assert.match(html,/该生图 API 已下架/);
  assert.doesNotMatch(html,/data-pc-edit="xiaolevideo"/);
});

test('弹窗：URL 可编辑、Key 留空保留、列出影响范围，且默认禁止启用',async()=>{
  const h=harness(ITEMS);
  await h.mod.load();
  h.mod.render();
  h.document.listeners.click({target:{closest:()=>({getAttribute:()=>'image.seedream'})}});
  const dlg=h.document._dlg;
  assert.ok(dlg&&dlg.open,'弹窗应打开');
  assert.match(dlg.innerHTML,/API URL/);
  assert.match(dlg.innerHTML,/留空表示保留当前值/);
  assert.match(dlg.innerHTML,/影响范围/);
  assert.match(dlg.innerHTML,/图片生成 → 黄雀引擎 1/);
  assert.match(dlg.innerHTML,/data-pc-publish disabled/,'未验证前不得允许启用');
  assert.match(dlg.innerHTML,/data-pc-rollback/);
});

test('验证通过后可启用；验证后修改输入立即失效',async()=>{
  const h=harness(ITEMS);
  await h.mod.load();
  h.document.listeners.click({target:{closest:()=>({getAttribute:()=>'image.seedream'})}});
  const dlg=h.document._dlg,form=dlg._form;
  assert.ok(form&&form.elements,'表单应存在');  form.elements.url.value='https://ark.cn-beijing.volces.com/api/v3';
  form.elements.secret.value='';
  await dlg._q['[data-pc-validate]'].onclick();
  assert.equal(dlg._q['[data-pc-publish]'].disabled,false,'验证通过后应可启用');
  assert.match(dlg._q['#pcResult'].textContent,/连接:通过/);
  // 再改输入 → 旧验证失效
  form.elements.url.value='https://ark.cn-beijing.volces.com/api/v4';
  form.elements.url.oninput();
  assert.equal(dlg._q['[data-pc-publish]'].disabled,true,'改输入后应重新验证');
  assert.match(dlg._q['#pcResult'].textContent,/请重新验证/);
  await dlg._q['[data-pc-publish]'].onclick();
  assert.match(dlg.querySelector('[role=alert]').textContent,/请先验证通过/);
});

test('发布与回滚都带 expected_version 与唯一 op_id',async()=>{
  const h=harness(ITEMS);
  await h.mod.load();
  h.document.listeners.click({target:{closest:()=>({getAttribute:()=>'image.seedream'})}});
  const dlg=h.document._dlg,form=dlg._form;
  form.elements.url.value='https://ark.cn-beijing.volces.com/api/v3';
  form.elements.secret.value='sk-new';
  await dlg._q['[data-pc-validate]'].onclick();
  await dlg._q['[data-pc-publish]'].onclick();
  const draft=h.requests.find(r=>r.path.endsWith('/draft'));
  const validate=h.requests.find(r=>r.path.endsWith('/validate'));
  const publish=h.requests.find(r=>r.path.endsWith('/publish'));
  assert.equal(draft.body.target_id,'image.seedream');
  assert.equal(draft.body.secret,'sk-new');
  assert.equal(validate.body.version,7);
  assert.equal(publish.body.version,7);
  assert.equal(publish.body.expected_version,3,'必须带上当前发布版本防并发覆盖');
  assert.match(String(publish.body.op_id),/^pc-/, '必须带幂等操作 ID');
});

test('验证失败不得允许启用',async()=>{
  const h=harness(ITEMS,(p,opt,reqs)=>{
    if(p==='/api/admin/provider-config')return {items:ITEMS};
    reqs.push({path:p,body:opt&&opt.body?JSON.parse(opt.body):null});
    if(p.endsWith('/draft'))return {seq:9};
    if(p.endsWith('/validate'))return {ok:false,checks:{connection:{ok:true},auth:{ok:false,note:'凭据被拒绝'}}};
    return {ok:true};
  });
  await h.mod.load();
  h.document.listeners.click({target:{closest:()=>({getAttribute:()=>'image.seedream'})}});
  const dlg=h.document._dlg;
  await dlg._q['[data-pc-validate]'].onclick();
  assert.equal(dlg._q['[data-pc-publish]'].disabled,true);
  assert.match(dlg._q['#pcResult'].textContent,/鉴权:未通过/);
});
