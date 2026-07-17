# Canvas Pure Modules PR 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract canvas graph, state/history, storage, API, and export responsibilities into five independently tested browser/Node modules without changing user-visible behavior or persisted/network contracts.

**Architecture:** Each module uses a CommonJS/browser UMD wrapper and attaches only to `window.HQCanvas.<name>`. `canvas-app.js` stays the orchestration entry point, passes plain snapshots and explicit dependencies into the modules, and removes each old implementation immediately after its replacement is integrated.

**Tech Stack:** Static browser JavaScript (ES5-compatible style), Node.js 22 `node:assert`, Python 3.12 `unittest`, existing HTML/cache-stamp tooling, no new dependencies or build step.

## Global Constraints

- Work only on `codex/canvas-pure-modules`, based on merged PR 1 commit `3752e2e` or a later `origin/main` descendant.
- Preserve all visible copy, DOM IDs, keyboard shortcuts, API paths, methods, headers, payloads, snapshot fields, localStorage keys, collaboration protocol, export formats, filenames, quality values, and script execution order.
- Do not extract renderer, pointer/keyboard interaction, runner scheduling, or collaboration orchestration; those belong to PR 3.
- Do not introduce React, Vue, ES modules, npm packages, a bundler, transpilation, or new server code.
- Each module must work through CommonJS in Node and `window.HQCanvas.<name>` in the browser.
- No migrated implementation may remain active in `canvas-app.js`; adapters may capture DOM state but must delegate the extracted decision logic.
- Keep every new module below 800 lines.
- Use TDD: observe the focused test fail before writing the module or changing its caller.
- Use selective `git add` paths; never use `git add -A`.
- Do not push, open PR2, merge, or deploy without explicit user authorization.

## File map

- Create `site/workbench/canvas/canvas-graph.js`: graph cycles, topology, layout, and content bounds.
- Create `site/workbench/canvas/canvas-state.js`: defensive cloning and 60-entry undo/redo history.
- Create `site/workbench/canvas/canvas-storage.js`: existing canvas storage keys, parsing, persistence, and heavy-output cleanup.
- Create `site/workbench/canvas/canvas-api.js`: JSON/asset requests, timeouts, aborts, and structured errors.
- Create `site/workbench/canvas/canvas-export.js`: template interchange helpers and JPG export helpers/orchestration.
- Modify `site/workbench/canvas/canvas-app.js`: consume the five modules and delete migrated implementations.
- Modify `site/workbench/canvas.html`: load each new cache-stamped script before `canvas-app.js`.
- Modify `scripts/stamp_assets.py`: register all five page-scoped scripts as optional assets.
- Modify `tests/test_stamp_assets.py`: require independent current stamps for all five scripts.
- Modify `tests/test_canvas_asset_extraction.py`: require the module loading order before `canvas-app.js`.
- Create `tests/test_canvas_graph.js`, `tests/test_canvas_state.js`, `tests/test_canvas_storage.js`, `tests/test_canvas_api.js`, and `tests/test_canvas_export.js`.
- Modify `tests/test_canvas_realtime_sync.js` only if its source assertions need to include the new modules; do not weaken behavioral assertions.

---

### Task 1: Extract graph algorithms and geometry

**Files:**
- Create: `tests/test_canvas_graph.js`
- Create: `site/workbench/canvas/canvas-graph.js`
- Modify: `site/workbench/canvas/canvas-app.js`
- Modify: `site/workbench/canvas.html`
- Modify: `scripts/stamp_assets.py`
- Modify: `tests/test_stamp_assets.py`

**Interfaces:**
- Consumes: plain node objects `{id,x,y,width?,height?}` and edges `{from:{node},to:{node}}`.
- Produces: `HQCanvas.graph.detectCycle(nodes, edges)`, `topologicalOrder(nodes, edges)`, `computeAutoLayout(nodes, edges, options)`, and `contentBounds(nodes, options)`.

- [ ] **Step 1: Write the failing Node test**

Create `tests/test_canvas_graph.js` with cases that assert:

