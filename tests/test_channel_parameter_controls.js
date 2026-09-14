const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const c={};vm.createContext(c);vm.runInContext(fs.readFileSync(path.join(__dirname,'../site/workbench/channel-parameter-controls.js'),'utf8'),c);
const C=c.ChannelParameterControls;
const spec={default:'a',fields:[{key:'size',label:'尺寸',visible:true},{key:'quality',label:'质量',visible:true},{key:'output_format',label:'格式',visible:false}],combinations:[
  {id:'a',values:{size:'1024x1024',quality:'low',output_format:'png'},points:10},
  {id:'b',values:{size:'1536x1024',quality:'low',output_format:'png'},points:20},
  {id:'c',values:{size:'1536x1024',quality:'high',output_format:'png'},points:35}]};
test('dependent options always select a real combination and its exact price',()=>{
  assert.equal(C.choose(spec,spec.combinations[0],'quality','high').id,'c');
  assert.equal(C.choose(spec,spec.combinations[2],'size','1024x1024').points,10);
});
test('changing size preserves other valid choices',()=>{
  assert.equal(C.choose(spec,spec.combinations[0],'size','1536x1024').id,'b');
});
test('unreachable options are disabled instead of silently snapping',()=>{
  const a=spec.combinations[0];               // 1024x1024 / low / png
  assert.deepEqual(Array.from(C.options(spec,a,'quality'),o=>o.value+':'+o.enabled),['low:true','high:false'],
    '高品质只在 1536x1024 有组合 → 1024x1024 下必须不可选');
  assert.deepEqual(Array.from(C.options(spec,a,'size'),o=>o.value+':'+o.enabled),['1024x1024:true','1536x1024:true'],
    '尺寸是最高优先级，契约里有的就都该可选');
  const c=spec.combinations[2];               // 1536x1024 / high / png
  assert.deepEqual(Array.from(C.options(spec,c,'quality'),o=>o.value+':'+o.enabled),['low:true','high:true'],
    '在 1536x1024 下两档都可达');
});
test('render marks disabled options and explains why',()=>{
  const host={innerHTML:''};
  C.mount(host,spec);
  assert.match(host.innerHTML,/<option value="high" disabled title="[^"]+">/,'不可达选项要置灰且带原因');
  assert.match(host.innerHTML,/高品质 · 当前不适用/,'灰掉的选项在文案里也要说清楚');
  assert.doesNotMatch(host.innerHTML,/<option value="low"[^>]*disabled/,'可达的选项不能被误伤');
});
test('every enabled option resolves to a combination that keeps it',()=>{
  let checked=0,disabled=0;
  for(const combo of spec.combinations){
    for(const key of C.fields(spec)){
      for(const o of C.options(spec,combo,key)){
        if(!o.enabled){disabled+=1;continue}
        const next=C.choose(spec,combo,key,o.value);
        assert.ok(next,'可选值必须能解析出组合：'+key+'='+o.value);
        assert.equal(String(next.values[key]),String(o.value),'点了 '+key+'='+o.value+' 就必须是它');
        checked+=1;
      }
    }
  }
  assert.ok(checked>0&&disabled>0,'既要有可选的也要有灰掉的：checked='+checked+' disabled='+disabled);
});
test('render omits fixed fields and reflects current combination in price',()=>{
  const host={innerHTML:''},changes=[];const control=C.mount(host,spec,v=>changes.push(v));
  assert.doesNotMatch(host.innerHTML,/data-cp-field="output_format"/);
  host.onchange({target:{dataset:{cpField:'quality'},value:'high'}});
  assert.equal(control.value().id,'c');assert.match(host.innerHTML,/35 点/);assert.equal(changes.length,2);
});
test('parameter labels and values cannot inject markup',()=>{
  assert.equal(C.esc('<script>"'), '&lt;script&gt;&quot;');
});
test('billing disabled hides point price without changing the authoritative combination',()=>{
  const price={textContent:''},note={textContent:''},host={innerHTML:'',querySelector:s=>s==='.cp-price'?price:note};
  const control=C.mount(host,spec,null,null,{billingEnabled:false});
  assert.match(host.innerHTML,/内测期间免费/);assert.doesNotMatch(host.innerHTML,/本次 \d+ 点/);assert.equal(control.value().points,10);
  host.onchange({target:{dataset:{cpField:'quality'},value:'high'}});assert.equal(control.value().points,35);assert.doesNotMatch(host.innerHTML,/请确认点数/);
  control.setBillingEnabled(true);assert.equal(price.textContent,'本次 35 点 · 1 个产物');
  control.setBillingEnabled(false);assert.match(price.textContent,/内测期间免费/);
});
test('all added browser scripts parse',()=>{
  for(const file of ['site/workbench/channel-parameters.js','site/admin/channel-parameters.js','site/workbench/channel-parameter-controls.js'])new vm.Script(fs.readFileSync(path.join(__dirname,'..',file),'utf8'));
});
test('both workbenches stay visible until a managed channel actually takes over',()=>{
  const source=fs.readFileSync(path.join(__dirname,'../site/workbench/channel-parameters.js'),'utf8');
  // 图片页的托管线路已内嵌为「乐创 · Image 2」引擎卡，视频页提交托管渠道时再接管；
  // 两个页面首屏都不应被平台面板顶掉。
  assert.match(source,/let managedActive=false/);
  assert.doesNotMatch(source,/let managedActive=kind==='image'/);
  assert.match(source,/if\(!managedActive\)\{if\(legacy\)legacy\.hidden=false;host\.hidden=true;host\.innerHTML=''/);
  assert.match(source,/managedActive=true;showLegacy=false;current=found;render\(\)/);
  // 待确认的提交需要重新展示面板
  assert.match(source,/if\(pending\)\{managedActive=true;render\(\)/);
  // 前台布局仍由面板脚本在 load 中应用，与是否接管无关
  assert.match(source,/applyWorkbenchLayout\(d\.layout,d\.layout_entries\)/);
  assert.match(source,/entry\.visible!==false/);
});
test('image panel exposes a direct entry to the mask inpainting engine',()=>{
  const source=fs.readFileSync(path.join(__dirname,'../site/workbench/channel-parameters.js'),'utf8');
  assert.match(source,/kind==='image'\?'<button id="cpInpaintEntry">涂抹局部修图（黄雀引擎 2）<\/button>':''/);
  assert.match(source,/window\.HQBananaWorkbench\?\.selectEngine\?\.\('gpt'\)/);
});

function imageLayoutRuntime(search,response,current='xiaole'){
  const source=fs.readFileSync(path.join(__dirname,'../site/workbench/channel-parameters.js'),'utf8');
  const cards=Object.fromEntries(['gpt','xiaole'].map(key=>[key,{style:{},attrs:{},setAttribute(name,value){this.attrs[name]=value}}]));
  const row={querySelector(selector){const match=selector.match(/data-engine="([^"]+)"/);return match?cards[match[1]]:null},appendChild(){}};
  const legacy={hidden:false,setAttribute(){}};
  const host={dataset:{kind:'image'},hidden:false,className:'',innerHTML:'',querySelector(){return null},scrollIntoView(){}};
  let interval=null,selected=[];
  const context={
    window:null,document:{hidden:false,getElementById:id=>id==='publishedChannelParameters'?host:id==='engineRow'?row:null,querySelector:s=>s==='.banana-workspace'?legacy:null},
    location:{search,href:'https://huangquechuanmei.com/workbench/banana.html'+search},URLSearchParams,URL,
    sessionStorage:{getItem(){return null},setItem(){}},crypto:{randomUUID:()=> 'id'},confirm:()=>true,
    fetch:async url=>({ok:true,status:200,json:async()=>url==='/api/auth/me'?{user:{username:'u'}}:response}),
    setInterval(fn){interval=fn;return 1},setTimeout(){return 1},clearInterval(){},FileReader:function(){},
  };
  context.window=context;context.window.ChannelParameterControls={esc:String,mount(){}};
  context.window.HQBananaWorkbench={getEngine:()=>current,selectEngine(key){current=key;selected.push(key)}};
  context.window.addEventListener=()=>{};
  vm.createContext(context);vm.runInContext(source,context);
  const settle=async()=>{for(let i=0;i<4;i++)await new Promise(resolve=>setImmediate(resolve))};
  return {cards,selected,settle,current:()=>current,refresh:async()=>{interval();await settle()}};
}

test('unavailable image deep link falls back to the effective default',async()=>{
  const runtime=imageLayoutRuntime('?engine=xiaole',{
    items:[],layout:{image:{order:['gpt','xiaole'],default:'gpt'}},layout_entries:{image:[
      {key:'gpt',visible:true,defaultable:true},{key:'xiaole',visible:true,defaultable:false}
    ]}
  });
  await runtime.settle();
  assert.equal(runtime.current(),'gpt');
  assert.deepEqual(runtime.selected,['gpt']);
});

test('catalog refresh moves an active engine away when its feature turns off',async()=>{
  const response={items:[],layout:{image:{order:['gpt','xiaole'],default:'xiaole'}},layout_entries:{image:[
    {key:'gpt',visible:true,defaultable:true},{key:'xiaole',visible:true,defaultable:true}
  ]}};
  const runtime=imageLayoutRuntime('',response);
  await runtime.settle();
  response.layout.image.default='gpt';
  response.layout_entries.image[1]={key:'xiaole',visible:false,defaultable:false};
  await runtime.refresh();
  assert.equal(runtime.cards.xiaole.style.display,'none');
  assert.equal(runtime.cards.xiaole.attrs['aria-hidden'],'true');
  assert.equal(runtime.current(),'gpt');
  assert.deepEqual(runtime.selected,['gpt']);
});
