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

function fakeStorage() {
  const values = new Map();
  return {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
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

function c2Snapshot() {
  const state = JSON.parse(JSON.stringify(snapshot()));
  state.unlocked_shot_count = 2;
  state.handoff_blocked = true;
  state.handoff_blockers = [{
    code: 'missing_locked_voice_shot', message: '仍有镜头尚未锁定',
    shot_id: 'shot-1',
  }];
  state.shots[0].status = 'ready';
  state.shots[0].lockable = true;
  state.shots[0].lock_blockers = [];
  state.shots[0].lines.forEach((line, index) => {
    const duration = index ? 1200 : 1000;
    const start = index ? 1150 : 0;
    line.current_version = 1;
    line.start_ms = start;
    line.end_ms = start + duration;
    line.suggested_start_ms = start;
    line.suggested_end_ms = start + duration;
    line.input_hash = `hash-${index}`;
    line.versions = [{
      version: 1, status: 'done', duration_ms: duration,
      audio_url: `/voice-${index + 1}.mp3`, cost: 10,
      voice_key: line.voice_key, input_hash: line.input_hash,
      settings: { speed: line.speed, pitch: line.pitch, volume: line.volume },
    }];
  });
  state.shots[1].lockable = true;
  state.shots[1].lock_blockers = [];
  return state;
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
  assert.match(html, /镜头列表[\s\S]*台词与字幕[\s\S]*验收控制台/);
  assert.match(html, /第一镜[\s\S]*第二镜/);
  assert.match(html, /待配音/);
  assert.match(html, /静音/);
  assert.doesNotMatch(html, /\bpending\b|\bsilent\b/);
  assert.match(html, /龙婉/);
  assert.match(html, /谁在那里？/);
  assert.doesNotMatch(html, /<script>|<b>/);
  assert.match(html, /林&lt;script&gt;探长/);
  assert.match(html, /&lt;b&gt;谁在那里？&lt;\/b&gt;/);
  assert.match(html, /data-action="generate-line"[^>]*>生成配音/);
  assert.doesNotMatch(html, /data-action="generate-line"[^>]*disabled/);
  assert.match(html, /data-action="save-timeline"[^>]*disabled/);
  assert.match(html, /data-action="generate-shot"/);
  assert.match(html, /data-action="generate-all"/);
  assert.match(html, /data-action="set-shot-lock"/);
  assert.match(html, /data-action="confirm-voice-stage"/);

  const readonly = voice.renderWorkspace(snapshot(), { voices, canEdit: false });
  assert.match(readonly, /data-action="generate-line"[^>]*disabled/);
  assert.match(readonly, /data-action="generate-shot"[^>]*disabled/);
  assert.match(readonly, /data-field="subtitle_text"[^>]*disabled/);
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

function testDraftTimingShowsOverflowAndRecommendedSpeedBeforeSave() {
  const state = c2Snapshot();
  const shot = state.shots[0];
  shot.lines = [shot.lines[0]];
  const line = shot.lines[0];
  line.speed = 1;
  line.start_ms = 300;
  line.end_ms = 4850;
  line.versions[0].duration_ms = 5150;
  line.versions[0].settings.speed = 1;
  const html = voice.renderWorkspace(state, {
    voices, selectedShotId: shot.id, timelineDirty: true,
  });
  assert.match(html, /音频超出镜头 0\.45s/);
  assert.match(html, /音频结束于 5\.45s/);
  assert.match(html, /采用推荐语速 1\.15/);
  assert.match(html, /当前修改尚未保存[\s\S]*配音或字幕超过镜头时长/);
  assert.match(html, /data-action="save-timeline"[^>]*disabled/);
}

function testDraftSettingsChangesBlockTimelineSave() {
  [
    line => { line.speed += 0.1; },
    line => { line.pitch += 1; },
    line => { line.volume += 1; },
    line => { line.voice_key = line.voice_key === 'longwan'?'longcheng':'longwan'; },
  ].forEach(change => {
    const state = c2Snapshot();
    const shot = state.shots[0];
    shot.lines = [shot.lines[0]];
    const line = shot.lines[0];
    line.start_ms = 0;
    line.end_ms = 1000;
    line.versions[0].duration_ms = 1000;
    change(line);
    const html = voice.renderWorkspace(state, {
      voices, selectedShotId: shot.id, timelineDirty: true,
    });
    assert.match(html, /配音参数已修改，请重新生成后再保存时间轴/);
    assert.match(html, /当前配音版本与音色参数不一致，请重新生成配音/);
    assert.match(html, /data-action="save-timeline"[^>]*disabled/);
  });
}

function testSubtitleOnlyOverflowDoesNotRecommendVoiceSpeed() {
  const state = c2Snapshot();
  const shot = state.shots[0];
  shot.lines = [shot.lines[0]];
  const line = shot.lines[0];
  line.start_ms = 0;
  line.end_ms = 5200;
  line.speed = 0.5;
  line.versions[0].duration_ms = 1000;
  line.versions[0].settings.speed = 0.5;
  const html = voice.renderWorkspace(state, {
    voices, selectedShotId: shot.id, timelineDirty: true,
  });
  assert.match(html, /字幕超出镜头 0\.20s/);
  assert.match(html, /请调整字幕结束时间/);
  assert.doesNotMatch(html, /data-action="apply-recommended-speed"/);
  assert.match(html, /data-action="save-timeline"[^>]*disabled/);
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
  assert.equal(host.added.length, 6);
  assert.equal(host.dispatchShot('shot-2'), true);
  assert.equal(workspace.getState().selectedShotId, 'shot-2');
  assert.match(host.innerHTML, /当前镜头为静音镜头/);
  workspace.destroy();
  assert.equal(host.removed.length, 6);
  assert.equal(host.removed[0].type, 'click');
  assert.equal(host.removed[0].handler, host.added[0].handler);
  assert.equal(host.removed[1].type, 'change');
  assert.equal(host.removed[2].type, 'pointerdown');
  assert.equal(host.removed[5].type, 'play');
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
  assert.equal(host.removed.length, 6);
}

async function testQuoteConfirmGenerateReloadAndVersionSelection() {
  const calls = [];
  let voiceLoads = 0;
  let submitAttempts = 0;
  const after = snapshot();
  after.shots[0].lines[0].job = {
    job_id: 901, status: 'pending', error: '', refunded: 0,
  };
  const client = {
    json(route, options = {}) {
      calls.push({ route, options });
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        voiceLoads += 1;
        return Promise.resolve(voiceLoads > 1 ? after : snapshot());
      }
      if (route === '/api/gen/audio/voices') return Promise.resolve({ items: voices });
      if (route === '/api/gen/short-drama/voice-quote') {
        return Promise.resolve({
          project_id: 'project-1', revision: 8, total_cost: 10,
          items: [{ line_id: 'voice-1', quote_token: 'quote-1', cost: 10 }],
        });
      }
      if (route === '/api/gen/short-drama/generate-voice') {
        submitAttempts += 1;
        if (submitAttempts === 1) {
          const timeout = new Error('timeout');
          timeout.code = 'timeout';
          return Promise.reject(timeout);
        }
        return Promise.resolve({ job_id: 901, cost: 10, points_left: 90 });
      }
      if (route === '/api/gen/short-drama/select-voice-version') {
        return Promise.resolve({ project_id: 'project-1', line_id: 'voice-1',
          current_version: 1, revision: 9 });
      }
      throw new Error(`unexpected route ${route}`);
    },
  };
  const confirmations = [];
  const workspace = voice.createWorkspace({
    projectId: 'project-1', client, document: null, pollInterval: 60000,
    confirm(cost, quote, items) {
      confirmations.push({ cost, quote, items });
      return true;
    },
  });
  await workspace.ready;
  const generated = await workspace.generateLine('voice-1');
  assert.equal(generated.cancelled, false);
  assert.equal(confirmations.length, 1);
  assert.equal(confirmations[0].cost, 10);
  assert.equal(confirmations[0].quote.kind, 'voice');
  const quoteCall = calls.find((call) =>
    call.route === '/api/gen/short-drama/voice-quote');
  assert.deepEqual(quoteCall.options.body.items, [{
    line_id: 'voice-1', voice_key: 'longwan', speed: 1.2, pitch: 1, volume: 4,
  }]);
  const submit = calls.find((call) =>
    call.route === '/api/gen/short-drama/generate-voice');
  assert.match(submit.options.headers['Idempotency-Key'], /^sdv-/);
  assert.equal(submit.options.body.quote_token, 'quote-1');
  const submits = calls.filter((call) =>
    call.route === '/api/gen/short-drama/generate-voice');
  assert.equal(submits.length, 2);
  assert.equal(submits[0].options.headers['Idempotency-Key'],
    submits[1].options.headers['Idempotency-Key'],
    'an ambiguous timeout retries the exact same paid operation');
  assert.equal(workspace.getState().shots[0].lines[0].job.status, 'pending');

  after.shots[0].lines[0].versions = [{
    version: 1, status: 'done', audio_url: '/voice-1.mp3',
    duration_ms: 1200, cost: 10, voice_key: 'longwan',
    input_hash: after.shots[0].lines[0].input_hash,
  }];
  after.shots[0].lines[0].current_version = 1;
  after.shots[0].lines[0].job = { job_id: 901, status: 'done', error: '', refunded: 0 };
  const selected = await workspace.selectVersion('voice-1', 1);
  assert.equal(selected.revision, 9);
  assert.ok(calls.some((call) =>
    call.route === '/api/gen/short-drama/select-voice-version'));
  workspace.destroy();
}

async function testPreviewStopsPreviousAudioAndReadonlyRejectsWrites() {
  const players = [];
  const workspace = voice.createWorkspace({
    projectId: 'project-1', canEdit: false, document: null,
    client: { json(route) {
      if (route.startsWith('/api/gen/short-drama/voice?')) return Promise.resolve(snapshot());
      return Promise.resolve({ items: voices });
    } },
    audioFactory(url) {
      const player = { url, pauses: 0, plays: 0,
        play() { this.plays += 1; return Promise.resolve(); },
        pause() { this.pauses += 1; } };
      players.push(player);
      return player;
    },
  });
  await workspace.ready;
  assert.equal(workspace.preview('/one.mp3'), true);
  assert.equal(workspace.preview('/two.mp3'), true);
  assert.equal(players[0].pauses, 1);
  assert.equal(players[1].plays, 1);
  await assert.rejects(workspace.generateLine('voice-1'), /只读权限/);
  workspace.destroy();
  assert.equal(players[1].pauses, 1);
}

async function testAmbiguousVoiceSubmissionSurvivesTwoFailuresAndWorkspaceReload() {
  const storage = fakeStorage();
  const calls = [];
  let quoteCalls = 0;
  let submitCalls = 0;
  let confirmations = 0;
  const client = {
    json(route, options = {}) {
      calls.push({ route, options });
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        return Promise.resolve(snapshot());
      }
      if (route === '/api/gen/audio/voices') {
        return Promise.resolve({ items: voices });
      }
      if (route === '/api/gen/short-drama/voice-quote') {
        quoteCalls += 1;
        return Promise.resolve({
          project_id: 'project-1', revision: 8, total_cost: 10,
          items: [{ line_id: 'voice-1', quote_token: 'durable-quote-1', cost: 10 }],
        });
      }
      if (route === '/api/gen/short-drama/generate-voice') {
        submitCalls += 1;
        if (submitCalls === 1) {
          const timeout = new Error('timeout');
          timeout.code = 'timeout';
          return Promise.reject(timeout);
        }
        if (submitCalls === 2) {
          const unavailable = new Error('auth service unavailable');
          unavailable.status = 502;
          return Promise.reject(unavailable);
        }
        return Promise.resolve({ job_id: 902, cost: 10, points_left: 90 });
      }
      throw new Error(`unexpected route ${route}`);
    },
  };
  const firstWorkspace = voice.createWorkspace({
    projectId: 'project-1', client, storage, document: null,
    pollInterval: 60000,
    confirm() { confirmations += 1; return true; },
  });
  await firstWorkspace.ready;
  const firstResult = await firstWorkspace.generateLine('voice-1');
  assert.equal(firstResult.results[0].ok, false);
  firstWorkspace.destroy();

  const secondWorkspace = voice.createWorkspace({
    projectId: 'project-1', client, storage, document: null,
    pollInterval: 60000,
    confirm() { confirmations += 1; return true; },
  });
  await secondWorkspace.ready;
  const recovered = await secondWorkspace.generateLine('voice-1');
  assert.equal(recovered.results[0].ok, true);
  secondWorkspace.destroy();

  const submits = calls.filter((call) =>
    call.route === '/api/gen/short-drama/generate-voice');
  assert.equal(submits.length, 3);
  assert.equal(quoteCalls, 1, 'manual retry must not request a new quote');
  assert.equal(confirmations, 1, 'persisted confirmed operation must not reconfirm');
  assert.equal(new Set(submits.map((call) =>
    call.options.headers['Idempotency-Key'])).size, 1);
  assert.equal(new Set(submits.map((call) =>
    call.options.body.quote_token)).size, 1);
  assert.deepEqual(submits[0].options.body, submits[2].options.body);
}

async function testC2TimelineDraftSaveLockAndHandoff() {
  const calls = [];
  const changes = [];
  let current = c2Snapshot();
  const client = {
    json(route, options = {}) {
      calls.push({ route, options });
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        return Promise.resolve(JSON.parse(JSON.stringify(current)));
      }
      if (route === '/api/gen/audio/voices') {
        return Promise.resolve({ items: voices });
      }
      if (route === '/api/gen/short-drama/save-voice-timeline') {
        current = JSON.parse(JSON.stringify(current));
        current.revision += 1;
        current.shots[0].timeline_revision += 1;
        options.body.items.forEach((item) => {
          Object.assign(current.shots[0].lines.find((line) =>
            line.id === item.line_id), item);
        });
        return Promise.resolve(current);
      }
      if (route === '/api/gen/short-drama/set-voice-shot-lock') {
        current = JSON.parse(JSON.stringify(current));
        current.revision += 1;
        current.shots.find((shot) => shot.id === options.body.shot_id).locked =
          options.body.lock;
        return Promise.resolve(current);
      }
      if (route === '/api/gen/short-drama/confirm') {
        current = JSON.parse(JSON.stringify(current));
        current.revision += 1;
        current.stage = 'video_review';
        return Promise.resolve(current);
      }
      throw new Error(`unexpected route ${route}`);
    },
  };
  const workspace = voice.createWorkspace({
    projectId: 'project-1', client, document: null,
    onChange(summary) { changes.push(summary); },
  });
  await workspace.ready;
  assert.match(workspace.render(), /data-action="restore-auto-timeline"/);
  assert.match(workspace.render(), /data-action="play-shot"/);
  assert.match(workspace.render(), /data-field="subtitle_text"/);
  workspace.updateTimelineLine('voice-1', {
    subtitle_text: '修改后的字幕', subtitle_visible: true,
    start_ms: 100, end_ms: 1100,
  });
  assert.equal(workspace.getState().shots[0].lines[0].subtitle_text, '修改后的字幕');
  assert.equal(workspace.getState().timelineDirty, true);
  const saved = await workspace.saveTimeline();
  assert.equal(saved.revision, 9);
  assert.equal(changes.at(-1).revision, 9);
  const saveCall = calls.find((call) =>
    call.route === '/api/gen/short-drama/save-voice-timeline');
  assert.equal(saveCall.options.body.timeline_revision, 1);
  assert.equal(saveCall.options.body.items[0].start_ms, 100);
  assert.equal(workspace.getState().timelineDirty, false);
  await workspace.setShotLock(true);
  assert.equal(workspace.getState().shots[0].locked, true);
  assert.equal(changes.at(-1).revision, 10);
  current.shots[1].locked = true;
  current.handoff_blocked = false;
  current.handoff_blockers = [];
  current.unlocked_shot_count = 0;
  await workspace.reload();
  const advanced = await workspace.confirmVoiceStage();
  assert.equal(advanced.stage, 'video_review');
  assert.deepEqual(
    { revision: changes.at(-1).revision, stage: changes.at(-1).stage },
    { revision: 11, stage: 'video_review' },
  );
  assert.match(workspace.render(), /data-action="save-timeline"[^>]*disabled/);
  workspace.destroy();
}

