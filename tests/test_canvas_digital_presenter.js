const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const presenter = require('../site/workbench/canvas/canvas-digital-presenter.js');
const canvasState = require('../site/workbench/canvas/canvas-state.js');

function testNodePersistenceAndCopyHelpers() {
  const dirty = {
    id: 'n1', type: 'digitalPresenter', x: 12, y: 30,
    params: {
      project_id: 'p1', title: '资讯', ratio: '16:9', target_duration: 90,
      stage: 'editing', progress: 65, spent_points: 18, estimated_points: 30,
      failed_segment_count: 2, avatar_thumbnail: 'asset:avatar-1',
      script_text: '不得进入画布', timeline: [{ secret: true }], role: 'owner',
    },
    outputs: { timeline: [1], video: 'secret' },
  };
  const clean = presenter.sanitizeNodeData(dirty);
  assert.equal(clean.id, 'n1');
  assert.equal(clean.params.project_id, 'p1');
  assert.deepEqual(Object.keys(clean.params).sort(), [
    'avatar_thumbnail', 'estimated_points', 'failed_segment_count', 'progress',
    'project_id', 'ratio', 'spent_points', 'stage', 'target_duration', 'title',
  ]);
  assert.deepEqual(clean.outputs, {});
  assert.equal('script_text' in clean.params, false);
  assert.notStrictEqual(clean, dirty);

  const stateClean = canvasState.sanitizeNodeData(dirty, {
    digitalPresenter: presenter.sanitizeNodeData,
  });
  assert.deepEqual(stateClean, clean);

  const copied = presenter.copyNodeData(dirty);
  assert.equal(copied.params.project_id, null);
  assert.equal(copied.params.stage, 'draft');
  assert.equal(copied.params.progress, 0);
  assert.equal(copied.params.spent_points, 0);
  assert.deepEqual(copied.outputs, {});

  assert.deepEqual(presenter.creationPayload(clean.params), {
    title: '资讯', ratio: '16:9', target_duration: 90,
  });
  assert.equal(presenter.canRegisterEntry({ enabled: true }), true);
  assert.equal(presenter.canRegisterEntry({ enabled: false }), false);
  assert.equal(presenter.canRegisterEntry({ enabled: 'true' }), false);
}

async function testCreateProjectCoordinatorIsScopeSafe() {
  let active = 'collab:board-a';
  let resolveCreate;
  let creates = 0;
  const boards = {
    'collab:board-a': { n1: { id: 'n1', params: presenter.normalizeNodeParams({ title: 'A' }) } },
    'collab:board-b': { n1: { id: 'n1', params: presenter.normalizeNodeParams({ title: 'B' }) } },
  };
  const coordinator = presenter.createProjectCoordinator({
    getNode(scope, id) { return scope === active ? boards[scope][id] : null; },
    create() { creates += 1; return new Promise((resolve) => { resolveCreate = resolve; }); },
    apply(node, project) { node.params = presenter.summarizeProject(project); },
  });
  const first = coordinator.ensure('collab:board-a', 'n1', { title: 'A' }, true, null);
  const duplicate = coordinator.ensure('collab:board-a', 'n1', { title: 'A' }, true, null);
  assert.strictEqual(first, duplicate);
  assert.equal(creates, 0, 'creation begins in a microtask');
  await Promise.resolve();
  assert.equal(creates, 1);
  active = 'collab:board-b';
  coordinator.cleanupScope('collab:board-a');
  resolveCreate({ id: 'project-a', title: 'A', stage: 'draft' });
  assert.equal(await first, 'project-a');
  assert.equal(boards['collab:board-b'].n1.params.project_id, null,
    'late project creation never links another board');
}

