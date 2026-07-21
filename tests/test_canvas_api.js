const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const apiModule = require('../site/workbench/canvas/canvas-api.js');

function response(options) {
  options = options || {};
  const text = options.text === undefined ? '{}' : options.text;
  return {
    ok: options.ok === undefined ? true : options.ok,
    status: options.status === undefined ? 200 : options.status,
    statusText: options.statusText || '',
    text() { return Promise.resolve(text); },
    blob() { return Promise.resolve(options.blob); },
  };
}

function abortError() {
  const error = new Error('aborted by signal');
  error.name = 'AbortError';
  return error;
}

function fakeControllerClass() {
  return class FakeAbortController {
    constructor() {
      this.signal = { aborted: false, onabort: null };
    }
    abort() {
      this.signal.aborted = true;
      if (this.signal.onabort) this.signal.onabort();
    }
  };
}

async function testGetDefaults() {
  const calls = [];
  const timers = [];
  const client = apiModule.createClient({
    fetchImpl(path, options) {
      calls.push({ path, options });
      return Promise.resolve(response({ text: '{"items":[1]}' }));
    },
    tokenProvider: () => '__cookie__',
    AbortControllerImpl: fakeControllerClass(),
    setTimeoutImpl(fn, delay) { timers.push({ fn, delay }); return 41; },
    clearTimeoutImpl(handle) { timers.push({ cleared: handle }); },
  });

  assert.deepEqual(await client.json('/api/items'), { items: [1] });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, '/api/items');
  assert.equal(calls[0].options.method, 'GET');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.cache, 'no-store');
  assert.deepEqual(calls[0].options.headers, {
    Accept: 'application/json',
    Authorization: 'Bearer __cookie__',
  });
  assert.equal(calls[0].options.body, undefined);
  assert.equal(calls[0].options.signal.aborted, false);
  assert.equal(timers[0].delay, 8000);
  assert.deepEqual(timers.filter((item) => item.cleared).map((item) => item.cleared), [41]);
}

async function testPostJson() {
  let call;
  const client = apiModule.createClient({
    fetchImpl(path, options) {
      call = { path, options };
      return Promise.resolve(response({ text: '{"ok":true}' }));
    },
    tokenProvider: () => 'token-a',
  });

  assert.deepEqual(await client.json('/api/jobs', {
    method: 'POST',
    headers: { 'X-Trace': 'trace-a' },
    body: { prompt: 'hello' },
  }), { ok: true });
  assert.equal(call.options.method, 'POST');
  assert.deepEqual(call.options.headers, {
    Accept: 'application/json',
    Authorization: 'Bearer token-a',
    'X-Trace': 'trace-a',
    'Content-Type': 'application/json',
  });
  assert.equal(call.options.body, '{"prompt":"hello"}');
  assert.equal(call.options.credentials, 'same-origin');
  assert.equal(call.options.cache, 'no-store');
}

async function testHttpErrorData() {
  const client = apiModule.createClient({
    fetchImpl: () => Promise.resolve(response({
      ok: false,
      status: 429,
      statusText: 'Too Many Requests',
      text: '{"detail":"queue full","code":"queue_full","retry_after_ms":4000}',
    })),
  });

  await assert.rejects(client.json('/api/jobs'), (error) => {
    assert.equal(error.message, 'queue full');
    assert.equal(error.status, 429);
    assert.equal(error.code, 'queue_full');
    assert.deepEqual(error.data, {
      detail: 'queue full',
      code: 'queue_full',
      retry_after_ms: 4000,
    });
    return true;
  });
}

async function testNonJsonFallback() {
  const client = apiModule.createClient({
    fetchImpl: () => Promise.resolve(response({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      text: 'upstream unavailable',
    })),
  });

  await assert.rejects(client.json('/api/jobs'), (error) => {
    assert.equal(error.message, 'upstream unavailable');
    assert.equal(error.status, 502);
    assert.equal(error.code, 'request_failed');
    assert.deepEqual(error.data, { detail: 'upstream unavailable' });
    return true;
  });
}

