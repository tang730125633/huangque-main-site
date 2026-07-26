const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const video = require('../site/workbench/canvas/canvas-short-drama-video.js');

function snapshot(overrides = {}) {
  return Object.assign({
    project_id: 'project-1', revision: 7, stage: 'video_review', ratio: '9:16',
    target_duration: 60, point_budget: 5000, spent_points: 100, reserved_points: 0,
    models: [
      { channel: 'micro', label: 'Seedance 480P + AI 超清', model: 'seedance', resolution: '480p', upscale: true, enabled: true },
      { channel: 'omni', label: 'Omni 720P', model: 'omni', resolution: '720p', upscale: false, enabled: true },
      { channel: 'grok', label: 'Grok 720P', model: 'grok', resolution: '720p', upscale: false, enabled: true },
    ],
    shots: [{
      id: 'shot-1', shot_key: '第一镜', sort_order: 0, duration: 10,
      scene_description: '雨夜车站', video_prompt: '人物自然抬头',
      video: { asset_id: null, current_version: null, locked: false, versions: [], job: null },
    }],
    ready: false,
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

function testRenderAndEscaping() {
  const state = video.normalizeState(snapshot(), {});
  assert.equal(state.modelKey, 'micro');
  assert.equal(state.shots[0].duration, 10);
  const html = video.renderWorkspace(snapshot(), {});
  assert.match(html, /C-3 视频工作台/);
  assert.match(html, /Seedance 480P \+ AI 超清/);
  assert.match(html, /生成当前镜头/);
  const hostile = video.renderWorkspace(snapshot({
    shots: [{
      id: 'x', shot_key: '<script>x</script>', sort_order: 0, duration: 10,
      scene_description: '<img onerror=boom>', video_prompt: '<svg>',
      video: { asset_id: null, current_version: null, locked: false, versions: [], job: null },
    }],
  }), {});
  assert.doesNotMatch(hostile, /<script>|<img|<svg/);
  assert.match(hostile, /&lt;script&gt;/);
}

async function testQuotedGenerationUsesIdempotencyAndRefreshes() {
  const calls = [];
  const confirmations = [];
  const host = fakeHost();
  const completed = snapshot({
    spent_points: 400,
    shots: [{
      id: 'shot-1', shot_key: '第一镜', sort_order: 0, duration: 10,
      scene_description: '雨夜车站', video_prompt: '人物自然抬头',
      video: {
        asset_id: 'asset-1', current_version: 1, locked: false, job: { job_id: 91, status: 'done', phase: 'completed' },
        versions: [{ version: 1, channel: 'micro', resolution: '1080p', url: '/video.mp4' }],
      },
    }],
  });
  let stateReads = 0;
  const workspace = video.createWorkspace({
    projectId: 'project-1', boardId: 'board-1', host,
    confirm(cost, quote) { confirmations.push({ cost, quote }); return true; },
    client: {
      json(url, options = {}) {
        calls.push({ url, options });
        if (url.startsWith('/api/gen/short-drama/video?')) {
          stateReads += 1;
          return Promise.resolve(stateReads === 1 ? snapshot() : completed);
        }
        if (url === '/api/gen/short-drama/video-quote') {
          return Promise.resolve({ cost: 300, quote_token: 'quote-1' });
        }
        if (url === '/api/gen/short-drama/generate-video') {
          return Promise.resolve({ job_id: 91, cost: 300 });
        }
        throw new Error(`unexpected ${url}`);
      },
    },
  });
  await workspace.ready;
  await workspace.generate();
  assert.deepEqual(confirmations, [{ cost: 300, quote: { kind: 'video', model: 'Seedance 480P + AI 超清' } }]);
  const quoteCall = calls.find((call) => call.url.endsWith('/video-quote'));
  assert.equal(quoteCall.options.body.upscale, true);
  assert.equal(quoteCall.options.headers['X-Canvas-Board-Id'], 'board-1');
  const submit = calls.find((call) => call.url.endsWith('/generate-video'));
  assert.equal(submit.options.body.quote_token, 'quote-1');
  assert.match(submit.options.headers['Idempotency-Key'], /^sd-video-/);
  assert.match(host.innerHTML, /候选版本/);
  workspace.destroy();
}

function testCanvasIntegration() {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
  const controller = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.js'), 'utf8');
  assert.match(html, /canvas-short-drama-video\.css\?v=[0-9a-f]{8}/);
  assert.match(html, /canvas-short-drama-video\.js\?v=[0-9a-f]{8}/);
  assert.match(controller, /shortDramaVideo/);
  assert.match(controller, /videoModule/);
  assert.match(controller, /stage==='video_review'/);
  for (const asset of ['canvas-short-drama-video.js', 'canvas-short-drama-video.css']) {
    const source = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', asset), 'utf8').replace(/\r\n/g, '\n');
    const stamp = crypto.createHash('md5').update(source).digest('hex').slice(0, 8);
    assert.ok(html.includes(`canvas/${asset}?v=${stamp}`), `${asset} cache stamp must match`);
  }
}

async function main() {
  testRenderAndEscaping();
  await testQuotedGenerationUsesIdempotencyAndRefreshes();
  testCanvasIntegration();
  console.log('short drama video canvas tests passed');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