async function testC2TimelineTimeoutRecoversWithoutBlindOldRevisionReplay() {
  let current = c2Snapshot();
  let saveCalls = 0;
  const submitted = [];
  const client = {
    json(route, options = {}) {
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        return Promise.resolve(JSON.parse(JSON.stringify(current)));
      }
      if (route === '/api/gen/audio/voices') {
        return Promise.resolve({ items: voices });
      }
      if (route === '/api/gen/short-drama/save-voice-timeline') {
        saveCalls += 1;
        submitted.push(JSON.parse(JSON.stringify(options.body)));
        current = JSON.parse(JSON.stringify(current));
        current.revision += 1;
        current.shots[0].timeline_revision += 1;
        options.body.items.forEach((item) => Object.assign(
          current.shots[0].lines.find((line) => line.id === item.line_id), item
        ));
        const error = new Error('timeout');
        error.code = 'timeout';
        return Promise.reject(error);
      }
      throw new Error(`unexpected route ${route}`);
    },
  };
  const workspace = voice.createWorkspace({
    projectId: 'project-1', client, document: null,
  });
  await workspace.ready;
  workspace.restoreAutoTimeline();
  const recovered = await workspace.saveTimeline();
  assert.equal(recovered.revision, 9);
  assert.equal(saveCalls, 1,
    'a lost free-write response must be recovered by refresh, not old-revision replay');
  assert.equal(submitted[0].revision, 8);
  workspace.destroy();
}

