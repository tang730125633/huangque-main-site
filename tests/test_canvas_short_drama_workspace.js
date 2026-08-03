const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const storeModule = require('../site/workbench/canvas/canvas-short-drama-store.js');
const apiModule = require('../site/workbench/canvas/canvas-short-drama-api.js');
const pollerModule = require('../site/workbench/canvas/canvas-short-drama-poller.js');
const playerModule = require('../site/workbench/canvas/canvas-short-drama-player.js');
const versionsModule = require('../site/workbench/canvas/canvas-short-drama-versions.js');
const locksModule = require('../site/workbench/canvas/canvas-short-drama-locks.js');
const formsModule = require('../site/workbench/canvas/canvas-short-drama-forms.js');
const completionModule = require(
  '../site/workbench/canvas/canvas-short-drama-completion.js',
);
const workspaceModule = require('../site/workbench/canvas/canvas-short-drama-workspace.js');


function snapshot(overrides = {}) {
  return Object.assign({
    project_id: 'project-d5',
    revision: 19,
    stage: 'assembly_review',
    ratio: '9:16',
    target_duration: 30,
    assembly_revision: 4,
    current_preview_version: 2,
    current_final_version: null,
    preview_locked: false,
    rendering_enabled: true,
    config: {
      subtitle: { enabled: true, preset: 'white_outline', position: 'bottom' },
      bgm: { asset_id: null, volume: 0.18, fade_in_ms: 500, fade_out_ms: 800 },
    },
    media_plan: {
      project_duration_ms: 30000,
      shots: [
        { id: 'shot-1', shot_key: '第一镜', duration_ms: 10000 },
        { id: 'shot-2', shot_key: '第二镜', duration_ms: 20000 },
      ],
    },
    shots: [{
      id: 'shot-1',
      shot_key: '第一镜',
      sort_order: 0,
      duration: 10,
      voice: { locked: true, status: 'ready', lines: [{ id: 'line-1' }] },
      video: { confirmed: true, status: 'ready', current_version: 3 },
      ready: true,
      blockers: [],
    }, {
      id: 'shot-2',
      shot_key: '第二镜',
      sort_order: 1,
      duration: 20,
      voice: { locked: false, status: 'blocked', lines: [] },
      video: { confirmed: false, status: 'blocked', current_version: null },
      ready: false,
      blockers: [{ code: 'missing_locked_voice_shot', message: '配音尚未锁定' }],
    }],
    versions: [{
      id: 'preview-2',
      kind: 'preview',
      version: 2,
      job_id: 'job-preview-2',
      status: 'succeeded',
      url: '/api/gen/file/preview-2.mp4',
      duration_ms: 30000,
      width: 720,
      height: 1280,
      created_at: 1720000000,
      created_by: 'owner',
    }, {
      id: 'preview-1',
      kind: 'preview',
      version: 1,
      job_id: 'job-preview-1',
      status: 'succeeded',
      url: '/api/gen/file/preview-1.mp4',
      duration_ms: 30000,
      width: 720,
      height: 1280,
      created_at: 1710000000,
      created_by: 'editor',
    }],
    active_job: null,
    latest_job: null,
    readiness: {
      ready: false,
      blockers: [{ code: 'missing_locked_voice_shot', message: '配音尚未锁定' }],
    },
    actions: {
      can_save_config: false,
      can_preview: false,
      can_lock_preview: false,
      can_export: true,
      can_confirm: false,
    },
    completion: {
      project_id: 'project-d5',
      revision: 19,
      stage: 'assembly_review',
      feature_enabled: false,
      ready: false,
      blockers: [],
      delivery_hash: '',
    },
  }, overrides);
}


