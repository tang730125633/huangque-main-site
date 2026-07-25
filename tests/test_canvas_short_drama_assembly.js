const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const assembly = require('../site/workbench/canvas/canvas-short-drama-assembly.js');


function snapshot(overrides = {}) {
  return Object.assign({
    project_id: 'project-1',
    revision: 12,
    stage: 'assembly_review',
    ratio: '9:16',
    target_duration: 30,
    assembly_revision: 1,
    implementation_status: 'contract_only',
    rendering_enabled: false,
    config: {
      subtitle: { enabled: true, preset: 'white_outline', position: 'bottom' },
      bgm: { asset_id: null, volume: 0.18, fade_in_ms: 500, fade_out_ms: 800 },
      profiles: { preview: 'short_drama_preview_v1', final: 'short_drama_final_v1' },
    },
    shots: [{
      id: 'shot-1',
      shot_key: '第一镜',
      sort_order: 0,
      duration: 5,
      voice: { locked: true, status: 'ready' },
      video: { confirmed: false, status: 'pending_c3', current_version: null },
      ready: false,
      blockers: [{
        code: 'missing_locked_video_shot',
        message: '镜头尚无已确认的电影化身视频版本',
        shot_id: 'shot-1',
      }],
    }],
    versions: [],
    active_job: null,
    readiness: {
      ready: false,
      blockers: [{
        code: 'missing_locked_video_shot',
        message: '镜头尚无已确认的电影化身视频版本',
        shot_id: 'shot-1',
      }],
    },
    actions: {
      can_save_config: false,
      can_preview: false,
      can_lock_preview: false,
      can_export: false,
      can_confirm: false,
    },
  }, overrides);
}


function fakeHost() {
  const listeners = new Map();
  return {
    innerHTML: '',
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type);
    },
    listener(type) { return listeners.get(type); },
  };
}


function testNormalizeAndRenderContract() {
  assert.deepEqual(
    Object.keys(assembly).sort(),
    ['createWorkspace', 'normalizeState', 'renderWorkspace'],
  );
  const normalized = assembly.normalizeState(snapshot(), {});
  assert.equal(normalized.project_id, 'project-1');
  assert.equal(normalized.shots[0].voice.locked, true);
  assert.equal(normalized.shots[0].video.status, 'pending_c3');
  assert.equal(normalized.actions.can_preview, false);

  const html = assembly.renderWorkspace(snapshot(), {});
  assert.match(html, /镜头与素材[\s\S]*项目级合成画布[\s\S]*合成控制台/);
  assert.match(html, /第一镜/);
  assert.match(html, /配音字幕[\s\S]*已锁定/);
  assert.match(html, /电影化身视频[\s\S]*等待 C-3/);
  assert.match(html, /镜头尚无已确认的电影化身视频版本/);
  assert.match(html, /预览渲染将在 D-3 开放/);
  for (const action of [
    'save-config', 'generate-preview', 'export-final', 'confirm-completed',
  ]) {
    assert.match(
      html,
      new RegExp(`data-action="${action}"[^>]*disabled`),
      `${action} must remain disabled in D-0`,
    );
  }
  const completed = assembly.renderWorkspace(snapshot({
    stage: 'completed',
    actions: {
      can_save_config: true,
      can_preview: true,
      can_lock_preview: true,
      can_export: true,
      can_confirm: true,
    },
  }), { canEdit: false });
  for (const action of [
    'save-config', 'generate-preview', 'export-final', 'confirm-completed',
  ]) {
    assert.match(
      completed,
      new RegExp(`data-action="${action}"[^>]*disabled`),
      `${action} must remain disabled for a completed project`,
    );
  }
}


