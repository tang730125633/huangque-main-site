const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const shortDrama = require('../site/workbench/canvas/canvas-short-drama.js');

function testCanvasIntegration() {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
  const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
  const moduleSource = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.js'), 'utf8').replace(/\r\n/g, '\n');
  const css = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.css'), 'utf8').replace(/\r\n/g, '\n');
  const appSource = app.replace(/\r\n/g, '\n');

  assert.ok(html.includes('canvas/canvas-short-drama.css?v='));
  assert.ok(html.includes('canvas/canvas-short-drama.js?v='));
  assert.ok(html.indexOf('canvas/canvas-short-drama.js?v=') < html.indexOf('canvas/canvas-app.js?v='));
  assert.equal((html.match(/data-add="shortDrama"/g) || []).length, 2);
  assert.match(app, /shortDrama:\s*\{name:'短剧项目',\s*color:'#[a-f0-9]+'\}/);
  assert.ok(app.includes('data-f="openShortDrama"'));
  assert.ok(app.includes('shortDramaModule.createWorkspace('));
  assert.match(app, /projectId:projectId,\s*apiClient:apiClient,\s*poll:apiModule\.poll,\s*canEdit:canEdit,\s*onChange:onChange/);
  assert.ok(app.includes('shortDramaModule.creationPayload(node.params)'));
  assert.ok(app.includes('shortDramaModule.createProjectCoordinator('));
  assert.ok(app.includes("function shortDramaScopeKey(scope,boardId)"));
  assert.ok(app.includes("var scopeKey=currentShortDramaScopeKey();"));
  assert.match(app, /getNode:function\(scopeKey,nodeId\)\{[\s\S]*?shortDramaNodeForScope\(scopeKey,nodeId\)/, 'reconciliation is board scoped');
  assert.match(app, /function shortDramaNodeForScope\(scopeKey,nodeId\)\{[\s\S]*?wrap\.classList\.contains\('editing'\)/, 'board home is never treated as an active scope');
  assert.match(app, /shortDramaProjectCoordinator\.ensure\(scopeKey,node\.id,[\s\S]*?node\.params\.project_id\|\|null\)/, 'creation captures the expected project link');
  assert.match(app, /onChange=function\(summary\)\{[\s\S]*?shortDramaNodeForScope\(scopeKey,nodeId\)/, 'workspace changes resolve the current scoped node');
  assert.ok(app.includes("shortDramaProjectCoordinator.cleanupScope(shortDramaScopeKey('local',id));"));
  assert.match(app, /finally\(function\(\)\{[\s\S]*?applyShortDramaOpenPolicy\(scopeKey,nodeId\)/, 'settlement reapplies the current scoped readonly policy');
  assert.match(app, /function shortDramaNodeOutputs\(node\)[\s\S]*?return node&&node\.type==='shortDrama'\?\{\}:/);
  assert.match(app, /outputs:shortDramaNodeOutputs\(n\)/, 'canvas snapshots must sanitize short-drama outputs');
  assert.match(app, /if\(type==='shortDrama'&&data\) data=shortDramaModule\.sanitizeNodeData\(data\)/, 'restore and paste must sanitize short-drama node data');
  assert.match(app, /if\(n\.type==='shortDrama'\)[\s\S]*?n\.outputs=\{\}/, 'template imports must sanitize short-drama outputs');
  assert.equal((app.match(/outputs:shortDramaNodeOutputs\((?:n|node)\)/g) || []).length, 4, 'snapshot, export, and both copy paths sanitize outputs');
  assert.ok(app.includes('snap=sanitizeShortDramaSnapshot(snap);'), 'restore sanitizes before rebuilding nodes');
  assert.ok(app.includes('copy.data=sanitizeShortDramaSnapshot(copy.data);'), 'board duplication sanitizes persisted nodes');
  assert.match(app, /openShortDrama\.disabled=!!readonly&&!\(node&&node\.params\.project_id\)/, 'readonly existing projects remain openable');
  const ensureSource = app.match(/function ensureShortDramaProject\(node,scopeKey\)\{[\s\S]*?\n  \}/)[0];
  assert.doesNotMatch(ensureSource, /scheduleSave\(/, 'coordinator apply is the single save path');

  for (const [asset, source] of [
    ['canvas/canvas-app.js', appSource],
    ['canvas/canvas-short-drama.js', moduleSource],
    ['canvas/canvas-short-drama.css', css],
  ]) {
    const stamp = crypto.createHash('md5').update(source).digest('hex').slice(0, 8);
    assert.ok(html.includes(`${asset}?v=${stamp}`), `${asset} cache stamp must be LF MD5`);
  }
}

function testNodePersistenceHelpers() {
  const dirty = {
    id: 'n7', type: 'shortDrama',
    params: {
      project_id: 'project-7', title: '雨夜来客', ratio: '16:9', target_duration: 45,
      stage: 'script_review', progress: 50, spent_points: 3, estimated_points: 12,
      characters: [{ name: '侦探' }], script: { hook: 'secret' }, shots: [{ key: 's1' }],
    },
    outputs: {
      characters: [{ name: '侦探' }], script: { hook: 'secret' }, shots: [{ key: 's1' }],
      video: 'must-not-persist',
    },
  };
  const clean = shortDrama.sanitizeNodeData(dirty);
  assert.equal(clean.id, 'n7');
  assert.equal(clean.params.project_id, 'project-7');
  assert.deepEqual(Object.keys(clean.params).sort(), [
    'estimated_points', 'progress', 'project_id', 'ratio', 'spent_points', 'stage', 'target_duration', 'title',
  ]);
  assert.deepEqual(clean.outputs, {});
  assert.notStrictEqual(clean, dirty);
  assert.notStrictEqual(clean.params, dirty.params);

  const payload = shortDrama.creationPayload(clean.params);
  assert.deepEqual(payload, {
    title: '雨夜来客', synopsis: '请在短剧工作区完善故事梗概', ratio: '16:9',
    target_duration: 45, shot_count: 6,
  });
  assert.ok(payload.synopsis.length >= 8, 'lazy creation payload must satisfy backend synopsis validation');
  assert.equal(shortDrama.canOpenNode({ project_id: 'project-7' }, false), true);
  assert.equal(shortDrama.canOpenNode({ project_id: null }, false), false);
  assert.equal(shortDrama.canOpenNode({ project_id: null }, true), true);
}

async function testCreateProjectCoordinatorIsBoardScoped() {
  let createCalls = 0;
  let saves = 0;
  let resolveCreate;
  let activeScope = 'local:board-a';
  const boardAOld = { id: 'n1', type: 'shortDrama', params: shortDrama.normalizeNodeParams({ title: 'A 旧节点' }), outputs: {} };
  const boardBNode = { id: 'n1', type: 'shortDrama', params: shortDrama.normalizeNodeParams({ title: 'B 节点' }), outputs: {} };
  const boards = {
    'local:board-a': { n1: boardAOld },
    'local:board-b': { n1: boardBNode },
  };
  const coordinator = shortDrama.createProjectCoordinator({
    getNode(scopeKey, nodeId) { return activeScope === scopeKey ? boards[scopeKey] && boards[scopeKey][nodeId] : null; },
    create(payload) {
      createCalls += 1;
      assert.equal(payload.title, 'A 旧节点');
      return new Promise((resolve) => { resolveCreate = resolve; });
    },
    apply(node, project) {
      node.params = shortDrama.normalizeNodeParams(Object.assign({}, node.params, project));
      node.outputs = {};
      saves += 1;
    },
  });
  const payload = shortDrama.creationPayload(boardAOld.params);
  const first = coordinator.ensure('local:board-a', 'n1', payload, true, null);
  const duplicate = coordinator.ensure('local:board-a', 'n1', payload, true, null);
  assert.strictEqual(duplicate, first, 'same board and node reuse the in-flight request');
  activeScope = 'local:board-b';
  await Promise.resolve();
  assert.equal(createCalls, 1);
  resolveCreate({ id: 'project-a', title: 'A 服务端标题', ratio: '9:16', target_duration: 30, stage: 'draft' });
  assert.equal(await first, 'project-a');
  assert.equal(boardBNode.params.project_id, null, 'same node id on board B is untouched');
  assert.equal(boardAOld.params.project_id, null, 'inactive board A object is not mutated');
  assert.equal(saves, 0);
  assert.equal(coordinator.hasPending('local:board-a', 'n1'), false, 'pending entry is cleaned after settlement');
  assert.equal(coordinator.hasCompleted('local:board-a', 'n1'), true, 'inactive result is retained by board scope');

  const boardARestored = { id: 'n1', type: 'shortDrama', params: shortDrama.normalizeNodeParams({ title: 'A 恢复节点' }), outputs: { shots: ['secret'] } };
  boards['local:board-a'].n1 = boardARestored;
  activeScope = 'local:board-a';
  const consumed = coordinator.ensure('local:board-a', 'n1', shortDrama.creationPayload(boardARestored.params), true, null);
  assert.equal(await consumed, 'project-a');
  assert.equal(createCalls, 1, 'reopening board A consumes the retained result without another POST');
  assert.equal(boardARestored.params.project_id, 'project-a');
  assert.deepEqual(boardARestored.outputs, {});
  assert.equal(saves, 1);
  assert.equal(coordinator.hasCompleted('local:board-a', 'n1'), false, 'completed entry clears after application');

  await assert.rejects(
    coordinator.ensure('local:board-a', 'n8', shortDrama.creationPayload({ title: '只读节点' }), false, null),
    /只读/,
  );
  assert.equal(createCalls, 1, 'id-less readonly node never creates a project');

  const failed = shortDrama.createProjectCoordinator({
    getNode() { return null; },
    create() { return Promise.reject(new Error('create failed')); },
    apply() { throw new Error('apply must not run'); },
  });
  await assert.rejects(failed.ensure('local:board-f', 'n9', shortDrama.creationPayload({ title: '失败节点' }), true, null), /create failed/);
  assert.equal(failed.hasPending('local:board-f', 'n9'), false, 'in-flight entry is also cleaned after rejection');
}

async function testCreateProjectCoordinatorPreservesConflictingLink() {
  let resolveCreate;
  let applyCalls = 0;
  const node = { id: 'n1', type: 'shortDrama', params: shortDrama.normalizeNodeParams({ title: '冲突节点' }), outputs: {} };
  const coordinator = shortDrama.createProjectCoordinator({
    getNode(scopeKey, nodeId) { return scopeKey === 'collab:board-c' && nodeId === 'n1' ? node : null; },
    create() { return new Promise((resolve) => { resolveCreate = resolve; }); },
    apply() { applyCalls += 1; },
  });
  const pending = coordinator.ensure('collab:board-c', 'n1', shortDrama.creationPayload(node.params), true, null);
  await Promise.resolve();
  node.params.project_id = 'project-from-collaboration';
  resolveCreate({ id: 'project-from-post', title: '迟到结果' });
  assert.equal(await pending, 'project-from-collaboration');
  assert.equal(node.params.project_id, 'project-from-collaboration');
  assert.equal(applyCalls, 0, 'late POST never overwrites a different project link');
  assert.equal(coordinator.hasCompleted('collab:board-c', 'n1'), false, 'conflicting retained result is discarded');
}

async function testCreateProjectCoordinatorScopeCleanup() {
  let resolveCreate;
  const coordinator = shortDrama.createProjectCoordinator({
    getNode() { return null; },
    create() { return new Promise((resolve) => { resolveCreate = resolve; }); },
    apply() { throw new Error('deleted scope must never apply'); },
  });
  const pending = coordinator.ensure('local:deleted-board', 'n1', shortDrama.creationPayload({ title: '待删除' }), true, null);
  await Promise.resolve();
  coordinator.cleanupScope('local:deleted-board');
  resolveCreate({ id: 'orphaned-project' });
  assert.equal(await pending, 'orphaned-project');
  assert.equal(coordinator.hasPending('local:deleted-board', 'n1'), false);
  assert.equal(coordinator.hasCompleted('local:deleted-board', 'n1'), false, 'deleted scope does not retain a late result');
}

async function testPureHelpers() {
  const settings = shortDrama.normalizeSettings({
    title: '雨夜来客', synopsis: '陌生女孩敲开侦探的门', ratio: '1:1',
    target_duration: 45, shot_count: 8,
  });
  assert.equal(settings.ratio, '9:16');
  assert.equal(settings.target_duration, 45);
  assert.equal(settings.shot_count, 8);
  assert.equal(shortDrama.normalizeSettings({ shot_count: 7.5 }).shot_count, 6);
  assert.equal(shortDrama.normalizeSettings({ shot_count: 'not-a-number' }).shot_count, 6);
  assert.equal(shortDrama.normalizeSettings({ shot_count: 5 }).shot_count, 6);
  assert.equal(shortDrama.normalizeSettings({ shot_count: 11 }).shot_count, 10);

  assert.deepEqual(shortDrama.planningPayload(settings), {
    format: 'short_drama', prompt: settings.synopsis, dur: '45s', ratio: '9:16',
    shot_count: 8, style: settings.visual_style, platform: settings.target_platform,
  });
  assert.equal(shortDrama.stageIndex('storyboard_review'), 3);
  assert.match(shortDrama.summarizeProject({
    title: '雨夜来客', ratio: '9:16', target_duration: 45, stage: 'script_review',
  }), /雨夜来客/);
}

async function testProjectRoutesAndPlanningFlow() {
  const calls = [];
  const api = {
    json(path, options) {
      calls.push({ path, options });
      if (path === '/api/gen/copy') return Promise.resolve({ job_id: 42 });
      if (path === '/api/gen/job/42') return Promise.resolve({
        status: 'done', result: JSON.stringify({ mode: 'short_drama', plan: { title: '雨夜来客' } }),
      });
      if (path === '/api/gen/short-drama/apply-plan') return Promise.resolve({
        id: 'project-1', revision: 8, spent_points: 3,
      });
      return Promise.resolve({ items: [] });
    },
  };
  function poll(options) {
    assert.equal(options.intervalMs, 3000);
    assert.equal(options.maxMs, 420000);
    return options.request().then((job) => {
      assert.deepEqual(options.inspect(job), {
        done: true,
        value: { mode: 'short_drama', plan: { title: '雨夜来客' } },
      });
      return { mode: 'short_drama', plan: { title: '雨夜来客' } };
    });
  }
  const client = shortDrama.createClient(api, poll);

  await client.list();
  await client.get('project 1');
  await client.create({ title: '雨夜来客' });
  await client.update('project 1', 5, { revision: 99, title: '新标题' });
  await client.applyPlan('project 1', 6, 41);
  await client.confirm('project 1', 7, 'characters_review');
  const applied = await client.generatePlan({
    id: 'project-1', revision: 7, synopsis: '陌生女孩敲开侦探的门', target_duration: 45,
    ratio: '16:9', shot_count: 8, visual_style: '电影写实', target_platform: '抖音',
  });

  assert.deepEqual(applied, { id: 'project-1', revision: 8, spent_points: 3 });
  assert.deepEqual(calls, [
    { path: '/api/gen/short-drama/projects', options: undefined },
    { path: '/api/gen/short-drama/project?id=project%201', options: undefined },
    {
      path: '/api/gen/short-drama/projects',
      options: { method: 'POST', body: { title: '雨夜来客' } },
    },
    {
      path: '/api/gen/short-drama/project?id=project%201',
      options: { method: 'PUT', body: { revision: 5, title: '新标题' } },
    },
    {
      path: '/api/gen/short-drama/apply-plan',
      options: { method: 'POST', body: { project_id: 'project 1', revision: 6, job_id: 41 } },
    },
    {
      path: '/api/gen/short-drama/confirm',
      options: { method: 'POST', body: { project_id: 'project 1', revision: 7, stage: 'characters_review' } },
    },
    {
      path: '/api/gen/copy',
      options: {
        method: 'POST', body: {
          format: 'short_drama', prompt: '陌生女孩敲开侦探的门', dur: '45s', ratio: '16:9',
          shot_count: 8, style: '电影写实', platform: '抖音',
        },
      },
    },
    { path: '/api/gen/job/42', options: undefined },
    {
      path: '/api/gen/short-drama/apply-plan',
      options: { method: 'POST', body: { project_id: 'project-1', revision: 7, job_id: 42 } },
    },
  ]);
}

async function testTerminalJobFailureDoesNotApplyPlan() {
  let applyCalled = false;
  const api = {
    json(path) {
      if (path === '/api/gen/copy') return Promise.resolve({ job_id: 44 });
      if (path === '/api/gen/job/44') return Promise.resolve({
        status: 'failed', error: 'model refused plan', code: 'model_failed',
      });
      applyCalled = true;
      return Promise.resolve({});
    },
  };
  function poll(options) {
    return options.request().then((job) => {
      const outcome = options.inspect(job);
      assert.equal(outcome.error.message, 'model refused plan');
      return Promise.reject(outcome.error);
    });
  }
  await assert.rejects(
    shortDrama.createClient(api, poll).generatePlan({ id: 'project-1', revision: 1, synopsis: '故事梗概' }),
    (error) => error.message === 'model refused plan' && error.code === 'model_failed',
  );
  assert.equal(applyCalled, false);
}

function testMissingPollFailsClearly() {
  assert.throws(
    () => shortDrama.createClient({ json() {} }),
    /requires json and poll methods/,
  );
}

async function testPlanningErrorsPropagateWithoutApplying() {
  const copyError = new Error('copy unavailable');
  const copyApi = {
    json(path) {
      assert.equal(path, '/api/gen/copy');
      return Promise.reject(copyError);
    },
    poll() { throw new Error('poll must not run after submit failure'); },
  };
  await assert.rejects(
    shortDrama.createClient(copyApi).generatePlan({ id: 'project-1', revision: 1, synopsis: '故事梗概' }),
    (error) => error === copyError,
  );

  const pollError = new Error('planning failed');
  let applyCalled = false;
  const pollApi = {
    json(path) {
      if (path === '/api/gen/copy') return Promise.resolve({ job_id: 43 });
      applyCalled = true;
      return Promise.resolve({});
    },
    poll() { return Promise.reject(pollError); },
  };
  await assert.rejects(
    shortDrama.createClient(pollApi).generatePlan({ id: 'project-1', revision: 1, synopsis: '故事梗概' }),
    (error) => error === pollError,
  );
  assert.equal(applyCalled, false);
}

async function main() {
  testCanvasIntegration();
  testNodePersistenceHelpers();
  await testCreateProjectCoordinatorIsBoardScoped();
  await testCreateProjectCoordinatorPreservesConflictingLink();
  await testCreateProjectCoordinatorScopeCleanup();
  await testPureHelpers();
  await testProjectRoutesAndPlanningFlow();
  await testPlanningErrorsPropagateWithoutApplying();
  await testTerminalJobFailureDoesNotApplyPlan();
  testMissingPollFailsClearly();
  const workspace = shortDrama.createWorkspace({
    projectId: 'project-1', apiClient: { json() {} }, poll() { return Promise.resolve({}); },
  });
  assert.equal(workspace.projectId, 'project-1');
  console.log('canvas short drama: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
