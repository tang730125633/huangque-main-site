const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const sync = require('../site/workbench/canvas-collab-sync.js');

function snap(nodes, edges) {
  return { nid: nodes.length, nodes, edges: edges || [], zoom: 1, scroll: { left: 0, top: 0 } };
}

function node(id, extra) {
  return Object.assign({ id, type: 'text', x: 10, y: 20, params: {}, outputs: {} }, extra || {});
}

function edge(from, to) {
  return { from: { node: from, port: 'prompt' }, to: { node: to, port: 'prompt' } };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
}

function testBasicDiff() {
  const base = snap([node('n1')]);
  const next = snap([node('n1', { x: 45 }), node('n2')]);
  const ops = sync.diffSnapshots(base, next);
  assert.deepEqual(ops.map((op) => op.type), ['node.create', 'node.patch']);
  assert.equal(ops[0].node.id, 'n2');
  assert.deepEqual(ops[1], { type: 'node.patch', id: 'n1', fields: { x: 45 } });
  assert.deepEqual(sync.applyOps(base, ops).nodes, next.nodes);
  assert.equal(base.nodes.length, 1, 'applyOps must not mutate the base snapshot');
}

{
  const base = snap([node('n1'), node('n2')], [edge('n1', 'n2')]);
  const ops = sync.diffSnapshots(base, snap([node('n2')], []));
  assert.ok(ops.some((op) => op.type === 'node.delete' && op.id === 'n1'));
  assert.ok(ops.some((op) => op.type === 'edge.delete'));
  const result = sync.applyOps(base, ops);
  assert.deepEqual(result.nodes.map((item) => item.id), ['n2']);
  assert.deepEqual(result.edges, []);
}

{
  const base = snap([node('n1'), node('n2')]);
  const link = edge('n1', 'n2');
  const ops = sync.diffSnapshots(base, snap(base.nodes, [link]));
  assert.equal(ops.length, 1);
  assert.equal(ops[0].type, 'edge.create');
  assert.equal(ops[0].id, 'n1:prompt->n2:prompt');
  assert.deepEqual(sync.applyOps(base, ops).edges, [link]);
}

{
  const result = sync.applyOps(snap([]), [
    { type: 'node.patch', id: 'deleted', fields: { x: 99 } },
  ]);
  assert.deepEqual(result.nodes, [], 'a stale patch must not recreate a deleted node');
}

{
  const base = snap([node('n1', { x: 10, y: 20 })]);
  const first = sync.applyOps(base, [{ type: 'node.patch', id: 'n1', fields: { x: 30 } }]);
  const merged = sync.applyOps(first, [{ type: 'node.patch', id: 'n1', fields: { y: 50 } }]);
  assert.equal(merged.nodes[0].x, 30);
  assert.equal(merged.nodes[0].y, 50);
}

{
  const base = snap([node('n1', {
    params: { title: 'old', text: 'keep', nested: { id: 'old', a: 1, b: 2 } },
    outputs: { image: 'old.png', video: 'keep.mp4' },
  })]);
  const next = snap([node('n1', {
    params: { title: 'new', text: 'keep', nested: { id: 'new', a: 1 } },
    outputs: { image: 'new.png', video: 'keep.mp4' },
  })]);
  const ops = sync.diffSnapshots(base, next);
  assert.deepEqual(ops[0].fields, {
    params: { title: 'new', nested: { id: 'new', b: null } },
    outputs: { image: 'new.png' },
  });
  assert.deepEqual(sync.applyOps(base, ops), next);
  assert.equal(sync.applyOps(base, [{ type: 'node.patch', id: 'n1', fields: { params: { title: 'remote' } } }]).nodes[0].params.text, 'keep');
}

{
  const withoutImage = snap([node('n1')]);
  const withNullImage = snap([node('n1', { image: null })]);
  assert.deepEqual(sync.diffSnapshots(withoutImage, withNullImage), [], 'missing and null are the same deleted value');
  const deleted = sync.applyOps(
    snap([node('n1', { image: 'old.png' })]),
    [{ type: 'node.patch', id: 'n1', fields: { image: null } }]
  );
  assert.deepEqual(sync.diffSnapshots(deleted, withNullImage), [], 'an acknowledged deletion must not be sent again');
}

