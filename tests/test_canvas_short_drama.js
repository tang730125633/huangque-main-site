const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const shortDrama = require('../site/workbench/canvas/canvas-short-drama.js');

function testCanvasIntegration() {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
  const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');

  assert.ok(html.includes('canvas/canvas-short-drama.css?v='));
  assert.ok(html.includes('canvas/canvas-short-drama.js?v='));
  assert.ok(html.indexOf('canvas/canvas-short-drama.js?v=') < html.indexOf('canvas/canvas-app.js?v='));
  assert.ok(html.includes('data-add="shortDrama"'));
  assert.match(app, /shortDrama:\s*\{name:'短剧项目'/);
  assert.ok(app.includes('data-f="openShortDrama"'));
  assert.ok(app.includes('shortDramaModule.createWorkspace('));
}

async function testPureHelpers() {
  const settings = shortDrama.normalizeSettings({
    title: '雨夜来客', synopsis: '陌生女孩敲开侦探的门', ratio: '1:1',
    target_duration: 45, shot_count: 8,
  });
  assert.equal(settings.ratio, '9:16');
  assert.equal(settings.target_duration, 45);
  assert.equal(settings.shot_count, 8);
  assert.equal(shortDrama.normalizeSettings({ shot_count: 7.5 }).shot_count, 6);
  assert.equal(shortDrama.normalizeSettings({ shot_count: 'not-a-number' }).shot_count, 6);
  assert.equal(shortDrama.normalizeSettings({ shot_count: 5 }).shot_count, 6);
  assert.equal(shortDrama.normalizeSettings({ shot_count: 11 }).shot_count, 10);

  assert.deepEqual(shortDrama.planningPayload(settings), {
    format: 'short_drama', prompt: settings.synopsis, dur: '45s', ratio: '9:16',
    shot_count: 8, style: settings.visual_style, platform: settings.target_platform,
  });
  assert.equal(shortDrama.stageIndex('storyboard_review'), 3);
  assert.match(shortDrama.summarizeProject({
    title: '雨夜来客', ratio: '9:16', target_duration: 45, stage: 'script_review',
  }), /雨夜来客/);
}

async function testProjectRoutesAndPlanningFlow() {
  const calls = [];
  const api = {
    json(path, options) {
      calls.push({ path, options });
      if (path === '/api/gen/copy') return Promise.resolve({ job_id: 42 });
      if (path === '/api/gen/job/42') return Promise.resolve({
        status: 'done', result: JSON.stringify({ mode: 'short_drama', plan: { title: '雨夜来客' } }),
      });
      if (path === '/api/gen/short-drama/apply-plan') return Promise.resolve({
        id: 'project-1', revision: 8, spent_points: 3,
      });
      return Promise.resolve({ items: [] });
    },
  };
  function poll(options) {
    assert.equal(options.intervalMs, 3000);
    assert.equal(options.maxMs, 420000);
    return options.request().then((job) => {
      assert.deepEqual(options.inspect(job), {
        done: true,
        value: { mode: 'short_drama', plan: { title: '雨夜来客' } },
      });
      return { mode: 'short_drama', plan: { title: '雨夜来客' } };
    });
  }
  const client = shortDrama.createClient(api, poll);

  await client.list();
  await client.get('project 1');
  await client.create({ title: '雨夜来客' });
  await client.update('project 1', 5, { revision: 99, title: '新标题' });
  await client.applyPlan('project 1', 6, 41);
  await client.confirm('project 1', 7, 'characters_review');
  const applied = await client.generatePlan({
    id: 'project-1', revision: 7, synopsis: '陌生女孩敲开侦探的门', target_duration: 45,
    ratio: '16:9', shot_count: 8, visual_style: '电影写实', target_platform: '抖音',
  });

  assert.deepEqual(applied, { id: 'project-1', revision: 8, spent_points: 3 });
  assert.deepEqual(calls, [
    { path: '/api/gen/short-drama/projects', options: undefined },
    { path: '/api/gen/short-drama/project?id=project%201', options: undefined },
    {
      path: '/api/gen/short-drama/projects',
      options: { method: 'POST', body: { title: '雨夜来客' } },
    },
    {
      path: '/api/gen/short-drama/project?id=project%201',
      options: { method: 'PUT', body: { revision: 5, title: '新标题' } },
    },
    {
      path: '/api/gen/short-drama/apply-plan',
      options: { method: 'POST', body: { project_id: 'project 1', revision: 6, job_id: 41 } },
    },
    {
      path: '/api/gen/short-drama/confirm',
      options: { method: 'POST', body: { project_id: 'project 1', revision: 7, stage: 'characters_review' } },
    },
    {
      path: '/api/gen/copy',
      options: {
        method: 'POST', body: {
          format: 'short_drama', prompt: '陌生女孩敲开侦探的门', dur: '45s', ratio: '16:9',
          shot_count: 8, style: '电影写实', platform: '抖音',
        },
      },
    },
    { path: '/api/gen/job/42', options: undefined },
    {
      path: '/api/gen/short-drama/apply-plan',
      options: { method: 'POST', body: { project_id: 'project-1', revision: 7, job_id: 42 } },
    },
  ]);
}

async function testTerminalJobFailureDoesNotApplyPlan() {
  let applyCalled = false;
  const api = {
    json(path) {
      if (path === '/api/gen/copy') return Promise.resolve({ job_id: 44 });
      if (path === '/api/gen/job/44') return Promise.resolve({
        status: 'failed', error: 'model refused plan', code: 'model_failed',
      });
      applyCalled = true;
      return Promise.resolve({});
    },
  };
  function poll(options) {
    return options.request().then((job) => {
      const outcome = options.inspect(job);
      assert.equal(outcome.error.message, 'model refused plan');
      return Promise.reject(outcome.error);
    });
  }
  await assert.rejects(
    shortDrama.createClient(api, poll).generatePlan({ id: 'project-1', revision: 1, synopsis: '故事梗概' }),
    (error) => error.message === 'model refused plan' && error.code === 'model_failed',
  );
  assert.equal(applyCalled, false);
}

function testMissingPollFailsClearly() {
  assert.throws(
    () => shortDrama.createClient({ json() {} }),
    /requires json and poll methods/,
  );
}

async function testPlanningErrorsPropagateWithoutApplying() {
  const copyError = new Error('copy unavailable');
  const copyApi = {
    json(path) {
      assert.equal(path, '/api/gen/copy');
      return Promise.reject(copyError);
    },
    poll() { throw new Error('poll must not run after submit failure'); },
  };
  await assert.rejects(
    shortDrama.createClient(copyApi).generatePlan({ id: 'project-1', revision: 1, synopsis: '故事梗概' }),
    (error) => error === copyError,
  );

  const pollError = new Error('planning failed');
  let applyCalled = false;
  const pollApi = {
    json(path) {
      if (path === '/api/gen/copy') return Promise.resolve({ job_id: 43 });
      applyCalled = true;
      return Promise.resolve({});
    },
    poll() { return Promise.reject(pollError); },
  };
  await assert.rejects(
    shortDrama.createClient(pollApi).generatePlan({ id: 'project-1', revision: 1, synopsis: '故事梗概' }),
    (error) => error === pollError,
  );
  assert.equal(applyCalled, false);
}

async function main() {
  testCanvasIntegration();
  await testPureHelpers();
  await testProjectRoutesAndPlanningFlow();
  await testPlanningErrorsPropagateWithoutApplying();
  await testTerminalJobFailureDoesNotApplyPlan();
  testMissingPollFailsClearly();
  const workspace = shortDrama.createWorkspace({
    projectId: 'project-1', apiClient: { json() {} }, poll() { return Promise.resolve({}); },
  });
  assert.equal(workspace.projectId, 'project-1');
  console.log('canvas short drama: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