async function testC2ShotPlaybackStopsOnSelectionAndDestroy() {
  const players = [];
  const workspace = voice.createWorkspace({
    projectId: 'project-1', document: null,
    client: { json(route) {
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        return Promise.resolve(c2Snapshot());
      }
      return Promise.resolve({ items: voices });
    } },
    audioFactory(url) {
      const player = {
        url, currentTime: 0, pauses: 0, plays: 0,
        play() { this.plays += 1; return Promise.resolve(); },
        pause() { this.pauses += 1; },
      };
      players.push(player);
      return player;
    },
  });
  await workspace.ready;
  assert.equal(workspace.playShot(), true);
  assert.equal(workspace.getState().timelinePlaying, true);
  workspace.selectShot('shot-2');
  assert.equal(workspace.getState().timelinePlaying, false);
  assert.ok(players.every((player) => player.pauses >= 1));
  workspace.selectShot('shot-1');
  workspace.playShot();
  workspace.destroy();
  assert.ok(players.every((player) => player.pauses >= 1));
}

function testAlignmentPanelUsesServerActionsAndEscapesProvider() {
  const snapshot = c2Snapshot();
  snapshot.alignment = {
    provider: {
      name: '<real-provider>', real_forced_alignment: true,
      model_version: 'zh-v1', feature_enabled: true,
      word_timing_enabled: true,
    },
    readiness: { ready: true, blockers: [] },
    actions: { generate: false, save: true, lock: false },
    current_version: {
      id: 'alignment-1', version: 2, revision: 1,
      status: 'needs_review', effective_status: 'needs_review',
      quality: {
        coverage: 0.75, mean_confidence: 0.72,
        unmatched_tokens: [{ token: '<漏词>' }],
        low_confidence_ranges: [{ line_id: 'voice-1' }],
        degradation: [{ line_id: 'voice-1', reason: 'partial_match' }],
        blockers: [{ code: 'manual_review_required', message: '<校对必需>' }],
      },
      timeline: [{
        line_id: 'voice-1', text: '第一句',
        audio_start_ms: 100, audio_end_ms: 1800,
        subtitle_start_ms: 100, subtitle_end_ms: 1800,
        confidence: 0.6, status: 'partial_match',
        unmatched_tokens: [{ token: '<漏词>' }],
        words: [{
          token: '<低>', start_ms: 100, end_ms: 300, confidence: 0.4,
        }],
      }],
    },
  };
  let html = voice.renderWorkspace(snapshot, { voices, canEdit: true });
  assert.match(html, /第 4 阶段 · 字幕强制对齐/);
  assert.match(html, /&lt;real-provider&gt; · 真实/);
  assert.match(html, /&lt;校对必需&gt;/);
  assert.match(html, /未匹配词[\s\S]*1/);
  assert.match(html, /低置信区间[\s\S]*1/);
  assert.match(html, /词级诊断（1）/);
  assert.match(html, /&lt;漏词&gt;/);
  assert.match(html, /nc-sdv-alignment-word is-low/);
  assert.match(html, /data-alignment-field="subtitle_start_ms"/);
  assert.match(html, /data-action="preview-alignment"/);
  assert.match(html, /data-action="review-alignment"/);
  assert.doesNotMatch(html, /data-action="review-alignment" disabled/);
  assert.match(html, /data-review-action="confirm_unchanged"/);
  assert.match(html, /确认当前估算结果正确/);
  html = voice.renderWorkspace(snapshot, {
    voices, canEdit: true,
    alignmentDraft: {
      versionId: 'alignment-1', revision: 1,
      lines: [{
        line_id: 'voice-1',
        subtitle_start_ms: 150,
        subtitle_end_ms: 1750,
      }],
    },
  });
  assert.doesNotMatch(html, /data-action="review-alignment" disabled/);
  assert.match(html, /data-review-action="save_adjustments"/);
  assert.match(html, /保存调整并确认/);
}

