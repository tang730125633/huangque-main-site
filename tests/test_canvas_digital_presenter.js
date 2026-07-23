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
  assert.ok(app.includes('digitalPresenterModule.canRegisterEntry'));
  assert.ok(app.includes('digitalPresenterModule.createProjectCoordinator'));
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
  await testPhaseOneWorkspaceOnlyLoadsAndSavesSettings();
  testCanvasIntegration();
  console.log('canvas digital presenter: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
