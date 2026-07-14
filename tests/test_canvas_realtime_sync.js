const assert = require('node:assert/strict');
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

{
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
  const left = sync.makeNodeId('client-a', 7);
  const right = sync.makeNodeId('client-b', 7);
  assert.notEqual(left, right);
  assert.match(left, /^n_clienta_7$/);
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
  const canvasHtml = fs.readFileSync(path.join(__dirname, '..', 'site', 'workbench', 'canvas.html'), 'utf8');
  assert.match(canvasHtml, /canvas-collab-sync\.js\?v=[a-f0-9]{8}/);
  assert.match(canvasHtml, /function startCollabSync\(/);
  assert.match(canvasHtml, /function stopCollabSync\(/);
  assert.match(canvasHtml, /function pollCollabOps\(/);
  assert.match(canvasHtml, /function captureCollabFocus\(/);
  assert.match(canvasHtml, /function restoreCollabFocus\(/);
  assert.match(canvasHtml, /\/sync\?since=/);
  assert.match(canvasHtml, /\/presence'/);
  assert.match(canvasHtml, /makeNodeId\(collabClientId/);
  assert.match(canvasHtml, /id="ncOnlineState"/);
  assert.match(canvasHtml, /currentBoardScope==='collab'\?'已同步':'已保存'/);
  assert.match(canvasHtml, /currentBoardScope==='collab'\?'同步失败':'保存失败'/);
  const inlineScripts = [...canvasHtml.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
    .map((match) => match[1])
    .filter((source) => source.trim());
  inlineScripts.forEach((source) => assert.doesNotThrow(() => new Function(source)));
}

console.log('canvas realtime sync helpers: pass');