```js
const assert = require('node:assert/strict');
const graph = require('../site/workbench/canvas/canvas-graph.js');

const nodes = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
const chain = [
  { from: { node: 'a' }, to: { node: 'b' } },
  { from: { node: 'b' }, to: { node: 'c' } },
];
assert.deepEqual(graph.detectCycle(nodes, chain), []);
assert.deepEqual(graph.topologicalOrder(nodes, chain), ['a', 'b', 'c']);
assert.deepEqual(graph.detectCycle(nodes, chain.concat({ from: { node: 'c' }, to: { node: 'a' } })), ['a', 'b', 'c']);
assert.deepEqual(graph.computeAutoLayout(nodes, chain), {
  a: { x: 60, y: 60 }, b: { x: 370, y: 60 }, c: { x: 680, y: 60 },
});
assert.deepEqual(graph.contentBounds([{ id: 'a', x: 100, y: 100, width: 250, height: 160 }]), {
  x: 40, y: 40, w: 370, h: 280,
});
assert.deepEqual(nodes, [{ id: 'a' }, { id: 'b' }, { id: 'c' }], 'inputs must not be mutated');
console.log('canvas graph: pass');
```

- [ ] **Step 2: Run the test and verify RED**

Run: `node tests/test_canvas_graph.js`

Expected: FAIL with `Cannot find module '../site/workbench/canvas/canvas-graph.js'`.

- [ ] **Step 3: Implement the UMD module**

Use this wrapper and preserve input order for deterministic cycle/layout results:

```js
(function(root,factory){
  var api=factory();
  if(typeof module==='object'&&module.exports) module.exports=api;
  if(root){ root.HQCanvas=root.HQCanvas||{}; root.HQCanvas.graph=api; }
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  function index(nodes){ var out={}; (nodes||[]).forEach(function(n){ if(n&&n.id) out[n.id]=n; }); return out; }
  function validEdges(nodes,edges){
    var byId=index(nodes);
    return (edges||[]).filter(function(e){ return e&&e.from&&e.to&&byId[e.from.node]&&byId[e.to.node]; });
  }
  function detectCycle(nodes,edges){
    var byId=index(nodes), usable=validEdges(nodes,edges), visiting={}, visited={}, stack=[], cycle=[];
    function dfs(id){
      if(cycle.length) return true;
      if(visiting[id]){ var at=stack.indexOf(id); cycle=stack.slice(at<0?0:at).concat(id); return true; }
      if(visited[id]) return false;
      visiting[id]=true; stack.push(id);
      usable.filter(function(e){ return e.from.node===id; }).some(function(e){ return dfs(e.to.node); });
      stack.pop(); visiting[id]=false; visited[id]=true;
      return !!cycle.length;
    }
    Object.keys(byId).some(dfs);
    return cycle.filter(function(id,i,list){ return list.indexOf(id)===i; });
  }
  function topologicalOrder(nodes,edges){
    var ids=(nodes||[]).filter(function(n){return n&&n.id;}).map(function(n){return n.id;});
    var usable=validEdges(nodes,edges), indegree={}, outgoing={}, result=[];
    ids.forEach(function(id){ indegree[id]=0; outgoing[id]=[]; });
    usable.forEach(function(e){ indegree[e.to.node]++; outgoing[e.from.node].push(e.to.node); });
    var queue=ids.filter(function(id){return indegree[id]===0;});
    while(queue.length){ var id=queue.shift(); result.push(id); outgoing[id].forEach(function(to){ if(--indegree[to]===0) queue.push(to); }); }
    return result.concat(ids.filter(function(id){return result.indexOf(id)<0;}));
  }
  function computeAutoLayout(nodes,edges,options){
    options=options||{};
    var ids=(nodes||[]).filter(function(n){return n&&n.id;}).map(function(n){return n.id;});
    var usable=validEdges(nodes,edges), level={}, outgoing={}, indegree={}, seen={}, buckets={}, positions={};
    ids.forEach(function(id){ level[id]=0; outgoing[id]=[]; indegree[id]=0; });
    usable.forEach(function(e){ indegree[e.to.node]++; outgoing[e.from.node].push(e.to.node); });
    var queue=ids.filter(function(id){return !indegree[id];});
    while(queue.length){ var id=queue.shift(); seen[id]=true; outgoing[id].forEach(function(to){ level[to]=Math.max(level[to],level[id]+1); if(--indegree[to]===0) queue.push(to); }); }
    ids.forEach(function(id){ if(!seen[id]) level[id]=level[id]||0; (buckets[level[id]]||(buckets[level[id]]=[])).push(id); });
    Object.keys(buckets).sort(function(a,b){return Number(a)-Number(b);}).forEach(function(value){
      buckets[value].forEach(function(id,i){ positions[id]={x:(options.startX||60)+Number(value)*(options.columnGap||310),y:(options.startY||60)+i*(options.rowGap||190)}; });
    });
    return positions;
  }
  function contentBounds(nodes,options){
    options=options||{}; if(!(nodes||[]).length) return null;
    var minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity,pad=options.padding==null?60:options.padding;
    nodes.forEach(function(n){ var w=n.width||250,h=n.height||160; minX=Math.min(minX,n.x||0); minY=Math.min(minY,n.y||0); maxX=Math.max(maxX,(n.x||0)+w); maxY=Math.max(maxY,(n.y||0)+h); });
    return {x:Math.max(0,minX-pad),y:Math.max(0,minY-pad),w:Math.max(360,maxX-minX+pad*2),h:Math.max(240,maxY-minY+pad*2)};
  }
  return {detectCycle:detectCycle,topologicalOrder:topologicalOrder,computeAutoLayout:computeAutoLayout,contentBounds:contentBounds};
});
```

