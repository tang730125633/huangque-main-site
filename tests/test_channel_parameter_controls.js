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
  assert.match(source,/if\(!layoutApplied\)applyWorkbenchLayout\(d\.layout\)/);
});
test('image panel exposes a direct entry to the mask inpainting engine',()=>{
  const source=fs.readFileSync(path.join(__dirname,'../site/workbench/channel-parameters.js'),'utf8');
  assert.match(source,/kind==='image'\?'<button id="cpInpaintEntry">涂抹局部修图（黄雀引擎 2）<\/button>':''/);
  assert.match(source,/window\.HQBananaWorkbench\?\.selectEngine\?\.\('gpt'\)/);
});