function fakeHost() {
  const listeners = new Map();
  return {
    innerHTML: '',
    addEventListener(type, handler) { listeners.set(type, handler); },
    removeEventListener(type, handler) {
      if (listeners.get(type) === handler) listeners.delete(type);
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    listener(type) { return listeners.get(type); },
  };
}


function dependencies() {
  return {
    storeModule,
    apiModule,
    pollerModule,
    playerModule,
    versionsModule,
    locksModule,
    formsModule,
    completionModule,
  };
}

async function flushWorkspace() {
  for (let index = 0; index < 6; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}


function testStoreNormalizesAndKeepsHistoryReadOnly() {
  const store = storeModule.createStore({
    projectId: 'project-d5',
    boardId: 'board-1',
    canEdit: true,
  });
  assert.equal(store.setWorkspace(snapshot()), true);
  let state = store.getState();
  assert.deepEqual(state.workspace.completion, snapshot().completion);
  assert.equal(state.selection.versionId, 'preview-2');
  assert.equal(store.selectors().currentVersion, true);
  assert.equal(store.selectors().readOnly, false);
  store.selectVersion('preview-1');
  assert.equal(store.selectors().historyOnly, true);
  assert.equal(store.selectors().readOnly, true);
  const completed = {
    project_id: 'project-d5',
    revision: 20,
    stage: 'completed',
    feature_enabled: true,
    ready: true,
    blockers: [],
    delivery_hash: 'delivery-hash-completed',
    completion: {
      completion_id: 'completion-1',
      asset_id: 'final-asset-1',
      completed_by: 'owner',
      completed_at: 1730000000,
    },
  };
  store.setWorkspace(snapshot({ stage: 'completed', completion: completed }));
  assert.equal(store.selectors().completed, true);
  assert.equal(store.selectors().readOnly, true);
  assert.deepEqual(store.getState().workspace.completion, completed);
  const html = workspaceModule.renderWorkspace(
    store.getState(), store.selectors(), {
      versions: versionsModule,
      locks: locksModule,
      completion: completionModule,
    },
  );
  assert.match(html, /completion-1/);
  assert.match(html, /final-asset-1/);
  store.destroy();
}

function testFailedPreviewDoesNotLockTheCurrentWorkspace() {
  const failed = {
    id: 'preview-failed',
    kind: 'preview',
    version: 1,
    job_id: '554',
    status: 'failed',
    phase: 'failed',
    error_code: 'subtitle_font_unavailable',
    error_message: '指定的中文字幕字体文件不存在',
    created_at: 1730000000,
  };
  const failedSnapshot = snapshot({
    current_preview_version: null,
    versions: [failed],
    latest_job: {
      job_id: '554',
      kind: 'preview',
      status: 'failed',
      phase: 'failed',
      progress: 5,
      error_message: failed.error_message,
    },
    readiness: { ready: true, blockers: [] },
    actions: {
      can_save_config: true,
      can_preview: true,
      can_lock_preview: false,
      can_export: false,
      can_confirm: false,
    },
  });
  const store = storeModule.createStore({
    projectId: 'project-d5',
    versionId: 'preview-failed',
    canEdit: true,
  });
  store.setWorkspace(failedSnapshot);
  assert.equal(store.getState().selection.versionId, '');
  assert.equal(store.selectors().historyOnly, false);
  assert.equal(store.selectors().readOnly, false);
  const html = workspaceModule.renderWorkspace(
    store.getState(), store.selectors(), {
      versions: versionsModule,
      locks: locksModule,
      completion: completionModule,
    },
  );
  assert.doesNotMatch(html, /历史版本只读/);
  assert.match(html, /data-action="generate-preview"(?! disabled)/);

  store.selectVersion('preview-failed');
  assert.equal(store.selectors().historyOnly, true);
  const historyHtml = workspaceModule.renderWorkspace(
    store.getState(), store.selectors(), {
      versions: versionsModule,
      locks: locksModule,
      completion: completionModule,
    },
  );
  assert.match(historyHtml, /data-action="return-current"/);
  store.selectVersion('');
  assert.equal(store.selectors().historyOnly, false);
  store.destroy();

  const staleStore = storeModule.createStore({
    projectId: 'project-d5',
    canEdit: true,
  });
  staleStore.setWorkspace(snapshot({
    current_preview_version: null,
    versions: [
      Object.assign({}, failed, { id: 'preview-failed-2', version: 2 }),
      Object.assign({}, snapshot().versions[1], { id: 'preview-stale' }),
    ],
    readiness: { ready: true, blockers: [] },
    actions: Object.assign({}, failedSnapshot.actions),
  }));
  assert.equal(
    staleStore.getState().selection.versionId,
    '',
    'a stale succeeded preview must not become the implicit editing context',
  );
  assert.equal(staleStore.selectors().readOnly, false);
  staleStore.destroy();
}


function testThreeColumnRenderAndPermissionGates() {
  const store = storeModule.createStore({
    projectId: 'project-d5',
    canEdit: false,
    project: { point_budget: 120, spent_points: 18, reserved_points: 3 },
  });
  store.setWorkspace(snapshot());
  let html = workspaceModule.renderWorkspace(
    store.getState(), store.selectors(), {
      versions: versionsModule,
      locks: locksModule,
      completion: completionModule,
    },
  );
  assert.match(html, /nc-sdw-left[\s\S]*nc-sdw-main[\s\S]*nc-sdw-right/);
  assert.match(html, /项目设置[\s\S]*配音字幕[\s\S]*成片确认[\s\S]*已交付/);
  assert.match(html, /第一镜[\s\S]*第二镜/);
  assert.match(html, /720p 预览[\s\S]*v2/);
  assert.match(html, /项目预算[\s\S]*120 点/);
  assert.match(html, /data-action="generate-preview" disabled/);
  assert.match(html, /当前为只读权限/);

  store.setWorkspace(snapshot({
    current_final_version: 1,
    versions: [{
      id: 'final-1',
      asset_id: 'asset-1',
      kind: 'final',
      version: 1,
      status: 'succeeded',
      url: '/api/gen/file/final-1.mp4',
      created_at: 1730000000,
    }],
  }));
  html = workspaceModule.renderWorkspace(
    store.getState(), store.selectors(), {
      versions: versionsModule,
      locks: locksModule,
      completion: completionModule,
    },
  );
  assert.doesNotMatch(html, />下载成片</);
  assert.match(html, /当前权限仅允许播放/);

  store.setWorkspace(snapshot());
  store.selectVersion('preview-1');
  html = workspaceModule.renderWorkspace(
    store.getState(), store.selectors(), {
      versions: versionsModule,
      locks: locksModule,
      completion: completionModule,
    },
  );
  assert.match(html, /历史版本只读/);
  assert.match(html, /选择版本只改变查看上下文/);
  store.destroy();
}

function testFinalAssetUsesCanonicalDeepLink() {
  assert.equal(
    workspaceModule.finalAssetHref(
      { asset_id: 'final-asset-1' },
      { project_id: 'project-d5' },
      { boardId: 'shared-board' },
    ),
    '/workbench/assets?cat=video&asset_id=final-asset-1&project_id=project-d5&board_id=shared-board',
  );
  const store = storeModule.createStore({
    projectId: 'project-d5',
    boardId: 'shared-board',
    canEdit: true,
  });
  store.setWorkspace(snapshot({
    current_final_version: 1,
    versions: [{
      id: 'final-1',
      asset_id: 'final-asset-1',
      kind: 'final',
      version: 1,
      status: 'succeeded',
      url: 'https://signed.example/final.mp4',
      created_at: 1730000000,
    }],
  }));
  const html = workspaceModule.renderWorkspace(
    store.getState(), store.selectors(), {
      versions: versionsModule,
      locks: locksModule,
      completion: completionModule,
    },
  );
  assert.match(
    html,
    /href="\/workbench\/assets\?cat=video&amp;asset_id=final-asset-1&amp;project_id=project-d5&amp;board_id=shared-board"/,
  );
  assert.doesNotMatch(html, /\.\.\/assets\.html\?asset_id=/);
  store.destroy();
}


async function testScopedApiAndMutationSerialization() {
  const calls = [];
  const api = apiModule.createApi({
    boardId: 'shared-board',
    client: {
      json(url, options) {
        calls.push({ url, options });
        return Promise.resolve({ project_id: 'project-d5' });
      },
    },
  });
  await api.load('project-d5');
  await api.preview(
    { project_id: 'project-d5', revision: 19, assembly_revision: 4 },
    'd5-preview-stable',
  );
  await api.audioAssets('project-d5');
  await api.soundDesign('project-d5');
  await api.analyzeSoundDesign({
    project_id: 'project-d5', revision: 19,
  });
  await api.generateSoundEffects({
    project_id: 'project-d5',
    revision: 19,
    assembly_revision: 4,
    quote_token: 'quote-1',
  }, 'sound-effect-stable');
  assert.equal(calls[0].options.headers['X-Canvas-Board-Id'], 'shared-board');
  assert.equal(calls[1].options.headers['X-Canvas-Board-Id'], 'shared-board');
  assert.match(calls[1].url, /short-drama\/playback/);
  assert.equal(calls[2].options.headers['Idempotency-Key'], 'd5-preview-stable');
  assert.equal(
    calls[3].url,
    '/api/gen/short-drama/assembly/audio-assets?project_id=project-d5&limit=120',
  );
  assert.equal(
    calls[3].options.headers['X-Canvas-Board-Id'],
    'shared-board',
  );
  assert.equal(
    calls[4].url,
    '/api/gen/short-drama/sound-design?project_id=project-d5',
  );
  assert.equal(
    calls[5].url,
    '/api/gen/short-drama/sound-design/analyze',
  );
  assert.equal(
    calls[6].options.headers['Idempotency-Key'],
    'sound-effect-stable',
  );

  const coordinator = apiModule.createMutationCoordinator();
  const order = [];
  const first = coordinator.run('first', async () => {
    order.push('first:start');
    await new Promise((resolve) => setImmediate(resolve));
    order.push('first:end');
  });
  const second = coordinator.run('second', async () => {
    order.push('second:start');
  });
  await Promise.all([first, second]);
  assert.deepEqual(order, ['first:start', 'first:end', 'second:start']);
  coordinator.destroy();
  api.destroy();
}


function testPollerHasOneTimerAndStops() {
  const timers = new Map();
  let nextId = 1;
  const poller = pollerModule.createPoller({
    poll: () => Promise.resolve({ terminal: false }),
    setTimeout(handler, delay) {
      const id = nextId++;
      timers.set(id, { handler, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    document: null,
  });
  poller.start(false);
  assert.equal(timers.size, 1);
  poller.start(false);
  assert.equal(timers.size, 1, 'restarting must replace the prior timer');
  poller.stop();
  assert.equal(timers.size, 0);
  poller.destroy();
}


function testPlayerErrorsAndDraftValidation() {
  assert.equal(playerModule.classifyMediaError({ status: 410 }).code, 'expired');
  assert.equal(playerModule.classifyMediaError({ status: 403 }).code, 'forbidden');
  const draft = formsModule.createDraft({
    bgm: { volume: 0.18 },
  }, 19, 'preview-2');
  assert.equal(draft.dirty(), false);
  draft.set('bgm.volume', 2);
  assert.equal(draft.dirty(), true);
  assert.equal(draft.validate()['bgm.volume'], '背景音乐音量必须在 0 到 1 之间');
}


function testPlaybackBundlePreservesSucceededPreviewStatus() {
  const version = workspaceModule.playerVersion({
    id: 'preview-2',
    kind: 'preview',
    version: 2,
    status: 'succeeded',
    url: '/api/gen/file/preview-2.mp4',
  }, {
    playback: {
      current_version: {
        id: 'playback-3',
        source_version_id: 'preview-2',
        status: 'ready',
        media_url: '/api/gen/file/playback-3.mp4',
        subtitle_url: '/api/gen/file/playback-3.vtt',
      },
    },
  });
  assert.equal(version.id, 'preview-2');
  assert.equal(version.status, 'succeeded');
  assert.equal(version.playback_version_id, 'playback-3');
  assert.equal(version.playback_status, 'ready');
  assert.equal(version.url, '/api/gen/file/playback-3.mp4');
  assert.equal(version.subtitle_url, '/api/gen/file/playback-3.vtt');
}


async function testWorkspaceLifecycleAndPreviewIdempotency() {
  const host = fakeHost();
  const calls = [];
  const ready = snapshot({
    readiness: { ready: true, blockers: [] },
    shots: [snapshot().shots[0]],
    actions: {
      can_save_config: false,
      can_preview: true,
      can_lock_preview: false,
      can_export: false,
      can_confirm: false,
    },
  });
  const workspace = workspaceModule.createWorkspace(Object.assign({
    projectId: 'project-d5',
    boardId: 'board-1',
    project: { point_budget: 100, spent_points: 9, reserved_points: 0 },
    host,
    storage: null,
    document: null,
    client: {
      json(url, options) {
        calls.push({ url, options });
        if (url.endsWith('/preview')) {
          return Promise.resolve({ job_id: 'preview-job', status: 'queued' });
        }
        return Promise.resolve(ready);
      },
    },
  }, dependencies()));
  await workspace.ready;
  assert.match(host.innerHTML, /D-5 完整交互/);
  assert.equal(typeof host.listener('click'), 'function');
  assert.equal(typeof host.listener('change'), 'function');
  assert.equal(typeof host.listener('keydown'), 'function');
  host.listener('click')({
    target: {
      getAttribute(name) {
        return name === 'data-action' ? 'generate-preview' : null;
      },
      parentNode: host,
    },
  });
  await flushWorkspace();
  host.listener('click')({
    target: {
      getAttribute(name) {
        return name === 'data-action' ? 'generate-preview' : null;
      },
      parentNode: host,
    },
  });
  await flushWorkspace();
  const submits = calls.filter((call) => call.url.endsWith('/preview'));
  assert.equal(submits.length, 2);
  assert.match(submits[0].options.headers['Idempotency-Key'], /^d5-preview-/);
  assert.notEqual(
    submits[0].options.headers['Idempotency-Key'],
    submits[1].options.headers['Idempotency-Key'],
  );
  assert.equal(submits[0].options.headers['X-Canvas-Board-Id'], 'board-1');
  assert.deepEqual(submits[0].options.body, {
    project_id: 'project-d5',
    revision: 19,
    assembly_revision: 4,
  });
  workspace.destroy();
  assert.equal(host.listener('click'), undefined);
  assert.equal(host.listener('change'), undefined);
  assert.equal(host.listener('keydown'), undefined);
}

async function testSoundDraftSurvivesRerenderingActions() {
  const host = fakeHost();
  const audioUrls = [];
  const panes = {
    '.nc-sdw-left': { scrollTop: 0, scrollLeft: 0 },
    '.nc-sdw-main': { scrollTop: 0, scrollLeft: 0 },
    '.nc-sdw-right': { scrollTop: 0, scrollLeft: 0 },
  };
  let renderedHtml = '';
  Object.defineProperty(host, 'innerHTML', {
    get() { return renderedHtml; },
    set(value) {
      renderedHtml = value;
      Object.values(panes).forEach((pane) => {
        pane.scrollTop = 0;
        pane.scrollLeft = 0;
      });
    },
  });
  const bgmAsset = {
    value: '7',
    getAttribute(name) { return name === 'data-bgm-asset' ? '' : null; },
  };
  const bgmVolume = {
    value: '0.73',
    getAttribute(name) { return name === 'data-bgm-volume' ? '' : null; },
  };
  host.querySelector = (selector) => ({
    '[data-bgm-asset]': bgmAsset,
    '[data-bgm-volume]': bgmVolume,
    ...panes,
  }[selector] || null);
  host.querySelectorAll = () => [];
  const editable = snapshot({
    config: {
      subtitle: { enabled: true, preset: 'white_outline', position: 'bottom' },
      bgm: { asset_id: null, volume: 0.18, fade_in_ms: 500, fade_out_ms: 800 },
      sound_cues: [],
    },
    actions: {
      can_save_config: true,
      can_preview: false,
      can_lock_preview: false,
      can_export: false,
      can_confirm: false,
    },
  });
  const workspace = workspaceModule.createWorkspace(Object.assign({
    projectId: 'project-d5',
    boardId: 'board-1',
    host,
    storage: null,
    document: null,
    client: {
      json(url) {
        if (url.includes('/assembly/audio-assets?')) {
          audioUrls.push(url);
          return Promise.resolve({ items: [{ id: 7, name: '脚步声' }] });
        }
        return Promise.resolve(editable);
      },
    },
  }, dependencies()));
  await workspace.ready;
  panes['.nc-sdw-left'].scrollTop = 41;
  panes['.nc-sdw-main'].scrollTop = 860;
  panes['.nc-sdw-main'].scrollLeft = 17;
  panes['.nc-sdw-right'].scrollTop = 93;
  host.listener('click')({
    target: {
      getAttribute(name) {
        return name === 'data-action' ? 'add-sound-cue' : null;
      },
      parentNode: host,
    },
  });
  let draft = workspace.getState().ui.configDraft;
  assert.equal(draft.bgm.asset_id, 7);
  assert.equal(draft.bgm.volume, 0.73);
  assert.equal(draft.sound_cues.length, 1);
  await flushWorkspace();
  assert.equal(panes['.nc-sdw-left'].scrollTop, 41);
  assert.equal(panes['.nc-sdw-main'].scrollTop, 860);
  assert.equal(panes['.nc-sdw-main'].scrollLeft, 17);
  assert.equal(panes['.nc-sdw-right'].scrollTop, 93);
  bgmVolume.value = '0.41';
  host.listener('click')({
    target: {
      getAttribute(name) {
        return name === 'data-action' ? 'refresh-audio-assets' : null;
      },
      parentNode: host,
    },
  });
  assert.equal(workspace.getState().ui.configDraft.bgm.volume, 0.41);
  bgmVolume.value = '0.62';
  host.listener('change')({ target: bgmVolume });
  assert.equal(workspace.getState().ui.configDraft.bgm.volume, 0.62);
  await flushWorkspace();
  assert.deepEqual(audioUrls, [
    '/api/gen/short-drama/assembly/audio-assets?project_id=project-d5&limit=120',
    '/api/gen/short-drama/assembly/audio-assets?project_id=project-d5&limit=120',
  ]);
  workspace.destroy();
  assert.equal(host.listener('change'), undefined);
}


async function testSuccessfulExportRotatesIdempotencyKey() {
  const host = fakeHost();
  const calls = [];
  const ready = snapshot({
    actions: {
      can_save_config: false,
      can_preview: false,
      can_lock_preview: false,
      can_export: true,
      can_confirm: false,
    },
  });
  const workspace = workspaceModule.createWorkspace(Object.assign({
    projectId: 'project-d5',
    boardId: 'board-1',
    host,
    storage: null,
    document: null,
    confirm: () => true,
    client: {
      json(url, options) {
        calls.push({ url, options });
        if (url.endsWith('/final-quote')) {
          return Promise.resolve({
            can_submit: true,
            quote_token: `quote-${calls.length}`,
            total_cost: 5,
          });
        }
        if (url.endsWith('/export')) {
          return Promise.resolve({ job_id: `final-job-${calls.length}` });
        }
        return Promise.resolve(ready);
      },
    },
  }, dependencies()));
  await workspace.ready;
  for (let index = 0; index < 2; index += 1) {
    host.listener('click')({
      target: {
        getAttribute(name) {
          return name === 'data-action' ? 'export-final' : null;
        },
        parentNode: host,
      },
    });
    await flushWorkspace();
  }
  const exports = calls.filter((call) => call.url.endsWith('/export'));
  assert.equal(exports.length, 2);
  assert.match(exports[0].options.headers['Idempotency-Key'], /^d5-export-/);
  assert.notEqual(
    exports[0].options.headers['Idempotency-Key'],
    exports[1].options.headers['Idempotency-Key'],
  );
  workspace.destroy();
}

async function testUncertainPreviewRetryKeepsIdempotencyKey() {
  const host = fakeHost();
  const calls = [];
  let previewCount = 0;
  const ready = snapshot({
    readiness: { ready: true, blockers: [] },
    shots: [snapshot().shots[0]],
    actions: {
      can_save_config: false,
      can_preview: true,
      can_lock_preview: false,
      can_export: false,
      can_confirm: false,
    },
  });
  const workspace = workspaceModule.createWorkspace(Object.assign({
    projectId: 'project-d5',
    boardId: 'board-1',
    host,
    storage: null,
    document: null,
    client: {
      json(url, options) {
        calls.push({ url, options });
        if (url.endsWith('/preview')) {
          previewCount += 1;
          if (previewCount === 1) {
            return Promise.reject(new Error('response lost'));
          }
          return Promise.resolve({ job_id: 'preview-job', status: 'queued' });
        }
        return Promise.resolve(ready);
      },
    },
  }, dependencies()));
  await workspace.ready;
  for (let index = 0; index < 2; index += 1) {
    host.listener('click')({
      target: {
        getAttribute(name) {
          return name === 'data-action' ? 'generate-preview' : null;
        },
        parentNode: host,
      },
    });
    await flushWorkspace();
  }
  const submits = calls.filter((call) => call.url.endsWith('/preview'));
  assert.equal(submits.length, 2);
  assert.equal(
    submits[0].options.headers['Idempotency-Key'],
    submits[1].options.headers['Idempotency-Key'],
  );
  workspace.destroy();
}


function testAssetsAreStampedAndOrdered() {
  const html = fs.readFileSync(
    path.join(__dirname, '../site/workbench/canvas.html'), 'utf8',
  );
  const canvasSource = fs.readFileSync(
    path.join(
      __dirname,
      '../site/workbench/canvas/canvas-short-drama.js',
    ),
    'utf8',
  );
  const workspaceCss = fs.readFileSync(
    path.join(
      __dirname,
      '../site/workbench/canvas/canvas-short-drama-workspace.css',
    ),
    'utf8',
  );
  const names = [
    'canvas-short-drama-store.js',
    'canvas-short-drama-api.js',
    'canvas-short-drama-poller.js',
    'canvas-short-drama-player.js',
    'canvas-short-drama-versions.js',
    'canvas-short-drama-locks.js',
    'canvas-short-drama-forms.js',
    'canvas-short-drama-completion.js',
    'canvas-short-drama-workspace.js',
    'canvas-short-drama-workspace.css',
  ];
  for (const name of names) {
    assert.match(html, new RegExp(`${name.replace('.', '\\.')}\\?v=[0-9a-f]{8}`));
  }
  assert.ok(
    html.indexOf('canvas-short-drama-assembly.js') <
      html.indexOf('canvas-short-drama-workspace.js'),
    'D-3/D-4 adapter must load before the D-5 orchestrator',
  );
  assert.match(
    canvasSource,
    /shortDramaWorkspace\s*\|\|\s*root\.HQCanvas\.shortDramaAssembly/,
    'the canvas must prefer D-5 while retaining the D-3/D-4 fallback',
  );
  assert.match(
    canvasSource,
    /project:\s*cloneValue\(project\)/,
    'the D-5 budget summary must receive the current project snapshot',
  );
  assert.match(workspaceCss, /@media\(max-width:1180px\)/);
  assert.match(workspaceCss, /@media\(max-width:900px\)/);
  assert.match(workspaceCss, /@media\(prefers-reduced-motion:reduce\)/);
  assert.match(workspaceCss, /:focus-visible/);
}

function extractInlineFunction(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  assert.notEqual(start, -1, `${name} must exist in assets.html`);
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`unable to extract ${name}`);
}


function testFinalAssetsStayOutOfGenericBulkSelection() {
  const source = fs.readFileSync(
    path.join(__dirname, '../site/workbench/assets.html'),
    'utf8',
  );
  const names = [
    'assetUrl',
    'assetId',
    'assetKey',
    'isBulkEligible',
    'bulkAssetMeta',
    'setVisible',
    'selectedAssets',
    'selectedCount',
    'selectVisible',
    'downloadSelected',
    'deleteSelected',
  ];
  const requests = [];
  const context = {
    selected: {},
    visibleItems: [],
    updateBulk() {},
    grid: { querySelectorAll() { return []; } },
    bulkDownload: { disabled: false, textContent: '' },
    bulkDelete: { disabled: false, textContent: '' },
    tok: 'test-token',
    confirm() { return true; },
    toast() {},
    fetch(url, options) {
      requests.push({ url, options });
      return new Promise(() => {});
    },
  };
  vm.createContext(context);
  vm.runInContext(
    names.map((name) => extractInlineFunction(source, name)).join('\n'),
    context,
  );
  const finalAsset = {
    id: 'final-asset-1',
    source_type: 'short_drama_final',
    video_url: '/final.mp4',
  };
  const genericVideo = {
    id: 'video-1',
    source_type: 'video',
    video_url: '/video.mp4',
  };
  const displayed = context.setVisible([finalAsset, genericVideo], 'video');
  assert.equal(displayed.length, 2, 'the final asset must remain visible');
  assert.deepEqual(
    Array.from(context.visibleItems, (item) => item.id),
    ['video-1'],
    'only ordinary videos may enter bulk selection',
  );
  assert.equal(context.bulkAssetMeta(finalAsset, 'video'), null);
  assert.equal(context.isBulkEligible(finalAsset, 'video'), false);
  assert.equal(context.isBulkEligible(genericVideo, 'video'), true);
  context.selectVisible(true);
  assert.equal(
    Object.keys(context.selected).length,
    1,
    'select all must select only one bulk-eligible asset',
  );
  assert.equal(
    Object.values(context.selected)[0].id,
    genericVideo.id,
    'select all must skip the final asset',
  );
  context.selected.protected = {
    key: 'protected',
    kind: 'video',
    id: finalAsset.id,
    source_type: finalAsset.source_type,
  };
  assert.deepEqual(
    Array.from(context.selectedAssets(), (item) => `${item.kind}:${item.id}`),
    ['video:video-1'],
    'bulk request payloads must exclude final assets defensively',
  );
  assert.equal(context.selectedCount(), 1);
  context.downloadSelected();
  context.deleteSelected();
  assert.deepEqual(
    requests.map(({ url, options }) => ({
      url,
      assets: JSON.parse(options.body).assets,
    })),
    [
      {
        url: '/api/gen/asset/batch-download',
        assets: [{ kind: 'video', id: 'video-1' }],
      },
      {
        url: '/api/gen/asset/batch-delete',
        assets: [{ kind: 'video', id: 'video-1' }],
      },
    ],
    'bulk download and delete requests must contain only ordinary videos',
  );
}


async function main() {
  testStoreNormalizesAndKeepsHistoryReadOnly();
  testFailedPreviewDoesNotLockTheCurrentWorkspace();
  testThreeColumnRenderAndPermissionGates();
  testFinalAssetUsesCanonicalDeepLink();
  await testScopedApiAndMutationSerialization();
  testPollerHasOneTimerAndStops();
  testPlayerErrorsAndDraftValidation();
  testPlaybackBundlePreservesSucceededPreviewStatus();
  await testWorkspaceLifecycleAndPreviewIdempotency();
  await testSoundDraftSurvivesRerenderingActions();
  await testSuccessfulExportRotatesIdempotencyKey();
  await testUncertainPreviewRetryKeepsIdempotencyKey();
  testAssetsAreStampedAndOrdered();
  testFinalAssetsStayOutOfGenericBulkSelection();
  console.log('short drama D-5 workspace tests passed');
}


main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