{
  const left = sync.makeNodeId('client-a', 7);
  const right = sync.makeNodeId('client-b', 7);
  assert.notEqual(left, right);
  assert.match(left, /^n_clienta_7$/);
  assert.notEqual(
    sync.makeNodeId('node-same-time-random-a', 1),
    sync.makeNodeId('node-same-time-random-b', 1),
    'long per-page seeds must retain random entropy',
  );
}

{
  const base = snap([node('n1', { x: 10, y: 20 })]);
  const current = snap([node('n1', { x: 10, y: 55 })]);
  const merged = sync.mergeRemote(base, current, [
    { type: 'node.patch', id: 'n1', fields: { x: 80 } },
  ]);
  assert.equal(merged.base.nodes[0].x, 80);
  assert.equal(merged.current.nodes[0].x, 80);
  assert.equal(merged.current.nodes[0].y, 55);
}

{
  const batches = [
    { client_id: 'self', ops: [{ type: 'node.create', node: node('own') }] },
    { client_id: 'peer', ops: [{ type: 'node.create', node: node('remote') }] },
  ];
  assert.deepEqual(sync.remoteOps(batches, 'self').map((op) => op.node.id), ['remote']);
  assert.equal(sync.pollDelay(false), 800);
  assert.equal(sync.pollDelay(true), 3000);
  assert.deepEqual([0, 1, 2, 3, 4].map(sync.retryDelay), [1000, 2000, 4000, 8000, 8000]);
}

{
  const batch = sync.makeBatch('client-a', 4, [{ type: 'node.delete', id: 'n1' }], () => 'fixed');
  assert.deepEqual(batch, {
    op_id: 'client-a-fixed',
    client_id: 'client-a',
    base_version: 4,
    ops: [{ type: 'node.delete', id: 'n1' }],
  });
}

{
  assert.equal(sync.canEditCanvas('local', ''), true);
  assert.equal(sync.canEditCanvas('collab', 'owner'), true);
  assert.equal(sync.canEditCanvas('collab', 'editor'), true);
  assert.equal(sync.canEditCanvas('collab', 'viewer'), false);
  assert.equal(sync.normalizeNodeTitle('  New\n title  ', 'Text'), 'New title');
  assert.equal(sync.normalizeNodeTitle('Text', 'Text'), '');
}

async function testControllerRetriesAndOrdersBatches() {
  const saves = [];
  let retry = null;
  let current = snap([node('n1')]);
  let nextId = 0;
  const controller = sync.createController({
    clientId: 'client-a',
    transport: {
      save(boardId, batch) {
        const request = deferred();
        saves.push({ boardId, batch, request });
        return request.promise;
      },
      sync() {
        throw new Error('poll is not expected while a save is active');
      },
    },
    getSnapshot: () => current,
    onSnapshot: (next) => { current = next; },
    idFactory: () => `batch-${++nextId}`,
    scheduleRetry(fn, delay) {
      retry = { fn, delay };
      return retry;
    },
    cancelRetry(handle) {
      if (retry === handle) retry = null;
    },
  });

  controller.start({ boardId: 'board-a', version: 1, role: 'editor', baseSnapshot: current });
  current = snap([node('n1', { x: 30 })]);
  controller.save(current);
  assert.equal(saves.length, 1);
  assert.equal(saves[0].boardId, 'board-a');
  assert.equal(controller.getState().saving, true);

  saves[0].request.reject(new Error('offline'));
  await flushPromises();
  assert.equal(retry.delay, 1000);

  current = snap([node('n1', { x: 30, y: 60 })]);
  controller.save(current);
  assert.equal(controller.getState().pending, true);
  assert.equal(saves.length, 1, 'new edits wait behind the failed batch');

  retry.fn();
  assert.equal(saves.length, 2);
  assert.equal(saves[1].batch.op_id, saves[0].batch.op_id, 'retry must reuse op_id');
  assert.deepEqual(saves[1].batch, saves[0].batch, 'retry must reuse the exact batch');

  saves[1].request.resolve({ ok: true, version: 2 });
  await flushPromises();
  assert.equal(retry.delay, 2000, 'a non-authoritative success must retry the same batch');
  retry.fn();
  assert.equal(saves.length, 3);
  assert.deepEqual(saves[2].batch, saves[0].batch);

  saves[2].request.resolve({
    version: 2,
    board: { id: 'board-a', version: 2, role: 'editor', data: snap([node('n1', { x: 30 })]) },
  });
  await flushPromises();
  assert.equal(saves.length, 4, 'the queued edit is sent only after authoritative success');
  assert.notEqual(saves[3].batch.op_id, saves[2].batch.op_id);
  assert.deepEqual(saves[3].batch.ops, [
    { type: 'node.patch', id: 'n1', fields: { y: 60 } },
  ]);

  saves[3].request.resolve({
    version: 3,
    board: { id: 'board-a', version: 3, role: 'editor', data: current },
  });
  await flushPromises();
  assert.deepEqual(controller.getState(), {
    active: true,
    boardId: 'board-a',
    generation: 1,
    pending: false,
    polling: false,
    saving: false,
    version: 3,
  });
}