function testAlignmentHandoffUsesServerCapabilityAndPreservesLegacyProjects() {
  const legacy = c2Snapshot();
  legacy.handoff_blocked = false;
  legacy.handoff_blockers = [];
  let html = voice.renderWorkspace(legacy, { voices, canEdit: true });
  assert.doesNotMatch(
    html,
    /data-action="confirm-voice-stage"[^>]*disabled/,
    'a legacy project that never started alignment remains eligible for handoff'
  );

  const started = c2Snapshot();
  started.handoff_blocked = false;
  started.handoff_blockers = [];
  started.alignment = {
    handoff: {
      required: true, ready: false,
      blockers: [{ code: 'alignment_not_locked', message: '请先锁定对齐版本' }],
    },
    readiness: { ready: true, blockers: [] },
    actions: { generate: true, save: true, lock: false },
    current_version: {
      id: 'alignment-review', version: 1, revision: 1,
      status: 'needs_review', effective_status: 'needs_review',
      quality: { coverage: 1, mean_confidence: 0.9, blockers: [] },
      timeline: [],
    },
  };
  html = voice.renderWorkspace(started, { voices, canEdit: true });
  assert.match(html, /data-action="confirm-voice-stage"[^>]*disabled/);

  started.alignment.handoff.ready = true;
  started.alignment.current_version.status = 'locked';
  started.alignment.current_version.effective_status = 'locked';
  html = voice.renderWorkspace(started, { voices, canEdit: true });
  assert.doesNotMatch(html, /data-action="confirm-voice-stage"[^>]*disabled/);
}

