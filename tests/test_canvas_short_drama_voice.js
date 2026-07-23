const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const voice = require('../site/workbench/canvas/canvas-short-drama-voice.js');

function snapshot(overrides = {}) {
  return Object.assign({
    project_id: 'project-1', revision: 8, stage: 'voice_review',
    ratio: '9:16', target_duration: 30,
    point_budget: 100, spent_points: 12, reserved_points: 0,
    shots: [
      {
        id: 'shot-1', shot_key: '第一镜', sort_order: 0, duration: 5,
        locked: false, timeline_revision: 1, status: 'pending',
        lines: [{
          id: 'voice-1', dialogue_line_id: 'line-1', line_type: 'narration',
          sort_order: 0, character_key: 'detective',
          character_name: '林<script>探长', source_text: '谁在那里？',
          speech_text: '谁在那里？', subtitle_text: '<b>谁在那里？</b>',
          subtitle_visible: true, voice_key: 'longwan',
          speed: 1.2, pitch: 1, volume: 4,
          current_version: null, start_ms: null, end_ms: null,
          versions: [], job: null,
        }, {
          id: 'voice-2', dialogue_line_id: 'line-2', line_type: 'dialogue',
          sort_order: 1, character_key: 'narrator', character_name: '旁白',
          source_text: '夜幕降临。', speech_text: '夜幕降临。', subtitle_text: '夜幕降临。',
          subtitle_visible: true, voice_key: 'longcheng',
          speed: 1, pitch: 0, volume: 0,
          current_version: null, start_ms: null, end_ms: null,
          versions: [], job: null,
        }],
      },
      {
        id: 'shot-2', shot_key: '第二镜', sort_order: 1, duration: 5,
        locked: false, timeline_revision: 1, status: 'silent', lines: [],
      },
    ],
  }, overrides);
}

const voices = [
  { voice_key: 'longwan', display_name: '龙婉', preview_url: '/voice.mp3' },
  { voice_key: 'longcheng', display_name: '龙城', preview_url: '' },
];

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function fakeHost() {
  const listeners = new Map();
  const added = [];
  const removed = [];
  const host = {
    innerHTML: '',
    added,
    removed,
    addEventListener(type, handler) {
      added.push({ type, handler });
      listeners.set(type, handler);
    },
    removeEventListener(type, handler) {
      removed.push({ type, handler });
      if (listeners.get(type) === handler) listeners.delete(type);
    },
    dispatchShot(shotId) {
      const handler = listeners.get('click');
      if (!handler) return false;
      const button = {
        parentNode: host,
        getAttribute(name) { return name === 'data-shot-id' ? shotId : null; },
      };
      handler({ target: { parentNode: button } });
      return true;
    },
  };
  return host;
}