Implement each named function directly in the factory; invalid/missing arrays are treated as empty arrays, and returned objects/arrays are newly allocated.

- [ ] **Step 4: Integrate and delete old graph logic**

In `canvas-app.js`, bind `var graphApi=window.HQCanvas&&window.HQCanvas.graph;`. Replace `autoLayout()` calculations with `graphApi.computeAutoLayout(...)`, apply returned positions to `nodes[id]` and its element, and replace `detectCycle()` with a thin plain-input adapter to `graphApi.detectCycle(...)`. Replace `canvasContentBounds()` with a plain-node adapter to `graphApi.contentBounds(...)`. Delete the old indegree/DFS/bounds calculations.

Add `canvas/canvas-graph.js?v=00000000` before `canvas-app.js` in `canvas.html`, register `Asset("canvas/canvas-graph.js", required=False)`, run `python scripts/stamp_assets.py`, and extend stamp tests for the new path.

- [ ] **Step 5: Verify GREEN and commit**

Run:

```powershell
node tests/test_canvas_graph.js
node --check site/workbench/canvas/canvas-graph.js
node --check site/workbench/canvas/canvas-app.js
node tests/test_canvas_realtime_sync.js
python scripts/stamp_assets.py --check
```

Expected: all exit 0 and `canvas graph: pass` is printed.

Commit only the Task 1 paths with `git commit -m "refactor: extract canvas graph logic"`.

---

### Task 2: Extract snapshot cloning and undo/redo history

**Files:**
- Create: `tests/test_canvas_state.js`
- Create: `site/workbench/canvas/canvas-state.js`
- Modify: `site/workbench/canvas/canvas-app.js`
- Modify: `site/workbench/canvas.html`
- Modify: `scripts/stamp_assets.py`
- Modify: `tests/test_stamp_assets.py`

**Interfaces:**
- Consumes: JSON-serializable snapshots.
- Produces: `cloneSnapshot(value)` and `createHistory({limit})`; history exposes `push`, `undo`, `redo`, `clear`, `canUndo`, and `canRedo`.

- [ ] **Step 1: Write the failing history test**

Create `tests/test_canvas_state.js`:

```js
const assert = require('node:assert/strict');
const state = require('../site/workbench/canvas/canvas-state.js');
const original = { nodes: [{ id: 'a', params: { text: 'one' } }], edges: [] };
const cloned = state.cloneSnapshot(original);
cloned.nodes[0].params.text = 'changed';
assert.equal(original.nodes[0].params.text, 'one');

const history = state.createHistory({ limit: 2 });
history.push({ value: 1 }); history.push({ value: 2 }); history.push({ value: 3 });
assert.equal(history.canUndo(), true);
assert.deepEqual(history.undo({ value: 4 }), { value: 3 });
assert.deepEqual(history.undo({ value: 3 }), { value: 2 });
assert.equal(history.canUndo(), false);
assert.deepEqual(history.redo({ value: 2 }), { value: 3 });
history.push({ value: 9 });
assert.equal(history.canRedo(), false, 'new edits clear redo');
history.clear();
assert.equal(history.canUndo(), false);
console.log('canvas state: pass');
```