async function testAlignmentReviewSubmitsEditedBoundaries() {
  const state = c2Snapshot();
  state.alignment = {
    handoff: { required: true, ready: false, blockers: [] },
    readiness: { ready: true, blockers: [] },
    actions: { generate: true, save: true, lock: false },
    current_version: {
      id: 'alignment-edit', version: 1, revision: 3,
      status: 'needs_review', effective_status: 'needs_review',
      quality: { coverage: 1, mean_confidence: 0.9, blockers: [] },
      timeline: [{
        line_id: 'voice-1', text: '第一句',
        audio_start_ms: 100, audio_end_ms: 1800,
        subtitle_start_ms: 100, subtitle_end_ms: 1800,
      }],
    },
  };
  let submitted = null;
  const workspace = voice.createWorkspace({
    projectId: 'project-1', document: null,
    client: { json(route, options = {}) {
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        return Promise.resolve(JSON.parse(JSON.stringify(state)));
      }
      if (route === '/api/gen/audio/voices') {
        return Promise.resolve({ items: voices });
      }
      if (route === '/api/gen/short-drama/subtitle-alignment/timeline') {
        submitted = options.body;
        return Promise.resolve({});
      }
      throw new Error(`unexpected route ${route}`);
    } },
  });
  await workspace.ready;
  workspace.updateAlignmentLine('voice-1', {
    subtitle_start_ms: 150,
    subtitle_end_ms: 1750,
  });
  await workspace.reviewAlignment();
  assert.equal(submitted.review_action, 'save_adjustments');
  assert.deepEqual(submitted.lines, [{
    line_id: 'voice-1',
    subtitle_start_ms: 150,
    subtitle_end_ms: 1750,
  }]);
  workspace.destroy();
}