function testNormalizeRenderAndReadonlyContract() {
  assert.deepEqual(
    Object.keys(voice).sort(),
    ['createWorkspace', 'normalizeState', 'renderWorkspace']
  );
  const normalized = voice.normalizeState(snapshot(), voices, {});
  assert.equal(normalized.selectedShotId, 'shot-1');
  assert.equal(normalized.shots[0].lines[0].voice_name, '龙婉');
  assert.equal(normalized.shots[0].lines[0].line_type, 'dialogue',
    'non-narrator character keys stay dialogue even when line_type disagrees');
  assert.equal(normalized.shots[0].lines[1].line_type, 'narration',
    'narrator character keys force narration even when line_type disagrees');

  const html = voice.renderWorkspace(snapshot(), { voices });
  assert.match(html, /镜头列表[\s\S]*台词与字幕[\s\S]*配音控制台/);
  assert.match(html, /第一镜[\s\S]*第二镜/);
  assert.match(html, /待配音/);
  assert.match(html, /静音/);
  assert.doesNotMatch(html, /\bpending\b|\bsilent\b/);
  assert.match(html, /龙婉/);
  assert.match(html, /谁在那里？/);
  assert.doesNotMatch(html, /<script>|<b>/);
  assert.match(html, /林&lt;script&gt;探长/);
  assert.match(html, /&lt;b&gt;谁在那里？&lt;\/b&gt;/);
  assert.match(html, /data-action="generate-line"[^>]*disabled/);
  assert.match(html, /data-action="save-timeline"[^>]*disabled/);
  assert.doesNotMatch(html, /data-action="(?:lock|confirm|advance)/);
}

function testRendererDistinguishesLoadingErrorEmptyPendingAndSilent() {
  const loading = voice.renderWorkspace({}, { busy: true });
  assert.match(loading, /data-state="loading"[\s\S]*正在加载配音数据/);
  assert.doesNotMatch(loading, /当前镜头没有台词|暂无镜头/);

  const loadError = voice.renderWorkspace({}, { error: '<load failed>' });
  assert.match(loadError, /data-state="error"[\s\S]*配音数据加载失败/);
  assert.match(loadError, /&lt;load failed&gt;/);
  assert.doesNotMatch(loadError, /当前镜头没有台词|暂无镜头/);

  const empty = voice.renderWorkspace({ shots: [] }, {});
  assert.match(empty, /data-state="empty"[\s\S]*暂无镜头/);
  assert.doesNotMatch(empty, /当前镜头没有台词|正在加载|加载失败/);

  const pending = voice.renderWorkspace(snapshot({
    shots: [{ id: 'pending-shot', shot_key: '待定镜头', sort_order: 0,
      duration: 1, status: 'pending', lines: [] }],
  }), {});
  assert.match(pending, /data-state="pending"[\s\S]*台词尚未就绪/);
  assert.doesNotMatch(pending, /静音镜头/);

  const silent = voice.renderWorkspace(snapshot(), { voices, selectedShotId: 'shot-2' });
  assert.match(silent, /data-state="silent"[\s\S]*当前镜头为静音镜头/);
}

function testRendererEscapesAttributesErrorsAndVoiceFallbacks() {
  const malicious = snapshot({
    shots: [{
      id: 'shot-" autofocus onfocus="boom',
      shot_key: '<script>镜头</script>', sort_order: 0, duration: 1,
      status: '<img src=x onerror=boom>', lines: [{
        id: 'line-1', sort_order: 0, character_key: '<character>',
        character_name: '<img src=x onerror=boom>',
        speech_text: '<svg onload=boom>', subtitle_text: '<iframe>字幕</iframe>',
        voice_key: '<em>fallback voice</em>', speed: 1, pitch: 0, volume: 0,
      }],
    }],
  });
  const html = voice.renderWorkspace(malicious, { voices: [] });
  assert.match(html, /data-shot-id="shot-&quot; autofocus onfocus=&quot;boom"/);
  assert.match(html, /&lt;script&gt;镜头&lt;\/script&gt;/);
  assert.match(html, /&lt;img src=x onerror=boom&gt;/);
  assert.match(html, /&lt;svg onload=boom&gt;/);
  assert.match(html, /&lt;iframe&gt;字幕&lt;\/iframe&gt;/);
  assert.match(html, /&lt;em&gt;fallback voice&lt;\/em&gt;/);
  assert.match(html, /状态未知/);
  assert.doesNotMatch(html, /<script>|<img|<svg|<iframe|<em>|onfocus="boom/);
}

function testPrototypeNamedVoiceAndStatusKeysUseNormalFallbacks() {
  for (const key of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
    const state = snapshot({
      shots: [{
        id: `shot-${key}`, shot_key: `镜头 ${key}`, sort_order: 0, duration: 1,
        status: key, lines: [{
          id: `line-${key}`, sort_order: 0, character_key: 'detective',
          character_name: '侦探', speech_text: '台词', subtitle_text: '字幕',
          voice_key: key, speed: 1, pitch: 0, volume: 0,
        }],
      }],
    });
    const normalized = voice.normalizeState(state, [], {});
    assert.equal(normalized.shots[0].lines[0].voice_name, key,
      `${key} must use the unknown-catalog voice fallback`);
    const html = voice.renderWorkspace(state, { voices: [] });
    assert.match(html, /状态未知/,
      `${key} must use the unknown status fallback`);
    assert.doesNotMatch(html, /function Object|native code/,
      `${key} must not resolve through Object.prototype`);
  }
}

function testNarrationRendersAnExplicitEscapedBadge() {
  const state = snapshot();
  state.shots[0].lines[1].character_name = '画外讲述者<script>';
  state.shots[0].lines[1].subtitle_text = '<b>夜幕降临。</b>';
  const html = voice.renderWorkspace(state, { voices });

  assert.equal((html.match(/旁白\/叙述/g) || []).length, 1,
    'only the narration line renders the explicit narration badge');
  assert.match(html, /class="nc-sdv-line-type"[^>]*>旁白\/叙述<\/span>/);
  assert.match(html, /画外讲述者&lt;script&gt;/);
  assert.match(html, /&lt;b&gt;夜幕降临。&lt;\/b&gt;/);
  assert.doesNotMatch(html, /<script>|<b>/);
}

function testBrowserUmdExport() {
  const filename = path.join(
    __dirname, '../site/workbench/canvas/canvas-short-drama-voice.js'
  );
  const context = {};
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), context, { filename });
  assert.ok(context.HQCanvas);
  assert.deepEqual(
    Array.from(Object.keys(context.HQCanvas.shortDramaVoice)).sort(),
    ['createWorkspace', 'normalizeState', 'renderWorkspace']
  );
}

