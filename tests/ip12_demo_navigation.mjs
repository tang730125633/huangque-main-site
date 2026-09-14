import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

assert(process.argv[2], 'Pass the generated showcase HTML path');
const html=fs.readFileSync(process.argv[2],'utf8');
const code=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(code=>code.includes('const states='));
assert(code,'Missing presentation script');
const split=code.indexOf('const visualAssets=');
const chapters=vm.runInNewContext(code.slice(0,split)+'\nchapters');
const app=code.slice(split);
const flow=app.slice(app.indexOf('const states='),app.indexOf('function renderTabs'));
function context(){return vm.createContext({chapters,location:{hash:'#results'},matchMedia:()=>({matches:false}),renderMap:()=>{},$:()=>({scrollTo(){}}),history:{replaceState(){}},assert});}
const c=context();
vm.runInContext(flow+`
while(state().selected!=='result-voice')nextStep();
const visibleBefore=[...state().visible];
undo();assert.equal(state().selected,'result-upload-output');
nextStep();assert.equal(state().selected,'result-voice');
assert.deepEqual([...state().visible],visibleBefore);
nextStep();assert.equal(state().selected,'result-voice-keep');
undo();assert.equal(state().selected,'result-voice');
nextStep();assert.equal(state().selected,'result-voice-keep');
`,c);
console.log('PASS: voice -> previous result -> voice -> voice detail; back/forward do not collapse the map.');
for(const chapter of chapters){
 const ctx=context();ctx.location.hash='#'+chapter.id;
 vm.runInContext(flow+`
 assert.equal(state().cursor,0);undo();assert.equal(state().selected,chapter().root.id);
 while(nextCandidate()){
  const previous=state().selected;nextStep();const current=state().selected;
  const visible=[...state().visible];undo();assert.equal(state().selected,previous);
  nextStep();assert.equal(state().selected,current);assert.deepEqual([...state().visible],visible);
 }
 const last=state().selected;switchChapter(chapters.find(c=>c.id!==active).id);switchChapter('${chapter.id}');assert.equal(state().selected,last);
 collapseNode(chapter().root.id);assert.equal(state().visible.size,1);assert.equal(state().selected,chapter().root.id);
 assert(state().trail.every(id=>state().visible.has(id)));resetChapter();assert.equal(state().cursor,0);
 `,ctx);
}
vm.runInContext(`
resetChapter();while(nextCandidate())nextStep();
const last=state().selected;expandNode('result-voice');
assert.equal(state().selected,'result-voice');undo();assert.equal(state().selected,last);
nextStep();assert.equal(state().selected,'result-voice');
undo();expandNode('result-miniapp-feedback');
assert.equal(state().selected,'result-miniapp-feedback');
assert.equal(state().cursor,state().trail.length-1);
nextStep();assert.equal(state().selected,'result-upload');
collapseNode('result-upload');assert(state().trail.every(id=>state().visible.has(id)));
`,c);
console.log('PASS: all 7 chapters, existing nodes, direct selection, forward-history replacement, collapse and reset.');
