const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const modalApi=require('../site/workbench/digital-human-error-modal.js');

class ClassList{
  constructor(){this.values=new Set();}
  add(name){this.values.add(name);}
  remove(name){this.values.delete(name);}
  contains(name){return this.values.has(name);}
}

class Element{
  constructor(){this.hidden=false;this.attributes={};this.classList=new ClassList();this.listeners={};this.style={};this.textContent='';this.focusCount=0;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name];}
  addEventListener(name,handler){(this.listeners[name]||(this.listeners[name]=[])).push(handler);}
  click(){for(const handler of this.listeners.click||[])handler({target:this});}
  focus(){this.focusCount++;}
  querySelector(selector){return this.children&&this.children[selector]||null;}
}

class Document{
  constructor(root){this.root=root;this.body=new Element();this.activeElement=new Element();this.listeners={};}
  querySelector(selector){return selector==='#error'?this.root:null;}
  addEventListener(name,handler){(this.listeners[name]||(this.listeners[name]=[])).push(handler);}
  dispatchKey(key){const event={key,prevented:false,preventDefault(){this.prevented=true;}};(this.listeners.keydown||[]).forEach(handler=>handler(event));return event;}
}

function fixture(){
  const root=new Element();
  const message=new Element();
  const close=new Element();
  const backdrop=new Element();
  const dialog=new Element();
  root.children={'#errorMessage':message,'[data-error-close]':close,'[data-error-backdrop]':backdrop,'[role="dialog"]':dialog};
  const doc=new Document(root);
  return {root,message,close,backdrop,dialog,doc};
}

test('show uses one accessible modal, updates message, and locks background scroll',()=>{
  const f=fixture();
  const modal=modalApi.create({document:f.doc,root:f.root});
  const state={input:'文案',idempotencyKey:'same-key'};
  const originalState=JSON.stringify(state);
  modal.show('声音复刻失败，请重新附加样音');
  assert.equal(f.root.hidden,false);
  assert.equal(f.root.getAttribute('aria-hidden'),'false');
  assert.equal(f.root.classList.contains('show'),true);
  assert.equal(f.message.textContent,'声音复刻失败，请重新附加样音');
  assert.equal(f.doc.body.classList.contains('error-modal-open'),true);
  assert.equal(f.doc.body.style.overflow,'hidden');
  assert.equal(f.close.focusCount,1);
  assert.equal(JSON.stringify(state),originalState);
});

test('top-right close hides modal without clearing task state and restores focus',()=>{
  const f=fixture();
  const modal=modalApi.create({document:f.doc,root:f.root});
  const state={photo:'photo.png',voice:'voice.mp3',idempotencyKey:'clone-1'};
  const focusTarget=new Element();
  const before=JSON.stringify(state);
  f.doc.activeElement=focusTarget;
  modal.show('请求失败');
  f.close.click();
  assert.equal(modal.isOpen(),false);
  assert.equal(f.root.hidden,true);
  assert.equal(f.doc.body.classList.contains('error-modal-open'),false);
  assert.equal(f.doc.body.style.overflow,'');
  assert.equal(JSON.stringify(state),before);
  assert.equal(focusTarget.focusCount,1);
});

test('Escape closes modal and prevents default browser handling',()=>{
  const f=fixture();
  const modal=modalApi.create({document:f.doc,root:f.root});
  modal.show('失败');
  const event=f.doc.dispatchKey('Escape');
  assert.equal(event.prevented,true);
  assert.equal(modal.isOpen(),false);
});

test('backdrop closes modal',()=>{
  const f=fixture();
  const modal=modalApi.create({document:f.doc,root:f.root});
  modal.show('失败');
  f.backdrop.click();
  assert.equal(modal.isOpen(),false);
});

test('repeated failures update the same modal instead of stacking',()=>{
  const f=fixture();
  const modal=modalApi.create({document:f.doc,root:f.root});
  modal.show('第一次失败');
  modal.show('第二次失败');
  assert.equal(modal.isOpen(),true);
  assert.equal(modal.getMessage(),'第二次失败');
  assert.equal(f.root.children['[data-error-close]'],f.close);
  assert.equal((f.close.listeners.click||[]).length,1);
  assert.equal((f.doc.listeners.keydown||[]).length,1);
  modal.close();
  assert.equal(f.doc.body.style.overflow,'');
});

test('page wires modal controller and no longer writes bottom error text',()=>{
  const page=fs.readFileSync(require.resolve('../site/workbench/digital-human-oneclick.html'),'utf8');
  assert.match(page,/digital-human-error-modal\.js\?v=1/);
  assert.match(page,/data-error-close/);
  assert.match(page,/data-error-backdrop/);
  assert.match(page,/errorModal\.show\(message\)/);
  assert.doesNotMatch(page,/\$\('error'\)\.textContent=message/);
  assert.doesNotMatch(page,/\.error\{display:none;margin-top:/);
});