async function testWorkspaceLoadsResourcesWithBoardHeaderAndExposesState() {
  const calls = [];
  const projectId = 'project /<one>';
  const client = {
    json(route, requestOptions) {
      calls.push({ route, requestOptions });
      if (route.startsWith('/api/gen/short-drama/voice?')) return Promise.resolve(snapshot());
      if (route === '/api/gen/audio/voices') return Promise.resolve({ items: voices });
      throw new Error(`unexpected route ${route}`);
    },
  };
  const workspace = voice.createWorkspace({
    projectId, boardId: 'board-7', client, document: null,
  });
  assert.equal(workspace.getState().busy, true);
  assert.equal(workspace.getState().destroyed, false);
  await workspace.ready;
  assert.deepEqual(calls.map((call) => call.route), [
    '/api/gen/short-drama/voice?project_id=project%20%2F%3Cone%3E',
    '/api/gen/audio/voices',
  ]);
  assert.deepEqual(calls.map((call) => call.requestOptions), [
    { headers: { 'X-Canvas-Board-Id': 'board-7' } },
    { headers: { 'X-Canvas-Board-Id': 'board-7' } },
  ]);
  const state = workspace.getState();
  assert.equal(state.project_id, 'project-1');
  assert.equal(state.busy, false);
  assert.equal(state.error, '');
  assert.equal(state.shots[0].lines[0].voice_name, '龙婉');
  assert.equal(workspace.selectShot('shot-2'), true);
  assert.match(workspace.render(), /当前镜头为静音镜头/);
  assert.equal(workspace.selectShot('missing'), false);
  workspace.destroy();
  assert.equal(await workspace.reload(), null);
  assert.equal(workspace.getState().destroyed, true);
  assert.equal(workspace.getState().busy, false);
}

async function testLatestReloadWinsOverOlderSuccessAndError() {
  const requests = Array.from({ length: 8 }, deferred);
  let requestIndex = 0;
  const workspace = voice.createWorkspace({
    projectId: 'project-1', document: null,
    client: { json() { return requests[requestIndex++].promise; } },
  });
  const second = workspace.reload();
  requests[2].resolve(snapshot({ project_id: 'newer-success' }));
  requests[3].resolve({ items: [
    { voice_key: 'longwan', display_name: '新音色', preview_url: '' },
  ] });
  assert.equal((await second).project_id, 'newer-success');
  assert.equal(workspace.getState().project_id, 'newer-success');
  assert.equal(workspace.getState().shots[0].lines[0].voice_name, '新音色');

  requests[0].resolve(snapshot({ project_id: 'older-success' }));
  requests[1].resolve({ items: voices });
  assert.equal(await workspace.ready, null);
  assert.equal(workspace.getState().project_id, 'newer-success',
    'an older success cannot replace the newest state');

  const olderFailure = workspace.reload();
  const newest = workspace.reload();
  requests[6].resolve(snapshot({ project_id: 'newest-success' }));
  requests[7].resolve({ items: [
    { voice_key: 'longwan', display_name: '最终音色', preview_url: '' },
  ] });
  await newest;
  requests[4].reject(new Error('older failure must be ignored'));
  requests[5].resolve({ items: voices });
  assert.equal(await olderFailure, null);
  assert.equal(requestIndex, 8);
  assert.equal(workspace.getState().project_id, 'newest-success');
  assert.equal(workspace.getState().shots[0].lines[0].voice_name, '最终音色');
  assert.equal(workspace.getState().error, '',
    'an older error cannot replace the newest successful state');
  workspace.destroy();
}