- [ ] **Step 2: Run RED, implement, and integrate**

Run `node tests/test_canvas_state.js`; expect missing-module failure.

Implement the same UMD namespace pattern as Task 1, attaching the returned API as `HQCanvas.state`, with this factory body:

```js
function cloneSnapshot(value){ return value==null?value:JSON.parse(JSON.stringify(value)); }
function createHistory(options){
  options=options||{};
  var limit=Math.max(1,Number(options.limit)||60), undoStack=[], redoStack=[];
  function cap(list){ while(list.length>limit) list.shift(); }
  return {
    push:function(snapshot){ if(snapshot==null) return; undoStack.push(cloneSnapshot(snapshot)); cap(undoStack); redoStack=[]; },
    undo:function(current){ if(!undoStack.length) return null; if(current!=null){ redoStack.push(cloneSnapshot(current)); cap(redoStack); } return cloneSnapshot(undoStack.pop()); },
    redo:function(current){ if(!redoStack.length) return null; if(current!=null){ undoStack.push(cloneSnapshot(current)); cap(undoStack); } return cloneSnapshot(redoStack.pop()); },
    clear:function(){ undoStack=[]; redoStack=[]; },
    canUndo:function(){ return undoStack.length>0; },
    canRedo:function(){ return redoStack.length>0; }
  };
}
return {cloneSnapshot:cloneSnapshot,createHistory:createHistory};
```

In `canvas-app.js`, replace `undoStack`/`redoStack` with `var history=stateApi.createHistory({limit:60})`. Update button availability to `history.canUndo()`/`canRedo()`. Replace `pushUndo`, `undo`, `redo`, and every direct stack reset with the module methods; keep `snapshot()` and `restoreSnapshot()` as DOM adapters. Delete `cloneData` only after all callers use `stateApi.cloneSnapshot`.

Add and stamp `canvas-state.js` before `canvas-app.js`.

- [ ] **Step 3: Verify and commit**

Run state test, app/module syntax, realtime sync, board layout, stamp check, and `git diff --check`. Expected: all exit 0.

Commit Task 2 paths with `git commit -m "refactor: extract canvas history state"`.

---

### Task 3: Extract local storage and compatibility handling

**Files:**
- Create: `tests/test_canvas_storage.js`
- Create: `site/workbench/canvas/canvas-storage.js`
- Modify: `site/workbench/canvas/canvas-app.js`
- Modify: `site/workbench/canvas.html`
- Modify: `scripts/stamp_assets.py`
- Modify: `tests/test_stamp_assets.py`

**Interfaces:**
- Consumes: injected Web Storage-compatible object and the four exact existing keys.
- Produces: `createStorage({storage, keys})`, `stripHeavyOutputs(snapshot)`, and structured `{ok,value?,error?}` results where error codes are `storage_unavailable`, `corrupt_json`, or `quota_exceeded`.

- [ ] **Step 1: Write a failing fake-storage test**

The test must define an in-memory object with `getItem`, `setItem`, and `removeItem`; verify exact key names, draft round-trip, invalid JSON returning `corrupt_json`, template/board array defaults, active board round-trip, defensive copies, heavy gen/video output removal, and a throwing `setItem` returning `quota_exceeded` without changing the previous value.

Run `node tests/test_canvas_storage.js`; expect missing-module failure.

- [ ] **Step 2: Implement the storage module**

Use defaults exactly:

```js
var DEFAULT_KEYS={
  draft:'hq_canvas_draft_v2', templates:'hq_canvas_templates_v2',
  boards:'hq_canvas_boards_v1', activeBoard:'hq_canvas_active_id'
};
```

Expose `loadDraft`, `saveDraft`, `removeDraft`, `loadBoards`, `saveBoards`, `loadTemplates`, `saveTemplates`, `loadActiveBoard`, and `saveActiveBoard`. Reads catch access errors separately from JSON parse errors. Writes serialize before calling `setItem`, classify `QuotaExceededError`/code 22/1014 as `quota_exceeded`, and never call a second write after failure.

