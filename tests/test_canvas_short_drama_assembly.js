const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const assembly = require('../site/workbench/canvas/canvas-short-drama-assembly.js');

function snapshot(overrides = {}) {
  return Object.assign({
    project_id: 'project-1', revision: 12, stage: 'assembly_review', ratio: '9:16',
    target_duration: 60, assembly_revision: 1, implementation_status: 'renderable',
    rendering_enabled: true, current_final_version: null,
    shots: [{
      id: 'shot-1', shot_key: '第一镜', sort_order: 0, duration: 10, ready: true,
      voice: { locked: true, status: 'ready' },
      video: { confirmed: true, status: 'ready', current_version: 1 }, blockers: [],
    }],
    versions: [], active_job: null,
    readiness: { ready: true, blockers: [] },
    actions: { can_save_config: false, can_preview: false, can_lock_preview: false, can_export: true, can_confirm: false },
  }, overrides);
}

function fakeHost() {
  const listeners = new Map();
  return {
    innerHTML: '',
    addEventListener(type, handler) { listeners.set(type, handler); },
    removeEventListener(type, handler) { if (listeners.get(type) === handler) listeners.delete(type); },
    listener(type) { return listeners.get(type); },
  };
}

function testRenderReadyAndCompletedStates() {
  const normalized = assembly.normalizeState(snapshot(), {});
  assert.equal(normalized.rendering_enabled, true);
  assert.equal(normalized.actions.can_export, true);
  const html = assembly.renderWorkspace(snapshot(), {});
  assert.match(html, /最终装配[\s\S]*生成 60 秒成片/);
  assert.match(html, /全部镜头已满足合成条件/);
  assert.doesNotMatch(html, /data-action="export-final"[^>]*disabled/);

  const finished = snapshot({
    current_final_version: 2,
    versions: [{ kind: 'final', version: 2, status: 'succeeded', url: '/final.mp4', duration_ms: 60000, width: 1080, height: 1920 }],
    actions: { can_export: true, can_confirm: true },
  });
  const finishedHtml = assembly.renderWorkspace(finished, {});
  assert.match(finishedHtml, /src="\/final\.mp4"/);
  assert.match(finishedHtml, /正式版 v2 · 60 秒 · 1080×1920/);
  assert.doesNotMatch(finishedHtml, /data-action="confirm-completed"[^>]*disabled/);
}

function testEscapingAndBlockers() {
  const html = assembly.renderWorkspace(snapshot({
    shots: [{
      id: 'x', shot_key: '<script>x</script>', sort_order: 0, duration: 10, ready: false,
      voice: { locked: false }, video: { confirmed: false }, blockers: [],
    }],
    readiness: { ready: false, blockers: [{ code: 'blocked', message: '<svg onload=boom>' }] },
    actions: { can_export: false, can_confirm: false },
  }), {});
  assert.doesNotMatch(html, /<script>|<svg/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;svg onload=boom&gt;/);
  assert.match(html, /data-action="export-final"[^>]*disabled/);
}

async function testRenderAndConfirmRequests() {
  const host = fakeHost();
  const calls = [];
  let state = snapshot();
  const workspace = assembly.createWorkspace({
    projectId: 'project-1', boardId: 'board-1', host,
    client: {
      json(url, options = {}) {
        calls.push({ url, options });
        if (url.startsWith('/api/gen/short-drama/assembly?')) return Promise.resolve(state);
        if (url.endsWith('/render-final')) {
          state = snapshot({ active_job: { job_id: 'render-1', status: 'running', phase: 'normalizing', progress: 20 }, actions: { can_export: false, can_confirm: false } });
          return Promise.resolve(state);
        }
        if (url.endsWith('/confirm-assembly')) return Promise.resolve(snapshot({ stage: 'completed', revision: 13, actions: { can_export: false, can_confirm: false } }));
        throw new Error(`unexpected ${url}`);
      },
    },
  });
  await workspace.ready;
  await workspace.renderFinal();
  const renderCall = calls.find((call) => call.url.endsWith('/render-final'));
  assert.match(renderCall.options.body.idempotency_key, /^sd-final-/);
  assert.equal(renderCall.options.headers['X-Canvas-Board-Id'], 'board-1');
  assert.match(host.innerHTML, /normalizing[\s\S]*20%/);
  workspace.destroy();

  state = snapshot({
    current_final_version: 1,
    versions: [{ kind: 'final', version: 1, status: 'succeeded', url: '/final.mp4', duration_ms: 60000, width: 1080, height: 1920 }],
    actions: { can_export: true, can_confirm: true },
  });
  const confirmWorkspace = assembly.createWorkspace({
    projectId: 'project-1', host: fakeHost(),
    client: {
      json(url, options = {}) {
        if (url.startsWith('/api/gen/short-drama/assembly?')) return Promise.resolve(state);
        if (url.endsWith('/confirm-assembly')) return Promise.resolve(snapshot({ stage: 'completed', revision: 13, actions: { can_export: false, can_confirm: false } }));
        throw new Error(`unexpected ${url} ${JSON.stringify(options)}`);
      },
    },
  });
  await confirmWorkspace.ready;
  const completed = await confirmWorkspace.confirmCompleted();
  assert.equal(completed.stage, 'completed');
  confirmWorkspace.destroy();
}

function testCanvasAndOpenApiMirrors() {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
  const controller = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.js'), 'utf8');
  assert.match(html, /canvas-short-drama-assembly\.css\?v=[0-9a-f]{8}/);
  assert.match(html, /canvas-short-drama-assembly\.js\?v=[0-9a-f]{8}/);
  assert.match(controller, /shortDramaAssembly/);
  const docs = JSON.parse(fs.readFileSync(path.join(root, 'docs', 'api', 'openapi.json'), 'utf8'));
  const site = JSON.parse(fs.readFileSync(path.join(root, 'site', 'api-docs', 'openapi.json'), 'utf8'));
  assert.deepEqual(site.components.schemas.ShortDramaAssemblyWorkspace, docs.components.schemas.ShortDramaAssemblyWorkspace);
  assert.deepEqual(site.paths['/api/gen/short-drama/assembly'], docs.paths['/api/gen/short-drama/assembly']);
  assert.deepEqual(docs.components.schemas.ShortDramaAssemblyWorkspace.properties.rendering_enabled.enum, [true]);
  assert.deepEqual(docs.components.schemas.ShortDramaAssemblyWorkspace.properties.implementation_status.enum, ['renderable']);
}

async function main() {
  testRenderReadyAndCompletedStates();
  testEscapingAndBlockers();
  await testRenderAndConfirmRequests();
  testCanvasAndOpenApiMirrors();
  console.log('short drama assembly canvas tests passed');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