async function testCreateProjectCoordinatorReusesPersistedIdempotencyKey() {
  const values = new Map();
  const storage = {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  const node = { id: 'n1', params: presenter.normalizeNodeParams({ title: 'A' }) };
  const keys = [];
  const first = presenter.createProjectCoordinator({
    storage,
    getNode() { return node; },
    create(_payload, key) {
      keys.push(key);
      return Promise.reject(new Error('response lost'));
    },
    apply() { throw new Error('unexpected apply'); },
  });
  await assert.rejects(
    first.ensure('collab:board-a', 'n1', { title: 'A' }, true, null),
    /response lost/,
  );
  assert.equal(values.size, 1, 'failed request keeps its identity across refresh');

  const refreshed = presenter.createProjectCoordinator({
    storage,
    getNode() { return node; },
    create(_payload, key) {
      keys.push(key);
      return Promise.resolve({ id: 'project-a', title: 'A' });
    },
    apply(current, project) { current.params = presenter.summarizeProject(project); },
  });
  assert.equal(
    await refreshed.ensure('collab:board-a', 'n1', { title: 'A' }, true, null),
    'project-a',
  );
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1], 'refresh retry reuses the original Idempotency-Key');
  assert.match(keys[0], /^dp-create-[A-Za-z0-9-]+$/);
  assert.equal(values.size, 1, 'identity remains until a later read confirms the node link');
  assert.equal(
    await refreshed.ensure('collab:board-a', 'n1', { title: 'A' }, true, null),
    'project-a',
  );
  assert.equal(keys.length, 2, 'confirmed node link does not create again');
  assert.equal(values.size, 1, 'the creating page cannot claim its debounced save is durable');
  const reloaded = presenter.createProjectCoordinator({
    storage,
    getNode() { return node; },
    create() { throw new Error('persisted node must not create again'); },
    apply() { throw new Error('persisted node must not apply again'); },
  });
  assert.equal(
    await reloaded.ensure('collab:board-a', 'n1', { title: 'A' }, true, null),
    'project-a',
  );
  assert.equal(values.size, 0, 'reload confirms the node link was durably restored and clears identity');
}

async function testCreateProjectCoordinatorRecoversAfterScopeSwitch() {
  const values = new Map();
  const storage = {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  let active = true;
  let resolveCreate;
  const keys = [];
  const firstNode = { id: 'n1', params: presenter.normalizeNodeParams({ title: 'A' }) };
  const first = presenter.createProjectCoordinator({
    storage,
    getNode() { return active ? firstNode : null; },
    create(_payload, key) {
      keys.push(key);
      return new Promise((resolve) => { resolveCreate = resolve; });
    },
    apply() { assert.fail('a switched-away canvas must not receive the project'); },
  });
  const pending = first.ensure('collab:board-a', 'n1', { title: 'A' }, true, null);
  await Promise.resolve();
  active = false;
  first.cleanupScope('collab:board-a');
  resolveCreate({ id: 'project-a', title: 'A' });
  assert.equal(await pending, 'project-a');
  assert.equal(firstNode.params.project_id, null);
  assert.equal(values.size, 1, 'switching away preserves the recoverable identity');

  const restoredNode = { id: 'n1', params: presenter.normalizeNodeParams({ title: 'A' }) };
  const refreshed = presenter.createProjectCoordinator({
    storage,
    getNode() { return restoredNode; },
    create(_payload, key) {
      keys.push(key);
      return Promise.resolve({ id: 'project-a', title: 'A' });
    },
    apply(node, project) { node.params = presenter.summarizeProject(project); },
  });
  assert.equal(
    await refreshed.ensure('collab:board-a', 'n1', { title: 'A' }, true, null),
    'project-a',
  );
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1], 'reload after a scope switch replays the same server project');
  assert.equal(restoredNode.params.project_id, 'project-a');
}

async function testDigitalPresenterClientSendsIdempotencyKey() {
  const calls = [];
  const client = presenter.createClient({
    json(path, options) { calls.push([path, options]); return Promise.resolve({ id: 'p1' }); },
  }, 'board-a');
  await client.create({ title: 'A' }, 'test-client-key');
  assert.equal(calls[0][0], '/api/gen/digital-presenter/projects');
  assert.equal(calls[0][1].headers['X-Canvas-Board-Id'], 'board-a');
  assert.equal(calls[0][1].headers['Idempotency-Key'], 'test-client-key');
}