async function testAlignmentReviewCanExplicitlyConfirmUnchangedEstimate() {
  const state = c2Snapshot();
  state.alignment = {
    handoff: { required: true, ready: false, blockers: [] },
    readiness: { ready: true, blockers: [] },
    actions: { generate: true, save: true, lock: false },
    current_version: {
      id: 'alignment-confirm', version: 1, revision: 2,
      status: 'needs_review', effective_status: 'needs_review',
      quality: { coverage: 1, mean_confidence: 0.9, blockers: [] },
      timeline: [{
        line_id: 'voice-1', text: '第一句',
        audio_start_ms: 100, audio_end_ms: 1800,
        subtitle_start_ms: 100, subtitle_end_ms: 1800,
      }],
    },
  };
  let submitted = null;
  const workspace = voice.createWorkspace({
    projectId: 'project-1', document: null,
    client: { json(route, options = {}) {
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        return Promise.resolve(JSON.parse(JSON.stringify(state)));
      }
      if (route === '/api/gen/audio/voices') {
        return Promise.resolve({ items: voices });
      }
      if (route === '/api/gen/short-drama/subtitle-alignment/timeline') {
        submitted = options.body;
        return Promise.resolve({});
      }
      throw new Error(`unexpected route ${route}`);
    } },
  });
  await workspace.ready;
  await workspace.reviewAlignment();
  assert.equal(submitted.review_action, 'confirm_unchanged');
  assert.deepEqual(submitted.lines, [{
    line_id: 'voice-1',
    subtitle_start_ms: 100,
    subtitle_end_ms: 1800,
  }]);
  workspace.destroy();
}

