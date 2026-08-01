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

const ip12 = agent.buildIP12Context({
  id: 'ip12_1', title: '美业主理人', status: 'confirmed', foundation_stage: {status: 'confirmed'},
  state: {questionnaire_state: {
    profile: {'1': {title: '定位诊断', summary: '经营七年的问题肌管理主理人'}},
    answers: {'0-0': {text: '唐姐', confirmed: true}, '0-1': {text: '未确认内容', confirmed: false}},
  }},
  confirmed_profile: {title: '问题肌管理主理人', one_liner: '不制造焦虑，讲清长期改善。'},
  confirmed_plans: {image_plan: {goal: '建立可信头像'}, next_steps: ['准备首条内容']},
});
assert.strictEqual(ip12.project_id, 'ip12_1');
assert.strictEqual(ip12.foundation_status, 'confirmed');
assert.ok(ip12.facts.some((fact) => fact.value.includes('经营七年')));
assert.ok(ip12.facts.every((fact) => !fact.value.includes('未确认内容')));
assert.strictEqual(agent.buildIP12Context(null), null);

const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
assert.ok(html.includes('data-side="agent"'));
assert.ok(html.includes('id="ncFsAgent"'));
assert.ok(html.includes('data-agent-start='));
assert.ok(html.indexOf('canvas/canvas-agent.js?v=') < html.indexOf('canvas/canvas-app.js?v='));
assert.ok(app.includes("'/api/gen/canvas-agent/quote'"));
assert.ok(app.includes("'/api/gen/canvas_agent'"));
assert.ok(app.includes("'/api/gen/digital-ip/projects'"));
assert.ok(app.includes("'hq_ip12_product_handoff_v1'"));
assert.ok(app.includes('data-agent-guide'));
assert.ok(app.includes("'Idempotency-Key':idempotencyKey"));
assert.ok(app.includes('确认应用所选操作'));
assert.ok(app.includes("openSidePanel('agent',true)"));
assert.ok(app.includes("canvasShell.classList.toggle('agent-open'"));
assert.ok(app.includes("session.draft='';"));

console.log('canvas agent tests passed');