function testLoadingErrorEmptyAndEscaping() {
  assert.match(
    assembly.renderWorkspace({}, { busy: true }),
    /data-state="loading"[\s\S]*正在加载合成工作区/,
  );
  const error = assembly.renderWorkspace({}, { error: '<load failed>' });
  assert.match(error, /data-state="error"[\s\S]*&lt;load failed&gt;/);
  assert.doesNotMatch(error, /<load failed>/);
  assert.match(
    assembly.renderWorkspace({ project_id: 'p', shots: [] }, {}),
    /data-state="empty"[\s\S]*暂无可合成镜头/,
  );

  const malicious = snapshot({
    shots: [{
      id: 'shot-" onfocus="boom',
      shot_key: '<script>镜头</script>',
      sort_order: 0,
      duration: 5,
      voice: { locked: false, status: '<img>' },
      video: { confirmed: false, status: 'pending_c3' },
      blockers: [],
    }],
    readiness: {
      ready: false,
      blockers: [{ code: 'x', message: '<svg onload=boom>' }],
    },
  });
  const html = assembly.renderWorkspace(malicious, {});
  assert.match(html, /&lt;script&gt;镜头&lt;\/script&gt;/);
  assert.match(html, /&lt;svg onload=boom&gt;/);
  assert.doesNotMatch(html, /<script>|<svg|onfocus="boom/);
}


async function testWorkspaceLoadsAndDestroysCleanly() {
  const host = fakeHost();
  const calls = [];
  const summaries = [];
  const workspace = assembly.createWorkspace({
    projectId: 'project-1',
    boardId: 'shared-board-1',
    host,
    client: {
      json(url, options) {
        calls.push({ url, options });
        return Promise.resolve(snapshot());
      },
    },
    onChange(summary) { summaries.push(summary); },
  });
  await workspace.ready;
  assert.deepEqual(calls, [
    {
      url: '/api/gen/short-drama/assembly?project_id=project-1',
      options: { headers: { 'X-Canvas-Board-Id': 'shared-board-1' } },
    },
  ]);
  assert.match(host.innerHTML, /项目级合成画布/);
  assert.equal(summaries.length, 1);
  assert.equal(summaries[0].stage, 'assembly_review');
  assert.equal(workspace.getState().rendering_enabled, false);
  assert.equal(typeof host.listener('click'), 'function');
  workspace.destroy();
  assert.equal(host.listener('click'), undefined);
}

async function testPersonalWorkspaceOmitsBoardHeader() {
  const calls = [];
  const workspace = assembly.createWorkspace({
    projectId: 'personal-project',
    host: fakeHost(),
    client: {
      json(url, options) {
        calls.push({ url, options });
        return Promise.resolve(snapshot({ project_id: 'personal-project' }));
      },
    },
  });
  await workspace.ready;
  assert.deepEqual(calls, [{
    url: '/api/gen/short-drama/assembly?project_id=personal-project',
    options: {},
  }]);
  workspace.destroy();
}


function testCanvasLoadsAndRoutesDedicatedAssemblyModule() {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(
    path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8',
  );
  const controller = fs.readFileSync(
    path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.js'),
    'utf8',
  );
  assert.match(html, /canvas-short-drama-assembly\.css\?v=[0-9a-f]+/);
  assert.match(html, /canvas-short-drama-assembly\.js\?v=[0-9a-f]+/);
  assert.match(controller, /shortDramaAssembly/);
  assert.match(controller, /assemblyModule/);
  assert.match(controller, /stage==='assembly_review'\|\|stage==='completed'/);
}

function testOpenApiContractAndMirrors() {
  const root = path.join(__dirname, '..');
  const docsText = fs.readFileSync(
    path.join(root, 'docs', 'api', 'openapi.json'), 'utf8',
  );
  const siteText = fs.readFileSync(
    path.join(root, 'site', 'api-docs', 'openapi.json'), 'utf8',
  );
  const spec = JSON.parse(docsText);
  const siteSpec = JSON.parse(siteText);
  assert.equal(spec.openapi, '3.0.3');
  assert.deepEqual(
    siteSpec.paths['/api/gen/short-drama/assembly'],
    spec.paths['/api/gen/short-drama/assembly'],
    'assembly operation must remain identical in both OpenAPI documents',
  );
  for (const schema of [
    'ShortDramaAssemblyBlocker', 'ShortDramaAssemblyShot',
    'ShortDramaAssemblyWorkspace', 'ShortDramaCompositionJob',
    'ShortDramaCompositionVersion',
  ]) {
    assert.deepEqual(
      siteSpec.components.schemas[schema],
      spec.components.schemas[schema],
      `${schema} must remain identical in both OpenAPI documents`,
    );
  }
  const operation = spec.paths['/api/gen/short-drama/assembly'].get;
  assert.ok(operation);
  assert.deepEqual(operation.security, [{ bearerAuth: [] }]);
  assert.ok(operation.parameters.some((parameter) =>
    parameter.$ref === '#/components/parameters/XCanvasBoardId'
      || parameter.name === 'X-Canvas-Board-Id'));
  assert.ok(operation.parameters.some((parameter) =>
    parameter.name === 'project_id' && parameter.required));
  for (const status of ['200', '400', '401', '403', '404']) {
    assert.ok(operation.responses[status], `assembly GET documents ${status}`);
  }
  assert.equal(
    operation.responses['200'].content['application/json'].schema.$ref,
    '#/components/schemas/ShortDramaAssemblyWorkspace',
  );
  const workspace = spec.components.schemas.ShortDramaAssemblyWorkspace;
  for (const field of [
    'project_id', 'revision', 'stage', 'ratio', 'target_duration',
    'assembly_revision', 'config', 'implementation_status',
    'rendering_enabled', 'shots', 'versions', 'active_job',
    'readiness', 'actions', 'blockers',
  ]) {
    assert.ok(workspace.required.includes(field), `workspace requires ${field}`);
  }
  assert.deepEqual(workspace.properties.rendering_enabled.enum, [false]);
  assert.deepEqual(workspace.properties.actions.properties.can_preview.enum, [false]);
  assert.equal(
    workspace.properties.versions.items.$ref,
    '#/components/schemas/ShortDramaCompositionVersion',
  );
  assert.equal(
    workspace.properties.active_job.$ref,
    '#/components/schemas/ShortDramaCompositionJob',
  );
  const job = spec.components.schemas.ShortDramaCompositionJob;
  assert.equal(Object.hasOwn(job.properties, 'idempotency_key'), false);
  assert.equal(Object.hasOwn(job.properties, 'request_hash'), false);
  const version = spec.components.schemas.ShortDramaCompositionVersion;
  assert.equal(Object.hasOwn(version.properties, 'file'), false);
  assert.equal(
    workspace.properties.current_preview_version.nullable,
    true,
    'OpenAPI 3.0.3 uses nullable instead of a 3.1 union type',
  );
}


async function main() {
  testNormalizeAndRenderContract();
  testLoadingErrorEmptyAndEscaping();
  await testWorkspaceLoadsAndDestroysCleanly();
  await testPersonalWorkspaceOmitsBoardHeader();
  testCanvasLoadsAndRoutesDedicatedAssemblyModule();
  testOpenApiContractAndMirrors();
  console.log('short drama assembly canvas tests passed');
}


main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