async function testTimeoutAbort() {
  let timer;
  let cleared;
  const client = apiModule.createClient({
    fetchImpl(path, options) {
      return new Promise((resolve, reject) => {
        options.signal.onabort = () => reject(abortError());
      });
    },
    AbortControllerImpl: fakeControllerClass(),
    setTimeoutImpl(fn, delay) { timer = { fn, delay }; return 72; },
    clearTimeoutImpl(handle) { cleared = handle; },
  });

  const pending = client.json('/api/slow', { timeout: 1250 });
  assert.equal(timer.delay, 1250);
  timer.fn();
  await assert.rejects(pending, (error) => {
    assert.equal(error.message, 'request aborted');
    assert.equal(error.status, 0);
    assert.equal(error.code, 'timeout');
    assert.equal(error.data, null);
    return true;
  });
  assert.equal(cleared, 72);
}

async function testCallerAbort() {
  const callerSignal = { name: 'caller-signal' };
  let receivedSignal;
  let timerCalls = 0;
  const client = apiModule.createClient({
    fetchImpl(path, options) {
      receivedSignal = options.signal;
      return Promise.reject(abortError());
    },
    AbortControllerImpl: fakeControllerClass(),
    setTimeoutImpl() { timerCalls += 1; },
  });

  await assert.rejects(client.json('/api/cancelled', { signal: callerSignal }), (error) => {
    assert.equal(error.code, 'aborted');
    assert.equal(error.status, 0);
    return true;
  });
  assert.strictEqual(receivedSignal, callerSignal);
  assert.equal(timerCalls, 0);
}

async function testAssetBlob() {
  const assetBlob = new Blob(['video-bytes'], { type: 'video/mp4' });
  let call;
  const client = apiModule.createClient({
    fetchImpl(path, options) {
      call = { path, options };
      return Promise.resolve(response({ blob: assetBlob }));
    },
    tokenProvider: () => '__cookie__',
  });

  assert.strictEqual(await client.asset('/api/gen/file/example.mp4'), assetBlob);
  assert.equal(call.options.headers.Authorization, 'Bearer __cookie__');
  assert.equal(call.options.headers.Accept, 'application/json');
  assert.equal(call.options.credentials, 'same-origin');
  assert.equal(call.options.cache, 'no-store');
  assert.equal(call.options.body, undefined);
}

async function testExternalAssetKeepsPublicFetchSemantics() {
  const assetBlob = new Blob(['image-bytes'], { type: 'image/png' });
  const calls = [];
  const timers = [];
  const client = apiModule.createClient({
    fetchImpl(path, options) {
      calls.push({ path, options });
      return Promise.resolve(response({ blob: assetBlob }));
    },
    tokenProvider: () => 'private-token',
    AbortControllerImpl: fakeControllerClass(),
    setTimeoutImpl(fn, delay) { timers.push({ fn, delay }); return 91; },
    clearTimeoutImpl(handle) { timers.push({ cleared: handle }); },
  });

  assert.strictEqual(await client.asset('https://cdn.example.com/public/image.png'), assetBlob);
  assert.equal(calls[0].options.credentials, 'include');
  assert.equal(calls[0].options.cache, 'no-store');
  assert.deepEqual(calls[0].options.headers, {});
  assert.equal(calls[0].options.body, undefined);
  assert.deepEqual(timers.filter((item) => item.cleared).map((item) => item.cleared), [91]);
}

async function testExternalAssetHttpErrorAndCleanup() {
  const timers = [];
  const client = apiModule.createClient({
    fetchImpl: () => Promise.resolve(response({ ok: false, status: 403 })),
    AbortControllerImpl: fakeControllerClass(),
    setTimeoutImpl(fn, delay) { timers.push({ fn, delay }); return 92; },
    clearTimeoutImpl(handle) { timers.push({ cleared: handle }); },
  });

  await assert.rejects(client.asset('https://cos.example.com/denied.png'), (error) => {
    assert.equal(error.message, 'HTTP 403');
    assert.equal(error.status, 403);
    return true;
  });
  assert.deepEqual(timers.filter((item) => item.cleared).map((item) => item.cleared), [92]);
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
}