async function testControllerInvalidatesOldCallbacks() {
  const saves = [];
  const retries = [];
  const snapshots = [];
  let current = snap([node('a')]);
  const controller = sync.createController({
    clientId: 'client-a',
    transport: {
      save(boardId, batch) {
        const request = deferred();
        saves.push({ boardId, batch, request });
        return request.promise;
      },
      sync() { return Promise.resolve({ version: 1, batches: [] }); },
    },
    getSnapshot: () => current,
    onSnapshot(next) { snapshots.push(next); current = next; },
    scheduleRetry(fn) {
      const handle = { fn, cancelled: false };
      retries.push(handle);
      return handle;
    },
    cancelRetry(handle) { handle.cancelled = true; },
    idFactory: () => String(saves.length + 1),
  });

  controller.start({ boardId: 'board-a', version: 1, role: 'editor', baseSnapshot: current });
  current = snap([node('a', { x: 40 })]);
  controller.save(current);
  saves[0].request.reject(new Error('offline'));
  await flushPromises();
  assert.equal(retries.length, 1);

  current = snap([node('b')]);
  controller.start({ boardId: 'board-b', version: 7, role: 'editor', baseSnapshot: current });
  assert.equal(retries[0].cancelled, true);
  retries[0].fn();
  assert.equal(saves.length, 1, 'stale retry callback must be inert');
  assert.equal(controller.getState().saving, false);
  assert.equal(controller.getState().pending, false);

  current = snap([node('b', { x: 70 })]);
  controller.save(current);
  controller.stop();
  saves[1].request.resolve({
    version: 8,
    board: { id: 'board-b', version: 8, role: 'editor', data: current },
  });
  await flushPromises();
  assert.equal(snapshots.length, 0, 'stale success must not apply a snapshot');
  assert.equal(controller.getState().active, false);
  assert.equal(controller.getState().saving, false);
  assert.equal(controller.getState().pending, false);

  current = snap([node('c')]);
  controller.start({ boardId: 'board-c', version: 1, role: 'editor', baseSnapshot: current });
  current = snap([node('c', { x: 80 })]);
  controller.save(current);
  controller.start({ boardId: 'board-d', version: 1, role: 'editor', baseSnapshot: snap([node('d')]) });
  saves[2].request.reject(new Error('late failure'));
  await flushPromises();
  assert.equal(retries.length, 1, 'stale failure must not schedule another retry');
}

