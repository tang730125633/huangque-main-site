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
    handoff_blocked: false,
    handoff_blockers: [],
    shots: [
      {
        id: 'shot-2', shot_key: '第二镜', sort_order: 2, duration: 5,
        image_prompt: '雨夜门口', image_prompt_hash: 'hash-rain',
        still: {
          asset_id: 'asset-2', current_version: null, locked: false, versions: [], job: null,
        },
      },
      {
        id: 'shot-1', shot_key: '第一镜', sort_order: 1, duration: 5,
        image_prompt: '侦探 <script>alert(1)</script>', image_prompt_hash: 'hash-detective',
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

function batchState() {
  const state = sampleState();
  state.shots.push(
    {
      id: 'shot-3', shot_key: '第三镜', sort_order: 3, duration: 5, image_prompt: '正在生成',
      still: {
        asset_id: 'asset-3', current_version: null, locked: false, versions: [],
        job: { id: 'link-3', job_id: 103, kind: 'still', status: 'running', quoted_cost: 8 },
      },
    },
    {
      id: 'shot-4', shot_key: '第四镜', sort_order: 4, duration: 5, image_prompt: '雨后天台',
      still: { asset_id: 'asset-4', current_version: null, locked: false, versions: [], job: null },
    },
  );
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
  assert.doesNotMatch(html, /<article[^>]*data-ratio=/, 'ratio belongs to the inner preview, not the card');
  assert.match(html, /<div class="nc-sdp-preview" data-ratio="9:16">/);
  assert.doesNotMatch(html, /<script>|<img src=x|<b>old/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /分镜画面提示词[\s\S]*readonly[\s\S]*本次生成补充要求/);
  assert.match(html, /最终提交提示词预览/);
  assert.match(html, /data-version="12"[\s\S]*data-action="lock-version"/);
  const historyState = sampleState();
  historyState.shots[1].still.versions.unshift({
    id: 'version-id-history', version: 10, job_id: 80,
    url: '/history.png', prompt: 'historical', ratio: '9:16', cost: 8,
    status: 'done', created_at: 0,
  });
  const historyHtml = production.renderWorkspace(historyState, { selectedShotId: 'shot-1' });
  assert.match(historyHtml,
    /data-version-id="version-id-history"[\s\S]*src="\/history\.png"[\s\S]*data-version="10"/,
    'every historical version remains previewable and selectable');

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
  const blockedState = terminalState();
  blockedState.shots[0].still.locked = true;
  blockedState.handoff_blocked = true;
  blockedState.handoff_blockers = [
    { code: 'active_job', shot_id: 'shot-2', message: '<关键帧任务运行中>' },
  ];
  const blocked = production.normalizeState(blockedState);
  assert.equal(blocked.handoff_blocked, true);
  assert.deepEqual(blocked.handoff_blockers.map((item) => item.code), ['active_job']);
  const blockedHtml = production.renderWorkspace(blockedState);
  assert.match(blockedHtml, /&lt;关键帧任务运行中&gt;/);
  assert.match(blockedHtml, /data-action="confirm-stage"[^>]*disabled/);
  const mixedBatchHtml = production.renderWorkspace(batchState(), { selectedShotId: 'shot-1' });
  assert.match(mixedBatchHtml, /data-action="generate-batch"/);
  assert.doesNotMatch(mixedBatchHtml, /data-action="generate-batch"[^>]*disabled/,
    'batch remains available when another unlocked idle shot is eligible');
}

function testResponsiveCssContract() {
  const css = fs.readFileSync(path.join(
    __dirname, '../site/workbench/canvas/canvas-short-drama-production.css',
  ), 'utf8');
  assert.match(css, /grid-template-columns:\s*260px\s+minmax\(0,\s*1fr\)\s+300px/);
  assert.match(css, /\[data-ratio="9:16"\][^{]*\{[^}]*aspect-ratio:\s*9\s*\/\s*16/s);
  assert.match(css, /\[data-ratio="16:9"\][^{]*\{[^}]*aspect-ratio:\s*16\s*\/\s*9/s);
  assert.match(css, /\.nc-sdp-preview\s+img[^{]*\{[^}]*object-fit:\s*contain/s);
  assert.doesNotMatch(css, /\.nc-sdp-candidate-grid\s*>\s*\[data-ratio=/,
    'candidate articles must not receive an aspect ratio');
  assert.doesNotMatch(css, /\.nc-sdp-candidate\s*\{[^}]*overflow:\s*hidden/s,
    'candidate articles must not clip their action controls');
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
      if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({
        cost: 24, count: 2, kind: 'still', quote_token: 'quote-101', expires_at: 9999999999,
        shot_id: 'shot-2', base_prompt: '雨夜门口', user_direction: '',
        compiled_prompt: 'Shot prompt: 雨夜门口', source_prompt_hash: 'hash-rain',
      });
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
    prompt: '', mode: 'single', count: 2,
  });
  assert.equal(calls[3].quote.compiled_prompt, 'Shot prompt: 雨夜门口');
  assert.deepEqual(submit.options.body, Object.assign({}, quoteBody, { quote_token: 'quote-101' }));
  assert.equal(submit.options.headers['Idempotency-Key'], 'still-action-1');
  workspace.destroy();

  let submissions = 0;
  const cancelled = production.createWorkspace({
    projectId: 'project/one', document: null,
    confirm() { calls.push({ path: 'cancel-confirm' }); return false; },
    client: {
      json(path) {
        if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(sampleState());
        if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still', quote_token: 'quote-cancel' });
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
      if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still', quote_token: 'quote-dedupe' });
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
        if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still', quote_token: 'quote-stale' });
        if (path === '/api/gen/short-drama/generate-stills') return Promise.resolve({ job_id: 101, shot_id: 'shot-2' });
        throw new Error(`unexpected route ${path}`);
      },
    },
  });
  await closing.ready;
  closing.selectShot('shot-2');
  const pending = closing.generateCurrent();
  for (let index = 0; index < 12 && timers.length === 0; index += 1) await Promise.resolve();
  assert.equal(timers.length, 1);
  closing.destroy();
  assert.deepEqual(cleared, [timers[0].id]);
  await assert.rejects(pending, /destroyed/i);
  assert.throws(() => closing.render(), /destroyed/i, 'destroyed workspaces reject stale rendering');
  await assert.rejects(closing.refresh(), /destroyed/i);
}

async function testOnChangePublishesOnlyProductionSummaryAfterServerUpdates() {
  let state = terminalState(10);
  const summaries = [];
  const client = {
    json(path, options = {}) {
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
  const workspace = production.createWorkspace({
    projectId: state.project_id, client, document: null,
    onChange(summary) { summaries.push(clone(summary)); },
  });
  await workspace.ready;
  assert.deepEqual(summaries, [], 'initial recovery load must not schedule a duplicate canvas save');
  await workspace.refresh();
  assert.deepEqual(summaries, [], 'an unchanged explicit refresh must not schedule a duplicate canvas save');

  workspace.selectShot('shot-2');
  await workspace.selectVersion(2, true);
  await workspace.refresh();
  assert.equal(summaries.length, 1, 'an unchanged poll or refresh publishes a server summary only once');
  await workspace.confirmStage();

  assert.equal(summaries.length, 2);
  assert.equal(summaries[0].stage, 'stills_review');
  assert.equal(summaries[1].stage, 'voice_review');
  assert.equal(summaries[1].revision, 12);
  assert.deepEqual(Object.keys(summaries[1]).sort(), [
    'point_budget', 'project_id', 'ratio', 'reserved_points', 'revision', 'spent_points', 'stage',
  ]);
  for (const summary of summaries) {
    const encoded = JSON.stringify(summary);
    assert.doesNotMatch(encoded, /shot|asset|version|job|url|prompt|reference/i,
      'production summaries must never leak detailed production state into the canvas node');
  }
  workspace.destroy();
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
        if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still', quote_token: 'quote-destroy' });
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
  for (let index = 0; index < 20 && !resolveSubmit; index += 1) await Promise.resolve();
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
        if (path === '/api/gen/short-drama/asset-quote') return Promise.resolve({ cost: 24, count: 2, kind: 'still', quote_token: 'quote-timeout' });
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
  for (let index = 0; index < 20 && !rejectSubmit; index += 1) await Promise.resolve();
  workspace.destroy();
  const timeout = new Error('request timed out'); timeout.code = 'timeout';
  rejectSubmit(timeout);
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  assert.equal(submissions, 1, 'destroy prevents a late timeout from starting a retry');
  await assert.rejects(pending, /destroyed/i);
}

async function testConfirmationRequiresEveryLockedCurrentMatchingDoneVersion() {
  const variants = [];
  const unlocked = sampleState();
  variants.push(unlocked);
  const missingCurrent = sampleState();
  missingCurrent.shots[0].still.locked = true;
  variants.push(missingCurrent);
  const failedCurrent = terminalState();
  failedCurrent.shots[0].still.locked = true;
  failedCurrent.shots[0].still.versions[0].status = 'failed';
  variants.push(failedCurrent);
  const wrongRatio = terminalState();
  wrongRatio.shots[0].still.locked = true;
  wrongRatio.shots[0].still.versions[0].ratio = '16:9';
  variants.push(wrongRatio);
  const missingRatio = terminalState();
  missingRatio.shots[0].still.locked = true;
  delete missingRatio.shots[0].still.versions[0].ratio;
  variants.push(missingRatio);
  const invalidRatio = terminalState();
  invalidRatio.shots[0].still.locked = true;
  invalidRatio.shots[0].still.versions[0].ratio = 'constructor';
  variants.push(invalidRatio);
  const activeJob = terminalState();
  activeJob.shots[0].still.locked = true;
  activeJob.shots[0].still.job = {
    id: 'active-handoff', job_id: 999, kind: 'still',
    status: 'running', quoted_cost: 12,
  };
  variants.push(activeJob);

  for (const state of variants) {
    let requests = 0;
    const workspace = production.createWorkspace({
      projectId: state.project_id, document: null,
      client: {
        json(path) {
          requests += 1;
          if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(clone(state));
          throw new Error(`confirmation must not request ${path}`);
        },
      },
    });
    await workspace.ready;
    await assert.rejects(workspace.confirmStage(), /locked|current|completed|关键帧/i);
    assert.equal(requests, 1, 'invalid confirmation performs no POST');
    workspace.destroy();
  }

  const blocked = terminalState();
  blocked.shots[0].still.locked = true;
  blocked.handoff_blocked = true;
  blocked.handoff_blockers = [
    { code: 'active_job', shot_id: 'shot-2', message: '关键帧任务仍在运行中' },
  ];
  let blockedRequests = 0;
  const blockedWorkspace = production.createWorkspace({
    projectId: blocked.project_id, document: null,
    client: {
      json(path) {
        blockedRequests += 1;
        if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(clone(blocked));
        throw new Error(`blocked confirmation must not request ${path}`);
      },
    },
  });
  await blockedWorkspace.ready;
  await assert.rejects(blockedWorkspace.confirmStage(), /关键帧任务仍在运行中/);
  assert.equal(blockedRequests, 1, 'server blocker prevents confirmation POST');
  blockedWorkspace.destroy();
}

async function testTrueBatchQuotesConfirmsSubmitsAndPollsEligibleShots() {
  let state = batchState();
  const calls = [];
  const submitAttempts = Object.create(null);
  let keyIndex = 0;
  let confirmations = 0;
  let productionGets = 0;
  const client = {
    json(path, options = {}) {
      calls.push({ path, options: clone(options) });
      if (path.startsWith('/api/gen/short-drama/production?')) {
        productionGets += 1;
        if (productionGets <= 3) return Promise.resolve(clone(state));
        const next = clone(state);
        for (const shot of next.shots) {
          if (!['shot-2', 'shot-4'].includes(shot.id)) continue;
          if (productionGets === 4) {
            shot.still.job = {
              id: `link-${shot.id}`, job_id: shot.id === 'shot-2' ? 202 : 204,
              kind: 'still', status: 'running', quoted_cost: shot.id === 'shot-2' ? 10 : 20,
            };
          } else {
            const jobId = shot.id === 'shot-2' ? 202 : 204;
            shot.still.job = null;
            shot.still.current_version = 1;
            shot.still.versions = [
              { id: `${shot.id}-done`, version: 1, job_id: jobId, url: '/done.png', prompt: shot.image_prompt,
                ratio: '9:16', cost: 10, status: 'done', created_at: 4 },
            ];
          }
        }
        return Promise.resolve(next);
      }
      if (path === '/api/gen/short-drama/asset-quote') {
        const shot = state.shots.find((item) => item.id === options.body.shot_id);
        return Promise.resolve({
          cost: options.body.shot_id === 'shot-2' ? 10 : 20, count: 2, kind: 'still',
          quote_token: 'quote-batch-'+options.body.shot_id,
          expires_at: 9999999999, shot_id: options.body.shot_id,
          base_prompt: shot.image_prompt, user_direction: options.body.prompt,
          compiled_prompt: `compiled prompt for ${options.body.shot_id}`,
          source_prompt_hash: options.body.shot_id === 'shot-2' ? '2'.repeat(64) : '4'.repeat(64),
        });
      }
      if (path === '/api/gen/short-drama/generate-stills') {
        const shotId = options.body.shot_id;
        submitAttempts[shotId] = (submitAttempts[shotId] || 0) + 1;
        if (shotId === 'shot-2' && submitAttempts[shotId] === 1) {
          const error = new Error('request timed out'); error.code = 'timeout';
          return Promise.reject(error);
        }
        return Promise.resolve({ job_id: shotId === 'shot-2' ? 202 : 204, shot_id: shotId });
      }
      throw new Error(`unexpected route ${path}`);
    },
  };
  const workspace = production.createWorkspace({
    projectId: state.project_id, client, document: null, pollIntervalMs: 0,
    idempotencyKey(shotId) { keyIndex += 1; return `batch-key-${keyIndex}-${shotId}`; },
    confirm(totalCost, quote, bodies) {
      confirmations += 1;
      assert.equal(totalCost, 30);
      assert.equal(quote.cost, 30);
      assert.equal(quote.shot_count, 2);
      assert.deepEqual(quote.shot_ids, ['shot-2', 'shot-4']);
      assert.deepEqual(quote.quotes.map((item) => ({
        shot_id: item.shot_id, base_prompt: item.base_prompt,
        compiled_prompt: item.compiled_prompt,
      })), [
        {
          shot_id: 'shot-2', base_prompt: state.shots.find((item) => item.id === 'shot-2').image_prompt,
          compiled_prompt: 'compiled prompt for shot-2',
        },
        {
          shot_id: 'shot-4', base_prompt: state.shots.find((item) => item.id === 'shot-4').image_prompt,
          compiled_prompt: 'compiled prompt for shot-4',
        },
      ]);
      assert.deepEqual(bodies.map((body) => body.shot_id), ['shot-2', 'shot-4']);
      return true;
    },
  });
  await workspace.ready;
  const first = workspace.generateBatch();
  const second = workspace.generateBatch();
  assert.equal(first, second, 'double-clicking one batch shares the action');
  await first;

  const quoteCalls = calls.filter((call) => call.path === '/api/gen/short-drama/asset-quote');
  const submitCalls = calls.filter((call) => call.path === '/api/gen/short-drama/generate-stills');
  assert.deepEqual(quoteCalls.map((call) => call.options.body.shot_id), ['shot-2', 'shot-4']);
  assert.ok(quoteCalls.every((call) => call.options.body.mode === 'batch' && call.options.body.count === 2));
  assert.deepEqual(submitCalls.map((call) => call.options.body.shot_id), ['shot-2', 'shot-2', 'shot-4']);
  assert.deepEqual(submitCalls.map((call) => call.options.headers['Idempotency-Key']), [
    'batch-key-1-shot-2', 'batch-key-1-shot-2', 'batch-key-2-shot-4',
  ]);
  assert.equal(confirmations, 1);
  assert.equal(keyIndex, 2, 'one stable key is created per eligible shot');
  assert.equal(calls.filter((call) => call.path.startsWith('/api/gen/short-drama/production?')).length, 5,
    'poll survives one stale missing snapshot, observes running, then waits for every terminal shot');
  assert.equal(calls.some((call) => call.options.body && ['shot-1', 'shot-3'].includes(call.options.body.shot_id)), false,
    'locked and active shots are never quoted or submitted');
  workspace.destroy();

  let cancelledSubmits = 0;
  let cancelledConfirms = 0;
  const cancelled = production.createWorkspace({
    projectId: state.project_id, document: null,
    confirm(cost, quote) {
      cancelledConfirms += 1;
      assert.equal(cost, 30); assert.equal(quote.shot_count, 2);
      return false;
    },
    client: {
      json(path, options = {}) {
        if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(clone(state));
        if (path === '/api/gen/short-drama/asset-quote') {
          return Promise.resolve({ cost: options.body.shot_id === 'shot-2' ? 10 : 20, count: 2, kind: 'still', quote_token: 'quote-'+options.body.shot_id });
        }
        if (path === '/api/gen/short-drama/generate-stills') cancelledSubmits += 1;
        return Promise.resolve({});
      },
    },
  });
  await cancelled.ready;
  assert.equal(await cancelled.generateBatch(), null);
  assert.equal(cancelledConfirms, 1);
  assert.equal(cancelledSubmits, 0);
  cancelled.destroy();

  const noEligibleState = batchState();
  noEligibleState.shots.forEach((shot) => {
    if (!shot.still.job) shot.still.locked = true;
  });
  let noEligibleRequests = 0;
  const noEligible = production.createWorkspace({
    projectId: noEligibleState.project_id, document: null,
    client: {
      json(path) {
        noEligibleRequests += 1;
        if (path.startsWith('/api/gen/short-drama/production?')) return Promise.resolve(clone(noEligibleState));
        throw new Error(`unexpected route ${path}`);
      },
    },
  });
  await noEligible.ready;
  assert.equal(await noEligible.generateBatch(), null);
  assert.equal(noEligibleRequests, 1, 'no eligible shots performs no quote or submit request');
  noEligible.destroy();
}

function fakeHost() {
  const listeners = Object.create(null);
  const counts = { added: 0, removed: 0, painted: 0 };
  let html = '';
  return {
    get innerHTML() { return html; },
    set innerHTML(value) { html = value; counts.painted += 1; },
    counts,
    addEventListener(type, handler) { listeners[type] = handler; counts.added += 1; },
    removeEventListener(type, handler) {
      if (listeners[type] === handler) delete listeners[type];
      counts.removed += 1;
    },
    emit(type, event) { return listeners[type](event); },
  };
}

async function testPromptInputUpdatesStateWithoutRepaintingTheHost() {
  const host = fakeHost();
  const workspace = production.createWorkspace({
    projectId: 'project/one', host,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) return sampleState();
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  const paints = host.counts.painted;
  const field = {
    value: '连续输入不会丢焦点',
    parentNode: host,
    getAttribute(name) { return name === 'data-field' ? 'prompt' : null; },
  };
  host.emit('input', { target: field });
  assert.equal(host.counts.painted, paints, 'input must not replace the focused textarea');
  assert.equal(workspace.getState().prompts['shot-1'], '连续输入不会丢焦点');
  workspace.destroy();
}

async function testSynchronousClientThrowsAreCapturedAcrossControllerBoundaries() {
  const timers = [];
  const host = fakeHost();
  const initial = production.createWorkspace({
    projectId: 'project/one', host,
    setTimeoutImpl(fn) { timers.push(fn); return timers.length; },
    clearTimeoutImpl() {},
    client: { json() { throw new Error('<sync init>'); } },
  });
  assert.equal(await initial.ready, null, 'synchronous initial GET is captured by ready');
  assert.equal(initial.getState().busy, false);
  assert.equal(initial.getState().error, '<sync init>');
  assert.match(initial.render(), /&lt;sync init&gt;/);
  initial.destroy();
  assert.deepEqual(
    { added: host.counts.added, removed: host.counts.removed },
    { added: 2, removed: 2 },
  );
  assert.equal(timers.length, 0);

  let refreshGets = 0;
  const refreshing = production.createWorkspace({
    projectId: 'project/one', document: null,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?') && refreshGets++ === 0) return sampleState();
      throw new Error('<sync refresh>');
    } },
  });
  await refreshing.ready;
  await assert.rejects(refreshing.refresh(), /sync refresh/);
  assert.equal(refreshing.getState().busy, false);
  assert.equal(refreshing.getState().error, '<sync refresh>');
  refreshing.destroy();

  const mutating = production.createWorkspace({
    projectId: 'project/one', document: null,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) return sampleState();
      throw new Error('<sync mutation>');
    } },
  });
  await mutating.ready;
  await assert.rejects(mutating.selectVersion(11, true), /sync mutation/);
  assert.equal(mutating.getState().busy, false);
  assert.equal(mutating.getState().error, '<sync mutation>');
  mutating.destroy();

  const generating = production.createWorkspace({
    projectId: 'project/one', document: null, confirm: () => true,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) return sampleState();
      if (path === '/api/gen/short-drama/asset-quote') throw new Error('<sync generate>');
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await generating.ready;
  await assert.rejects(generating.generateCurrent(), /sync generate/);
  assert.equal(generating.getState().busy, false);
  assert.equal(generating.getState().error, '<sync generate>');
  generating.destroy();

  let pollGets = 0;
  const polling = production.createWorkspace({
    projectId: 'project/one', document: null, confirm: () => true, pollIntervalMs: 0,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        pollGets += 1;
        if (pollGets === 1) return sampleState();
        throw new Error('<sync poll>');
      }
      if (path === '/api/gen/short-drama/asset-quote') return { cost: 24, count: 2, kind: 'still', quote_token: 'quote-sync' };
      if (path === '/api/gen/short-drama/generate-stills') return { job_id: 101, shot_id: 'shot-1' };
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await polling.ready;
  await assert.rejects(polling.generateCurrent(), /sync poll/);
  assert.equal(polling.getState().busy, false);
  assert.equal(polling.getState().error, '<sync poll>');
  polling.destroy();
}

async function testMalformedJobStatusesAreNeverActive() {
  for (const status of ['constructor', 'toString', '__proto__']) {
    const state = sampleState();
    state.shots[1].still.job = {
      id: `bad-${status}`, job_id: 909, kind: 'still', status, quoted_cost: 1,
    };
    const html = production.renderWorkspace(state, { selectedShotId: 'shot-1' });
    assert.doesNotMatch(html, new RegExp(`data-status="${status}"`));

    let gets = 0;
    const workspace = production.createWorkspace({
      projectId: state.project_id, document: null, confirm: () => true, pollIntervalMs: 0,
      client: { json(path) {
        if (path.startsWith('/api/gen/short-drama/production?')) { gets += 1; return clone(state); }
        if (path === '/api/gen/short-drama/asset-quote') return { cost: 1, count: 2, kind: 'still', quote_token: 'quote-status' };
        if (path === '/api/gen/short-drama/generate-stills') return { job_id: 909, shot_id: 'shot-1' };
        throw new Error(`unexpected route ${path}`);
      } },
    });
    await workspace.ready;
    await assert.rejects(workspace.generateCurrent(), /没有成功候选图/);
    assert.equal(gets, 4, `${status} is ignored and bounded missing-job polling rejects`);
    workspace.destroy();
  }
}

async function testBatchDestroyDuringFirstSubmitStopsLaterPaidWork() {
  const state = batchState();
  let resolveFirstSubmit;
  const submittedShots = [];
  let productionGets = 0;
  const timers = [];
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null, confirm: () => true,
    setTimeoutImpl(fn) { timers.push(fn); return timers.length; },
    clearTimeoutImpl() {},
    idempotencyKey(shotId) { return `destroy-key-${shotId}`; },
    client: { json(path, options = {}) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        productionGets += 1; return clone(state);
      }
      if (path === '/api/gen/short-drama/asset-quote') return { cost: 10, count: 2, kind: 'still', quote_token: 'quote-stop' };
      if (path === '/api/gen/short-drama/generate-stills') {
        submittedShots.push(options.body.shot_id);
        if (options.body.shot_id === 'shot-2') {
          return new Promise((resolve) => { resolveFirstSubmit = resolve; });
        }
        return { job_id: 404, shot_id: options.body.shot_id };
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  const pending = workspace.generateBatch();
  pending.catch(() => {});
  for (let index = 0; index < 40 && !resolveFirstSubmit; index += 1) await Promise.resolve();
  assert.equal(typeof resolveFirstSubmit, 'function');
  assert.deepEqual(submittedShots, ['shot-2']);
  workspace.destroy();
  resolveFirstSubmit({ job_id: 202, shot_id: 'shot-2' });
  await assert.rejects(pending, /destroyed/i);
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(submittedShots, ['shot-2'], 'late first response cannot start the next paid shot');
  assert.equal(productionGets, 3, 'destroyed batch performs initial load, consent preflight, and first-wave preflight');
  assert.equal(timers.length, 0, 'destroyed batch leaves no polling timer');
}

async function testPartialBatchFailureRecoversSubmittedShotsBeforeRetryingEligibleWork() {
  const initial = batchState();
  let productionGets = 0;
  let keyIndex = 0;
  let confirms = 0;
  const calls = [];
  const client = { json(path, options = {}) {
    calls.push({ path, options: clone(options) });
    if (path.startsWith('/api/gen/short-drama/production?')) {
      productionGets += 1;
      if (productionGets <= 3) return clone(initial);
      const next = clone(initial);
      const shotA = next.shots.find((shot) => shot.id === 'shot-2');
      const shotB = next.shots.find((shot) => shot.id === 'shot-4');
      if (productionGets === 4) {
        shotA.still.job = { id: 'link-a', job_id: 202, kind: 'still', status: 'running', quoted_cost: 10 };
      } else {
        shotA.still.job = null;
        shotA.still.current_version = 1;
        shotA.still.versions = [
          { id: 'a-done', version: 1, job_id: 202, url: '/a.png', prompt: shotA.image_prompt,
            ratio: '9:16', cost: 10, status: 'done', created_at: 5 },
        ];
      }
      if (productionGets >= 7) {
        shotB.still.job = null;
        shotB.still.current_version = 1;
        shotB.still.versions = [
          { id: 'b-done', version: 1, job_id: 304, url: '/b.png', prompt: shotB.image_prompt,
            ratio: '9:16', cost: 20, status: 'done', created_at: 6 },
        ];
      }
      return next;
    }
    if (path === '/api/gen/short-drama/asset-quote') {
      return { cost: options.body.shot_id === 'shot-2' ? 10 : 20, count: 2, kind: 'still', quote_token: 'quote-partial-'+options.body.shot_id };
    }
    if (path === '/api/gen/short-drama/generate-stills') {
      const shotId = options.body.shot_id;
      const key = options.headers['Idempotency-Key'];
      if (shotId === 'shot-2') return { job_id: 202, shot_id: shotId };
      if (key === 'partial-key-2-shot-4') {
        const previous = calls.filter((call) => call.path === path &&
          call.options.headers['Idempotency-Key'] === key).length;
        if (previous <= 2) {
          const error = new Error(previous === 1 ? 'B timeout' : 'B final failure');
          if (previous === 1) error.code = 'timeout';
          throw error;
        }
        return { job_id: 304, shot_id: shotId };
      }
      return { job_id: 304, shot_id: shotId };
    }
    throw new Error(`unexpected route ${path}`);
  } };
  const workspace = production.createWorkspace({
    projectId: initial.project_id, client, document: null, pollIntervalMs: 0,
    idempotencyKey(shotId) { keyIndex += 1; return `partial-key-${keyIndex}-${shotId}`; },
    confirm(total, quote) {
      confirms += 1;
      if (confirms === 1) { assert.equal(total, 30); assert.equal(quote.shot_count, 2); }
      else { assert.equal(total, 20); assert.deepEqual(quote.shot_ids, ['shot-4']); }
      return true;
    },
  });
  await workspace.ready;
  await assert.rejects(workspace.generateBatch(), /B final failure/);
  assert.equal(workspace.getState().error, 'B final failure', 'primary submit error remains visible');
  const recoveredA = workspace.getState().shots.find((shot) => shot.id === 'shot-2');
  assert.equal(recoveredA.still.current_version, 1, 'successful A is reconciled before the batch rejects');
  assert.equal(productionGets, 5, 'partial failure polls A through running and terminal');

  await workspace.generateBatch();
  const quoteShots = calls.filter((call) => call.path === '/api/gen/short-drama/asset-quote')
    .map((call) => call.options.body.shot_id);
  const submitCalls = calls.filter((call) => call.path === '/api/gen/short-drama/generate-stills');
  assert.deepEqual(quoteShots, ['shot-2', 'shot-4'],
    'an ambiguous retry reuses the confirmed quote instead of creating a new operation');
  assert.equal(submitCalls.filter((call) => call.options.body.shot_id === 'shot-2').length, 1);
  assert.deepEqual(submitCalls.filter((call) => call.options.body.shot_id === 'shot-4')
    .map((call) => call.options.headers['Idempotency-Key']), [
      'partial-key-2-shot-4', 'partial-key-2-shot-4', 'partial-key-2-shot-4',
    ]);
  assert.equal(keyIndex, 2);
  workspace.destroy();
}

async function testPollTracksOnlyTheExactSubmittedJobId() {
  const state = sampleState();
  let gets = 0;
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null, confirm: () => true, pollIntervalMs: 0,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        gets += 1;
        const next = clone(state);
        const shot = next.shots[0];
        if (gets === 2) {
          shot.still.job = { id: 'unrelated', job_id: 400, kind: 'still', status: 'running', quoted_cost: 1 };
        } else if (gets === 4) {
          shot.still.job = { id: 'target', job_id: 500, kind: 'still', status: 'running', quoted_cost: 1 };
        } else {
          shot.still.job = null;
        }
        return next;
      }
      if (path === '/api/gen/short-drama/asset-quote') return { cost: 1, count: 2, kind: 'still', quote_token: 'quote-target' };
      if (path === '/api/gen/short-drama/generate-stills') return { job_id: 500, shot_id: 'shot-2' };
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  workspace.selectShot('shot-2');
  await assert.rejects(workspace.generateCurrent(), /没有成功候选图/);
  assert.equal(gets, 5,
    'unrelated active job and its disappearance do not complete the never-observed target job');
  workspace.destroy();
}

async function testPartialRecoveryPollFailureKeepsSubmittedGuardAndPrimaryError() {
  const initial = batchState();
  let gets = 0;
  let secondConfirmShots = null;
  const quoteShots = [];
  const workspace = production.createWorkspace({
    projectId: initial.project_id, document: null, pollIntervalMs: 0,
    idempotencyKey(shotId) { return `guard-${shotId}`; },
    confirm(_total, quote) {
      if (quoteShots.length > 2) { secondConfirmShots = quote.shot_ids; return false; }
      return true;
    },
    client: { json(path, options = {}) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        gets += 1;
        if (gets === 5) throw new Error('recovery poll failed');
        if (gets === 4) {
          const active = clone(initial);
          active.shots.find((shot) => shot.id === 'shot-2').still.job = {
            id: 'active-a', job_id: 202, kind: 'still', status: 'running', quoted_cost: 10,
          };
          return active;
        }
        return clone(initial);
      }
      if (path === '/api/gen/short-drama/asset-quote') {
        quoteShots.push(options.body.shot_id);
        return { cost: options.body.shot_id === 'shot-2' ? 10 : 20, count: 2, kind: 'still', quote_token: 'quote-guard-'+options.body.shot_id };
      }
      if (path === '/api/gen/short-drama/generate-stills') {
        if (options.body.shot_id === 'shot-2') return { job_id: 202, shot_id: 'shot-2' };
        throw new Error('primary B failure');
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  await assert.rejects(workspace.generateBatch(), /primary B failure/);
  assert.equal(workspace.getState().error, 'primary B failure');
  assert.equal(gets, 6, 'failed poll is followed by one refresh attempt');
  assert.deepEqual(quoteShots, ['shot-2', 'shot-4']);
  assert.equal(secondConfirmShots, null, 'ambiguous work is not silently quoted under a new key');
  workspace.destroy();
}

async function testKnownJobMissingFallbackAndDelayedAppearance() {
  const state = sampleState();
  let delayedGets = 0;
  const delayed = production.createWorkspace({
    projectId: state.project_id, document: null, confirm: () => true, pollIntervalMs: 0,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        delayedGets += 1;
        const next = clone(state);
        const shot = next.shots[0];
        if (delayedGets === 4) {
          shot.still.job = { id: 'late', job_id: 500, kind: 'still', status: 'running', quoted_cost: 1 };
        } else {
          shot.still.job = null;
        }
        return next;
      }
      if (path === '/api/gen/short-drama/asset-quote') return { cost: 1, count: 2, kind: 'still', quote_token: 'quote-late' };
      if (path === '/api/gen/short-drama/generate-stills') return { job_id: 500, shot_id: 'shot-2' };
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await delayed.ready;
  delayed.selectShot('shot-2');
  await assert.rejects(delayed.generateCurrent(), /没有成功候选图/);
  assert.equal(delayedGets, 5, 'two missing snapshots do not pre-empt a target that then appears');
  delayed.destroy();

  const timers = [];
  let fastGets = 0;
  const fast = production.createWorkspace({
    projectId: state.project_id, document: null, confirm: () => true,
    setTimeoutImpl(fn) { timers.push(fn); return timers.length; },
    clearTimeoutImpl() {},
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) { fastGets += 1; return clone(state); }
      if (path === '/api/gen/short-drama/asset-quote') return { cost: 1, count: 2, kind: 'still', quote_token: 'quote-missing' };
      if (path === '/api/gen/short-drama/generate-stills') return { job_id: 600, shot_id: 'shot-2' };
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await fast.ready;
  fast.selectShot('shot-2');
  let settled = false;
  const pending = fast.generateCurrent().then(
    (value) => { settled = true; return value; },
    (error) => { settled = true; throw error; },
  );
  pending.catch(() => {});
  for (let spin = 0; spin < 30 && timers.length === 0; spin += 1) await Promise.resolve();
  for (let attempt = 0; attempt < 3; attempt += 1) {
    assert.equal(timers.length, 1, `missing poll ${attempt + 1} is scheduled once`);
    timers.shift()();
    for (let spin = 0; spin < 30 && !settled && timers.length === 0; spin += 1) await Promise.resolve();
  }
  assert.equal(settled, true, 'three consecutive missing snapshots terminate a fast omitted failure');
  assert.equal(fast.getState().busy, false);
  assert.equal(timers.length, 0, 'terminal missing fallback leaves no timer');
  assert.equal(fastGets, 4, 'initial GET plus three bounded missing polls');
  await assert.rejects(pending, /状态缺失且没有成功候选图/);
  fast.destroy();
}

async function testSubmittedGuardsDisableBatchRendererUntilReconciled() {
  const pureHtml = production.renderWorkspace(batchState(), {
    selectedShotId: 'shot-1', submittedShotIds: ['shot-2', 'shot-4'],
  });
  assert.match(pureHtml, /data-action="generate-batch"[^>]*disabled/);
  assert.match(pureHtml, /已提交镜头正在同步生产状态/);

  const state = sampleState();
  let gets = 0;
  let quotes = 0;
  let submits = 0;
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null, confirm: () => true, pollIntervalMs: 0,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        gets += 1;
        if (gets > 3) throw new Error('poll unavailable');
        return clone(state);
      }
      if (path === '/api/gen/short-drama/asset-quote') { quotes += 1; return { cost: 1, count: 2, kind: 'still', quote_token: 'quote-render' }; }
      if (path === '/api/gen/short-drama/generate-stills') {
        submits += 1;
        return { job_id: 700, shot_id: 'shot-2' };
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  await assert.rejects(workspace.generateBatch(), /poll unavailable/);
  assert.match(workspace.render(), /data-action="generate-batch"[^>]*disabled/);
  assert.match(workspace.render(), /已提交镜头正在同步生产状态/);
  await assert.rejects(workspace.generateBatch(), /poll unavailable/,
    'guarded batch keeps reconciling instead of creating a new operation');
  assert.equal(workspace.getState().busy, false);
  assert.equal(workspace.getState().error, 'poll unavailable');
  assert.equal(quotes, 1);
  assert.equal(submits, 1, 'guarded shot is not resubmitted from stale state');
  workspace.destroy();
}

async function testTerminalFailedJobRejectsPollingAndRendersRetryableError() {
  let state = sampleState();
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null, confirm: () => true, pollIntervalMs: 0,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) return clone(state);
      if (path === '/api/gen/short-drama/asset-quote') {
        return { cost: 7, count: 2, kind: 'still', quote_token: 'quote-failed' };
      }
      if (path === '/api/gen/short-drama/generate-stills') {
        state.shots[0].still.job = {
          id: 'failed-link', job_id: 777, kind: 'still', status: 'failed', quoted_cost: 7,
          error: 'upstream rejected', refunded: true, refund_pending: false,
        };
        return { job_id: 777, shot_id: 'shot-1' };
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  workspace.selectShot('shot-2');
  await assert.rejects(workspace.generateCurrent(), /upstream rejected/);
  const html = workspace.render();
  assert.match(html, /upstream rejected/);
  assert.match(html, /已退款/);
  assert.match(html, /data-action="retry-current"/);
  assert.equal(workspace.getState().busy, false);
  workspace.destroy();
}

async function testSingleSubmitRequiresPositiveJobIdAndPollingOutageDoesNotResubmit() {
  const state = sampleState();
  let submits = 0;
  let gets = 0;
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null, confirm: () => true, pollIntervalMs: 0,
    idempotencyKey: () => 'single-stable-key',
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        gets += 1;
        if (gets > 1) throw new Error('poll unavailable');
        return clone(state);
      }
      if (path === '/api/gen/short-drama/asset-quote') return { cost: 1, quote_token: 'single-quote' };
      if (path === '/api/gen/short-drama/generate-stills') { submits += 1; return { job_id: 808, shot_id: 'shot-2' }; }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  await assert.rejects(workspace.generateCurrent(), /poll unavailable/);
  await assert.rejects(workspace.generateCurrent(), /poll unavailable/);
  assert.equal(submits, 1, 'polling outage must never cause a second paid submit');
  workspace.destroy();

  const invalid = production.createWorkspace({
    projectId: state.project_id, document: null, confirm: () => true, pollIntervalMs: 0,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) return clone(state);
      if (path === '/api/gen/short-drama/asset-quote') return { cost: 1, quote_token: 'bad-quote' };
      if (path === '/api/gen/short-drama/generate-stills') return { job_id: null, shot_id: 'shot-2' };
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await invalid.ready;
  await assert.rejects(invalid.generateCurrent(), /job_id/);
  invalid.destroy();
}

async function testAmbiguousSingleSubmitPersistsAcrossClicksAndControllerReload() {
  const values = new Map();
  const storage = {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  let state = sampleState();
  let quoteCalls = 0;
  let keyCalls = 0;
  const submitKeys = [];
  let submitCalls = 0;
  const client = { json(path, options = {}) {
    if (path.startsWith('/api/gen/short-drama/production?')) {
      if (submitCalls >= 5) {
        const done = clone(state);
        done.shots[1].still.current_version = 1;
        done.shots[1].still.versions = [{ id: 'v-909', version: 1, job_id: 909,
          url: '/done.png', prompt: 'city skyline', ratio: '9:16', cost: 7,
          status: 'done', created_at: 1 }];
        done.shots[1].still.job = null;
        return done;
      }
      return clone(state);
    }
    if (path === '/api/gen/short-drama/asset-quote') {
      quoteCalls += 1;
      return { cost: 7, quote_token: 'persisted-quote' };
    }
    if (path === '/api/gen/short-drama/generate-stills') {
      submitCalls += 1;
      submitKeys.push(options.headers['Idempotency-Key']);
      if (submitCalls <= 4) { const error = new Error('network response lost'); error.code = 'timeout'; return Promise.reject(error); }
      return { job_id: 909, shot_id: 'shot-2' };
    }
    throw new Error(`unexpected route ${path}`);
  } };
  const first = production.createWorkspace({
    projectId: state.project_id, document: null, client, storage, confirm: () => true,
    idempotencyKey() { keyCalls += 1; return 'persisted-single-key'; }, pollIntervalMs: 0,
  });
  await first.ready;
  await assert.rejects(first.generateCurrent(), /response lost/);
  await assert.rejects(first.generateCurrent(), /response lost/);
  assert.equal(values.size, 1, 'ambiguous attempt remains durable after repeated lost responses');
  first.destroy();

  const reloaded = production.createWorkspace({
    projectId: state.project_id, document: null, client, storage, confirm: () => {
      throw new Error('reload must not ask for consent or quote again');
    }, idempotencyKey() { keyCalls += 1; return 'new-key-must-not-be-used'; }, pollIntervalMs: 0,
  });
  await reloaded.ready;

  assert.equal(quoteCalls, 1);
  assert.equal(keyCalls, 1);
  assert.deepEqual(submitKeys, Array(submitKeys.length).fill('persisted-single-key'));
  assert.equal(values.size, 0, 'exact completed version clears persisted attempt');
  reloaded.destroy();
}

async function testDurableTerminalSubmitFailureClearsPersistedAttempt() {
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) || null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  let submits = 0, quotes = 0;
  const workspace = production.createWorkspace({
    projectId: 'project/one', document: null, storage, confirm: () => true,
    idempotencyKey: () => 'terminal-single-key', pollIntervalMs: 0,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) return sampleState();
      if (path === '/api/gen/short-drama/asset-quote') { quotes += 1; return { cost: 7, quote_token: 'terminal-quote-'+quotes }; }
      if (path === '/api/gen/short-drama/generate-stills') {
        submits += 1;
        const error = new Error('durable create failure');
        error.status = 500; error.code = 'still_job_create_failed'; error.data = { operation_terminal: true };
        return Promise.reject(error);
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  await assert.rejects(workspace.generateCurrent(), /durable create failure/);
  assert.equal(submits, 1);
  assert.equal(values.size, 0, 'durable terminal response releases the pending operation');
  await assert.rejects(workspace.generateCurrent(), /durable create failure/);
  assert.equal(quotes, 2, 'a fresh quote is allowed only after explicit terminal evidence');
  assert.equal(submits, 2);
  workspace.destroy();
}

async function testPendingSingleClearsOnlyForExactDurableOperationEvidence() {
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) || null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  const state = sampleState();
  state.shots[1].still.current_version = 1;
  state.shots[1].still.locked = true;
  state.shots[1].still.versions = [{ id: 'old', version: 1, job_id: 41, url: '/old.png',
    prompt: 'old', ratio: '9:16', cost: 1, status: 'done', created_at: 1 }];
  values.set('hq.short-drama.still.pending:'+state.project_id, JSON.stringify({
    projectId: state.project_id,
    body: { project_id: state.project_id, revision: state.revision, shot_id: 'shot-2',
      prompt: 'regenerate', mode: 'retry', count: 2, quote_token: 'old-quote' },
    key: 'exact-correlation-key', jobId: 909,
  }));
  let submits = 0;
  const unrelated = clone(state);
  unrelated.shots[1].still.job = { job_id: 808, status: 'failed', error: 'other job' };
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null, storage, pollIntervalMs: 0,
    confirm: () => { throw new Error('must not quote/confirm again'); },
    client: { json(path, options = {}) {
      if (path.startsWith('/api/gen/short-drama/production?')) return clone(unrelated);
      if (path === '/api/gen/short-drama/generate-stills') {
        submits += 1;
        assert.equal(options.headers['Idempotency-Key'], 'exact-correlation-key');
        return { job_id: 909, shot_id: 'shot-2' };
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  assert.equal(submits, 0, 'known job is polled, not replaced by unrelated terminal evidence');
  assert.equal(values.size, 1, 'old locked/current version and unrelated failure cannot clear pending');
  workspace.destroy();

  values.set('hq.short-drama.still.pending:'+state.project_id, JSON.stringify({
    projectId: state.project_id,
    body: { project_id: state.project_id, revision: state.revision, shot_id: 'shot-2',
      prompt: 'regenerate', mode: 'retry', count: 2, quote_token: 'old-quote' },
    key: 'exact-correlation-key', jobId: null,
  }));
  const unknown = production.createWorkspace({
    projectId: state.project_id, document: null, storage, pollIntervalMs: 0,
    confirm: () => { throw new Error('must not quote/confirm again'); },
    client: workspaceClient(),
  });
  function workspaceClient() {
    return { json(path, options = {}) {
      if (path.startsWith('/api/gen/short-drama/production?')) return clone(unrelated);
      if (path === '/api/gen/short-drama/generate-stills') {
        submits += 1;
        assert.equal(options.headers['Idempotency-Key'], 'exact-correlation-key');
        return { job_id: 909, shot_id: 'shot-2' };
      }
      throw new Error(`unexpected route ${path}`);
    } };
  }
  await unknown.ready;
  assert.equal(submits, 1, 'unknown job resubmits the persisted body/key before polling');
  assert.equal(values.size, 1, 'unrelated failure cannot clear an unknown persisted operation');
  unknown.destroy();
}

async function testMalformedSuccessAndRefundPendingStayOnSamePersistedAttempt() {
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) || null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  const state = sampleState();
  let quotes = 0, keys = 0, submits = 0;
  const seenKeys = [];
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null, storage, pollIntervalMs: 0,
    confirm: () => true,
    idempotencyKey() { keys += 1; return 'malformed-stable-key'; },
    client: { json(path, options = {}) {
      if (path.startsWith('/api/gen/short-drama/production?')) return clone(state);
      if (path === '/api/gen/short-drama/asset-quote') { quotes += 1; return { cost: 7, quote_token: 'q' }; }
      if (path === '/api/gen/short-drama/generate-stills') {
        submits += 1; seenKeys.push(options.headers['Idempotency-Key']);
        if (submits <= 2) return { ok: true };
        const error = new Error(submits === 3 ? 'refund is still reconciling' : 'durable create failure');
        error.status = submits === 3 ? 503 : 500;
        error.code = submits === 3 ? 'refund_pending' : 'still_job_create_failed';
        error.retryable = submits === 3;
        error.operation_terminal = submits !== 3;
        return Promise.reject(error);
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  await assert.rejects(workspace.generateCurrent(), /job_id/);
  await assert.rejects(workspace.generateCurrent(), /job_id/);
  await assert.rejects(workspace.generateCurrent(), /refund is still reconciling/);
  assert.equal(quotes, 1);
  assert.equal(keys, 1);
  assert.deepEqual(seenKeys, Array(seenKeys.length).fill('malformed-stable-key'));
  assert.equal(values.size, 1, 'malformed 200 and refund_pending remain durable and retryable');
  await assert.rejects(workspace.generateCurrent(), /durable create failure/);
  assert.equal(values.size, 0, 'only the same operation durable terminal response clears it');
  workspace.destroy();
}

async function testFailedRefundPendingJobStaysPersistedUntilRefundSettles() {
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) || null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  const state = sampleState();
  const storageKey = 'hq.short-drama.still.pending:'+state.project_id;
  values.set(storageKey, JSON.stringify({
    projectId: state.project_id,
    body: { project_id: state.project_id, revision: state.revision, shot_id: 'shot-2',
      prompt: 'same operation', mode: 'retry', count: 2, quote_token: 'persisted-quote' },
    key: 'refund-pending-key', jobId: 777,
  }));
  const pending = clone(state);
  pending.shots[0].still.job = { job_id: 777, status: 'failed', error: 'provider failed',
    refunded: false, refund_pending: true };
  const settled = clone(pending);
  settled.shots[0].still.job.refunded = true;
  settled.shots[0].still.job.refund_pending = false;
  let gets = 0, release;
  const waitForSettlement = new Promise((resolve) => { release = resolve; });
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null, storage, pollIntervalMs: 0,
    confirm: () => true,
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        gets += 1;
        if (gets === 1) return clone(pending);
        if (gets === 2) return waitForSettlement.then(() => clone(settled));
        return clone(settled);
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(gets, 2, 'known failed+refund_pending job keeps polling the same operation');
  assert.equal(values.size, 1, 'refund_pending failure must retain its persisted body/key');
  release();
  await workspace.ready;
  assert.equal(values.size, 0, 'confirmed refunded terminal job releases the persisted operation');
  workspace.selectShot('shot-2');
  assert.match(workspace.render(), /已退款/);
  workspace.destroy();
}

async function testSixShotBatchRunsInFiveJobWaves() {
  const state = sampleState({
    point_budget: 1000, spent_points: 0, reserved_points: 0,
    shots: Array.from({ length: 6 }, (_, index) => ({
      id: `wave-shot-${index + 1}`, shot_key: `第${index + 1}镜`,
      sort_order: index, duration: 5, image_prompt: `画面 ${index + 1}`,
      still: {
        asset_id: `wave-asset-${index + 1}`, current_version: null,
        locked: false, versions: [], job: null,
      },
    })),
  });
  const active = new Map();
  const events = [];
  let jobId = 700;
  let maxActive = 0;
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null, pollIntervalMs: 0,
    confirm(total, quote) {
      assert.equal(total, 6);
      assert.equal(quote.shot_count, 6);
      return true;
    },
    idempotencyKey(shotId) { return `wave-key-${shotId}`; },
    client: { json(path, options = {}) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        events.push('get');
        for (const [shotId, currentJobId] of active) {
          const shot = state.shots.find((item) => item.id === shotId);
          shot.still.current_version = 1;
          shot.still.versions = [{
            id: `done-${shotId}`, version: 1, job_id: currentJobId,
            url: `/${shotId}.png`, prompt: shot.image_prompt, ratio: '9:16',
            cost: 1, status: 'done', created_at: 1,
          }];
          shot.still.job = null;
        }
        active.clear();
        return clone(state);
      }
      if (path === '/api/gen/short-drama/asset-quote') {
        return {
          cost: 1, count: 2, kind: 'still',
          quote_token: `wave-quote-${options.body.shot_id}`,
          expires_at: Math.floor(Date.now() / 1000) + 300,
        };
      }
      if (path === '/api/gen/short-drama/generate-stills') {
        if (active.size >= 5) {
          const error = new Error('active job cap');
          error.status = 429; error.code = 'active_job_cap';
          error.data = { operation_terminal: true };
          throw error;
        }
        jobId += 1;
        events.push(`submit:${options.body.shot_id}`);
        active.set(options.body.shot_id, jobId);
        maxActive = Math.max(maxActive, active.size);
        return { job_id: jobId, shot_id: options.body.shot_id };
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  await workspace.generateBatch();
  assert.equal(maxActive, 5, 'the client never exceeds the shared active-job limit');
  assert.equal(state.shots.every((shot) => shot.still.current_version === 1), true);
  const sixthSubmit = events.indexOf('submit:wave-shot-6');
  assert.ok(events.slice(0, sixthSubmit).includes('get'),
    'the first wave reaches terminal state before the sixth shot is submitted');
  workspace.destroy();
}

async function testBatchBudgetIsCheckedBeforeConsentOrSubmission() {
  const state = sampleState({ point_budget: 50, spent_points: 40, reserved_points: 5 });
  let confirms = 0;
  let submits = 0;
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null,
    confirm() { confirms += 1; return true; },
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) return clone(state);
      if (path === '/api/gen/short-drama/asset-quote') {
        return { cost: 10, quote_token: 'over-budget', expires_at: Math.floor(Date.now() / 1000) + 300 };
      }
      if (path === '/api/gen/short-drama/generate-stills') {
        submits += 1; return { job_id: 999, shot_id: 'shot-2' };
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  await assert.rejects(workspace.generateBatch(), /预算|budget/i);
  assert.equal(confirms, 0, 'an impossible aggregate price is never presented as submit-ready');
  assert.equal(submits, 0, 'aggregate budget failure happens before the first deduction');
  workspace.destroy();
}

async function testFirstBatchWaveRechecksExpiredQuoteAndBudgetAfterConsent() {
  const state = sampleState({ point_budget: 100, spent_points: 0, reserved_points: 0 });
  let gets = 0;
  let quotes = 0;
  let confirms = 0;
  let submits = 0;
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null,
    confirm() {
      confirms += 1;
      state.point_budget = 5;
      return true;
    },
    client: { json(path) {
      if (path.startsWith('/api/gen/short-drama/production?')) {
        gets += 1;
        return clone(state);
      }
      if (path === '/api/gen/short-drama/asset-quote') {
        quotes += 1;
        return {
          cost: 10, quote_token: `first-wave-quote-${quotes}`,
          expires_at: quotes === 1 ? 1 : Math.floor(Date.now() / 1000) + 300,
        };
      }
      if (path === '/api/gen/short-drama/generate-stills') {
        submits += 1;
        return { job_id: 999, shot_id: 'shot-2' };
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  await assert.rejects(workspace.generateBatch(), /预算|budget/i);
  assert.equal(confirms, 1);
  assert.equal(quotes, 2, 'an expired confirmed quote is refreshed before the first wave');
  assert.equal(gets, 3, 'the first wave rechecks live project state after consent');
  assert.equal(submits, 0, 'a post-consent budget change blocks the first paid request');
  assert.deepEqual(workspace.getState().submittedShotIds, [],
    'budget failure releases every unstarted shot so single generation remains available');
  workspace.destroy();
}

async function testRevisionChangeClearsExpiredUnstartedBatchBeforeRequote() {
  const state = sampleState({ point_budget: 100, spent_points: 0, reserved_points: 0 });
  let quotes = 0;
  let confirms = 0;
  let submits = 0;
  const workspace = production.createWorkspace({
    projectId: state.project_id, document: null,
    confirm() {
      confirms += 1;
      if (confirms === 1) {
        state.revision += 1;
        return true;
      }
      return false;
    },
    client: { json(path, options = {}) {
      if (path.startsWith('/api/gen/short-drama/production?')) return clone(state);
      if (path === '/api/gen/short-drama/asset-quote') {
        quotes += 1;
        if (options.body.revision !== state.revision) {
          const error = new Error('revision conflict');
          error.status = 409;
          error.code = 'revision_conflict';
          throw error;
        }
        return {
          cost: 10, quote_token: `revision-quote-${quotes}`, expires_at: 1,
        };
      }
      if (path === '/api/gen/short-drama/generate-stills') {
        submits += 1;
        return { job_id: 999, shot_id: 'shot-2' };
      }
      throw new Error(`unexpected route ${path}`);
    } },
  });
  await workspace.ready;
  await assert.rejects(workspace.generateBatch(), /状态已变化|revision/i);
  assert.deepEqual(workspace.getState().submittedShotIds, [],
    'a stale unstarted batch is removed instead of retrying the old revision forever');
  assert.equal(quotes, 1, 'live state is checked before refreshing an expired quote');
  assert.equal(submits, 0);

  await workspace.refresh();
  assert.equal(await workspace.generateBatch(), null);
  assert.equal(quotes, 2, 'a fresh operation can quote under the new revision');
  assert.equal(confirms, 2);
  workspace.destroy();
}

async function testAmbiguousBatchAttemptPersistsItsKeyAcrossReload() {
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) || null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  const state = sampleState({ point_budget: 100, spent_points: 0, reserved_points: 0 });
  let quoteCalls = 0;
  let keyCalls = 0;
  let submitCalls = 0;
  const keys = [];
  const client = { json(path, options = {}) {
    if (path.startsWith('/api/gen/short-drama/production?')) {
      if (submitCalls >= 3) {
        const done = clone(state);
        done.shots[0].still.current_version = 1;
        done.shots[0].still.versions = [{
          id: 'batch-done', version: 1, job_id: 909, url: '/batch-done.png',
          prompt: done.shots[0].image_prompt, ratio: '9:16', cost: 7,
          status: 'done', created_at: 1,
        }];
        return done;
      }
      return clone(state);
    }
    if (path === '/api/gen/short-drama/asset-quote') {
      quoteCalls += 1;
      return {
        cost: 7, quote_token: 'persisted-batch-quote',
        expires_at: Math.floor(Date.now() / 1000) + 300,
      };
    }
    if (path === '/api/gen/short-drama/generate-stills') {
      submitCalls += 1;
      keys.push(options.headers['Idempotency-Key']);
      if (submitCalls <= 2) {
        const error = new Error('batch response lost'); error.code = 'timeout'; throw error;
      }
      return { job_id: 909, shot_id: 'shot-2' };
    }
    throw new Error(`unexpected route ${path}`);
  } };
  const first = production.createWorkspace({
    projectId: state.project_id, document: null, client, storage, confirm: () => true,
    idempotencyKey() { keyCalls += 1; return 'persisted-batch-key'; }, pollIntervalMs: 0,
  });
  await first.ready;
  await assert.rejects(first.generateBatch(), /response lost/);
  assert.equal(values.size, 1, 'an ambiguous batch attempt remains durable');
  first.destroy();

  const reloaded = production.createWorkspace({
    projectId: state.project_id, document: null, client, storage, pollIntervalMs: 0,
    confirm() { throw new Error('reload must not quote or confirm again'); },
    idempotencyKey() { keyCalls += 1; return 'new-key-must-not-be-used'; },
  });
  await reloaded.ready;
  assert.equal(quoteCalls, 1);
  assert.equal(keyCalls, 1);
  assert.deepEqual(keys, ['persisted-batch-key', 'persisted-batch-key', 'persisted-batch-key']);
  assert.equal(values.size, 0);
  reloaded.destroy();
}

async function main() {
  testNormalizationAndRenderer();
  testResponsiveCssContract();
  await testQuoteConfirmSubmitOrderAndCancellation();
  await testDeduplicationTimeoutRetryAndPolling();
  await testRevisionedMutationsStaleRefreshAndDestroy();
  await testOnChangePublishesOnlyProductionSummaryAfterServerUpdates();
  await testDestroyDuringSubmissionNeverCreatesAPollTimer();
  await testDestroyDuringTimedOutSubmissionNeverRetries();
  await testConfirmationRequiresEveryLockedCurrentMatchingDoneVersion();
  await testTrueBatchQuotesConfirmsSubmitsAndPollsEligibleShots();
  await testSynchronousClientThrowsAreCapturedAcrossControllerBoundaries();
  await testMalformedJobStatusesAreNeverActive();
  await testBatchDestroyDuringFirstSubmitStopsLaterPaidWork();
  await testPartialBatchFailureRecoversSubmittedShotsBeforeRetryingEligibleWork();
  await testPollTracksOnlyTheExactSubmittedJobId();
  await testPartialRecoveryPollFailureKeepsSubmittedGuardAndPrimaryError();
  await testKnownJobMissingFallbackAndDelayedAppearance();
  await testSubmittedGuardsDisableBatchRendererUntilReconciled();
  await testTerminalFailedJobRejectsPollingAndRendersRetryableError();
  await testSingleSubmitRequiresPositiveJobIdAndPollingOutageDoesNotResubmit();
  await testAmbiguousSingleSubmitPersistsAcrossClicksAndControllerReload();
  await testDurableTerminalSubmitFailureClearsPersistedAttempt();
  await testPendingSingleClearsOnlyForExactDurableOperationEvidence();
  await testMalformedSuccessAndRefundPendingStayOnSamePersistedAttempt();
  await testFailedRefundPendingJobStaysPersistedUntilRefundSettles();
  await testPromptInputUpdatesStateWithoutRepaintingTheHost();
  await testSixShotBatchRunsInFiveJobWaves();
  await testBatchBudgetIsCheckedBeforeConsentOrSubmission();
  await testFirstBatchWaveRechecksExpiredQuoteAndBudgetAfterConsent();
  await testRevisionChangeClearsExpiredUnstartedBatchBeforeRequote();
  await testAmbiguousBatchAttemptPersistsItsKeyAcrossReload();
  console.log('canvas short drama production: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
