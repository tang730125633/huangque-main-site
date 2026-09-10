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