async function testPollRejectsAndCleansUpAfterDeadline() {
  const requestError = new Error('temporary network failure');
  let now = 0;
  let tick;
  let intervalDelay;
  let cleared;
  const pending = apiModule.poll({
    request: () => Promise.reject(requestError),
    inspect: () => ({ pending: true }),
    maxMs: 420000,
    intervalMs: 3000,
    now: () => now,
    setIntervalImpl(fn, delay) { tick = fn; intervalDelay = delay; return 81; },
    clearIntervalImpl(handle) { cleared = handle; },
    timeoutError: () => apiModule.apiError('超时', { code: 'timeout' }),
  });

  assert.equal(intervalDelay, 3000);
  now = 419000;
  tick();
  await flushPromises();
  assert.equal(cleared, undefined, 'transient polling failures before the deadline keep retrying');

  now = 423000;
  tick();
  await assert.rejects(pending, (error) => {
    assert.equal(error.message, '超时');
    assert.equal(error.code, 'timeout');
    return true;
  });
  assert.equal(cleared, 81, 'deadline failure clears the polling interval');
}

async function testPollKeepsSuccessfulTerminalResult() {
  let tick;
  let cleared;
  let nowCalls = 0;
  const pending = apiModule.poll({
    request: () => Promise.resolve({ status: 'done', result: '{"url":"/done.png"}' }),
    inspect(data) {
      return { done: true, value: JSON.parse(data.result) };
    },
    maxMs: 420000,
    intervalMs: 3000,
    now: () => nowCalls++ === 0 ? 0 : 500000,
    setIntervalImpl(fn) { tick = fn; return 82; },
    clearIntervalImpl(handle) { cleared = handle; },
  });

  tick();
  assert.deepEqual(await pending, { url: '/done.png' });
  assert.equal(cleared, 82);
}

async function testPollKeepsFailedTerminalResultAfterDeadline() {
  let tick;
  const terminalError = new Error('generation failed');
  let nowCalls = 0;
  const pending = apiModule.poll({
    request: () => Promise.resolve({ status: 'failed' }),
    inspect: () => ({ error: terminalError }),
    maxMs: 420000,
    now: () => nowCalls++ === 0 ? 0 : 500000,
    setIntervalImpl(fn) { tick = fn; return 83; },
    clearIntervalImpl() {},
  });

  tick();
  await assert.rejects(pending, (error) => error === terminalError);
}

