const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const context={};vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../site/admin/channel-parameters.js'),'utf8'),context);
const C=context.ChannelParameterEditorLogic;
test('rebuild preserves prices, IDs and default for unchanged combinations',()=>{
  const old=[{id:'option-1',values:{size:'square',quality:'high'},points:35},{id:'option-4',values:{quality:'medium',size:'square'},points:21}];
  const next=C.rebuild({quality:['medium','high'],size:['square','wide']},old,'option-4',50);
  assert.equal(next.retained,2);assert.equal(next.added,2);assert.equal(next.removed,0);assert.equal(next.default,'option-4');
  assert.equal(next.rows.find(r=>r.id==='option-4').points,21);assert.equal(next.rows.find(r=>r.id==='option-1').points,35);
  assert.equal(new Set(next.rows.map(r=>r.id)).size,4);assert.equal(next.rows.find(r=>r.values.size==='wide').points,50);
});
test('removed default moves to an existing valid combination; invalid JPEG omitted',()=>{
  const next=C.rebuild({background:['transparent'],output_format:['png','jpeg']},[{id:'old',values:{background:'opaque',output_format:'jpeg'},points:10}],'old',20);
  assert.equal(next.rows.length,1);assert.equal(next.removed,1);assert.equal(next.rows[0].values.output_format,'png');assert.equal(next.default,next.rows[0].id);
});
test('empty, impossible and excessive selections are rejected',()=>{
  assert.throws(()=>C.rebuild({size:[]},[],null,10),/至少/);
  assert.throws(()=>C.rebuild({background:['transparent'],output_format:['jpeg']},[],null,10),/没有有效/);
  assert.throws(()=>C.rebuild({size:Array.from({length:129},(_,i)=>i)},[],null,10),/128/);
});
test('fixed field conflicts and invalid prices prevent saving',()=>{
  const spec={fields:[{key:'size',label:'尺寸',visible:false}],combinations:[{id:'a',values:{size:'square'},points:0},{id:'b',values:{size:'wide'},points:5.5}],default:'gone',reference_min:2,reference_max:1};
  const errors=C.issues(spec);assert.equal(errors.length,4);assert.ok(errors.some(x=>x.includes('固定值')));assert.ok(errors.some(x=>x.includes('整数')));
  spec.fields[0].visible=true;spec.combinations.forEach(r=>r.points=20);spec.default='a';spec.reference_max=2;assert.equal(C.issues(spec).length,0);
});