Use these shared helpers inside the UMD factory and attach as `HQCanvas.storage`:

```js
function result(value){ return {ok:true,value:value}; }
function failure(code,error){ return {ok:false,error:{code:code,message:String(error&&error.message||error||code)}}; }
function quota(error){ return error&&(error.name==='QuotaExceededError'||error.code===22||error.code===1014); }
function stripHeavyOutputs(snapshot){
  var copy=snapshot==null?snapshot:JSON.parse(JSON.stringify(snapshot));
  (copy&&copy.nodes||[]).forEach(function(node){
    if(!node||!node.outputs) return;
    if(node.type==='gen') delete node.outputs.image;
    if(node.type==='video'){ delete node.outputs.video; delete node.outputs.video_url; }
  });
  return copy;
}
function createStorage(options){
  options=options||{}; var storage=options.storage, keys=Object.assign({},DEFAULT_KEYS,options.keys||{});
  function read(key,fallback){
    var raw; try{ raw=storage.getItem(key); }catch(error){ return failure('storage_unavailable',error); }
    if(raw==null||raw==='') return result(fallback);
    try{ return result(JSON.parse(raw)); }catch(error){ return failure('corrupt_json',error); }
  }
  function write(key,value){
    var raw=JSON.stringify(value);
    try{ storage.setItem(key,raw); return result(value); }
    catch(error){ return failure(quota(error)?'quota_exceeded':'storage_unavailable',error); }
  }
  function remove(key){ try{ storage.removeItem(key); return result(null); }catch(error){ return failure('storage_unavailable',error); } }
  return {
    loadDraft:function(){return read(keys.draft,null);}, saveDraft:function(v){return write(keys.draft,v);}, removeDraft:function(){return remove(keys.draft);},
    loadBoards:function(){return read(keys.boards,[]);}, saveBoards:function(v){return write(keys.boards,v);},
    loadTemplates:function(){return read(keys.templates,[]);}, saveTemplates:function(v){return write(keys.templates,v);},
    loadActiveBoard:function(){ try{return result(storage.getItem(keys.activeBoard)||'');}catch(e){return failure('storage_unavailable',e);} },
    saveActiveBoard:function(v){ try{ if(v) storage.setItem(keys.activeBoard,String(v)); else storage.removeItem(keys.activeBoard); return result(v||''); }catch(e){return failure(quota(e)?'quota_exceeded':'storage_unavailable',e);} }
  };
}
return {DEFAULT_KEYS:DEFAULT_KEYS,createStorage:createStorage,stripHeavyOutputs:stripHeavyOutputs};
```

- [ ] **Step 3: Integrate all four keys**

Instantiate once in `canvas-app.js` with `window.localStorage`. Replace direct accesses for draft, boards, templates, and active-board ID. Keep current fallbacks (`null` draft, empty arrays, false save result) in the adapter so visible messages do not change. Route cleanup through `stripHeavyOutputs`; keep `compressImageSource` injected/browser-local as designed. Use `rg "localStorage.*(DRAFT_KEY|TPL_KEY|BOARD_KEY|ACTIVE_BOARD_KEY)"` to prove no direct accesses remain.

Add/stamp the module, run storage tests plus existing canvas tests, and commit with `git commit -m "refactor: extract canvas storage"`.

---

### Task 4: Extract request and error normalization

**Files:**
- Create: `tests/test_canvas_api.js`
- Create: `site/workbench/canvas/canvas-api.js`
- Modify: `site/workbench/canvas/canvas-app.js`
- Modify: `site/workbench/canvas.html`
- Modify: `scripts/stamp_assets.py`
- Modify: `tests/test_stamp_assets.py`

**Interfaces:**
- Consumes: `createClient({fetchImpl, tokenProvider, AbortControllerImpl, setTimeoutImpl, clearTimeoutImpl})`.
- Produces: `client.json(path, options)`, `client.asset(path, options)`, and errors with `status`, `code`, `data`, and safe `message`.

- [ ] **Step 1: Write failing deterministic request tests**

