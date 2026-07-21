const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const shortDrama = require('../site/workbench/canvas/canvas-short-drama.js');

function testOpenApiContract() {
  const root = path.join(__dirname, '..');
  const spec = JSON.parse(fs.readFileSync(path.join(root, 'docs', 'api', 'openapi.json'), 'utf8'));
  const operations = [
    ['get', '/api/gen/short-drama/projects'],
    ['post', '/api/gen/short-drama/projects'],
    ['get', '/api/gen/short-drama/project'],
    ['put', '/api/gen/short-drama/project'],
    ['post', '/api/gen/short-drama/apply-plan'],
    ['post', '/api/gen/short-drama/confirm'],
  ];
  for (const [method, route] of operations) {
    const operation = spec.paths[route] && spec.paths[route][method];
    assert.ok(operation, `OpenAPI must document ${method.toUpperCase()} ${route}`);
    assert.ok(operation.responses['401'], `${method.toUpperCase()} ${route} must document authentication failure`);
  }
  assert.ok(spec.paths['/api/gen/short-drama/project'].get.responses['404'],
    'project detail must document owner isolation as an indistinguishable not-found response');
  for (const [method, route] of operations.filter(([name]) => name !== 'get')) {
    assert.ok(spec.paths[route][method].responses['400'], `${method.toUpperCase()} ${route} must document validation failure`);
  }
  for (const [method, route] of [
    ['put', '/api/gen/short-drama/project'],
    ['post', '/api/gen/short-drama/apply-plan'],
    ['post', '/api/gen/short-drama/confirm'],
  ]) {
    assert.ok(spec.paths[route][method].responses['404'], `${method.toUpperCase()} ${route} must document owner isolation`);
    assert.equal(
      spec.paths[route][method].responses['409'].content['application/json'].schema.$ref,
      '#/components/schemas/RevisionConflict',
      `${method.toUpperCase()} ${route} must document optimistic-concurrency conflict`,
    );
  }
  const applyConflict = spec.paths['/api/gen/short-drama/apply-plan'].post.responses['409'];
  assert.match(applyConflict.description, /job_already_applied/,
    'apply-plan conflict must document duplicate job application');

  for (const name of [
    'ShortDramaProject', 'ShortDramaCharacter', 'ShortDramaScriptVersion', 'ShortDramaShot',
    'RevisionConflict', 'ShortDramaPlanningRequest', 'ShortDramaPlanningResult',
  ]) assert.ok(spec.components.schemas[name], `OpenAPI must define ${name}`);

  const updateSchema = spec.paths['/api/gen/short-drama/project'].put
    .requestBody.content['application/json'].schema;
  assert.equal(updateSchema.oneOf.length, 4, 'PUT project must document settings plus three content variants');
  for (const section of ['characters', 'script', 'shots']) {
    const variant = updateSchema.oneOf.find((candidate) => candidate.required && candidate.required.includes(section));
    assert.ok(variant, `PUT project must document the ${section} content variant`);
    assert.deepEqual(variant.required.sort(), ['revision', section].sort());
    assert.equal(variant.additionalProperties, false, `${section} PUT accepts exactly revision plus one content section`);
  }

  const copySchema = spec.paths['/api/gen/copy'].post.requestBody.content['application/json'].schema;
  const variants = copySchema.oneOf.map((candidate) => {
    if (!candidate.$ref) return candidate;
    return spec.components.schemas[candidate.$ref.split('/').at(-1)];
  });
  const shortDramaVariant = variants.find((candidate) => candidate.properties &&
    candidate.properties.format && (candidate.properties.format.const === 'short_drama' ||
      (candidate.properties.format.enum || []).includes('short_drama')));
  assert.ok(shortDramaVariant, 'copy request must document format=short_drama');
  assert.equal(shortDramaVariant.properties.ratio.enum.includes('16:9'), true);
  assert.equal(shortDramaVariant.properties.shot_count.minimum, 6);
  assert.equal(shortDramaVariant.properties.shot_count.maximum, 10);
  const planningResult = spec.components.schemas.ShortDramaPlanningResult;
  assert.ok(planningResult.required.includes('type') && planningResult.required.includes('dur'));
  assert.ok(planningResult.properties.plan.properties.characters.items.properties.key,
    'copy result uses planning character key before persistence');
  assert.ok(planningResult.properties.plan.properties.script.properties.conflict,
    'copy result documents normalized planning script fields');
  assert.ok(planningResult.properties.plan.properties.shots.items.properties.key,
    'copy result uses planning shot key before persistence');
  assert.equal(spec.paths['/api/gen/copy'].post.responses['200'].description.includes('3'), true,
    'copy planning documents the current 3-point server-reported cost');
  assert.match(spec.paths['/api/gen/short-drama/projects'].post.description, /free|no points/i);
  assert.match(spec.paths['/api/gen/short-drama/project'].put.description, /free|no points/i);
}

