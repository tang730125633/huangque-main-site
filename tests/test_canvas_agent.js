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
assert.ok(app.includes("source_page:'canvas'"));
assert.ok(app.includes('agentModule.submitRequest(apiClient,imageRequest,imageSubmissionKey)'));
assert.ok(app.includes('agentModule.submitRequest(apiClient,videoRequest,videoSubmissionKey)'));
assert.ok(app.includes('确认应用所选操作'));
assert.ok(app.includes("openSidePanel('agent',true)"));
assert.ok(app.includes("canvasShell.classList.toggle('agent-open'"));
assert.ok(app.includes("session.draft='';"));
assert.ok(app.includes("node._imageSubmissionKey||(node._imageSubmissionKey="));
assert.ok(app.includes("node._videoSubmissionKey||(node._videoSubmissionKey="));

(async function(){
  const calls = [];
  const client = {json(endpoint, options){ calls.push({endpoint, options}); return Promise.resolve({job_id: 1}); }};
  for (const [engine, endpoint, marker] of [
    ['nb2', '/api/gen/banana', ['model', 'nb2']],
    ['pro', '/api/gen/banana', ['model', 'pro']],
    ['gpt', '/api/gen/image', ['provider', 'openai']],
    ['zelong', '/api/gen/image', ['provider', 'zelong']],
  ]) {
    const request = agent.imageRequest({engine, prompt: ' 产品主图 ', ratio: '9:16', quality: 'hd', references: ['data:image/png;base64,AAAA']});
    await agent.submitRequest(client, request, 'image-'+engine);
    const call = calls.pop();
    assert.equal(call.endpoint, endpoint);
    assert.equal(call.options.headers['Idempotency-Key'], 'image-'+engine);
    assert.equal(call.options.body.source_page, 'canvas');
    assert.equal(call.options.body[marker[0]], marker[1]);
    assert.equal(call.options.body.image, 'AAAA');
  }
  for (const channel of ['grok', 'micro']) {
    const request = agent.videoRequest({channel, prompt: ' 产品视频 ', duration: '10', ratio: '16:9', references: ['data:image/png;base64,BBBB']});
    await agent.submitRequest(client, request, 'video-'+channel);
    const call = calls.pop();
    assert.equal(call.endpoint, '/api/gen/xiaole_video');
    assert.equal(call.options.headers['Idempotency-Key'], 'video-'+channel);
    assert.deepEqual(call.options.body, {channel, prompt:'产品视频', duration:10, ratio:'16:9', source_page:'canvas', reference_images:['BBBB']});
  }
  for (const failure of [{status:429, code:'active_job_cap'}, {status:0, code:'timeout'}]) {
    const attempted = [];
    const failing = {json(endpoint, options){ attempted.push(options.headers['Idempotency-Key']); return Promise.reject(failure); }};
    const request = agent.imageRequest({engine:'nb2', prompt:'重试保持同一订单'});
    await assert.rejects(agent.submitRequest(failing, request, 'stable-key'));
    await assert.rejects(agent.submitRequest(failing, request, 'stable-key'));
    assert.deepEqual(attempted, ['stable-key', 'stable-key']);
  }
  assert.deepEqual(agent.submissionRetryPolicy({status:429,data:{code:'queue_full',retry_after_ms:5000}}),
    {retryable:true,keepKey:false,code:'queue_full',retryAfterMs:5000});
  assert.deepEqual(agent.submissionRetryPolicy({status:429,data:{code:'active_job_cap',retry_after_ms:4000}}),
    {retryable:true,keepKey:true,code:'active_job_cap',retryAfterMs:4000});
  assert.deepEqual(agent.submissionRetryPolicy({status:409,data:{code:'idempotency_in_progress',retry_after_ms:1000}}),
    {retryable:true,keepKey:true,code:'idempotency_in_progress',retryAfterMs:1000});
  assert.deepEqual(agent.submissionRetryPolicy({status:0,code:'timeout'}),
    {retryable:false,keepKey:true,code:'timeout',retryAfterMs:0});
  console.log('canvas agent tests passed');
})().catch(function(error){ console.error(error); process.exitCode=1; });