async function testSilentAlignmentCanBeReviewedWithAnEmptyTimeline() {
  const state = c2Snapshot();
  state.alignment = {
    handoff: { required: true, ready: false, blockers: [] },
    readiness: { ready: true, blockers: [] },
    actions: { generate: true, save: true, lock: false },
    current_version: {
      id: 'alignment-silent', version: 1, revision: 1,
      status: 'needs_review', effective_status: 'needs_review',
      quality: { coverage: 0, mean_confidence: 0, blockers: [] },
      timeline: [],
    },
  };
  const html = voice.renderWorkspace(state, { voices, canEdit: true });
  assert.match(html, /当前项目没有对白，无需调整字幕边界/);
  assert.match(html, /确认当前无对白结果/);
  assert.doesNotMatch(html, /data-action="review-alignment" disabled/);

  let submitted = null;
  const workspace = voice.createWorkspace({
    projectId: 'project-1', document: null,
    client: { json(route, options = {}) {
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        return Promise.resolve(JSON.parse(JSON.stringify(state)));
      }
      if (route === '/api/gen/audio/voices') {
        return Promise.resolve({ items: voices });
      }
      if (route === '/api/gen/short-drama/subtitle-alignment/timeline') {
        submitted = options.body;
        return Promise.resolve({});
      }
      throw new Error(`unexpected route ${route}`);
    } },
  });
  await workspace.ready;
  await workspace.reviewAlignment();
  assert.equal(submitted.review_action, 'confirm_unchanged');
  assert.deepEqual(submitted.lines, []);
  workspace.destroy();
}

async function main() {
  testNormalizeRenderAndReadonlyContract();
  testRendererDistinguishesLoadingErrorEmptyPendingAndSilent();
  testDraftTimingShowsOverflowAndRecommendedSpeedBeforeSave();
  testDraftSettingsChangesBlockTimelineSave();
  testSubtitleOnlyOverflowDoesNotRecommendVoiceSpeed();
  testRendererEscapesAttributesErrorsAndVoiceFallbacks();
  testPrototypeNamedVoiceAndStatusKeysUseNormalFallbacks();
  testNarrationRendersAnExplicitEscapedBadge();
  testAlignmentPanelUsesServerActionsAndEscapesProvider();
  testAlignmentHandoffUsesServerCapabilityAndPreservesLegacyProjects();
  await testAlignmentReviewSubmitsEditedBoundaries();
  await testAlignmentReviewCanExplicitlyConfirmUnchangedEstimate();
  await testSilentAlignmentCanBeReviewedWithAnEmptyTimeline();
  testBrowserUmdExport();
  await testWorkspaceLoadsResourcesWithBoardHeaderAndExposesState();
  await testLatestReloadWinsOverOlderSuccessAndError();
  await testLoadErrorRendersOnlyEscapedErrorState();
  await testHostSelectionAndHandlerRemoval();
  await testDestroyInvalidatesPendingRequestsWithoutHostOrStateMutation();
  await testQuoteConfirmGenerateReloadAndVersionSelection();
  await testPreviewStopsPreviousAudioAndReadonlyRejectsWrites();
  await testAmbiguousVoiceSubmissionSurvivesTwoFailuresAndWorkspaceReload();
  await testC2TimelineDraftSaveLockAndHandoff();
  await testC2TimelineTimeoutRecoversWithoutBlindOldRevisionReplay();
  await testC2ShotPlaybackStopsOnSelectionAndDestroy();
  const css = fs.readFileSync(path.join(
    __dirname, '../site/workbench/canvas/canvas-short-drama-voice.css'
  ), 'utf8');
  assert.match(css, /grid-template-columns:\s*260px\s+minmax\(0,\s*1fr\)\s+300px/);
  assert.match(css, /\.nc-sdv-timing\.is-error/);
  console.log('canvas short drama voice: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
