// 前端必须按功能身份（operation_id）工作，不能只用 front 查找，
// 否则同 front 的两个功能会串用彼此的配置。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const src=fs.readFileSync(require('node:path').join(__dirname,'../site/workbench/channel-parameters.js'),'utf8');
const body=src.slice(src.indexOf('  const keyOf='), src.indexOf('  async function identity('));
assert.ok(body.length>1000,'取不到片段');

function harness(items,opts){
  const store={};
  const ctx={items,current:null,currentKey:'',owner:'u1',kind:'image',pending:null,controls:null,
    host:opts.host,console:console,sessionStorage:{getItem:k=>store[k]||null,
      setItem:(k,v)=>{store[k]=String(v)},removeItem:k=>{delete store[k]}},
    Object:Object,JSON:JSON,String:String,Array:Array,document:{},esc:v=>String(v??'')};
  ctx._store=store;
  const vm=require('node:vm');vm.createContext(ctx);
  vm.runInContext(body+';this.h={keyOf,isCompat,autoSelectable,invalidKey,markInvalid,clearInvalid};',ctx);
  return ctx;
}

const E=(operation_id,front,points,extra)=>Object.assign({operation_id:operation_id,front:front,
  combinations:[{id:'std',points:points}],default:'std',revision:operation_id+':1'},extra||{});
const A=E('image.banana.nb2.text','nb2',12);
const B=E('image.banana.nb2.reference','nb2',20);
const C=E('image.banana.pro.text','pro',30);
const COMPAT=Object.assign(E(null,'nb2',99),{legacy_compat:true,operation_id:undefined});

test('同 front 的两个功能身份不同，不会互相顶替',()=>{
  const ctx=harness([A,B],{host:{}});
  assert.notEqual(ctx.h.keyOf(A),ctx.h.keyOf(B));
  assert.equal(ctx.h.keyOf(A),'image.banana.nb2.text');
});

test('兼容条目不参与自动选择',()=>{
  const ctx=harness([COMPAT,A],{host:{}});
  assert.equal(ctx.h.isCompat(COMPAT),true);
  const usable=ctx.h.autoSelectable();
  assert.equal(usable.length,1);
  assert.equal(ctx.h.keyOf(usable[0]),'image.banana.nb2.text');
});

test('失效状态持久：刷新后仍不自动选第一项',()=>{
  const ctx=harness([A,B],{host:{}});
  ctx.h.markInvalid('image.banana.nb2.text');
  assert.equal(ctx.h.invalidKey(),'image.banana.nb2.text');
  // 用户明确重选后才清掉
  ctx.h.clearInvalid();
  assert.equal(ctx.h.invalidKey(),'');
});

test('提交请求字段来自识别条件，且不覆写参考图数量与蒙版',()=>{
  const src2=src;
  assert.match(src2,/const match=current\.match\|\|\{\}/);
  assert.match(src2,/if\(k==='kind'\|\|k==='reference_count'\|\|k==='mask_present'\)return;/);
});
const vm=require('node:vm');
function redirectHarness(){
  const prompt={value:''};
  const text={operation_id:'image.banana.nb2.text',match:{kind:'image',source_page:'banana',provider:'banana',model:'nb2',reference_count:0}};
  const ref={operation_id:'image.banana.nb2.reference',match:{...text.match,reference_count:'>0'}};
  const ctx={kind:'image',sourceDraft:null,managedActive:false,showLegacy:false,current:null,currentKey:'',controls:null,
    window:{},host:{querySelector:()=>prompt,scrollIntoView(){}},autoSelectable:()=>[text,ref],
    keyOf:e=>e.operation_id,clearInvalid(){},render(){ctx.renderedKey=ctx.currentKey;},note(){}};
  vm.createContext(ctx);
  vm.runInContext(src.slice(src.indexOf('  function matchesInput('),src.indexOf('  (async()=>')),ctx);
  return {ctx,prompt};
}
test('原生参考图入口选中参考图功能并保留提示词和素材',()=>{
  const {ctx,prompt}=redirectHarness();
  assert.equal(ctx.window.PublishedChannelParameters.redirect('image',{
    source_page:'banana',provider:'banana',model:'nb2',prompt:'保留我的提示词',reference_images:['data:image/png;base64,AA==']}),true);
  assert.equal(ctx.currentKey,'image.banana.nb2.reference');
  assert.equal(ctx.renderedKey,ctx.currentKey);
  assert.equal(prompt.value,'保留我的提示词');
  assert.equal(ctx.sourceDraft.reference_images.length,1);
});
test('模型简称不会模糊匹配到其他功能',()=>{
  const {ctx}=redirectHarness();
  assert.equal(ctx.window.PublishedChannelParameters.redirect('image','nb2'),false);
  assert.equal(ctx.current,null);
});
