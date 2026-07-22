const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const production = require('../site/workbench/canvas/canvas-short-drama-production.js');

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function sampleState(overrides = {}) {
  const base = {
    project_id: 'project/one',
    revision: 7,
    stage: 'stills_review',
    ratio: '9:16',
    point_budget: 100,
    spent_points: 24,
    reserved_points: 12,
    shots: [
      {
        id: 'shot-2', shot_key: '第二镜', sort_order: 2, duration: 5,
        image_prompt: '雨夜门口',
        still: {
          asset_id: 'asset-2', current_version: null, locked: false, versions: [], job: null,
        },
      },
      {
        id: 'shot-1', shot_key: '第一镜', sort_order: 1, duration: 5,
        image_prompt: '侦探 <script>alert(1)</script>',
        references: ['<img src=x onerror=alert(1)>'],
        still: {
          asset_id: 'asset-1', current_version: 12, locked: true,
          versions: [
            {
              id: 'version-id-a', version: 11, job_id: 90,
              url: 'https://example.test/a.png?x=<bad>', prompt: '<b>old</b>',
              ratio: '9:16', cost: 12, status: 'done', created_at: 1,
            },
            {
              id: 'version-id-b', version: 12, job_id: 90,
              url: 'https://example.test/b.png', prompt: 'current',
              ratio: '9:16', cost: 12, status: 'done', created_at: 2,
            },
          ],
          job: null,
        },
      },
    ],
  };
  return Object.assign(base, overrides);
}

function terminalState(revision = 7) {
  const state = sampleState({ revision });
  state.shots[0].still.versions = [
    { id: 'generated-a', version: 1, job_id: 101, url: 'a.png', prompt: '雨夜门口', ratio: '9:16', cost: 12, status: 'done', created_at: 3 },
    { id: 'generated-b', version: 2, job_id: 101, url: 'b.png', prompt: '雨夜门口', ratio: '9:16', cost: 12, status: 'done', created_at: 3 },
  ];
  state.shots[0].still.current_version = 1;
  state.shots[0].still.job = null;
  return state;
}

function testNormalizationAndRenderer() {
  assert.deepEqual(Object.keys(production).sort(), ['createWorkspace', 'normalizeState', 'renderWorkspace']);

  const normalized = production.normalizeState(sampleState(), { selectedShotId: 'missing' });
  assert.deepEqual(normalized.shots.map((shot) => shot.id), ['shot-1', 'shot-2']);
  assert.equal(normalized.selectedShotId, 'shot-1');
  assert.deepEqual(
    normalized.shots[0].still.versions.map((version) => version.id),
    ['version-id-a', 'version-id-b'],
    'normalization preserves every server version id',
  );

  const html = production.renderWorkspace(sampleState(), { selectedShotId: 'shot-1' });
  assert.match(html, /镜头列表[\s\S]*关键帧候选[\s\S]*生成控制台/);
  assert.match(html, /data-filter="all"[\s\S]*data-filter="pending"[\s\S]*data-filter="locked"/);
  assert.match(html, /data-ratio="9:16"/);
  assert.doesNotMatch(html, /<script>|<img src=x|<b>old/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /data-version="12"[\s\S]*data-action="lock-version"/);

  assert.match(production.renderWorkspace(sampleState({ ratio: '16:9' }), {}), /data-ratio="16:9"/);
  for (const state of [
    sampleState({ canEdit: false }),
    sampleState({ busy: true }),
    sampleState({ stale: true }),
    sampleState({ stage: 'voice_review' }),
  ]) {
    const disabled = production.renderWorkspace(state, {});
    assert.doesNotMatch(disabled, /data-action="generate-current"(?![^>]*disabled)/);
    assert.doesNotMatch(disabled, /data-action="select-version"(?![^>]*disabled)/);
    assert.doesNotMatch(disabled, /data-action="confirm-stage"(?![^>]*disabled)/);
  }
  assert.match(
    production.renderWorkspace(sampleState({ stale: true, error: '<stale>' }), {}),
    /&lt;stale&gt;[\s\S]*data-action="refresh"/,
  );
}

