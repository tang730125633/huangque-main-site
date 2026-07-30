'use strict';
const assert = require('assert');
const path = require('path');
const fs = require('fs');

const root = path.resolve(__dirname, '..');
const agent = require(path.join(root, 'site', 'workbench', 'canvas', 'canvas-agent.js'));

const snapshot = agent.createSnapshot({
  projectId: 'local:board_1', scope: 'local', selectedNodeIds: ['n1'],
  nodes: [
    {id: 'n1', type: 'text', title: '卖点', content: '轻薄'},
    {id: 'n2', type: 'gen', title: '作图', content: ''},
  ],
  edges: [{from_node_id: 'n1', to_node_id: 'n2'}],
});
assert.deepStrictEqual(snapshot.nodes.map((node) => node.id), ['n1']);
assert.deepStrictEqual(snapshot.edges, []);
assert.match(snapshot.snapshot_digest, /^[a-f0-9]{8}$/);

const all = agent.createSnapshot({
  projectId: 'local:board_1', scope: 'local', selectedNodeIds: [],
  nodes: [
    {id: 'n1', type: 'text', title: '卖点', content: '轻薄'},
    {id: 'n2', type: 'gen', title: '作图', content: ''},
  ],
  edges: [{from_node_id: 'n1', to_node_id: 'n2'}],
});
const plan = {
  project_id: all.project_id, snapshot_digest: all.snapshot_digest, selected_node_ids: [],
  actions: [{id: 'a1', type: 'connect_nodes', from_node_id: 'n1', to_node_id: 'n2'}],
};
assert.strictEqual(agent.validatePlan(all, plan), true);
assert.deepStrictEqual(agent.connectionPorts('text', 'gen'), {from: 'prompt', to: 'prompt'});
assert.throws(() => agent.validatePlan({...all, snapshot_digest: 'deadbeef'}, plan), /画布已发生变化/);

const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
assert.ok(html.includes('data-side="agent"'));
assert.ok(html.indexOf('canvas/canvas-agent.js?v=') < html.indexOf('canvas/canvas-app.js?v='));
assert.ok(app.includes("'/api/gen/canvas-agent/quote'"));
assert.ok(app.includes("'/api/gen/canvas_agent'"));
assert.ok(app.includes("'Idempotency-Key':idempotencyKey"));
assert.ok(app.includes('确认应用所选操作'));

console.log('canvas agent tests passed');
