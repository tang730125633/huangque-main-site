const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const modulePath = path.join(
  __dirname, '..', 'site', 'workbench', 'canvas',
  'canvas-short-drama-lipsync.js'
);
const lipsync = require(modulePath);

function snapshot(overrides = {}) {
  return Object.assign({
    project_id: 'project-1',
    revision: 3,
    input_hash: 'a'.repeat(64),
    can_quote: true,
    blockers: [],
    features: {
      ui_enabled: true, mutations_enabled: true, batch_enabled: false
    },
    permissions: {
      read: true, quote: true, can_edit: true,
      can_create_job: true, can_select: true, can_lock: true
    },
    dependencies: {
      timeline: {
        visible_segments: [{
          id: 'segment-1', shot_id: 'shot-1',
          face_target: { type: 'character', value: 'host' }
        }]
      },
      provider_catalog: {
        default_provider: 'fal-latentsync',
        providers: [{
          name: 'fal-latentsync', profile: 'standard',
          capability_version: 'v1'
        }]
      }
    }
  }, overrides);
}

{
  const payload = lipsync.quotePayload(
    snapshot(), 'shot-1', { type: 'character', value: 'host' }
  );
  assert.equal(payload.project_id, 'project-1');
  assert.equal(payload.expected_revision, 3);
  assert.equal(payload.provider, 'fal-latentsync');
  assert.deepEqual(payload.face_target, { type: 'character', value: 'host' });
}

{
  const multi = snapshot();
  multi.dependencies.timeline.visible_segments.push({
    id: 'segment-2', shot_id: 'shot-1',
    face_target: { type: 'character', value: 'guest' }
  });
  const targets = lipsync.faceTargets(multi, 'shot-1');
  assert.deepEqual(targets, [
    { type: 'character', value: 'host' },
    { type: 'character', value: 'guest' }
  ]);
  const payload = lipsync.quotePayload(multi, 'shot-1', targets[1]);
  assert.deepEqual(payload.face_target, {
    type: 'character', value: 'guest'
  });
  assert.match(payload.idempotency_key, /guest/);
  const html = lipsync.renderPanel(multi, null, {
    shotId: 'shot-1', canEdit: true, faceTargetIndex: 1
  });
  assert.match(html, /data-field="lipsync-face-target"/);
  assert.match(html, /<option value="1" selected>guest<\/option>/);
}

{
  const html = lipsync.renderPanel(snapshot(), null, {
    shotId: 'shot-1', canEdit: true
  });
  assert.match(html, /PR-G 口型画布工作区/);
  assert.match(html, /获取当前镜头报价/);
  assert.match(html, /data-action="save-lipsync-speakers"/);
  assert.doesNotMatch(html, /data-action="create-lipsync-job"/);
}

{
  const blocked = snapshot({
    can_quote: false,
    blockers: [{
      code: 'missing_locked_visual',
      message: '<script>alert(1)</script>',
      scope: 'shot'
    }]
  });
  const html = lipsync.renderPanel(blocked, null, {
    shotId: 'shot-1', canEdit: true
  });
  assert.match(html, /missing_locked_visual/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /disabled/);
}

{
  const html = lipsync.renderPanel(snapshot(), {
    input_hash: 'a'.repeat(64), replayed: true, chargeable: true,
    quote_id: 'quote-1', shot_id: 'shot-1', provider: 'fal-latentsync',
    cost: { points: 0, currency: 'USD', external_estimate: 0.2 }
  }, { shotId: 'shot-1', canEdit: true });
  assert.match(html, /0 点/);
  assert.match(html, /确认扣点并生成口型/);
}

{
  const canvas = fs.readFileSync(
    path.join(__dirname, '..', 'site', 'workbench', 'canvas.html'), 'utf8'
  );
  assert.match(canvas, /canvas-short-drama-lipsync\.js\?v=/);
  assert.match(canvas, /canvas-short-drama-lipsync\.css\?v=/);
}

{
  const html = lipsync.renderJobStatus({
    active_jobs: [{
      id: 'job-1', state: 'running', progress: 40,
      allowed_actions: { retry: false, cancel: true }
    }],
    billing: { refund_pending: 1, manual_review: 1 }
  });
  assert.match(html, /job-1/);
  assert.match(html, /退款处理中/);
  assert.match(html, /人工处理/);
  assert.equal(lipsync.JOBS_PATH, '/api/gen/short-drama/lipsync/jobs');
}

{
  const html = lipsync.renderPanel(snapshot({
    features: {
      ui_enabled: false, mutations_enabled: false, batch_enabled: false
    }
  }), null, { shotId: 'shot-1', canEdit: true });
  assert.match(html, /灰度关闭状态/);
  assert.doesNotMatch(html, /data-action="quote-lipsync"/);
  assert.doesNotMatch(html, /data-action="create-lipsync-job"/);
}

{
  const requests = [];
  const api = lipsync.createApi({
    boardId: 'board-1',
    client: {
      json(route, options) {
        requests.push({ route, options: options || {} });
        return Promise.resolve({});
      }
    }
  });
  Promise.all([
    api.snapshot('project-1'),
    api.speakers({ project_id: 'project-1' }, 'speaker-key'),
    api.createJob({ project_id: 'project-1' }, 'job-key'),
    api.selectVersion('version-1', { project_id: 'project-1' }),
    api.lockVersion('version-1', { project_id: 'project-1' })
  ]).then(() => {
    assert.equal(requests.length, 5);
    requests.forEach(item => {
      assert.equal(item.options.headers['X-Canvas-Board-Id'], 'board-1');
    });
    assert.equal(requests[1].options.method, 'PUT');
    assert.equal(requests[1].options.headers['Idempotency-Key'], 'speaker-key');
    assert.equal(requests[2].options.headers['Idempotency-Key'], 'job-key');
    assert.equal(requests[3].options.method, 'PUT');
    assert.equal(requests[4].options.method, 'POST');
  });
}

{
  const docs = JSON.parse(fs.readFileSync(
    path.join(__dirname, '..', 'docs', 'api', 'openapi.json'), 'utf8'
  ));
  const site = JSON.parse(fs.readFileSync(
    path.join(__dirname, '..', 'site', 'api-docs', 'openapi.json'), 'utf8'
  ));
  assert.deepEqual(site, docs);
  assert.ok(docs.paths['/api/gen/short-drama/lipsync/snapshot'].get);
  assert.ok(docs.paths['/api/gen/short-drama/lipsync/quote'].post);
  assert.equal(
    docs.components.schemas.ShortDramaLipsyncQuote
      .properties.chargeable.type,
    'boolean'
  );
  assert.ok(docs.paths['/api/gen/short-drama/lipsync/jobs'].post);
  assert.ok(docs.paths['/api/gen/short-drama/lipsync/jobs/{job_id}'].get);
  assert.ok(docs.paths['/api/gen/short-drama/lipsync/speakers'].put);
  assert.ok(docs.paths[
    '/api/gen/short-drama/lipsync/versions/{version_id}/select'
  ].put);
  assert.ok(docs.paths[
    '/api/gen/short-drama/lipsync/versions/{version_id}/lock'
  ].post);
  [
    '/api/gen/short-drama/lipsync/create',
    '/api/gen/short-drama/lipsync/confirm',
    '/api/gen/short-drama/lipsync/cancel',
    '/api/gen/short-drama/lipsync/refund'
  ].forEach(route => assert.equal(docs.paths[route], undefined));
}

console.log('canvas short-drama lipsync tests passed');