async function testControllerResetAndViewerMerge() {
  const polls = [];
  let current = snap([node('n1', { x: 90, y: 20 })]);
  const controller = sync.createController({
    clientId: 'viewer-client',
    transport: {
      save() { throw new Error('viewer must not save'); },
      sync(boardId, since) {
        const request = deferred();
        polls.push({ boardId, since, request });
        return request.promise;
      },
    },
    getSnapshot: () => current,
    onSnapshot: (next) => { current = next; },
  });
  const base = snap([node('n1', { x: 10, y: 20 })]);

  controller.start({ boardId: 'board-view', version: 4, role: 'viewer', baseSnapshot: base });
  controller.save(current);
  assert.equal(controller.getState().saving, false);
  controller.poll();
  assert.deepEqual({ boardId: polls[0].boardId, since: polls[0].since }, { boardId: 'board-view', since: 4 });
  polls[0].request.resolve({
    reset: true,
    version: 9,
    board: { id: 'board-view', version: 9, role: 'viewer', data: snap([node('n1', { x: 50, y: 20 })]) },
  });
  await flushPromises();
  assert.equal(current.nodes[0].x, 50, 'viewer reset must not replay local x=90');
  assert.equal(controller.getState().version, 9);

  current = snap([node('n1', { x: 90, y: 20 })]);
  controller.poll();
  polls[1].request.resolve({
    version: 10,
    batches: [{
      client_id: 'peer',
      ops: [{ type: 'node.patch', id: 'n1', fields: { y: 70 } }],
    }],
  });
  await flushPromises();
  assert.deepEqual({ x: current.nodes[0].x, y: current.nodes[0].y }, { x: 50, y: 70 });
}

async function testControllerSerializesPollBeforeSave() {
  const order = [];
  const pollRequest = deferred();
  const saveRequest = deferred();
  let current = snap([node('n1', { x: 0, y: 0 })]);
  let sentBatch = null;
  const controller = sync.createController({
    clientId: 'client-a',
    transport: {
      sync() { order.push('sync'); return pollRequest.promise; },
      save(boardId, batch) { order.push('save'); sentBatch = batch; return saveRequest.promise; },
    },
    getSnapshot: () => current,
    onSnapshot: (next) => { current = next; },
    idFactory: () => 'serialized',
  });

  controller.start({ boardId: 'board-a', version: 1, role: 'editor', baseSnapshot: current });
  controller.poll();
  current = snap([node('n1', { x: 0, y: 45 })]);
  controller.save(current);
  assert.deepEqual(order, ['sync']);
  assert.equal(controller.getState().pending, true);

  pollRequest.resolve({
    version: 2,
    role: 'editor',
    batches: [{ client_id: 'peer', ops: [{ type: 'node.patch', id: 'n1', fields: { x: 80 } }] }],
  });
  await flushPromises();
  assert.deepEqual(order, ['sync', 'save']);
  assert.deepEqual(sentBatch.ops, [{ type: 'node.patch', id: 'n1', fields: { y: 45 } }], 'queued save must not write the old remote x value back');
  saveRequest.resolve({
    version: 3,
    board: { id: 'board-a', version: 3, role: 'editor', data: current },
  });
  await flushPromises();
}

async function testControllerStopsPermanentClientErrors() {
  const saves = [];
  const retries = [];
  let current = snap([node('n1')]);
  const controller = sync.createController({
    clientId: 'client-a',
    transport: {
      save(boardId, batch) { const request = deferred(); saves.push({ boardId, batch, request }); return request.promise; },
      sync() { return Promise.resolve({ version: 1, role: 'viewer', batches: [] }); },
    },
    getSnapshot: () => current,
    scheduleRetry(fn) { retries.push(fn); return fn; },
    cancelRetry() {},
  });
  controller.start({ boardId: 'board-a', version: 1, role: 'editor', baseSnapshot: current });
  current = snap([node('n1', { x: 20 })]);
  controller.save(current);
  const forbidden = new Error('forbidden');
  forbidden.status = 403;
  saves[0].request.reject(forbidden);
  await flushPromises();
  assert.equal(retries.length, 0);
  assert.equal(controller.getState().saving, false);
  assert.equal(controller.getState().pending, false);
}