function testResponsiveCssContract() {
  const css = fs.readFileSync(path.join(
    __dirname, '../site/workbench/canvas/canvas-short-drama-production.css',
  ), 'utf8');
  assert.match(css, /grid-template-columns:\s*260px\s+minmax\(0,\s*1fr\)\s+300px/);
  assert.match(css, /\[data-ratio="9:16"\][^{]*\{[^}]*aspect-ratio:\s*9\s*\/\s*16/s);
  assert.match(css, /\[data-ratio="16:9"\][^{]*\{[^}]*aspect-ratio:\s*16\s*\/\s*9/s);
  assert.match(css, /\.nc-sdp-preview\s+img[^{]*\{[^}]*object-fit:\s*contain/s);
  assert.match(css, /@media\s*\(max-width:\s*980px\)[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(css, /@media\s*\(max-width:\s*980px\)[\s\S]*overflow-x:\s*auto/);
}

async function testQuoteConfirmSubmitOrderAndCancellation() {
  const calls = [];
  let state = sampleState();
  const client = {
    json(path, options = {}) {
      calls.push({ path, options: clone(options) });
      if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(clone(state));
      if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still' });
      if (path === '/api/gen/short-drama/generate-stills') {
        state = terminalState();
        return Promise.resolve({ job_id: 101, cost: 24, project_id: 'project/one', shot_id: 'shot-2' });
      }
      throw new Error(`unexpected route ${path}`);
    },
  };
  const workspace = production.createWorkspace({
    projectId: 'project/one', client, document: null, pollIntervalMs: 0,
    idempotencyKey() { calls.push({ path: 'key' }); return 'still-action-1'; },
    confirm(cost, quote) {
      calls.push({ path: 'confirm', cost, quote: clone(quote) });
      return true;
    },
  });
  await workspace.ready;
  workspace.selectShot('shot-2');
  const result = await workspace.generateCurrent();
  assert.equal(result.shots[1].still.current_version, 1);
  assert.deepEqual(calls.slice(1, 5).map((call) => call.path), [
    'key', '/api/gen/short-drama/asset-quote', 'confirm', '/api/gen/short-drama/generate-stills',
  ]);
  const quoteBody = calls[2].options.body;
  const submit = calls[4];
  assert.deepEqual(quoteBody, {
    project_id: 'project/one', revision: 7, shot_id: 'shot-2',
    prompt: '雨夜门口', mode: 'single', count: 2,
  });
  assert.deepEqual(submit.options.body, quoteBody);
  assert.equal(submit.options.headers['Idempotency-Key'], 'still-action-1');
  workspace.destroy();

  let submissions = 0;
  const cancelled = production.createWorkspace({
    projectId: 'project/one', document: null,
    confirm() { calls.push({ path: 'cancel-confirm' }); return false; },
    client: {
      json(path) {
        if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(sampleState());
        if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still' });
        if (path === '/api/gen/short-drama/generate-stills') submissions += 1;
        return Promise.resolve({});
      },
    },
  });
  await cancelled.ready;
  assert.equal(await cancelled.generateCurrent(), null);
  assert.equal(submissions, 0, 'cancelled confirmation never submits generate-stills');
  cancelled.destroy();
}

async function testDeduplicationTimeoutRetryAndPolling() {
  let state = sampleState();
  let submits = 0;
  let gets = 0;
  const keys = [];
  const client = {
    json(path, options = {}) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        gets += 1;
        if (gets === 1) return Promise.resolve(clone(state));
        if (gets === 2) {
          state.shots[0].still.job = { id: 'link-101', job_id: 101, kind: 'still', status: 'running', quoted_cost: 24 };
          return Promise.resolve(clone(state));
        }
        return Promise.resolve(terminalState());
      }
      if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still' });
      if (path === '/api/gen/short-drama/generate-stills') {
        submits += 1;
        keys.push(options.headers['Idempotency-Key']);
        if (submits === 1) {
          const error = new Error('request timed out'); error.code = 'timeout';
          return Promise.reject(error);
        }
        return Promise.resolve({ job_id: 101, cost: 24, project_id: 'project/one', shot_id: 'shot-2' });
      }
      throw new Error(`unexpected route ${path}`);
    },
  };
  const workspace = production.createWorkspace({
    projectId: 'project/one', client, document: null, confirm: () => true,
    idempotencyKey: () => 'one-key-only', pollIntervalMs: 0,
  });
  await workspace.ready;
  workspace.selectShot('shot-2');
  const first = workspace.generateCurrent();
  const second = workspace.generateCurrent();
  assert.equal(first, second, 'a double click shares one in-flight user action');
  await first;
  assert.equal(submits, 2, 'one timeout is retried once');
  assert.deepEqual(keys, ['one-key-only', 'one-key-only'], 'timeout retry reuses the action key');
  assert.equal(gets, 3, 'production polling continues through the linked running job');
  workspace.destroy();
}

async function testRevisionedMutationsStaleRefreshAndDestroy() {
  let state = terminalState(10);
  const calls = [];
  const client = {
    json(path, options = {}) {
      calls.push({ path, options: clone(options) });
      if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(clone(state));
      if (path === '/api/gen/short-drama/select-asset') {
        state = clone(state); state.revision += 1;
        state.shots[0].still.current_version = options.body.version;
        state.shots[0].still.locked = options.body.lock;
        return Promise.resolve(clone(state));
      }
      if (path === '/api/gen/short-drama/confirm-production-stage') {
        state = clone(state); state.revision += 1; state.stage = 'voice_review';
        return Promise.resolve(clone(state));
      }
      throw new Error(`unexpected route ${path}`);
    },
  };
  const workspace = production.createWorkspace({ projectId: 'project/one', client, document: null });
  await workspace.ready;
  workspace.selectShot('shot-2');
  await workspace.selectVersion(2, true);
  await workspace.confirmStage();
  assert.deepEqual(calls.slice(1).map((call) => [call.path, call.options.body]), [
    ['/api/gen/short-drama/select-asset', {
      project_id: 'project/one', revision: 10, asset_id: 'asset-2', version: 2, lock: true,
    }],
    ['/api/gen/short-drama/confirm-production-stage', {
      project_id: 'project/one', revision: 11, stage: 'stills_review',
    }],
  ]);
  workspace.destroy();

  let reloads = 0;
  const stale = production.createWorkspace({
    projectId: 'project/one', document: null,
    client: {
      json(path) {
        if (path.startsWith('/api/gen/short-drama/production?')) { reloads += 1; return Promise.resolve(sampleState()); }
        const error = new Error('<conflict>'); error.status = 409; error.code = 'revision_conflict';
        return Promise.reject(error);
      },
    },
  });
  await stale.ready;
  await assert.rejects(stale.selectVersion(11, true), (error) => error.status === 409);
  assert.equal(stale.getState().stale, true);
  assert.match(stale.render(), /&lt;conflict&gt;[\s\S]*data-action="refresh"/);
  assert.doesNotMatch(stale.render(), /data-action="generate-current"(?![^>]*disabled)/);
  await assert.rejects(stale.generateCurrent(), /refresh|stale/i);
  assert.equal(reloads, 1, 'stale writes do not silently mutate or refresh');
  await stale.refresh();
  assert.equal(reloads, 2);
  assert.equal(stale.getState().stale, false);
  stale.destroy();

  let timerId = 0;
  const cleared = [];
  const timers = [];
  const closing = production.createWorkspace({
    projectId: 'project/one', document: null, confirm: () => true,
    setTimeoutImpl(fn) { timerId += 1; timers.push({ id: timerId, fn }); return timerId; },
    clearTimeoutImpl(id) { cleared.push(id); },
    client: {
      json(path) {
        if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(sampleState());
        if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still' });
        if (path === '/api/gen/short-drama/generate-stills') return Promise.resolve({ job_id: 101, shot_id: 'shot-2' });
        throw new Error(`unexpected route ${path}`);
      },
    },
  });
  await closing.ready;
  closing.selectShot('shot-2');
  const pending = closing.generateCurrent();
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  assert.equal(timers.length, 1);
  closing.destroy();
  assert.deepEqual(cleared, [timers[0].id]);
  await assert.rejects(pending, /destroyed/i);
  assert.throws(() => closing.render(), /destroyed/i, 'destroyed workspaces reject stale rendering');
  await assert.rejects(closing.refresh(), /destroyed/i);
}

async function testDestroyDuringSubmissionNeverCreatesAPollTimer() {
  let resolveSubmit;
  const timers = [];
  const workspace = production.createWorkspace({
    projectId: 'project/one', document: null, confirm: () => true,
    setTimeoutImpl(fn) { timers.push(fn); return timers.length; },
    clearTimeoutImpl() {},
    client: {
      json(path) {
        if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(sampleState());
        if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still' });
        if (path === '/api/gen/short-drama/generate-stills') {
          return new Promise((resolve) => { resolveSubmit = resolve; });
        }
        throw new Error(`unexpected route ${path}`);
      },
    },
  });
  await workspace.ready;
  workspace.selectShot('shot-2');
  const pending = workspace.generateCurrent();
  pending.catch(() => {});
  for (let index = 0; index < 6 && !resolveSubmit; index += 1) await Promise.resolve();
  assert.equal(typeof resolveSubmit, 'function', 'test reaches the in-flight submission boundary');
  workspace.destroy();
  resolveSubmit({ job_id: 101, shot_id: 'shot-2' });
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  assert.equal(timers.length, 0, 'a late submit response cannot schedule polling after destroy');
  await assert.rejects(pending, /destroyed/i);
}

async function testDestroyDuringTimedOutSubmissionNeverRetries() {
  let rejectSubmit;
  let submissions = 0;
  const workspace = production.createWorkspace({
    projectId: 'project/one', document: null, confirm: () => true,
    client: {
      json(path) {
        if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(sampleState());
        if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still' });
        if (path === '/api/gen/short-drama/generate-stills') {
          submissions += 1;
          return new Promise((_resolve, reject) => { rejectSubmit = reject; });
        }
        throw new Error(`unexpected route ${path}`);
      },
    },
  });
  await workspace.ready;
  const pending = workspace.generateCurrent();
  pending.catch(() => {});
  for (let index = 0; index < 6 && !rejectSubmit; index += 1) await Promise.resolve();
  workspace.destroy();
  const timeout = new Error('request timed out'); timeout.code = 'timeout';
  rejectSubmit(timeout);
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  assert.equal(submissions, 1, 'destroy prevents a late timeout from starting a retry');
  await assert.rejects(pending, /destroyed/i);
}

async function main() {
  testNormalizationAndRenderer();
  testResponsiveCssContract();
  await testQuoteConfirmSubmitOrderAndCancellation();
  await testDeduplicationTimeoutRetryAndPolling();
  await testRevisionedMutationsStaleRefreshAndDestroy();
  await testDestroyDuringSubmissionNeverCreatesAPollTimer();
  await testDestroyDuringTimedOutSubmissionNeverRetries();
  console.log('canvas short drama production: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