Use fake fetch responses and fake timers to assert: GET defaults; POST JSON headers/body; `credentials:'same-origin'`; `cache:'no-store'`; non-2xx status/data; non-JSON response fallback; timeout abort with code `timeout`; caller abort with code `aborted`; and asset Blob response with Authorization header. Do not use real network or real timers.

Run `node tests/test_canvas_api.js`; expect missing-module failure.

- [ ] **Step 2: Implement and integrate the client**

`json` must preserve the existing default 8000 ms timeout and return parsed JSON. `asset` returns a Blob and does not JSON-decode. Compose the cookie token as `Authorization: Bearer <token>` exactly as today. Normalize failures without showing UI.

Implement the client with this shape, attaching it as `HQCanvas.api`:

```js
function apiError(message,options){
  options=options||{}; var error=new Error(message||'request failed');
  error.status=options.status||0; error.code=options.code||'request_failed'; error.data=options.data||null;
  return error;
}
function createClient(options){
  options=options||{};
  var fetchImpl=options.fetchImpl, tokenProvider=options.tokenProvider||function(){return '';};
  var Controller=options.AbortControllerImpl, later=options.setTimeoutImpl||setTimeout, cancel=options.clearTimeoutImpl||clearTimeout;
  function request(path,requestOptions,wantBlob){
    requestOptions=requestOptions||{};
    var headers=Object.assign({'Accept':'application/json','Authorization':'Bearer '+tokenProvider()},requestOptions.headers||{});
    var body=requestOptions.body, callerSignal=requestOptions.signal, controller=!callerSignal&&Controller?new Controller():null, timer=null;
    if(body!==undefined&&!wantBlob){ headers['Content-Type']='application/json'; body=JSON.stringify(body); }
    if(controller) timer=later(function(){ controller.abort(); },requestOptions.timeout||8000);
    return fetchImpl(path,{method:requestOptions.method||'GET',credentials:'same-origin',cache:'no-store',headers:headers,body:body,signal:requestOptions.signal||(controller&&controller.signal)})
      .then(function(response){
        if(wantBlob){ if(!response.ok) throw apiError('HTTP '+response.status,{status:response.status}); return response.blob(); }
        return response.text().then(function(text){
          var data={}; try{data=text?JSON.parse(text):{};}catch(e){data={detail:text||response.statusText};}
          if(!response.ok) throw apiError(data.detail||('HTTP '+response.status),{status:response.status,code:data.code,data:data});
          return data;
        });
      }).catch(function(error){
        if(error&&error.name==='AbortError') throw apiError('request aborted',{code:callerSignal?'aborted':'timeout'});
        throw error;
      }).finally(function(){ if(timer) cancel(timer); });
  }
  return {json:function(path,opts){return request(path,opts,false);},asset:function(path,opts){return request(path,opts,true);}};
}
return {createClient:createClient,apiError:apiError};
```

Replace `authJson` and `playableAssetUrl` request internals first. Then replace direct generation submit/job polling and account-asset fetch calls with the client while keeping their endpoints, payload construction, polling intervals (3000 ms), and runner state handling in `canvas-app.js`. Confirm with `rg -n "\bfetch\(" site/workbench/canvas/canvas-app.js`; any remaining fetch must be export image loading, which Task 5 removes.

Add/stamp the module, run API and runner/collaboration regressions, then commit with `git commit -m "refactor: extract canvas API client"`.

---

### Task 5: Extract template and JPG export logic

**Files:**
- Create: `tests/test_canvas_export.js`
- Create: `site/workbench/canvas/canvas-export.js`
- Modify: `site/workbench/canvas/canvas-app.js`
- Modify: `site/workbench/canvas.html`
- Modify: `scripts/stamp_assets.py`
- Modify: `tests/test_stamp_assets.py`
- Modify: `tests/test_canvas_asset_extraction.py`

**Interfaces:**
- Consumes: plain snapshots plus injected `createCanvas`, `loadImage`, `createObjectURL`, `revokeObjectURL`, `download`, `now`, and drawing callbacks/data.
- Produces: `serializeTemplate`, `parseTemplate`, `safeFilename`, `wrappedLines`, `nodeImageSource`, and `exportJpeg(options)`.

