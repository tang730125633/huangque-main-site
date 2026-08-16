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
    implementation_status: 'formal_export',
    rendering_enabled: true,
    planner_version: 'short_drama_media_plan_v1',
    input_hash: null,
    media_plan: null,
    audio_subtitle: {
      engine_version: 'short_drama_audio_subtitle_v1',
      input_hash: null,
      status: 'blocked',
      error_code: '',
      artifacts: [],
      blockers: [{
        code: 'missing_d1_media_plan',
        message: 'D-1 媒体计划尚未就绪',
      }],
    },
    master_audio: {
      engine_version: 'short_drama_master_audio_v1',
      contract_version: 'short_drama_master_timeline_v1',
      master_audio_hash: 'a'.repeat(64),
      status: 'not_built',
      cache_hit: false,
      duration_ms: 30000,
      sample_rate: 48000,
      channels: 2,
      codec: 'pcm_s16le',
      artifact: null,
      timeline: { shots: [] },
      blockers: [],
    },
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
      video: { confirmed: false, status: 'blocked', current_version: null },
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
  assert.equal(normalized.shots[0].video.status, 'blocked');
  assert.equal(normalized.planner_version, 'short_drama_media_plan_v1');
  assert.equal(normalized.audio_subtitle.status, 'blocked');
  assert.equal(normalized.master_audio.sample_rate, 48000);
  assert.equal(normalized.master_audio.master_audio_hash.length, 64);
  assert.equal(normalized.actions.can_preview, false);

  const html = assembly.renderWorkspace(snapshot(), {});
  assert.match(html, /镜头与素材[\s\S]*项目级合成画布[\s\S]*合成控制台/);
  assert.match(html, /第一镜/);
  assert.match(html, /配音字幕[\s\S]*已锁定/);
  assert.match(html, /D-2 音频与字幕/);
  assert.match(html, /电影化身视频[\s\S]*未确认/);
  assert.match(html, /镜头尚无已确认的电影化身视频版本/);
  assert.match(html, /尚未生成 720p 预览/);
  assert.match(html, /主音轨[\s\S]*48kHz[\s\S]*2 声道/);
  assert.match(html, /等待前序锁定素材通过探测与时间线校验/);
  assert.match(html, /音频与字幕引擎 · 输入未就绪/);
  for (const action of [
    'save-config', 'generate-preview', 'export-final', 'confirm-completed',
  ]) {
    assert.match(
      html,
      new RegExp(`data-action="${action}"[^>]*disabled`),
      `${action} must remain disabled in D-1`,
    );
  }
  const planned = assembly.renderWorkspace(snapshot({
    input_hash: '1234567890abcdef',
    media_plan: {
      project_duration_ms: 5000,
      shots: [{
        id: 'shot-1', start_ms: 0, end_ms: 5000, duration_ms: 5000,
      }],
    },
    shots: [{
      id: 'shot-1',
      shot_key: '第一镜',
      sort_order: 0,
      duration: 5,
      voice: { locked: true, status: 'ready', timeline_revision: 2, lines: [] },
      video: {
        confirmed: true, status: 'ready', current_version: 3,
        video_revision: 4,
        source_kind: 'lipsync',
        lipsync: { version_id: 'lip-1', version: 2, provider: 'fal' },
      },
      ready: true,
      blockers: [],
    }],
    readiness: { ready: true, blockers: [] },
    audio_subtitle: {
      engine_version: 'short_drama_audio_subtitle_v1',
      input_hash: 'abcdef',
      status: 'not_built',
      error_code: '',
      artifacts: [],
      blockers: [],
    },
  }), {});
  assert.doesNotMatch(planned, /已确认 v3/);
  assert.match(planned, /D-1 媒体计划已生成 · 输入哈希 1234567890ab/);
  assert.match(planned, /nc-sda-plan-shot/);
  assert.match(planned, /口型成片 v2/);
  assert.match(planned, /音频与字幕引擎 · 引擎就绪/);
  assert.match(planned, /等待 D-3 预览任务调用/);
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
  assert.equal(workspace.getState().rendering_enabled, true);
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