async function testPhaseOneWorkspaceOnlyLoadsAndSavesSettings() {
  let project = {
    id: 'p1', title: '资讯项目', script_text: '一段口播', ratio: '9:16',
    target_duration: 45, stage: 'draft', revision: 1, spent_points: 0,
  };
  const calls = [];
  const workspace = presenter.createWorkspace({
    projectId: 'p1', document: null, canEdit: true,
    client: {
      get(id) { calls.push(['get', id]); return Promise.resolve({ ...project }); },
      update(id, revision, patch) {
        calls.push(['update', id, revision, { ...patch }]);
        project = { ...project, ...patch, revision: revision + 1 };
        return Promise.resolve({ ...project });
      },
      delete() { throw new Error('unexpected delete'); },
    },
  });
  await workspace.ready;
  assert.match(workspace.render(), /项目设置/);
  assert.match(workspace.render(), /后续阶段尚未开放/);
  await workspace.saveSettings({ title: '新版资讯', ratio: '16:9', target_duration: 60 });
  assert.deepEqual(calls[1], [
    'update', 'p1', 1,
    { title: '新版资讯', ratio: '16:9', target_duration: 60 },
  ]);
  assert.equal(workspace.getProject().revision, 2);
  assert.equal(calls.some((call) => /generate|audio|video|render/.test(call[0])), false);
  workspace.destroy();
  await assert.rejects(workspace.saveSettings({ title: '关闭后' }), /destroyed/i);
}

async function captureUnhandled(run) {
  const errors = [];
  const listener = (error) => { errors.push(error); };
  process.on('unhandledRejection', listener);
  try {
    await run();
    await new Promise((resolve) => setImmediate(resolve));
  } finally {
    process.removeListener('unhandledRejection', listener);
  }
  return errors;
}

function createFakeDocument() {
  const body = {
    children: [],
    appendChild(node) { node.parentNode = this; this.children.push(node); },
    removeChild(node) { this.children = this.children.filter((item) => item !== node); node.parentNode = null; },
  };
  return {
    body,
    createElement() {
      return {
        className: '', innerHTML: '', parentNode: null, handlers: {},
        addEventListener(type, handler) { this.handlers[type] = handler; },
      };
    },
  };
}

async function testRejectedProjectLoadIsContainedAndErrorCanClose() {
  let errorCalls = 0;
  const unhandled = await captureUnhandled(async () => {
    const document = createFakeDocument();
    const workspace = presenter.createWorkspace({
      projectId: 'missing', document, canEdit: true,
      client: { get() { return Promise.reject(new Error('project unavailable')); } },
    });
    const opened = await presenter.observeWorkspaceReady(workspace, {
      isActive() { return true; },
      onReady() { assert.fail('rejected load must not be marked opened'); },
      onError(error) { errorCalls += 1; assert.match(error.message, /project unavailable/); },
    });
    assert.equal(opened, null);
    assert.match(workspace.render(), /project unavailable/);
    assert.match(workspace.render(), /data-action="close"/);
    const host = document.body.children[0];
    host.handlers.click({ target: { getAttribute(name) { return name === 'data-action' ? 'close' : null; } } });
    assert.equal(document.body.children.length, 0, 'error close action removes the overlay');
  });
  assert.equal(errorCalls, 1);
  assert.deepEqual(unhandled, []);
}

async function testDestroyDuringReadyIsContainedAsInactive() {
  let rejectLoad;
  let opened = 0;
  let failed = 0;
  const unhandled = await captureUnhandled(async () => {
    const workspace = presenter.createWorkspace({
      projectId: 'slow', document: null, canEdit: false,
      client: { get() { return new Promise((_resolve, reject) => { rejectLoad = reject; }); } },
    });
    let active = true;
    const observed = presenter.observeWorkspaceReady(workspace, {
      isActive() { return active; },
      onReady() { opened += 1; },
      onError() { failed += 1; },
    });
    active = false;
    workspace.destroy();
    rejectLoad(new Error('late failure'));
    assert.equal(await observed, null);
  });
  assert.equal(opened, 0);
  assert.equal(failed, 0, 'inactive workspace does not overwrite downgraded/switched UI');
  assert.deepEqual(unhandled, []);
}

async function testOpenedStateWaitsForWorkspaceReady() {
  let resolveLoad;
  let opened = 0;
  const workspace = presenter.createWorkspace({
    projectId: 'slow-success', document: null, canEdit: true,
    client: { get() { return new Promise((resolve) => { resolveLoad = resolve; }); } },
  });
  const observed = presenter.observeWorkspaceReady(workspace, {
    isActive() { return true; },
    onReady() { opened += 1; },
    onError(error) { throw error; },
  });
  await Promise.resolve();
  assert.equal(opened, 0);
  resolveLoad({ id: 'slow-success', title: 'Ready', ratio: '9:16', target_duration: 30, revision: 1 });
  assert.strictEqual(await observed, workspace);
  assert.equal(opened, 1);
  workspace.destroy();
}