- [ ] **Step 1: Write failing export tests**

Assert template version 1 round-trip, acceptance of legacy raw snapshots, rejection of missing `nodes`, 40-character imported names, invalid filename-character replacement, wrapped line truncation, image source selection, null image on load failure, and cleanup of object URLs after download. Use fake canvas/image/download dependencies; do not require a browser DOM.

Run `node tests/test_canvas_export.js`; expect missing-module failure.

- [ ] **Step 2: Implement and integrate export**

Move the current template JSON envelope, filename rules, `exportRoundRect`, wrapped-text calculation, image loading, node image selection, drawing helpers, and JPG orchestration into the module. Preserve JPG quality `.92`, maximum dimensions/scaling, light/dark palette, background grid, edge curves, node text, and timestamp filename pattern.

Start the UMD factory with these testable helpers and attach it as `HQCanvas.exporter`:

```js
function clone(value){ return value==null?value:JSON.parse(JSON.stringify(value)); }
function safeFilename(value){ return String(value||'canvas-template').replace(/[\\/:*?"<>|]+/g,'-'); }
function serializeTemplate(item,now){
  item=item||{}; return JSON.stringify({version:1,name:item.name||'画布模板',createdAt:item.createdAt||(now||Date.now)(),data:clone(item.data)},null,2);
}
function parseTemplate(text,fallbackName){
  var parsed=JSON.parse(text), snapshot=parsed&&parsed.data&&parsed.data.nodes?parsed.data:(parsed&&parsed.nodes?parsed:null);
  if(!snapshot||!Array.isArray(snapshot.nodes)) throw new Error('模板格式不正确');
  return {name:String(parsed.name||fallbackName||'导入模板').slice(0,40),data:clone(snapshot)};
}
function wrappedLines(measure,text,maxWidth,maxLines){
  var lines=[],line=''; String(text||'').trim().split(/\r?\n/).forEach(function(part,index,parts){
    Array.from(part).forEach(function(ch){ var next=line+ch; if(line&&measure(next)>maxWidth){lines.push(line);line=ch;}else line=next; });
    if(index<parts.length-1){lines.push(line);line='';}
  });
  if(line) lines.push(line); return lines.slice(0,maxLines);
}
function nodeImageSource(node){
  if(!node) return ''; if(node.type==='image') return node.image||node.outputs&&node.outputs.image||'';
  if(node.type==='gen') return node.outputs&&node.outputs.image||''; return '';
}
```

`exportJpeg(options)` must use only values from `options`: `bounds`, `nodes`, `edges`, `theme`, `portCenter`, `createCanvas`, `loadImage`, `createObjectURL`, `revokeObjectURL`, `download`, and `now`. Copy the existing drawing statements without changing constants; return a Promise that resolves to `{filename,blob}` after `download` and rejects on missing context/blob. Put `revokeObjectURL` in a scheduled cleanup that always runs after a created URL.

Keep UI panel rendering and `updateState` calls in `canvas-app.js`; pass plain nodes with measured `width`, `height`, and `collapsed` fields plus explicit port-center values into `exportJpeg`. Replace FileReader parsing with `parseTemplate` after the file text is read. Delete all migrated helpers and confirm remaining app `fetch` calls are zero.

Add/stamp the module. Extend the HTML loading-order test to assert graph → state → storage → API → export → collab sync → app, using actual script positions rather than one broad regex.

- [ ] **Step 3: Verify and commit**

Run all five new Node tests, all modified JS syntax checks, existing canvas Node tests, extraction/stamp Python tests, and `git diff --check`.

Commit with `git commit -m "refactor: extract canvas export logic"`.

---

### Task 6: Run repository gates and inspect PR2 scope

**Files:**
- Verify only; modify only a concrete extraction regression and add a focused failing test first.

- [ ] **Step 1: Run JavaScript and focused canvas gates**

```powershell
node tests/test_canvas_graph.js
node tests/test_canvas_state.js
node tests/test_canvas_storage.js
node tests/test_canvas_api.js
node tests/test_canvas_export.js
node tests/test_canvas_realtime_sync.js
node tests/test_canvas_board_card_layout.js
node tests/test_cloud_shell_sidebar.js
Get-ChildItem site -Recurse -Filter *.js | ForEach-Object { node --check $_.FullName; if ($LASTEXITCODE -ne 0) { exit 1 } }
```