async function testLoadErrorRendersOnlyEscapedErrorState() {
  const host = fakeHost();
  const workspace = voice.createWorkspace({
    projectId: 'project-1', host,
    client: { json(route) {
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        throw new Error('<img src=x onerror=boom>');
      }
      return Promise.resolve({ items: voices });
    } },
  });
  assert.match(host.innerHTML, /data-state="loading"/);
  assert.equal(await workspace.ready, null);
  const state = workspace.getState();
  assert.equal(state.busy, false);
  assert.equal(state.error, '<img src=x onerror=boom>');
  assert.match(host.innerHTML, /data-state="error"/);
  assert.match(host.innerHTML, /&lt;img src=x onerror=boom&gt;/);
  assert.doesNotMatch(host.innerHTML, /<img|当前镜头没有台词|暂无镜头/);
  workspace.destroy();
}

async function testHostSelectionAndHandlerRemoval() {
  const host = fakeHost();
  const workspace = voice.createWorkspace({
    projectId: 'project-1', host,
    client: { json(route) {
      if (route.startsWith('/api/gen/short-drama/voice?')) return Promise.resolve(snapshot());
      return Promise.resolve({ items: voices });
    } },
  });
  await workspace.ready;
  assert.equal(host.added.length, 1);
  assert.equal(host.dispatchShot('shot-2'), true);
  assert.equal(workspace.getState().selectedShotId, 'shot-2');
  assert.match(host.innerHTML, /当前镜头为静音镜头/);
  workspace.destroy();
  assert.equal(host.removed.length, 1);
  assert.equal(host.removed[0].type, 'click');
  assert.equal(host.removed[0].handler, host.added[0].handler);
  const htmlAfterDestroy = host.innerHTML;
  assert.equal(host.dispatchShot('shot-1'), false);
  assert.equal(host.innerHTML, htmlAfterDestroy);
  assert.equal(workspace.getState().destroyed, true);
  assert.equal(workspace.getState().busy, false);
}

async function testDestroyInvalidatesPendingRequestsWithoutHostOrStateMutation() {
  const host = fakeHost();
  const voiceRequest = deferred();
  const catalogRequest = deferred();
  let requestCalls = 0;
  const workspace = voice.createWorkspace({
    projectId: 'project-1', host,
    client: { json(route) {
      requestCalls += 1;
      return route.startsWith('/api/gen/short-drama/voice?') ?
        voiceRequest.promise : catalogRequest.promise;
    } },
  });
  const loadingHtml = host.innerHTML;
  await Promise.resolve();
  assert.equal(requestCalls, 2, 'both requests are pending before destroy');
  workspace.destroy();
  const destroyedState = workspace.getState();
  assert.equal(destroyedState.destroyed, true);
  assert.equal(destroyedState.busy, false);
  voiceRequest.resolve(snapshot({ project_id: 'late-project' }));
  catalogRequest.resolve({ items: [
    { voice_key: 'longwan', display_name: '迟到音色', preview_url: '' },
  ] });
  assert.equal(await workspace.ready, null);
  assert.deepEqual(workspace.getState(), destroyedState);
  assert.equal(host.innerHTML, loadingHtml);
  assert.doesNotMatch(host.innerHTML, /late-project|迟到音色/);
  assert.equal(host.removed.length, 1);
}

async function main() {
  testNormalizeRenderAndReadonlyContract();
  testRendererDistinguishesLoadingErrorEmptyPendingAndSilent();
  testRendererEscapesAttributesErrorsAndVoiceFallbacks();
  testPrototypeNamedVoiceAndStatusKeysUseNormalFallbacks();
  testNarrationRendersAnExplicitEscapedBadge();
  testBrowserUmdExport();
  await testWorkspaceLoadsResourcesWithBoardHeaderAndExposesState();
  await testLatestReloadWinsOverOlderSuccessAndError();
  await testLoadErrorRendersOnlyEscapedErrorState();
  await testHostSelectionAndHandlerRemoval();
  await testDestroyInvalidatesPendingRequestsWithoutHostOrStateMutation();
  const css = fs.readFileSync(path.join(
    __dirname, '../site/workbench/canvas/canvas-short-drama-voice.css'
  ), 'utf8');
  assert.match(css, /grid-template-columns:\s*260px\s+minmax\(0,\s*1fr\)\s+300px/);
  console.log('canvas short drama voice: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