function testCanvasIntegration() {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
  const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
  const moduleSource = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.js'), 'utf8').replace(/\r\n/g, '\n');
  const css = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.css'), 'utf8').replace(/\r\n/g, '\n');
  const appSource = app.replace(/\r\n/g, '\n');

  assert.ok(html.includes('canvas/canvas-short-drama.css?v='));
  assert.ok(html.includes('canvas/canvas-short-drama.js?v='));
  assert.ok(html.indexOf('canvas/canvas-short-drama.js?v=') < html.indexOf('canvas/canvas-app.js?v='));
  assert.equal((html.match(/data-add="shortDrama"/g) || []).length, 2);
  assert.match(app, /shortDrama:\s*\{name:'短剧项目',\s*color:'#[a-f0-9]+'\}/);
  assert.ok(app.includes('data-f="openShortDrama"'));
  assert.ok(app.includes('shortDramaModule.createWorkspace('));
  assert.match(app, /projectId:projectId,\s*apiClient:apiClient,\s*poll:apiModule\.poll,\s*canEdit:canEdit,\s*onChange:onChange/);
  assert.ok(app.includes('shortDramaModule.creationPayload(node.params)'));
  assert.ok(app.includes('shortDramaModule.createProjectCoordinator('));
  assert.ok(app.includes("function shortDramaScopeKey(scope,boardId)"));
  assert.ok(app.includes("var scopeKey=currentShortDramaScopeKey();"));
  assert.match(app, /getNode:function\(scopeKey,nodeId\)\{[\s\S]*?shortDramaNodeForScope\(scopeKey,nodeId\)/, 'reconciliation is board scoped');
  assert.match(app, /function shortDramaNodeForScope\(scopeKey,nodeId\)\{[\s\S]*?wrap\.classList\.contains\('editing'\)/, 'board home is never treated as an active scope');
  assert.match(app, /shortDramaProjectCoordinator\.ensure\(scopeKey,node\.id,[\s\S]*?node\.params\.project_id\|\|null\)/, 'creation captures the expected project link');
  assert.match(app, /onChange=function\(summary\)\{[\s\S]*?shortDramaNodeForScope\(scopeKey,nodeId\)/, 'workspace changes resolve the current scoped node');
  assert.ok(app.includes("shortDramaProjectCoordinator.cleanupScope(shortDramaScopeKey('local',id));"));
  assert.match(app, /finally\(function\(\)\{[\s\S]*?applyShortDramaOpenPolicy\(scopeKey,nodeId\)/, 'settlement reapplies the current scoped readonly policy');
  assert.match(app, /function shortDramaNodeOutputs\(node\)[\s\S]*?return node&&node\.type==='shortDrama'\?\{\}:/);
  assert.match(app, /outputs:shortDramaNodeOutputs\(n\)/, 'canvas snapshots must sanitize short-drama outputs');
  assert.match(app, /if\(type==='shortDrama'&&data\) data=shortDramaModule\.sanitizeNodeData\(data\)/, 'restore and paste must sanitize short-drama node data');
  assert.match(app, /if\(n\.type==='shortDrama'\)[\s\S]*?n\.outputs=\{\}/, 'template imports must sanitize short-drama outputs');
  assert.equal((app.match(/outputs:shortDramaNodeOutputs\((?:n|node)\)/g) || []).length, 4, 'snapshot, export, and both copy paths sanitize outputs');
  assert.ok(app.includes('snap=sanitizeShortDramaSnapshot(snap);'), 'restore sanitizes before rebuilding nodes');
  assert.ok(app.includes('copy.data=sanitizeShortDramaSnapshot(copy.data);'), 'board duplication sanitizes persisted nodes');
  assert.match(app, /openShortDrama\.disabled=!!readonly&&!\(node&&node\.params\.project_id\)/, 'readonly existing projects remain openable');
  const ensureSource = app.match(/function ensureShortDramaProject\(node,scopeKey\)\{[\s\S]*?\n  \}/)[0];
  assert.doesNotMatch(ensureSource, /scheduleSave\(/, 'coordinator apply is the single save path');

  for (const [asset, source] of [
    ['canvas/canvas-app.js', appSource],
    ['canvas/canvas-short-drama.js', moduleSource],
    ['canvas/canvas-short-drama.css', css],
  ]) {
    const stamp = crypto.createHash('md5').update(source).digest('hex').slice(0, 8);
    assert.ok(html.includes(`${asset}?v=${stamp}`), `${asset} cache stamp must be LF MD5`);
  }
}

function testNodePersistenceHelpers() {
  const dirty = {
    id: 'n7', type: 'shortDrama',
    params: {
      project_id: 'project-7', title: '雨夜来客', ratio: '16:9', target_duration: 45,
      stage: 'script_review', progress: 50, spent_points: 3, estimated_points: 12,
      characters: [{ name: '侦探' }], script: { hook: 'secret' }, shots: [{ key: 's1' }],
    },
    outputs: {
      characters: [{ name: '侦探' }], script: { hook: 'secret' }, shots: [{ key: 's1' }],
      video: 'must-not-persist',
    },
  };
  const clean = shortDrama.sanitizeNodeData(dirty);
  assert.equal(clean.id, 'n7');
  assert.equal(clean.params.project_id, 'project-7');
  assert.deepEqual(Object.keys(clean.params).sort(), [
    'estimated_points', 'progress', 'project_id', 'ratio', 'spent_points', 'stage', 'target_duration', 'title',
  ]);
  assert.deepEqual(clean.outputs, {});
  assert.notStrictEqual(clean, dirty);
  assert.notStrictEqual(clean.params, dirty.params);

  const payload = shortDrama.creationPayload(clean.params);
  assert.deepEqual(payload, {
    title: '雨夜来客', synopsis: '请在短剧工作区完善故事梗概', ratio: '16:9',
    target_duration: 45, shot_count: 6,
  });
  assert.ok(payload.synopsis.length >= 8, 'lazy creation payload must satisfy backend synopsis validation');
  assert.equal(shortDrama.canOpenNode({ project_id: 'project-7' }, false), true);
  assert.equal(shortDrama.canOpenNode({ project_id: null }, false), false);
  assert.equal(shortDrama.canOpenNode({ project_id: null }, true), true);
}

async function testCreateProjectCoordinatorIsBoardScoped() {
  let createCalls = 0;
  let saves = 0;
  let resolveCreate;
  let activeScope = 'local:board-a';
  const boardAOld = { id: 'n1', type: 'shortDrama', params: shortDrama.normalizeNodeParams({ title: 'A 旧节点' }), outputs: {} };
  const boardBNode = { id: 'n1', type: 'shortDrama', params: shortDrama.normalizeNodeParams({ title: 'B 节点' }), outputs: {} };
  const boards = {
    'local:board-a': { n1: boardAOld },
    'local:board-b': { n1: boardBNode },
  };
  const coordinator = shortDrama.createProjectCoordinator({
    getNode(scopeKey, nodeId) { return activeScope === scopeKey ? boards[scopeKey] && boards[scopeKey][nodeId] : null; },
    create(payload) {
      createCalls += 1;
      assert.equal(payload.title, 'A 旧节点');
      return new Promise((resolve) => { resolveCreate = resolve; });
    },
    apply(node, project) {
      node.params = shortDrama.normalizeNodeParams(Object.assign({}, node.params, project));
      node.outputs = {};
      saves += 1;
    },
  });
  const payload = shortDrama.creationPayload(boardAOld.params);
  const first = coordinator.ensure('local:board-a', 'n1', payload, true, null);
  const duplicate = coordinator.ensure('local:board-a', 'n1', payload, true, null);
  assert.strictEqual(duplicate, first, 'same board and node reuse the in-flight request');
  activeScope = 'local:board-b';
  await Promise.resolve();
  assert.equal(createCalls, 1);
  resolveCreate({ id: 'project-a', title: 'A 服务端标题', ratio: '9:16', target_duration: 30, stage: 'draft' });
  assert.equal(await first, 'project-a');
  assert.equal(boardBNode.params.project_id, null, 'same node id on board B is untouched');
  assert.equal(boardAOld.params.project_id, null, 'inactive board A object is not mutated');
  assert.equal(saves, 0);
  assert.equal(coordinator.hasPending('local:board-a', 'n1'), false, 'pending entry is cleaned after settlement');
  assert.equal(coordinator.hasCompleted('local:board-a', 'n1'), true, 'inactive result is retained by board scope');

  const boardARestored = { id: 'n1', type: 'shortDrama', params: shortDrama.normalizeNodeParams({ title: 'A 恢复节点' }), outputs: { shots: ['secret'] } };
  boards['local:board-a'].n1 = boardARestored;
  activeScope = 'local:board-a';
  const consumed = coordinator.ensure('local:board-a', 'n1', shortDrama.creationPayload(boardARestored.params), true, null);
  assert.equal(await consumed, 'project-a');
  assert.equal(createCalls, 1, 'reopening board A consumes the retained result without another POST');
  assert.equal(boardARestored.params.project_id, 'project-a');
  assert.deepEqual(boardARestored.outputs, {});
  assert.equal(saves, 1);
  assert.equal(coordinator.hasCompleted('local:board-a', 'n1'), false, 'completed entry clears after application');

  await assert.rejects(
    coordinator.ensure('local:board-a', 'n8', shortDrama.creationPayload({ title: '只读节点' }), false, null),
    /只读/,
  );
  assert.equal(createCalls, 1, 'id-less readonly node never creates a project');

  const failed = shortDrama.createProjectCoordinator({
    getNode() { return null; },
    create() { return Promise.reject(new Error('create failed')); },
    apply() { throw new Error('apply must not run'); },
  });
  await assert.rejects(failed.ensure('local:board-f', 'n9', shortDrama.creationPayload({ title: '失败节点' }), true, null), /create failed/);
  assert.equal(failed.hasPending('local:board-f', 'n9'), false, 'in-flight entry is also cleaned after rejection');
}

async function testCreateProjectCoordinatorPreservesConflictingLink() {
  let resolveCreate;
  let applyCalls = 0;
  const node = { id: 'n1', type: 'shortDrama', params: shortDrama.normalizeNodeParams({ title: '冲突节点' }), outputs: {} };
  const coordinator = shortDrama.createProjectCoordinator({
    getNode(scopeKey, nodeId) { return scopeKey === 'collab:board-c' && nodeId === 'n1' ? node : null; },
    create() { return new Promise((resolve) => { resolveCreate = resolve; }); },
    apply() { applyCalls += 1; },
  });
  const pending = coordinator.ensure('collab:board-c', 'n1', shortDrama.creationPayload(node.params), true, null);
  await Promise.resolve();
  node.params.project_id = 'project-from-collaboration';
  resolveCreate({ id: 'project-from-post', title: '迟到结果' });
  assert.equal(await pending, 'project-from-collaboration');
  assert.equal(node.params.project_id, 'project-from-collaboration');
  assert.equal(applyCalls, 0, 'late POST never overwrites a different project link');
  assert.equal(coordinator.hasCompleted('collab:board-c', 'n1'), false, 'conflicting retained result is discarded');
}

async function testCreateProjectCoordinatorScopeCleanup() {
  let resolveCreate;
  const coordinator = shortDrama.createProjectCoordinator({
    getNode() { return null; },
    create() { return new Promise((resolve) => { resolveCreate = resolve; }); },
    apply() { throw new Error('deleted scope must never apply'); },
  });
  const pending = coordinator.ensure('local:deleted-board', 'n1', shortDrama.creationPayload({ title: '待删除' }), true, null);
  await Promise.resolve();
  coordinator.cleanupScope('local:deleted-board');
  resolveCreate({ id: 'orphaned-project' });
  assert.equal(await pending, 'orphaned-project');
  assert.equal(coordinator.hasPending('local:deleted-board', 'n1'), false);
  assert.equal(coordinator.hasCompleted('local:deleted-board', 'n1'), false, 'deleted scope does not retain a late result');
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
  assert.equal(shortDrama.summarizeProject({
    title: '雨夜来客', ratio: '9:16', target_duration: 45, stage: 'script_review',
  }).title, '雨夜来客');
}

async function testProjectRoutesAndPlanningFlow() {
  const calls = [];
  const planningCosts = [];
  const planningProgress = [];
  const api = {
    json(path, options) {
      calls.push({ path, options });
      if (path === '/api/gen/copy') return Promise.resolve({ job_id: 42, cost: 3 });
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
  }, {
    onCost(cost) { planningCosts.push(cost); },
    onProgress(progress) { planningProgress.push(progress); },
  });

  assert.deepEqual(applied, { id: 'project-1', revision: 8, spent_points: 3 });
  assert.deepEqual(planningCosts, [3], 'server-returned cost is exposed before plan application');
  assert.ok(planningProgress.some((progress) => progress.status === 'done'), 'poll status reaches the workspace');
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

function workspaceProject(overrides = {}) {
  const characters = [
    {
      character_key: 'detective', name: '侦探', identity_text: '私家侦探', personality: '冷静',
      source_type: 'ai_character', avatar_id: null, appearance_prompt: '年轻女侦探',
      wardrobe_prompt: '黑色风衣', voice_key: 'calm', voice_settings: { speed: 1 }, sort_order: 0,
    },
    {
      character_key: 'visitor', name: '访客', identity_text: '神秘访客', personality: '紧张',
      source_type: 'cinematic_avatar', avatar_id: 'avatar-2', appearance_prompt: '湿透的中年人',
      wardrobe_prompt: '灰色大衣', voice_key: null, voice_settings: {}, sort_order: 1,
    },
  ];
  const dialogue = [
    { id: 'line-1', character_key: 'visitor', text: '我只有五分钟。' },
    { id: 'line-2', character_key: 'detective', text: '足够找到真相。' },
  ];
  const shots = Array.from({ length: 6 }, (_, index) => ({
    shot_key: `shot-${index + 1}`, sort_order: index, script_version: 1, duration: 5,
    scene_description: `雨夜办公室 ${index + 1}`, camera_description: '缓慢推近',
    character_keys: index % 2 ? ['detective'] : ['visitor'], dialogue_line_ids: [dialogue[index % 2].id],
    image_prompt: `cinematic rainy office ${index + 1}`, video_prompt: `slow push in ${index + 1}`,
  }));
  return Object.assign({
    id: 'project-1', revision: 7, title: '雨夜来客',
    synopsis: '陌生访客在雨夜带来一宗危险委托', ratio: '9:16',
    target_duration: 30, shot_count: 6, visual_style: '电影写实', target_platform: '抖音',
    point_budget: 30, spent_points: 3, estimated_points: 12, stage: 'characters_review',
    characters,
    script_versions: [{
      version: 1, title: '雨夜来客', logline: '五分钟内找出真相', hook: '门外响起脚步声',
      conflict_text: '线索即将被毁', turn_text: '访客才是目标', ending: '侦探推开暗门',
      dialogue_lines: dialogue,
    }],
    shots,
  }, overrides);
}

function testWorkspaceSourceAndRenderContract() {
  const root = path.join(__dirname, '..');
  const source = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.js'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.css'), 'utf8');
  const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
  for (const text of [
    '项目设置', '角色确认', '剧本确认', '分镜确认', '生成短剧策划（3点）',
    '确认角色并继续', '确认剧本并继续', '确认分镜',
    '项目已在其他页面更新，请刷新后重试',
  ]) assert.ok(source.includes(text), `workspace source must include ${text}`);
  for (const endpoint of [
    '/api/gen/short-drama/project', '/api/gen/short-drama/confirm', '/api/gen/copy',
    '/api/gen/short-drama/apply-plan',
  ]) assert.ok(source.includes(endpoint), `workspace client must use ${endpoint}`);
  assert.ok(css.includes('.nc-short-drama-workspace'));
  assert.ok(css.includes('.nc-short-drama-character-rail'));
  assert.ok(css.includes('.nc-short-drama-editor'));
  assert.ok(css.includes('.nc-short-drama-inspector'));
  assert.match(app, /current\.params\.project_id!==projectId[\s\S]*?return/,
    'stale workspace callbacks must not overwrite a relinked scoped node');
  assert.match(app, /function destroyShortDramaWorkspace\(node\)/);
  assert.match(app, /function destroyAllShortDramaWorkspaces\(\)/);
  assert.match(app, /function restoreSnapshot\(snap\)\{[\s\S]*?destroyAllShortDramaWorkspaces\(\)/,
    'snapshot rebuild destroys open workspaces first');
  assert.match(app, /function showBoardHome\(\)\{[\s\S]*?destroyAllShortDramaWorkspaces\(\)/,
    'leaving the board destroys open workspaces');
  assert.match(app, /function deleteSelectedNodes\(\)\{[\s\S]*?destroyShortDramaWorkspace\(nodes\[id\]\)/,
    'multi-delete destroys each open workspace');
  assert.match(app, /function delNode\(id\)\{[\s\S]*?destroyShortDramaWorkspace\(nodes\[id\]\)/,
    'single delete destroys its open workspace');
  assert.match(app, /function clearCanvas\(\)\{[\s\S]*?destroyAllShortDramaWorkspaces\(\)/,
    'clearing the canvas destroys all open workspaces');
  assert.match(app, /shortDramaWorkspace\.projectId!==node\.params\.project_id[\s\S]*?destroyShortDramaWorkspace\(node\)/,
    'changing a node project link destroys the stale workspace');
  const roleSetterMatch = app.match(/function setCurrentCollabRole\(role\)\{[\s\S]*?\n  \}/);
  assert.ok(roleSetterMatch, 'canvas app needs an explicit collaboration-role transition boundary');
  const roleSetter = roleSetterMatch[0];
  assert.match(roleSetter, /shortDramaModule\.isRoleDowngrade\(previousRole,currentCollabRole\)/);
  assert.match(roleSetter, /destroyAllShortDramaWorkspaces\(\)/,
    'editable-to-viewer transition destroys active short-drama workspaces');
  assert.match(app, /onBoard:function\(board\)\{[\s\S]*?setCurrentCollabRole\(board\.role\)/);
  assert.match(app, /onRole:function\(role\)\{[\s\S]*?setCurrentCollabRole\(role\)/);
  assert.match(app, /phase==='save-permanent'[\s\S]*?status===403[\s\S]*?setCurrentCollabRole\('viewer'\)/);
  assert.match(app, /currentCollabRole=''[\s\S]*?setCurrentCollabRole\(board\.role\|\|'viewer'\)/,
    'initial viewer open flows through a blank role and is not treated as a downgrade');
  const readonlySetter = app.match(/function setEditorReadonly\(readonly\)\{[\s\S]*?\n  \}/)[0];
  assert.doesNotMatch(readonlySetter, /destroyAllShortDramaWorkspaces/,
    'routine readonly UI refresh must not destroy legitimate viewer workspaces');
  assert.match(source, /data-character-jump[\s\S]*?function handleClick[\s\S]*?scrollIntoView[\s\S]*?\.focus\(/,
    'character rail click scrolls to and focuses the matching character card');
  const compactCss = css.match(/@media \(max-width: 1080px\) \{[\s\S]*?(?=@media \(max-width: 760px\))/)[0];
  assert.doesNotMatch(compactCss, /\.nc-short-drama-inspector\s*\{[^}]*display:\s*none/,
    'responsive layout must not hide planning/status/error controls');
  assert.match(compactCss, /\.nc-short-drama-inspector\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/,
    'compact inspector stacks below the editor');

  const project = workspaceProject({ stage: 'storyboard_review' });
  const html = shortDrama.renderWorkspace(project, { activeStage: 'storyboard_review', canEdit: true });
  assert.ok(html.includes('nc-short-drama-workspace'));
  assert.equal((html.match(/class="nc-short-drama-shot-card"/g) || []).length, 6);
  for (const shot of project.shots) {
    const marker = `data-shot-key="${shot.shot_key}"`;
    const start = html.indexOf(marker);
    assert.notEqual(start, -1);
    const end = html.indexOf('class="nc-short-drama-shot-card"', start + marker.length);
    const card = html.slice(start, end < 0 ? html.length : end);
    assert.match(card, /data-field="duration"/);
    assert.ok(card.includes('角色'));
    assert.ok(card.includes('台词摘要'));
    assert.ok(card.includes('画面提示词'));
    assert.ok(card.includes('视频提示词'));
    assert.match(card, /(?:5|10)秒/);
  }
  assert.match(shortDrama.renderWorkspace(workspaceProject({ stage: 'stills_review' }), {
    activeStage: 'stills_review', canEdit: true,
  }), /已完成第一阶段[\s\S]*第二阶段素材制作/);
  assert.match(shortDrama.renderWorkspace(workspaceProject({ stage: 'stills_review' }), {
    activeStage: 'settings', canEdit: true,
  }), /data-tab="stills_review"/,
  'completion has a navigation tab and can be revisited after viewing prior stages');
  assert.doesNotMatch(shortDrama.renderWorkspace(workspaceProject({ stage: 'characters_review' }), {
    activeStage: 'stills_review', canEdit: true,
  }), /已完成第一阶段/,
  'completion cannot be opened before the server advances to stills_review');
}

function testWorkspacePureStateAndPayloadHelpers() {
  const project = workspaceProject();
  assert.equal(shortDrama.isStageEnabled(project, 'settings'), true);
  assert.equal(shortDrama.isStageEnabled(project, 'characters_review'), true);
  assert.equal(shortDrama.isStageEnabled(project, 'script_review'), false);
  assert.equal(shortDrama.isStageEditable(project, 'characters_review', true), true);
  assert.equal(shortDrama.isStageEditable(project, 'characters_review', false), false);
  assert.equal(shortDrama.isStageEditable(workspaceProject({ stage: 'script_review' }), 'characters_review', true), false);
  assert.equal(shortDrama.isStageEditable(workspaceProject({ stage: 'draft' }), 'settings', true), true);
  assert.equal(shortDrama.isStageEditable(workspaceProject({ stage: 'characters_review' }), 'settings', true), false,
    'project settings are view-only after plan application');

  const placeholder = workspaceProject({ stage: 'draft', synopsis: shortDrama.PLACEHOLDER_SYNOPSIS });
  assert.equal(shortDrama.canGeneratePlan(placeholder, true), false);
  assert.equal(shortDrama.canGeneratePlan(workspaceProject({ stage: 'draft', synopsis: '太短' }), true), false);
  assert.equal(shortDrama.canGeneratePlan(workspaceProject({ stage: 'draft' }), true), true);
  assert.equal(shortDrama.canGeneratePlan(workspaceProject({ stage: 'characters_review' }), true), false);

  assert.deepEqual(shortDrama.makeSettingsPatch(project), {
    title: project.title, synopsis: project.synopsis, ratio: '9:16', target_duration: 30,
    shot_count: 6, visual_style: project.visual_style, target_platform: project.target_platform,
    point_budget: 30,
  });
  assert.deepEqual(shortDrama.makeCharactersPatch(project.characters), {
    characters: project.characters.map((character) => ({
      character_key: character.character_key, name: character.name, identity_text: character.identity_text,
      personality: character.personality, source_type: character.source_type, avatar_id: character.avatar_id,
      appearance_prompt: character.appearance_prompt, wardrobe_prompt: character.wardrobe_prompt,
      voice_key: character.voice_key, voice_settings: character.voice_settings,
    })),
  });
  assert.deepEqual(shortDrama.makeScriptPatch(project.script_versions[0]).script.dialogue_lines,
    project.script_versions[0].dialogue_lines);
  assert.deepEqual(shortDrama.makeShotsPatch(project.shots).shots[0], {
    shot_key: 'shot-1', duration: 5, scene_description: '雨夜办公室 1', camera_description: '缓慢推近',
    character_keys: ['visitor'], dialogue_line_ids: ['line-1'],
    image_prompt: 'cinematic rainy office 1', video_prompt: 'slow push in 1',
  });
  assert.deepEqual(shortDrama.validateShots(project.shots, project), []);
  assert.match(shortDrama.validateShots(project.shots.slice(0, 5), project).join(' '), /6–10/);
  assert.match(shortDrama.validateShots(project.shots.map((shot, index) => Object.assign({}, shot,
    index === 0 ? { image_prompt: '' } : {})), project).join(' '), /画面提示词/);

  const ownerOnly = shortDrama.renderLoadState({
    canEdit: false, busy: false, loadFailed: true, loadStatus: 404, error: 'not found',
  });
  assert.ok(ownerOnly.includes('仅项目创建者可查看短剧详情'));
  assert.match(ownerOnly, /data-action="reload"/);
  assert.match(ownerOnly, /data-action="close"/);
  const networkFailure = shortDrama.renderLoadState({
    canEdit: true, busy: false, loadFailed: true, loadStatus: 0, error: '网络连接失败',
  });
  assert.ok(networkFailure.includes('网络连接失败'));
  assert.match(networkFailure, /data-action="reload"[\s\S]*data-action="close"/);
  assert.match(shortDrama.renderLoadState({ canEdit: true, busy: true }), /data-action="close"/,
    'initial loading state is always escapable');
}

async function testWorkspaceSavesUseExactRevisionedBodiesAndSummaries() {
  let project = workspaceProject({ stage: 'draft' });
  const calls = [];
  const summaries = [];
  const client = {
    get(id) { calls.push(['get', id]); return Promise.resolve(project); },
    update(id, revision, patch) {
      calls.push(['update', id, revision, patch]);
      project = Object.assign({}, project, patch, {
        revision: revision + 1,
        stage: Object.prototype.hasOwnProperty.call(patch, 'title') ? 'characters_review' : project.stage,
      });
      if (patch.script) project.script_versions = project.script_versions.concat([Object.assign({ version: 2 }, patch.script)]);
      return Promise.resolve(project);
    },
    confirm(id, revision, stage) {
      calls.push(['confirm', id, revision, stage]);
      const nextStage = {
        characters_review: 'script_review', script_review: 'storyboard_review', storyboard_review: 'stills_review',
      }[stage];
      project = Object.assign({}, project, { revision: revision + 1, stage: nextStage });
      return Promise.resolve(project);
    },
    generatePlan() { throw new Error('paid planning must not run'); },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: project.id, client, document: null, canEdit: true,
    onChange(summary) { summaries.push(summary); },
  });
  await workspace.ready;
  const settings = Object.assign({}, project, { title: '新标题' });
  await workspace.saveSettings(settings);
  await workspace.saveCharacters(project.characters);
  const beforeScript = workspace.getProject();
  await workspace.confirm('characters_review');
  await workspace.saveScript(beforeScript.script_versions[0]);
  await workspace.confirm('script_review');
  await workspace.saveShots(project.shots);

  assert.deepEqual(calls.slice(0, 7), [
    ['get', 'project-1'],
    ['update', 'project-1', 7, shortDrama.makeSettingsPatch(settings)],
    ['update', 'project-1', 8, shortDrama.makeCharactersPatch(project.characters)],
    ['confirm', 'project-1', 9, 'characters_review'],
    ['update', 'project-1', 10, shortDrama.makeScriptPatch(beforeScript.script_versions[0])],
    ['confirm', 'project-1', 11, 'script_review'],
    ['update', 'project-1', 12, shortDrama.makeShotsPatch(project.shots)],
  ]);
  assert.equal(workspace.getProject().script_versions.length, 2, 'script save preserves prior versions');
  assert.equal(summaries.length, 6);
  assert.deepEqual(summaries.at(-1), shortDrama.summarizeProject(workspace.getProject()));
  assert.equal(typeof summaries.at(-1), 'object');
  workspace.destroy();
}

async function testWorkspaceLoadRecoveryOwnerIsolationAndDestroy() {
  let loads = 0;
  let updates = 0;
  const project = workspaceProject({ stage: 'script_review' });
  const client = {
    get() {
      loads += 1;
      if (loads === 1) {
        const error = new Error('not found'); error.status = 404; error.code = 'not_found';
        return Promise.reject(error);
      }
      return Promise.resolve(project);
    },
    update() { updates += 1; return Promise.resolve(project); },
    confirm() { return Promise.resolve(project); },
    generatePlan() { throw new Error('must not submit'); },
  };
  const workspace = shortDrama.createWorkspace({ projectId: project.id, client, document: null, canEdit: false });
  assert.equal(await workspace.ready, null);
  assert.equal(workspace.getState().loadFailed, true);
  assert.equal(workspace.getState().loadStatus, 404);
  assert.ok(workspace.render().includes('仅项目创建者可查看短剧详情'));
  assert.match(workspace.render(), /data-action="reload"[\s\S]*data-action="close"/);

  assert.equal((await workspace.reload()).id, project.id, 'retry replaces the load error with the owner-readable project');
  assert.equal(workspace.getState().loadFailed, false);
  assert.match(workspace.render(), /data-readonly="true"/);
  await assert.rejects(workspace.saveSettings(project), /read.only/i);
  assert.equal(updates, 0);

  workspace.destroy();
  workspace.destroy();
  assert.equal(workspace.getState().destroyed, true, 'destroy is idempotent and observable');
  await assert.rejects(workspace.reload(), /destroyed/i);
  await assert.rejects(workspace.saveScript(project.script_versions[0]), /destroyed/i);

  let resolveLoad;
  let summaries = 0;
  const closing = shortDrama.createWorkspace({
    projectId: project.id, document: null, canEdit: true,
    onChange() { summaries += 1; },
    client: {
      get() { return new Promise((resolve) => { resolveLoad = resolve; }); },
      update() { throw new Error('must not update'); }, confirm() { throw new Error('must not confirm'); },
      generatePlan() { throw new Error('must not submit'); },
    },
  });
  const stateAfterOpen = closing.getState();
  closing.destroy();
  const stateAfterClose = closing.getState();
  assert.notDeepEqual(stateAfterClose, stateAfterOpen);
  resolveLoad(project);
  assert.equal(await closing.ready, null, 'closing during initial load settles without a stale mutation');
  assert.deepEqual(closing.getState(), stateAfterClose,
    'late GET does not assign synopsis, active stage, or any other controller state after close');
  assert.equal(summaries, 0);
}

async function testCollaborationRoleDowngradeDestroysEditableWorkspaceOnly() {
  const project = workspaceProject({ stage: 'draft' });
  let mutations = 0;
  const client = {
    get() { return Promise.resolve(project); },
    update() { mutations += 1; return Promise.resolve(project); },
    confirm() { mutations += 1; return Promise.resolve(project); },
    generatePlan() { mutations += 1; return Promise.resolve(project); },
  };

  const initialViewer = shortDrama.createWorkspace({
    projectId: project.id, client, document: null, canEdit: false,
  });
  await initialViewer.ready;
  assert.equal(shortDrama.isRoleDowngrade('', 'viewer'), false);
  assert.equal(initialViewer.getState().destroyed, false,
    'an initially read-only viewer keeps the workspace open');
  assert.match(initialViewer.render(), /data-readonly="true"/);

  const editable = shortDrama.createWorkspace({
    projectId: project.id, client, document: null, canEdit: true, confirm: () => true,
  });
  await editable.ready;
  assert.equal(shortDrama.isRoleDowngrade('editor', 'viewer'), true);
  if (shortDrama.isRoleDowngrade('editor', 'viewer')) editable.destroy();
  assert.equal(editable.getState().destroyed, true);
  await assert.rejects(editable.saveSettings(project), /destroyed/i);
  await assert.rejects(editable.confirm('characters_review'), /destroyed/i);
  await assert.rejects(editable.generatePlan(), /destroyed/i);
  assert.equal(mutations, 0, 'downgraded editable workspace cannot save, confirm, or submit planning');
}

async function testWorkspaceLocksSettingsAndRejectsConcurrentPaidPlanning() {
  let updates = 0;
  const lockedProject = workspaceProject({ stage: 'characters_review' });
  const locked = shortDrama.createWorkspace({
    projectId: lockedProject.id, document: null, canEdit: true,
    client: {
      get() { return Promise.resolve(lockedProject); },
      update() { updates += 1; return Promise.resolve(lockedProject); },
      confirm() { return Promise.resolve(lockedProject); },
      generatePlan() { throw new Error('must not submit'); },
    },
  });
  await locked.ready;
  assert.equal(locked.selectStage('settings'), true);
  assert.match(locked.render(), /nc-short-drama-settings-form[\s\S]*data-action="save-settings" disabled/);
  await assert.rejects(locked.saveSettings(lockedProject), /stage is not editable/i);
  assert.equal(updates, 0, 'post-plan settings cannot diverge from generated content');

  let submits = 0;
  let confirmations = 0;
  let resolvePlan;
  let draft = workspaceProject({ stage: 'draft' });
  const planning = shortDrama.createWorkspace({
    projectId: draft.id, document: null, canEdit: true,
    confirm() { confirmations += 1; return true; },
    client: {
      get() { return Promise.resolve(draft); },
      update() { throw new Error('unexpected update'); },
      confirm() { throw new Error('unexpected confirm'); },
      generatePlan() {
        submits += 1;
        return new Promise((resolve) => { resolvePlan = resolve; });
      },
    },
  });
  await planning.ready;
  const first = planning.generatePlan();
  await assert.rejects(planning.generatePlan(), /busy/i);
  assert.equal(submits, 1, 'overlapping paid calls submit only once');
  assert.equal(confirmations, 1, 'busy rejection happens before a second paid confirmation');
  draft = workspaceProject({ stage: 'characters_review', revision: 8, spent_points: 6 });
  resolvePlan(draft);
  await first;

  let gets = 0;
  let resolveLatePlan;
  let summaries = 0;
  const closing = shortDrama.createWorkspace({
    projectId: 'closing-paid', document: null, canEdit: true, confirm: () => true,
    onChange() { summaries += 1; },
    client: {
      get() { gets += 1; return Promise.resolve(workspaceProject({ id: 'closing-paid', stage: 'draft' })); },
      update() { throw new Error('unexpected update'); }, confirm() { throw new Error('unexpected confirm'); },
      generatePlan() { return new Promise((resolve) => { resolveLatePlan = resolve; }); },
    },
  });
  await closing.ready;
  const late = closing.generatePlan();
  closing.destroy();
  resolveLatePlan(workspaceProject({ id: 'closing-paid', stage: 'characters_review' }));
  await assert.rejects(late, /destroyed/i);
  assert.equal(gets, 1, 'destroyed planning controller does not refresh or apply a late paid result');
  assert.equal(summaries, 0);
}

async function testWorkspaceOrderConflictReadonlyAndPlanning() {
  let project = workspaceProject();
  let updates = 0;
  let confirms = 0;
  let planningCalls = 0;
  let resolvePlan;
  let allowPaid = false;
  const confirmMessages = [];
  const client = {
    get() { return Promise.resolve(project); },
    update(id, revision, patch) {
      updates += 1;
      if (patch.characters && patch.characters[0] && patch.characters[0].name === '冲突') {
        const error = new Error('stale'); error.status = 409; error.code = 'revision_conflict';
        return Promise.reject(error);
      }
      project = Object.assign({}, project, patch, { revision: revision + 1 });
      return Promise.resolve(project);
    },
    confirm(id, revision, stage) {
      confirms += 1;
      project = Object.assign({}, project, { revision: revision + 1, stage: 'script_review' });
      return Promise.resolve(project);
    },
    generatePlan(received) {
      planningCalls += 1;
      assert.equal(received.synopsis, project.synopsis);
      return new Promise((resolve) => { resolvePlan = resolve; });
    },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: project.id, client, document: null, canEdit: true,
    confirm(message) { confirmMessages.push(message); return allowPaid; },
  });
  await workspace.ready;
  await assert.rejects(workspace.confirm('script_review'), /current stage|order/i);
  assert.equal(confirms, 0, 'confirmation cannot skip the current stage');
  await assert.rejects(workspace.saveCharacters(project.characters.map((character, index) => Object.assign({}, character,
    index === 0 ? { name: '冲突' } : {}))), /stale/);
  assert.equal(workspace.getState().error, '项目已在其他页面更新，请刷新后重试');
  assert.equal(workspace.getState().stale, true);

  const readonly = shortDrama.createWorkspace({ projectId: project.id, client, document: null, canEdit: false });
  await readonly.ready;
  await assert.rejects(readonly.saveCharacters(project.characters), /read.only/i);
  assert.equal(updates, 1, 'read-only workspace does not submit an update');
  assert.match(readonly.render(), /data-readonly="true"/);

  project = workspaceProject({ stage: 'draft' });
  const planning = shortDrama.createWorkspace({
    projectId: project.id, client, document: null, canEdit: true,
    confirm(message) { confirmMessages.push(message); return allowPaid; },
  });
  await planning.ready;
  assert.equal(await planning.generatePlan(), null);
  assert.equal(planningCalls, 0, 'cancelling the 3-point confirmation submits nothing');
  allowPaid = true;
  const pending = planning.generatePlan();
  assert.equal(planningCalls, 1);
  assert.equal(planning.getState().planning.running, true);
  assert.ok(planning.getState().planning.percent > 0);
  assert.match(planning.render(), /正在生成策划/);
  project = workspaceProject({ revision: 9, spent_points: 6 });
  resolvePlan(project);
  await pending;
  assert.equal(planning.getState().planning.running, false);
  assert.equal(planning.getState().planning.percent, 100);
  assert.ok(confirmMessages.some((message) => message.includes('3') && message.includes('点')));

  const placeholder = shortDrama.createWorkspace({
    projectId: 'placeholder', document: null, canEdit: true, confirm: () => true,
    client: {
      get() { return Promise.resolve(workspaceProject({ id: 'placeholder', stage: 'draft', synopsis: shortDrama.PLACEHOLDER_SYNOPSIS })); },
      update(id, revision, patch) { return Promise.resolve(workspaceProject(Object.assign({ id, revision: revision + 1, stage: 'draft' }, patch))); },
      generatePlan() { planningCalls += 100; return Promise.resolve({}); }, confirm() { return Promise.resolve({}); },
    },
  });
  await placeholder.ready;
  await assert.rejects(placeholder.generatePlan(), /synopsis|placeholder/i);
  assert.equal(placeholder.canGeneratePlan(), false);
  await placeholder.saveSettings(Object.assign({}, placeholder.getProject(), {
    synopsis: '用户已保存的全新故事梗概内容',
  }));
  assert.equal(placeholder.canGeneratePlan(), true, 'a replacement synopsis unlocks planning only after save');
}

async function testNoChargeFortyFiveSecondAcceptance() {
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const planned = workspaceProject({
    revision: 3, stage: 'characters_review', title: '横屏雨夜来客',
    synopsis: '一名侦探在暴雨夜必须用四十五秒识破危险访客的谎言',
    ratio: '16:9', target_duration: 45, shot_count: 8, spent_points: 3,
  });
  planned.shots = Array.from({ length: 8 }, (_, index) => ({
    id: `shot-id-${index + 1}`, project_id: planned.id, script_version: 1,
    shot_key: `shot-${index + 1}`, sort_order: index, duration: index === 0 ? 10 : 5,
    scene_description: `横屏雨夜场景 ${index + 1}`, camera_description: `镜头调度 ${index + 1}`,
    character_keys: [index % 2 ? 'detective' : 'visitor'],
    dialogue_line_ids: [index % 2 ? 'line-2' : 'line-1'],
    image_prompt: `16:9 cinematic still ${index + 1}`,
    video_prompt: `16:9 cinematic motion ${index + 1}`,
  }));

  let persisted = workspaceProject({
    revision: 1, stage: 'draft', title: '待完善短剧', synopsis: '这是一个等待完善的有效故事梗概',
    ratio: '9:16', target_duration: 30, shot_count: 6, spent_points: 0,
    characters: [], script_versions: [], shots: [],
  });
  const routeCalls = [];
  const paidPrompts = [];
  const summaries = [];
  let copySubmissions = 0;
  let jobPolls = 0;

  function revisionConflict() {
    const error = new Error('stale revision');
    error.status = 409; error.code = 'revision_conflict';
    return Promise.reject(error);
  }
  function acceptRevision(revision) {
    return revision === persisted.revision ? null : revisionConflict();
  }
  const api = {
    json(route, options) {
      routeCalls.push({ route, options: clone(options || null) });
      if (route === `/api/gen/short-drama/project?id=${encodeURIComponent(persisted.id)}` && !options) {
        return Promise.resolve(clone(persisted));
      }
      if (route === `/api/gen/short-drama/project?id=${encodeURIComponent(persisted.id)}` && options.method === 'PUT') {
        const body = clone(options.body);
        const rejected = acceptRevision(body.revision);
        if (rejected) return rejected;
        delete body.revision;
        if (body.characters) persisted.characters = body.characters;
        else if (body.script) {
          const version = (persisted.script_versions.at(-1)?.version || 0) + 1;
          persisted.script_versions = persisted.script_versions.concat([Object.assign({ version }, body.script)]);
        } else if (body.shots) {
          persisted.shots = body.shots.map((shot, index) => Object.assign({
            id: `saved-shot-${index + 1}`, project_id: persisted.id,
            script_version: persisted.script_versions.at(-1).version, sort_order: index,
          }, shot));
        } else persisted = Object.assign({}, persisted, body);
        persisted.revision += 1;
        return Promise.resolve(clone(persisted));
      }
      if (route === '/api/gen/copy') {
        copySubmissions += 1;
        assert.deepEqual(options.body, {
          format: 'short_drama', prompt: persisted.synopsis, dur: '45s', ratio: '16:9',
          shot_count: 8, style: persisted.visual_style, platform: persisted.target_platform,
        });
        return Promise.resolve({ job_id: 4516, cost: 3, points_left: 97 });
      }
      if (route === '/api/gen/job/4516') {
        jobPolls += 1;
        if (jobPolls === 1) return Promise.resolve({ status: 'running', progress: 50, phase: 'planning' });
        return Promise.resolve({
          status: 'done', result: JSON.stringify({ mode: 'short_drama', plan: { title: planned.title } }),
        });
      }
      if (route === '/api/gen/short-drama/apply-plan') {
        const rejected = acceptRevision(options.body.revision);
        if (rejected) return rejected;
        assert.deepEqual(options.body, {
          project_id: persisted.id, revision: persisted.revision, job_id: 4516,
        });
        persisted = Object.assign(clone(planned), { id: persisted.id, revision: persisted.revision + 1 });
        return Promise.resolve(clone(persisted));
      }
      if (route === '/api/gen/short-drama/confirm') {
        const rejected = acceptRevision(options.body.revision);
        if (rejected) return rejected;
        assert.equal(options.body.stage, persisted.stage, 'stage confirmation cannot skip ahead');
        persisted.stage = {
          characters_review: 'script_review', script_review: 'storyboard_review',
          storyboard_review: 'stills_review',
        }[persisted.stage];
        persisted.revision += 1;
        return Promise.resolve(clone(persisted));
      }
      throw new Error(`unexpected no-charge acceptance route: ${route}`);
    },
  };
  async function poll(options) {
    assert.equal(options.intervalMs, 3000);
    assert.equal(options.maxMs, 420000);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const outcome = options.inspect(await options.request());
      if (outcome.done) return outcome.value;
      if (outcome.error) throw outcome.error;
    }
    throw new Error('intercepted planning job did not complete');
  }
  const client = shortDrama.createClient(api, poll);
  const workspace = shortDrama.createWorkspace({
    projectId: persisted.id, client, document: null, canEdit: true,
    confirm(message) { paidPrompts.push(message); return true; },
    onChange(summary) { summaries.push(summary); },
  });
  await workspace.ready;

  await workspace.saveSettings(Object.assign({}, workspace.getProject(), {
    title: '横屏雨夜来客', synopsis: planned.synopsis, ratio: '16:9',
    target_duration: 45, shot_count: 8,
  }));
  assert.equal(persisted.spent_points, 0, 'settings save is free');

  persisted = Object.assign({}, persisted, { revision: persisted.revision + 1, title: '另一页面保存的标题' });
  await assert.rejects(
    workspace.saveSettings(Object.assign({}, workspace.getProject(), { title: '过期页面标题' })),
    (error) => error.status === 409 && error.code === 'revision_conflict',
  );
  assert.equal(workspace.getState().stale, true);
  assert.equal(workspace.getState().error, '项目已在其他页面更新，请刷新后重试');
  await workspace.reload();
  assert.equal(workspace.getProject().title, '另一页面保存的标题');

  await workspace.generatePlan();
  assert.equal(copySubmissions, 1, 'confirmed acceptance submits exactly one intercepted paid planning request');
  assert.equal(jobPolls, 2, 'intercepted job is polled through running and done states');
  assert.ok(paidPrompts.some((message) => message.includes('3') && message.includes('点')));
  assert.equal(workspace.getProject().stage, 'characters_review');
  assert.equal(workspace.getProject().shots.length, 8);
  assert.equal(workspace.getProject().shots.reduce((total, shot) => total + shot.duration, 0), 45);

  const editedCharacters = workspace.getProject().characters.map((character, index) =>
    Object.assign({}, character, index === 0 ? { name: `${character.name}（已确认）` } : {}));
  await workspace.saveCharacters(editedCharacters);
  await workspace.confirm('characters_review');
  const editedScript = Object.assign({}, workspace.getProject().script_versions.at(-1), {
    ending: '侦探在横屏画面中揭开最终真相',
  });
  await workspace.saveScript(editedScript);
  await workspace.confirm('script_review');
  const editedShots = workspace.getProject().shots.map((shot, index) =>
    Object.assign({}, shot, index === 7 ? { scene_description: '第八张分镜：真相揭晓' } : {}));
  await workspace.saveShots(editedShots);
  await workspace.confirm('storyboard_review');
  assert.equal(workspace.getProject().stage, 'stills_review');
  assert.equal(workspace.getProject().shots.length, 8);
  assert.equal(workspace.getProject().spent_points, 3, 'free saves and confirmations do not add to planning cost');

  await workspace.reload();
  assert.equal(workspace.getProject().stage, 'stills_review');
  assert.equal(workspace.getProject().characters[0].name.endsWith('（已确认）'), true);
  assert.equal(workspace.getProject().script_versions.length, 2);
  assert.equal(workspace.getProject().shots[7].scene_description, '第八张分镜：真相揭晓');

  const summary = summaries.at(-1);
  const node = shortDrama.sanitizeNodeData({
    id: 'acceptance-node', type: 'shortDrama',
    params: Object.assign({}, summary, {
      characters: workspace.getProject().characters,
      script: workspace.getProject().script_versions.at(-1), shots: workspace.getProject().shots,
    }),
    outputs: { characters: workspace.getProject().characters, shots: workspace.getProject().shots },
  });
  assert.deepEqual(node.params, shortDrama.summarizeProject(workspace.getProject()));
  assert.deepEqual(node.outputs, {}, 'canvas node persists only project id and summary fields');
  assert.equal(routeCalls.some(({ route }) => /image|audio|video/.test(route)), false,
    'Phase 1 acceptance creates no image, audio, or video task');
  workspace.destroy();
}

async function main() {
  testOpenApiContract();
  testCanvasIntegration();
  testNodePersistenceHelpers();
  await testCreateProjectCoordinatorIsBoardScoped();
  await testCreateProjectCoordinatorPreservesConflictingLink();
  await testCreateProjectCoordinatorScopeCleanup();
  await testPureHelpers();
  await testProjectRoutesAndPlanningFlow();
  await testPlanningErrorsPropagateWithoutApplying();
  await testTerminalJobFailureDoesNotApplyPlan();
  testMissingPollFailsClearly();
  testWorkspaceSourceAndRenderContract();
  testWorkspacePureStateAndPayloadHelpers();
  await testWorkspaceSavesUseExactRevisionedBodiesAndSummaries();
  await testWorkspaceLoadRecoveryOwnerIsolationAndDestroy();
  await testCollaborationRoleDowngradeDestroysEditableWorkspaceOnly();
  await testWorkspaceLocksSettingsAndRejectsConcurrentPaidPlanning();
  await testWorkspaceOrderConflictReadonlyAndPlanning();
  await testNoChargeFortyFiveSecondAcceptance();
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