function testEntryRegistrationAndLifecycleBehaviorsExecute() {
  let entries = 0;
  const entry = presenter.createEntryRegistrar(() => { entries += 1; });
  assert.equal(entry.register({ enabled: false }), false);
  assert.equal(entry.register({ enabled: true }), true);
  assert.equal(entry.register({ enabled: true }), false);
  assert.equal(entries, 1);

  const lifecycle = presenter.createWorkspaceLifecycle();
  const destroyed = [];
  const workspace = (name) => ({ destroy() { destroyed.push(name); } });
  lifecycle.attach('local:a', 'restored', workspace('restore'));
  lifecycle.restoreScope('local:a');
  lifecycle.attach('collab:a', 'deleted', workspace('delete'));
  lifecycle.removeNode('collab:a', 'deleted');
  lifecycle.attach('collab:a', 'switched', workspace('switch'));
  lifecycle.switchScope('collab:a');
  lifecycle.attach('collab:b', 'downgraded', workspace('downgrade'));
  lifecycle.roleChanged('editor', 'viewer');
  assert.deepEqual(destroyed, ['restore', 'delete', 'switch', 'downgrade']);
  assert.equal(lifecycle.size(), 0);
}

function testNodeCreationPolicyGuardsEveryEntry() {
  let context = { canEdit: true, scope: 'local', entryEnabled: true };
  const policy = presenter.createNodeCreationPolicy({ context: () => context });
  const created = [];
  for (const entry of ['context-menu', 'fullscreen-menu']) {
    assert.equal(policy.canCreate('digitalPresenter'), false, `${entry} hidden on local canvas`);
  }
  assert.equal(policy.canMaterialize('digitalPresenter', 'create'), false,
    'cross-canvas paste cannot materialize on a local canvas');
  assert.equal(policy.canMaterialize('digitalPresenter', 'restore'), false,
    'local snapshots cannot restore a digital presenter node');
  assert.equal(policy.canMaterialize('digitalPresenter', 'trusted-collab'), false,
    'trusted restoration still requires collaborative scope');
  assert.equal(policy.canMaterialize('digitalPresenter', 'local-template'), false,
    'a template cannot create a local canvas containing a digital presenter');
  for (const entry of ['addAt', 'top-button']) {
    assert.equal(policy.run('digitalPresenter', () => created.push(entry)), false);
  }
  assert.deepEqual(created, []);

  context = { canEdit: true, scope: 'collab', entryEnabled: false };
  assert.equal(policy.canCreate('digitalPresenter'), false, 'capability remains required');
  assert.equal(policy.canMaterialize('digitalPresenter', 'restore'), false,
    'snapshot restoration also requires the capability');
  context = { canEdit: false, scope: 'collab', entryEnabled: true };
  assert.equal(policy.canMaterialize('digitalPresenter', 'create'), false,
    'viewer cannot paste or import a new node');
  assert.equal(policy.canMaterialize('digitalPresenter', 'restore'), false,
    'viewer cannot use undo or a local snapshot to introduce a node');
  assert.equal(policy.canMaterialize('digitalPresenter', 'trusted-collab'), true,
    'viewer may render a server-authoritative collaborative snapshot');
  context = { canEdit: true, scope: 'collab', entryEnabled: true };
  assert.equal(policy.canMaterialize('digitalPresenter', 'create'), true);
  assert.equal(policy.canMaterialize('digitalPresenter', 'restore'), true);
  for (const entry of ['context-menu', 'fullscreen-menu']) {
    assert.equal(policy.canCreate('digitalPresenter'), true, `${entry} visible on collaborative canvas`);
  }
  for (const entry of ['addAt', 'top-button']) {
    assert.equal(policy.run('digitalPresenter', () => created.push(entry)), true);
  }
  assert.deepEqual(created, ['addAt', 'top-button']);
}