async function testControllerDropsQueuedEditsAfterRoleDowngrade() {
  const pollRequest = deferred();
  let saveCalls = 0;
  let current = snap([node('n1', { x: 0 })]);
  const controller = sync.createController({
    clientId: 'client-a',
    transport: {
      sync() { return pollRequest.promise; },
      save() { saveCalls += 1; return Promise.reject(new Error('must not save')); },
    },
    getSnapshot: () => current,
    onSnapshot: (next) => { current = next; },
  });
  controller.start({ boardId: 'board-a', version: 1, role: 'editor', baseSnapshot: current });
  controller.poll();
  current = snap([node('n1', { x: 25 })]);
  controller.save(current);
  pollRequest.resolve({ version: 1, role: 'viewer', batches: [] });
  await flushPromises();
  assert.equal(saveCalls, 0);
  assert.equal(controller.getState().pending, false);
  assert.equal(current.nodes[0].x, 0, 'viewer downgrade discards unsent local edits');
}

function testCanvasIntegration() {
  const canvasHtml = fs.readFileSync(path.join(__dirname, '..', 'site', 'workbench', 'canvas.html'), 'utf8');
  const moduleSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'workbench', 'canvas-collab-sync.js'), 'utf8').replace(/\r\n/g, '\n');
  const moduleStamp = crypto.createHash('md5').update(moduleSource).digest('hex').slice(0, 8);
  const referencedStamp = canvasHtml.match(/canvas-collab-sync\.js\?v=([a-f0-9]{8})/);
  assert.ok(referencedStamp);
  assert.equal(referencedStamp[1], moduleStamp, 'collab module cache stamp must be LF MD5');
  assert.match(canvasHtml, /canvas-collab-sync\.js\?v=[a-f0-9]{8}/);
  assert.match(canvasHtml, /function startCollabSync\(/);
  assert.match(canvasHtml, /function stopCollabSync\(/);
  assert.match(canvasHtml, /function pollCollabOps\(/);
  assert.match(canvasHtml, /function captureCollabFocus\(/);
  assert.match(canvasHtml, /function restoreCollabFocus\(/);
  assert.match(canvasHtml, /function flushActiveCollabTitle\(/);
  assert.match(canvasHtml, /range\.toString\(\)\.length/);
  assert.match(canvasHtml, /beginInlineRename\(node,true\)/);
  assert.match(canvasHtml, /collabSyncGeneration/);
  assert.match(canvasHtml, /collabPendingBatch/);
  assert.match(canvasHtml, /function canEditCanvas\(/);
  assert.ok((canvasHtml.match(/if\(!canEditCanvas\(\)\) return/g) || []).length >= 10);
  assert.match(canvasHtml, /function cleanupLocalSpace\(\)\{\s*if\(!canEditCanvas\(\)\) return;/);
  assert.match(canvasHtml, /function finish\(\)\{\s*if\(!canEditCanvas\(\)\)/);
  assert.match(canvasHtml, /\/sync\?since=/);
  assert.match(canvasHtml, /\/presence'/);
  assert.match(canvasHtml, /collabNodeSeed='node'/);
  assert.ok((canvasHtml.match(/makeNodeId\(collabNodeSeed/g) || []).length >= 3);
  assert.match(canvasHtml, /id="ncOnlineState"/);
  assert.match(canvasHtml, /currentBoardScope==='collab'\?'已同步':'已保存'/);
  assert.match(canvasHtml, /currentBoardScope==='collab'\?'同步失败':'保存失败'/);
  const inlineScripts = [...canvasHtml.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
    .map((match) => match[1])
    .filter((source) => source.trim());
  inlineScripts.forEach((source) => assert.doesNotThrow(() => new Function(source)));
}

Promise.resolve()
  .then(testBasicDiff)
  .then(testControllerRetriesAndOrdersBatches)
  .then(testControllerInvalidatesOldCallbacks)
  .then(testControllerResetAndViewerMerge)
  .then(testControllerSerializesPollBeforeSave)
  .then(testControllerStopsPermanentClientErrors)
  .then(testControllerDropsQueuedEditsAfterRoleDowngrade)
  .then(testCanvasIntegration)
  .then(() => console.log('canvas realtime sync helpers: pass'))
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