Expected: every command exits 0.

- [ ] **Step 2: Run Python and repository gates**

```powershell
python -m unittest tests.test_canvas_asset_extraction tests.test_stamp_assets tests.test_auth_canvas_collab -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
python -m compileall -q server scripts worker
git diff --check origin/main...HEAD
```

Expected: all focused tests and checks pass. Run `python -m unittest discover -s tests -v`; record known Windows-only failures without weakening tests, and rely on Ubuntu GitHub Actions for the authoritative full suite.

- [ ] **Step 3: Inspect scope and size**

Run `git diff --name-only origin/main...HEAD`, `git diff --stat origin/main...HEAD`, `git status --short`, and line counts for all five modules. Expected: only the design/plan, five modules/tests, app/HTML, stamp tooling/tests, and necessary existing canvas regression files are present; each module is below 800 lines; the worktree is clean.

---

### Task 7: Rebase, re-verify, and stop for publication approval

**Files:**
- Verify only; conflict resolution must stay inside the approved PR2 file map.

- [ ] **Step 1: Fetch and rebase**

Run `git fetch origin --prune` and `git rebase origin/main`. If conflicts occur, resolve only PR2 files, stage selectively, continue the rebase, and repeat Task 6 completely.

- [ ] **Step 2: Repeat final mandatory gates**

Repeat all five module tests, existing canvas regressions, targeted Python tests, syntax checks, `ci_validate.py`, cache-stamp check, compileall, diff check, and clean-status check. Expected: all applicable focused checks exit 0.

- [ ] **Step 3: Stop and report**

Report branch, commits, changed files, module line counts, local test evidence, and any Windows-only full-suite limitations. Do not push, open a PR, merge, or deploy until the user explicitly authorizes publication.

---

### Task 8: Address PR2 review findings before merge

**Files:**
- Modify: `tests/test_canvas_export.js`
- Modify: `site/workbench/canvas/canvas-export.js`
- Modify: `tests/test_canvas_storage.js`
- Modify: `site/workbench/canvas/canvas-storage.js`

**Interfaces:**
- Preserves: `exportJpeg(options) -> Promise<{filename,blob}>` and schedules download URL cleanup through an injected timer.
- Preserves: storage writes return `{ok:false,error}` for both serialization and browser storage failures.

- [ ] **Step 1: Write and run the failing export cleanup test**

Inject `setTimeoutImpl` into `exportJpeg`, assert that `revokeObjectURL` is not called before the timer runs, and assert the scheduled delay is `1500`. Run `node tests/test_canvas_export.js`; expected failure: the current microtask cleanup revokes immediately and never schedules the timer.

- [ ] **Step 2: Implement delayed download cleanup**

Replace the microtask cleanup with:

```js
var later=options.setTimeoutImpl||setTimeout;
later(function(){options.revokeObjectURL(url);},1500);
```

Run `node tests/test_canvas_export.js`; expected: pass.

- [ ] **Step 3: Write and run the failing storage serialization test**

Save a circular draft, assert the call does not throw, assert `{ok:false,error:{code:'serialization_failed'}}`, and assert `setItem` was not called. Run `node tests/test_canvas_storage.js`; expected failure: `JSON.stringify` throws outside the adapter result contract.

- [ ] **Step 4: Implement serialization failure handling**

Wrap serialization separately before the storage write:

```js
var raw;
try{ raw=JSON.stringify(value); }
catch(error){ return failure('serialization_failed',error); }
```

Keep the existing single `setItem` attempt and quota classification. Run `node tests/test_canvas_storage.js`; expected: pass.

- [ ] **Step 5: Verify, commit, update PR, and merge**

Run all five module tests, the canvas extraction/stamp tests, full Ubuntu `unittest` discovery, and `git diff --check`. Commit only the plan, two modules, and two tests; push `codex/canvas-pure-modules` without force. Fetch current `origin/main`, merge the PR branch into a clean isolated main worktree, repeat the complete Ubuntu suite, then push `main` without force so GitHub records PR2 as merged.