function testCanvasIntegration() {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
  const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-digital-presenter.css'), 'utf8');
  const ci = fs.readFileSync(path.join(root, '.github', 'workflows', 'ci.yml'), 'utf8');
  const stamps = fs.readFileSync(path.join(root, 'scripts', 'stamp_assets.py'), 'utf8');
  assert.ok(html.includes('canvas/canvas-digital-presenter.css?v='));
  assert.ok(html.includes('canvas/canvas-digital-presenter.js?v='));
  assert.ok(html.indexOf('canvas/canvas-digital-presenter.js?v=') < html.indexOf('canvas/canvas-app.js?v='));
  assert.equal((html.match(/data-add="digitalPresenter"/g) || []).length, 0,
    'disabled-by-default entry is not statically registered');
  assert.match(app, /digitalPresenter:\s*\{name:'数字人口播'/);
  assert.ok(app.includes('/api/gen/digital-presenter/capability'));
  assert.ok(app.includes('digitalPresenterModule.createProjectCoordinator'));
  assert.ok(app.includes("headers['Idempotency-Key']=idempotencyKey"));
  assert.ok(app.includes('digitalPresenterModule.createEntryRegistrar'));
  assert.ok(app.includes('digitalPresenterModule.createWorkspaceLifecycle'));
  assert.ok(app.includes('digitalPresenterModule.observeWorkspaceReady'));
  assert.ok(app.includes('digitalPresenterModule.createNodeCreationPolicy'));
  assert.ok(app.includes('nodeCreationPolicy.canCreate'));
  assert.ok(app.includes('nodeCreationPolicy.canMaterialize'));
  assert.match(app, /function addNode\(type, x, y, data,materializeSource\)[\s\S]*?nodeCreationPolicy\.canMaterialize/,
    'the lowest-level node constructor enforces the policy');
  assert.match(app, /function pasteNode\(\)[\s\S]*?pastedNodes[\s\S]*?nodeCreationPolicy\.canMaterialize/,
    'clipboard paste checks every copied node');
  assert.match(app, /function appendTemplateToCanvas\(item\)[\s\S]*?nodeCreationPolicy\.canMaterialize/,
    'template append checks every imported node');
  assert.match(app, /function restoreSnapshot\(snap\)[\s\S]*?restoredNodes[\s\S]*?nodeCreationPolicy\.canMaterialize/,
    'snapshot restore filters unauthorized nodes and their edges');
  assert.ok((app.match(/nodeCreationPolicy\.run/g) || []).length >= 2);
  assert.ok(app.includes('digitalPresenterModule.copyNodeData'));
  assert.ok(app.includes('digitalPresenterModule.createWorkspace'));
  assert.ok(app.includes('data-f="openDigitalPresenter"'));
  assert.match(app, /destroyDigitalPresenterWorkspace/);
  assert.match(app, /stateApi\.sanitizeNodeData/);
  assert.match(css, /nc-digital-presenter-workspace/);
  assert.ok(ci.includes('node tests/test_canvas_digital_presenter.js'));
  for (const asset of ['canvas/canvas-digital-presenter.js', 'canvas/canvas-digital-presenter.css']) {
    assert.ok(stamps.includes(`Asset("${asset}", required=False)`), `${asset} must be registered for cache stamping`);
    const source = fs.readFileSync(path.join(root, 'site', 'workbench', asset), 'utf8').replace(/\r\n/g, '\n');
    const hash = crypto.createHash('md5').update(source).digest('hex').slice(0, 8);
    assert.ok(html.includes(`${asset}?v=${hash}`), `${asset} cache stamp must match content`);
  }
}

async function main() {
  testNodePersistenceAndCopyHelpers();
  await testCreateProjectCoordinatorIsScopeSafe();
  await testCreateProjectCoordinatorReusesPersistedIdempotencyKey();
  await testCreateProjectCoordinatorRecoversAfterScopeSwitch();
  await testDigitalPresenterClientSendsIdempotencyKey();
  await testPhaseOneWorkspaceOnlyLoadsAndSavesSettings();
  await testRejectedProjectLoadIsContainedAndErrorCanClose();
  await testDestroyDuringReadyIsContainedAsInactive();
  await testOpenedStateWaitsForWorkspaceReady();
  testEntryRegistrationAndLifecycleBehaviorsExecute();
  testNodeCreationPolicyGuardsEveryEntry();
  testCanvasIntegration();
  console.log('canvas digital presenter: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