function testCanvasIntegration() {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
  const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas.css'), 'utf8');
  const appStamp = crypto.createHash('md5').update(app.replace(/\r\n/g, '\n')).digest('hex').slice(0, 8);
  const cssStamp = crypto.createHash('md5').update(css.replace(/\r\n/g, '\n')).digest('hex').slice(0, 8);
  const order = [
    'canvas/canvas-graph.js?v=',
    'canvas/canvas-state.js?v=',
    'canvas/canvas-storage.js?v=',
    'canvas/canvas-api.js?v=',
    'canvas/canvas-export.js?v=',
    'canvas-collab-sync.js?v=',
    'canvas/canvas-app.js?v=',
  ].map((asset) => html.indexOf(asset));

  assert.ok(order.every((index) => index >= 0), 'all canvas modules must be loaded');
  assert.deepEqual(order, [...order].sort((left, right) => left - right), 'modules, collaboration sync, and app must load in dependency order');
  assert.ok(html.includes(`canvas/canvas.css?v=${cssStamp}`));
  assert.ok(html.includes(`canvas/canvas-app.js?v=${appStamp}`));
  assert.match(app, /var apiModule=window\.HQCanvas&&window\.HQCanvas\.api;/);
  assert.match(app, /apiModule\.createClient\(/);
  assert.equal((app.match(/apiModule\.poll\(/g) || []).length, 2, 'image and video jobs share bounded polling');
  assert.equal((app.match(/maxMs:420000/g) || []).length, 1, 'image jobs retain their 420 second limit');
  assert.equal((app.match(/900000/g) || []).length, 1, 'Grok video jobs retain their 900 second limit');
  assert.equal((app.match(/1800000/g) || []).length, 1, 'cinematic jobs use the backend 30 minute limit');
  assert.match(app, /error&&error\.code==='timeout'/);
  assert.ok(app.includes("error.message='协作服务响应超时'"), 'collaboration timeout keeps its existing UI message');
  assert.equal((app.match(/\bfetch\(/g) || []).length, 0, 'all direct requests must be behind extracted modules');

  for (const endpoint of [
    '/api/gen/history?limit=60',
    '/api/gen/video/assets?limit=60',
    '/api/gen/reverse',
    '/api/gen/image',
    '/api/gen/banana',
    '/api/gen/video/avatars?limit=120',
    '/api/gen/xiaole_video',
    '/api/gen/cinematic',
    '/api/gen/job/',
  ]) assert.ok(app.includes(endpoint), endpoint);

  const videoStart = app.indexOf("if(type==='video') body=");
  const videoEnd = app.indexOf("el.innerHTML=", videoStart);
  const videoNodeSource = app.slice(videoStart, videoEnd);
  assert.ok(videoStart >= 0 && videoEnd > videoStart, 'video node markup is present');
  assert.ok(videoNodeSource.includes('电影化身·开放式'));
  assert.ok(!videoNodeSource.includes('豆姐视频'));
  assert.ok(videoNodeSource.includes('data-v="9:16"'));
  assert.ok(videoNodeSource.includes('data-v="16:9"'));
  assert.ok(!videoNodeSource.includes('data-v="1:1"'));
  assert.ok(videoNodeSource.includes('data-v="cinematic"'));
  assert.ok(videoNodeSource.includes('data-f="avatarWrap"'));
  assert.ok(videoNodeSource.includes('data-f="videoCost"'));
  assert.ok(videoNodeSource.includes('<button class="nc-go" data-f="run"><span>生成视频</span><span class="nc-video-cost" data-f="videoCost"></span></button>'));
  assert.ok(!videoNodeSource.includes('</button><span class="nc-video-cost"'));

  assert.match(app, /var VIDEO_POINTS_PER_SECOND=30;/);
  assert.match(app, /function normalizeVideoNodeParams\(params\)/);
  assert.match(app, /params\.channel==='micro'/);
  assert.match(app, /function refreshVideoNodeControls\(node\)/);
  assert.match(app, /function referenceImageDataUrl\(source\)/);
  assert.match(app, /Promise\.all\(videoRefs\.map\(referenceImageDataUrl\)\)/);
  assert.ok(app.includes("cost.textContent=videoPointCost(node)+'点';"));
  assert.ok(!app.includes("cost.textContent='消耗 '+videoPointCost(node)+' 点';"));
  assert.ok(css.includes('.nc-video-submit .nc-go{ display:flex; align-items:center; justify-content:center; gap:8px; width:100%; min-width:0; }'));
  assert.match(app, /cine_mode:'open'/);
  assert.match(app, /avatar_ids:/);
  assert.match(app, /resolution:'1080p'/);
  assert.match(app, /duration:parseInt\(node\.params\.duration,10\)/);
  assert.ok(app.includes('请先选择至少一个电影化身'));
  assert.equal((app.match(/intervalMs:3000/g) || []).length, 2, 'image and video jobs retain 3000 ms polling');
  for (const state of ['提交中…', '生成中… 已用 ', '提交中...', '生成中，已用 ', '点数不足', '等待队列空位']) {
    assert.ok(app.includes(state), state);
  }
  assert.doesNotThrow(() => new Function(app));
}

Promise.resolve()
  .then(testGetDefaults)
  .then(testPostJson)
  .then(testHttpErrorData)
  .then(testNonJsonFallback)
  .then(testTimeoutAbort)
  .then(testCallerAbort)
  .then(testAssetBlob)
  .then(testExternalAssetKeepsPublicFetchSemantics)
  .then(testExternalAssetHttpErrorAndCleanup)
  .then(testPollRejectsAndCleansUpAfterDeadline)
  .then(testPollKeepsSuccessfulTerminalResult)
  .then(testPollKeepsFailedTerminalResultAfterDeadline)
  .then(testCanvasIntegration)
  .then(() => console.log('canvas API client: pass'))
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