async function testPreviewSubmissionUsesIdempotencyAndReloads() {
  const host = fakeHost();
  const calls = [];
  const readySnapshot = snapshot({
    input_hash: 'a'.repeat(64),
    media_plan: { project_duration_ms: 5000, shots: [] },
    readiness: { ready: true, blockers: [] },
    actions: {
      can_save_config: false, can_preview: true, can_lock_preview: false,
      can_export: false, can_confirm: false,
    },
  });
  const workspace = assembly.createWorkspace({
    projectId: 'project-1',
    boardId: 'board-1',
    host,
    client: {
      json(url, options) {
        calls.push({ url, options });
        if (url.endsWith('/preview')) {
          return Promise.resolve({
            project_id: 'project-1', job_id: 41, status: 'queued', replayed: false,
          });
        }
        return Promise.resolve(readySnapshot);
      },
    },
  });
  await workspace.ready;
  host.listener('click')({
    target: {
      getAttribute(name) {
        return name === 'data-action' ? 'generate-preview' : null;
      },
      parentNode: host,
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const submit = calls.find((call) => call.url.endsWith('/preview'));
  assert.ok(submit);
  assert.equal(submit.options.method, 'POST');
  assert.equal(submit.options.headers['X-Canvas-Board-Id'], 'board-1');
  assert.match(submit.options.headers['Idempotency-Key'], /^d3-/);
  assert.deepEqual(submit.options.body, {
    project_id: 'project-1', revision: 12, assembly_revision: 1,
  });
  assert.equal(calls.filter((call) => call.url.includes('?project_id=')).length, 2);
  workspace.destroy();
}

async function testFinalQuoteExportUsesBoundPreviewAndIdempotency() {
  const host = fakeHost();
  const calls = [];
  const ready = snapshot({
    input_hash: 'b'.repeat(64),
    media_plan: { project_duration_ms: 30000, shots: [] },
    readiness: { ready: true, blockers: [] },
    current_preview_version: 2,
    versions: [{
      id: 'preview-2', kind: 'preview', version: 2, job_id: '42',
      status: 'succeeded', url: '/api/gen/file/preview.mp4',
      duration_ms: 30000,
    }],
    actions: {
      can_save_config: false, can_preview: true, can_lock_preview: false,
      can_export: true, can_confirm: false,
    },
  });
  const workspace = assembly.createWorkspace({
    projectId: 'project-1', boardId: 'board-1', host,
    confirmExport: () => true,
    client: {
      json(url, options) {
        calls.push({ url, options });
        if (url.endsWith('/final-quote')) {
          return Promise.resolve({
            quote_token: 'a'.repeat(32), total_cost: 3,
          });
        }
        if (url.endsWith('/export')) {
          return Promise.resolve({
            project_id: 'project-1', job_id: 99, status: 'queued',
            cost: 3, replayed: false,
          });
        }
        return Promise.resolve(ready);
      },
    },
  });
  await workspace.ready;
  host.listener('click')({
    target: {
      getAttribute(name) {
        return name === 'data-action' ? 'export-final' : null;
      },
      parentNode: host,
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const quote = calls.find((call) => call.url.endsWith('/final-quote'));
  const submit = calls.find((call) => call.url.endsWith('/export'));
  assert.ok(quote);
  assert.ok(submit);
  assert.equal(quote.options.body.preview_version, 2);
  assert.equal(quote.options.body.cover_time_ms, 1000);
  assert.equal(submit.options.body.quote_token, 'a'.repeat(32));
  assert.match(submit.options.headers['Idempotency-Key'], /^d4-/);
  assert.equal(submit.options.headers['X-Canvas-Board-Id'], 'board-1');
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
  assert.equal(siteText, docsText, 'OpenAPI mirrors must remain byte-identical');
  const spec = JSON.parse(docsText);
  assert.equal(spec.openapi, '3.0.3');
  const operation = spec.paths['/api/gen/short-drama/assembly'].get;
  assert.ok(operation);
  assert.deepEqual(operation.security, [{ bearerAuth: [] }]);
  assert.ok(operation.parameters.some((parameter) =>
    parameter.$ref === '#/components/parameters/XCanvasBoardId'));
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
    'rendering_enabled', 'planner_version', 'input_hash', 'media_plan',
    'audio_subtitle',
    'shots', 'versions', 'active_job', 'latest_job',
    'readiness', 'actions', 'blockers',
  ]) {
    assert.ok(workspace.required.includes(field), `workspace requires ${field}`);
  }
  assert.deepEqual(workspace.properties.rendering_enabled.enum, [true]);
  const blockerCodes =
    spec.components.schemas.ShortDramaAssemblyBlocker.properties.code.enum;
  for (const code of [
    'active_lipsync_job',
    'lipsync_source_hash_mismatch',
    'lipsync_manifest_invalid',
    'lipsync_manifest_mismatch',
  ]) {
    assert.ok(blockerCodes.includes(code), `assembly blocker documents ${code}`);
  }
  assert.equal(
    Object.hasOwn(workspace.properties.actions.properties.can_preview, 'enum'),
    false,
  );
  assert.ok(spec.paths['/api/gen/short-drama/assembly/preview'].post);
  assert.ok(spec.paths['/api/gen/short-drama/assembly/final-quote'].post);
  assert.ok(spec.paths['/api/gen/short-drama/assembly/export'].post);
  assert.ok(spec.paths['/api/gen/short-drama/assembly/confirm'].post);
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
  await testPreviewSubmissionUsesIdempotencyAndReloads();
  await testFinalQuoteExportUsesBoundPreviewAndIdempotency();
  testCanvasLoadsAndRoutesDedicatedAssemblyModule();
  testOpenApiContractAndMirrors();
  console.log('short drama assembly canvas tests passed');
}


main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
