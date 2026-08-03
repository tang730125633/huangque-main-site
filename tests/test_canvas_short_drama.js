const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const shortDrama = require('../site/workbench/canvas/canvas-short-drama.js');

function testOpenApiContract() {
  const root = path.join(__dirname, '..');
  const spec = JSON.parse(fs.readFileSync(path.join(root, 'docs', 'api', 'openapi.json'), 'utf8'));
  const operations = [
    ['get', '/api/gen/short-drama/projects'],
    ['post', '/api/gen/short-drama/projects'],
    ['get', '/api/gen/short-drama/project'],
    ['put', '/api/gen/short-drama/project'],
    ['post', '/api/gen/short-drama/project/delete'],
    ['post', '/api/gen/short-drama/apply-plan'],
    ['post', '/api/gen/short-drama/confirm'],
    ['post', '/api/gen/short-drama/generate-character-reference'],
    ['get', '/api/gen/short-drama/planning-quote'],
    ['get', '/api/gen/short-drama/planning-job'],
    ['get', '/api/gen/short-drama/production'],
    ['get', '/api/gen/short-drama/voice'],
    ['get', '/api/gen/short-drama/master-timeline'],
    ['get', '/api/gen/short-drama/master-timeline/versions'],
    ['put', '/api/gen/short-drama/master-timeline'],
    ['post', '/api/gen/short-drama/master-timeline/rebuild'],
    ['post', '/api/gen/short-drama/master-timeline/confirm'],
    ['get', '/api/gen/short-drama/video'],
    ['get', '/api/gen/short-drama/avatar-candidates'],
    ['get', '/api/gen/short-drama/video-cast/avatars'],
    ['post', '/api/gen/short-drama/video-cast'],
    ['post', '/api/gen/short-drama/video-quote'],
    ['post', '/api/gen/short-drama/generate-video'],
    ['post', '/api/gen/short-drama/select-video-version'],
    ['post', '/api/gen/short-drama/set-video-shot-lock'],
    ['post', '/api/gen/short-drama/asset-quote'],
    ['post', '/api/gen/short-drama/select-asset'],
    ['post', '/api/gen/short-drama/confirm-production-stage'],
    ['post', '/api/gen/short-drama/generate-stills'],
  ];
  for (const [method, route] of operations) {
    const operation = spec.paths[route] && spec.paths[route][method];
    assert.ok(operation, `OpenAPI must document ${method.toUpperCase()} ${route}`);
    assert.ok(operation.responses['401'], `${method.toUpperCase()} ${route} must document authentication failure`);
    assert.ok(operation.responses['403'],
      `${method.toUpperCase()} ${route} must document forced-password-change failure`);
  }
  assert.ok(spec.paths['/api/gen/short-drama/project'].get.responses['404'],
    'project detail must document owner isolation as an indistinguishable not-found response');
  for (const [method, route] of operations.filter(([name]) => name !== 'get')) {
    assert.ok(spec.paths[route][method].responses['400'], `${method.toUpperCase()} ${route} must document validation failure`);
  }
  for (const [method, route] of [
    ['put', '/api/gen/short-drama/project'],
    ['post', '/api/gen/short-drama/apply-plan'],
  ]) {
    assert.ok(spec.paths[route][method].responses['404'], `${method.toUpperCase()} ${route} must document owner isolation`);
    assert.equal(
      spec.paths[route][method].responses['409'].content['application/json'].schema.$ref,
      '#/components/schemas/RevisionConflict',
      `${method.toUpperCase()} ${route} must document optimistic-concurrency conflict`,
    );
  }
  const confirmConflicts = spec.paths['/api/gen/short-drama/confirm'].post
    .responses['409'].content['application/json'].schema.oneOf;
  assert.deepEqual(
    new Set(confirmConflicts.map((candidate) => candidate.$ref)),
    new Set([
      '#/components/schemas/RevisionConflict',
      '#/components/schemas/TimelineHandoffConflict',
    ]),
    'voice stage confirmation must document revision and master-timeline conflicts',
  );
  assert.deepEqual(
    spec.components.schemas.TimelineHandoffConflict.properties.code.enum,
    ['timeline_handoff_not_ready'],
  );
  assert.equal(
    spec.paths['/api/gen/short-drama/project/delete'].post.responses['409']
      .content['application/json'].schema.$ref,
    '#/components/schemas/ShortDramaDeleteConflict',
  );
  const applyConflict = spec.paths['/api/gen/short-drama/apply-plan'].post.responses['409'];
  assert.match(applyConflict.description, /job_already_applied/,
    'apply-plan conflict must document duplicate job application');

  for (const name of [
    'ShortDramaProject', 'ShortDramaProjectSummary', 'ShortDramaCharacter',
    'ShortDramaScriptVersion', 'ShortDramaShot', 'RevisionConflict',
    'ShortDramaDeleteConflict', 'ShortDramaPlanningRequest', 'ShortDramaPlanningResult',
  ]) assert.ok(spec.components.schemas[name], `OpenAPI must define ${name}`);
  assert.equal(
    spec.paths['/api/gen/short-drama/projects'].get.responses['200']
      .content['application/json'].schema.properties.items.items.$ref,
    '#/components/schemas/ShortDramaProjectSummary',
  );
  assert.equal(spec.components.schemas.ShortDramaProject.properties.characters.maxItems, 20);
  assert.equal(spec.components.schemas.ShortDramaProject.properties.script_versions.maxItems, 20);
  assert.equal(spec.components.schemas.ShortDramaScriptVersion.properties.dialogue_lines.maxItems, 120);
  const quote = spec.paths['/api/gen/short-drama/planning-quote'].get;
  assert.equal(quote.responses['200'].content['application/json'].schema.properties.cost.type, 'integer');
  assert.match(quote.description, /free|no points/i);
  assert.ok(spec.paths['/api/gen/short-drama/planning-job'].get.responses['404']);
  const voiceOperation = spec.paths['/api/gen/short-drama/voice'].get;
  assert.deepEqual(voiceOperation.security, [{ bearerAuth: [] }]);
  const voiceProjectId = voiceOperation.parameters.find((parameter) =>
    parameter.name === 'project_id' && parameter.in === 'query');
  assert.ok(voiceProjectId && voiceProjectId.required,
    'voice workspace requires the project_id query parameter');
  for (const status of ['400', '401', '403', '404']) {
    assert.ok(voiceOperation.responses[status],
      `voice workspace must document ${status}`);
  }
  const voiceSchema = voiceOperation.responses['200']
    .content['application/json'].schema;
  const alignmentReviewSchema = spec.paths[
    '/api/gen/short-drama/subtitle-alignment/timeline'
  ].post.requestBody.content['application/json'].schema;
  assert.ok(alignmentReviewSchema.required.includes('review_action'));
  assert.deepEqual(
    alignmentReviewSchema.properties.review_action.enum,
    ['save_adjustments', 'confirm_unchanged'],
  );
  assert.equal(alignmentReviewSchema.additionalProperties, false);
  const productionSchema = spec.paths['/api/gen/short-drama/production'].get
    .responses['200'].content['application/json'].schema;
  assert.ok(productionSchema.required.includes('handoff_blocked'));
  assert.ok(productionSchema.required.includes('handoff_blockers'));
  assert.equal(productionSchema.properties.handoff_blocked.type, 'boolean');
  assert.deepEqual(
    productionSchema.properties.handoff_blockers.items.required,
    ['code', 'message'],
  );
  assert.equal(spec.openapi, '3.0.3');
  const blockerShotId = productionSchema.properties.handoff_blockers
    .items.properties.shot_id;
  assert.equal(blockerShotId.type, 'string');
  assert.equal(Object.hasOwn(blockerShotId, 'nullable'), false);
  assert.equal(
    productionSchema.properties.handoff_blockers.items.required.includes('shot_id'),
    false,
  );
  assert.match(voiceOperation.responses['403'].description, /密码|画布基础访问/);
  assert.doesNotMatch(voiceOperation.responses['403'].description, /项目权限/);
  assert.match(voiceOperation.responses['404'].description, /不存在|无权发现/);
  const voiceShot = voiceSchema.properties.shots.items;
  for (const field of [
    'id', 'shot_key', 'sort_order', 'duration', 'locked',
    'timeline_revision', 'status', 'lines',
  ]) assert.ok(voiceShot.required.includes(field),
    `voice shot must require ${field}`);
  const voiceLine = voiceShot.properties.lines.items;
  for (const field of [
    'id', 'dialogue_line_id', 'line_type', 'sort_order', 'character_key',
    'character_name', 'source_text', 'speech_text', 'subtitle_text',
    'subtitle_visible', 'voice_key', 'speed', 'pitch', 'volume',
    'current_version', 'start_ms', 'end_ms', 'input_hash', 'versions', 'job',
  ]) assert.ok(voiceLine.required.includes(field),
    `voice line must require ${field}`);
  const cinematicQuote = spec.paths['/api/gen/cinematic/quote'].post;
  assert.ok(cinematicQuote.responses['400'] && cinematicQuote.responses['401']);
  assert.match(cinematicQuote.description, /free|no points/i);

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
  assert.ok(shortDramaVariant.required.includes('project_id'));
  assert.ok(shortDramaVariant.required.includes('project_revision'));
  const planningResult = spec.components.schemas.ShortDramaPlanningResult;
  assert.ok(planningResult.required.includes('type') && planningResult.required.includes('dur'));
  for (const field of ['project_id', 'project_revision', 'settings']) {
    assert.ok(planningResult.required.includes(field), `planning result must bind ${field}`);
  }
  assert.ok(planningResult.properties.plan.properties.characters.items.properties.key,
    'copy result uses planning character key before persistence');
  assert.ok(planningResult.properties.plan.properties.script.properties.conflict,
    'copy result documents normalized planning script fields');
  assert.ok(planningResult.properties.plan.properties.shots.items.properties.key,
    'copy result uses planning shot key before persistence');
  const copyOperation = spec.paths['/api/gen/copy'].post;
  assert.doesNotMatch(copyOperation.summary + copyOperation.description + copyOperation.responses['200'].description,
    /(?:current|currently|目前|当前)\s*3|3\s*点/i, 'copy pricing must come from the authenticated quote');
  assert.match(copyOperation.responses['400'].description, /before deduction|未扣点/i);
  assert.match(copyOperation.responses['400'].description, /budget|预算/i);
  assert.match(copyOperation.description, /asynchronous|异步/i,
    'copy documentation distinguishes accepted-job failures from synchronous validation');
  const characterSchema = spec.components.schemas.ShortDramaCharacter;
  assert.equal(characterSchema.oneOf.length, 2);
  const cinematicAvatar = characterSchema.oneOf.find((candidate) =>
    candidate.properties.source_type.enum.includes('cinematic_avatar'));
  assert.ok(cinematicAvatar.required.includes('avatar_id'));
  assert.equal(cinematicAvatar.properties.avatar_id.minLength, 1);
  assert.match(characterSchema.description || '', /owner|当前用户|本人/i);
  const jobResult = spec.components.schemas.JobStatus.properties.result.oneOf;
  assert.equal(jobResult.some((candidate) => candidate.$ref === '#/components/schemas/ShortDramaPlanningResult'), true);
  const genericJobResult = jobResult.find((candidate) => candidate.type === 'object');
  assert.ok(genericJobResult.not, 'generic job result must exclude the dedicated short-drama shape');
  assert.match(spec.paths['/api/gen/short-drama/projects'].post.description, /free|no points/i);
  assert.match(spec.paths['/api/gen/short-drama/project'].put.description, /free|no points/i);
  const optionalBoardHeader = '#/components/parameters/XCanvasBoardId';
  const requiredBoardHeader = '#/components/parameters/XCanvasBoardIdRequired';
  assert.equal(spec.components.parameters.XCanvasBoardId.name, 'X-Canvas-Board-Id');
  assert.equal(spec.components.parameters.XCanvasBoardId.in, 'header');
  assert.equal(spec.components.parameters.XCanvasBoardId.required, false);
  assert.equal(spec.components.parameters.XCanvasBoardIdRequired.name, 'X-Canvas-Board-Id');
  assert.equal(spec.components.parameters.XCanvasBoardIdRequired.required, true);
  for (const [route, pathItem] of Object.entries(spec.paths)) {
    for (const [method, operation] of Object.entries(pathItem)) {
      if (!operation || !Array.isArray(operation.parameters)) continue;
      const parameterKeys = operation.parameters.map((parameter) => {
        if (!parameter.$ref) return `${parameter.in}:${parameter.name}`;
        const componentName = parameter.$ref.split('/').pop();
        const component = spec.components.parameters[componentName];
        return `${component.in}:${component.name}`;
      });
      assert.equal(
        new Set(parameterKeys).size,
        parameterKeys.length,
        `${method.toUpperCase()} ${route} must not declare duplicate parameters`,
      );
    }
  }
  for (const [method, route] of [
    ['get', '/api/gen/short-drama/planning-job'],
    ['get', '/api/gen/short-drama/production'],
    ['get', '/api/gen/short-drama/voice'],
    ['get', '/api/gen/short-drama/projects'],
    ['post', '/api/gen/short-drama/projects'],
    ['get', '/api/gen/short-drama/project'],
    ['put', '/api/gen/short-drama/project'],
    ['post', '/api/gen/short-drama/project/delete'],
    ['post', '/api/gen/short-drama/apply-plan'],
    ['post', '/api/gen/short-drama/confirm'],
    ['post', '/api/gen/short-drama/asset-quote'],
    ['post', '/api/gen/short-drama/select-asset'],
    ['post', '/api/gen/short-drama/confirm-production-stage'],
    ['post', '/api/gen/short-drama/generate-stills'],
    ['post', '/api/gen/copy'],
  ]) {
    assert.ok(spec.paths[route][method].parameters.some((parameter) =>
      parameter.$ref === optionalBoardHeader),
    `${method.toUpperCase()} ${route} must document its optional local/shared board scope`);
  }
  assert.ok(spec.components.schemas.ShortDramaProject.required.includes('board_id'));
  assert.equal(spec.components.schemas.ShortDramaProject.properties.board_id.nullable, true);
  assert.ok(spec.components.schemas.ShortDramaProjectSummary.required.includes('board_id'));
  const createProjectSchema = spec.paths['/api/gen/short-drama/projects'].post
    .requestBody.content['application/json'].schema;
  assert.equal(createProjectSchema.additionalProperties, false);
  assert.ok(createProjectSchema.properties.board_id);
  for (const schemaName of [
    'ShortDramaStillRequest',
    'ShortDramaStillSubmission',
    'ShortDramaAssetSelectionRequest',
    'ShortDramaProductionStageRequest',
  ]) {
    assert.equal(
      spec.components.schemas[schemaName].additionalProperties,
      false,
      `${schemaName} must reject undocumented request fields`,
    );
  }
  for (const route of [
    '/api/gen/short-drama/asset-quote',
    '/api/gen/short-drama/select-asset',
    '/api/gen/short-drama/confirm-production-stage',
    '/api/gen/short-drama/generate-stills',
  ]) {
    const operation = spec.paths[route].post;
    assert.ok(operation.requestBody.content['application/json'].schema,
      `${route} must document its JSON request`);
    assert.ok(operation.responses['200'].content['application/json'].schema,
      `${route} must document its success response`);
  }
  assert.equal(
    spec.paths['/api/gen/short-drama/generate-stills'].post.parameters
      .find((parameter) => parameter.$ref === '#/components/parameters/IdempotencyKeyRequired')
      .$ref,
    '#/components/parameters/IdempotencyKeyRequired',
  );
  const assetQuoteResponses = spec.paths['/api/gen/short-drama/asset-quote'].post.responses;
  assert.ok(assetQuoteResponses['409'], 'stale asset quote revisions return HTTP 409');
  assert.match(assetQuoteResponses['409'].description, /revision_conflict/i);
  const assetQuoteConflict =
    assetQuoteResponses['409'].content['application/json'].schema;
  assert.deepEqual(assetQuoteConflict.required, ['detail', 'code']);
  assert.equal(assetQuoteConflict.additionalProperties, false);
  assert.deepEqual(assetQuoteConflict.properties.code.enum, [
    'revision_conflict', 'asset_snapshot_missing', 'asset_snapshot_blocked',
    'asset_snapshot_stale', 'asset_snapshot_invalid',
  ]);
  assert.doesNotMatch(
    assetQuoteResponses['400'].description,
    /revision/i,
    'revision conflicts must not be documented as HTTP 400',
  );
  const stillSubmissionResponses =
    spec.paths['/api/gen/short-drama/generate-stills'].post.responses;
  assert.ok(stillSubmissionResponses['502'], 'points service failures return HTTP 502');
  assert.match(stillSubmissionResponses['502'].description, /points|点数/i);
  const pointsFailure =
    stillSubmissionResponses['502'].content['application/json'].schema;
  assert.deepEqual(pointsFailure.required, ['detail', 'need']);
  assert.equal(pointsFailure.additionalProperties, false);
  const productionShot =
    spec.components.schemas.ShortDramaProductionWorkspace.properties.shots.items;
  const continuityReference = productionShot.properties.references.items.oneOf
    .find((candidate) => candidate.properties.type.enum.includes('continuity'));
  const stillVersion = productionShot.properties.still.properties.versions.items;
  assert.equal(
    continuityReference.properties.url.format,
    'uri-reference',
    'continuity URLs may be absolute or relative API paths',
  );
  assert.equal(
    stillVersion.properties.url.format,
    'uri-reference',
    'asset URLs may be absolute or relative API paths',
  );
  assert.match(spec.paths['/api/gen/short-drama/projects'].get.description,
    /owner\/editor\/viewer/i);
  assert.match(spec.paths['/api/gen/short-drama/project'].put.responses['403'].description,
    /viewer/i);
  assert.match(spec.paths['/api/gen/short-drama/project/delete'].post.description,
    /owner\/editor/i);
  const listProjects = spec.paths['/api/gen/short-drama/projects'].get;
  assert.deepEqual(
    listProjects.parameters.map((parameter) => parameter.name || parameter.$ref),
    [optionalBoardHeader, 'page', 'page_size'],
  );
  const listSchema = listProjects.responses['200'].content['application/json'].schema;
  for (const field of ['items', 'page', 'page_size', 'total', 'total_pages']) {
    assert.ok(listSchema.required.includes(field), `project list requires ${field}`);
  }
  assert.ok(spec.paths['/api/gen/short-drama/projects'].post.responses['429'],
    'project creation documents the per-user cap');
  assert.match(spec.components.schemas.ShortDramaProject.properties.spent_points.description, /deduct|refund|扣点|退款/i);
  assert.doesNotMatch(spec.paths['/api/gen/short-drama/apply-plan'].post.description, /spent_points 增加/,
    'applying a paid plan must not claim to charge or count the job a second time');
}

function testCanvasIntegration() {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas.html'), 'utf8');
  const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
  const moduleSource = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.js'), 'utf8').replace(/\r\n/g, '\n');
  const css = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.css'), 'utf8').replace(/\r\n/g, '\n');
  const productionSource = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama-production.js'), 'utf8').replace(/\r\n/g, '\n');
  const productionCss = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama-production.css'), 'utf8').replace(/\r\n/g, '\n');
  const voiceSource = fs.readFileSync(
    path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama-voice.js'),
    'utf8'
  ).replace(/\r\n/g, '\n');
  const voiceCss = fs.readFileSync(
    path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama-voice.css'),
    'utf8'
  ).replace(/\r\n/g, '\n');
  const ci = fs.readFileSync(path.join(root, '.github', 'workflows', 'ci.yml'), 'utf8');
  const appSource = app.replace(/\r\n/g, '\n');

  assert.ok(html.includes('canvas/canvas-short-drama.css?v='));
  assert.ok(html.includes('canvas/canvas-short-drama.js?v='));
  assert.ok(html.includes('canvas/canvas-short-drama-production.css?v='));
  assert.ok(html.includes('canvas/canvas-short-drama-production.js?v='));
  assert.ok(html.includes('canvas/canvas-short-drama-voice.css?v='));
  assert.ok(html.includes('canvas/canvas-short-drama-voice.js?v='));
  assert.ok(html.indexOf('canvas/canvas-short-drama.css?v=') < html.indexOf('canvas/canvas-short-drama-production.css?v='));
  assert.ok(html.indexOf('canvas/canvas-short-drama-production.css?v=') < html.indexOf('canvas/canvas-short-drama-voice.css?v='));
  assert.ok(html.indexOf('canvas/canvas-short-drama-production.js?v=') < html.indexOf('canvas/canvas-short-drama.js?v='));
  assert.ok(html.indexOf('canvas/canvas-short-drama-production.js?v=') < html.indexOf('canvas/canvas-short-drama-voice.js?v='));
  assert.ok(html.indexOf('canvas/canvas-short-drama.js?v=') < html.indexOf('canvas/canvas-short-drama-voice.js?v='),
    'the main workspace registers before the late-bound voice module');
  assert.ok(html.indexOf('canvas/canvas-short-drama.js?v=') < html.indexOf('canvas/canvas-app.js?v='));
  for (const command of [
    'node tests/test_canvas_api.js',
    'node tests/test_canvas_short_drama.js',
    'node tests/test_canvas_short_drama_production.js',
    'node tests/test_canvas_short_drama_voice.js',
  ]) assert.ok(ci.includes(command), `CI must run ${command}`);
  assert.equal((html.match(/data-add="shortDrama"/g) || []).length, 2);
  assert.equal((html.match(/data-add="image"/g) || []).length, 2);
  assert.equal((html.match(/data-add="reverse"/g) || []).length, 2);
  assert.match(html, /双击画布空白处添加节点/);
  assert.match(html, /id="ncFsAdd"[^>]*>\+ 添加节点<\/button>/);
  assert.match(app, /shortDrama:\s*\{name:'短剧项目',\s*color:'#[a-f0-9]+'\}/);
  assert.ok(app.includes('data-f="openShortDrama"'));
  assert.ok(app.includes('shortDramaModule.createWorkspace('));
  assert.match(app, /projectId:projectId,\s*apiClient:apiClient,\s*poll:apiModule\.poll,\s*boardId:currentBoardScope==='collab'\?currentBoardId:null,\s*canEdit:canEdit,\s*onChange:onChange/);
  assert.match(app, /onDelete:function\(\)\{[\s\S]*?delete nodes\[nodeId\]/,
    'deleting a short drama project removes its bound canvas node');
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
  assert.match(app, /nodeAriaDisabled\(node,readonly\)[\s\S]*?shortDramaModule\.canOpenNode\(node\.params,false\)/,
    'readonly linked short-drama nodes must not expose disabled ARIA semantics');
  const ensureSource = app.match(/function ensureShortDramaProject\(node,scopeKey\)\{[\s\S]*?\n  \}/)[0];
  assert.doesNotMatch(ensureSource, /scheduleSave\(/, 'coordinator apply is the single save path');

  for (const [asset, source] of [
    ['canvas/canvas-app.js', appSource],
    ['canvas/canvas-short-drama.js', moduleSource],
    ['canvas/canvas-short-drama.css', css],
    ['canvas/canvas-short-drama-production.js', productionSource],
    ['canvas/canvas-short-drama-production.css', productionCss],
    ['canvas/canvas-short-drama-voice.js', voiceSource],
    ['canvas/canvas-short-drama-voice.css', voiceCss],
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
  assert.equal(shortDrama.normalizeSettings({ shot_count: 11 }).shot_count, 6,
    '30 second projects normalize to the only feasible shot count');
  assert.deepEqual(shortDrama.validShotCounts(30), [6]);
  assert.deepEqual(shortDrama.validShotCounts(45), [6, 7, 8, 9]);
  assert.deepEqual(shortDrama.validShotCounts(60), [6, 7, 8, 9, 10]);
  assert.ok(shortDrama.validateSettings({
    title: '短剧', synopsis: '这是足够长的故事梗概', ratio: '9:16',
    target_duration: 30, shot_count: 7, visual_style: '写实', target_platform: '抖音',
  }).some((message) => message.includes('不匹配')));

  const project = workspaceProject();
  const tooManyCharacters = Array.from({ length: 21 }, (_, index) => ({
    ...project.characters[0], character_key: `character-${index}`, name: `角色${index}`,
  }));
  assert.ok(shortDrama.validateCharacters(tooManyCharacters).some((message) => message.includes('20')));
  const tooManyLines = Array.from({ length: 121 }, (_, index) => ({
    id: `line-${index}`, character_key: project.characters[0].character_key, text: `台词${index}`,
  }));
  assert.ok(shortDrama.validateScript({
    ...project.script_versions[0], dialogue_lines: tooManyLines,
  }, project).some((message) => message.includes('120')));

  assert.deepEqual(shortDrama.planningPayload(settings), {
    format: 'short_drama', project_id: undefined, project_revision: undefined,
    prompt: settings.synopsis, dur: '45s', ratio: '9:16',
    shot_count: 8, style: settings.visual_style, platform: settings.target_platform,
  });
  assert.equal(shortDrama.stageIndex('storyboard_review'), 3);
  assert.equal(shortDrama.stageIndex('stills_review'), 4);
  assert.equal(shortDrama.stageIndex('voice_review'), 5);
  assert.equal(shortDrama.stageIndex('video_review'), 6);
  assert.equal(shortDrama.stageIndex('assembly_review'), 7);
  assert.equal(shortDrama.stageIndex('completed'), 8);
  assert.equal(shortDrama.summarizeProject({ stage: 'stills_review' }).progress, 50);
  assert.equal(shortDrama.summarizeProject({ stage: 'voice_review' }).progress, 63);
  assert.equal(shortDrama.summarizeProject({ stage: 'completed' }).progress, 100);
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
      if (path === '/api/gen/short-drama/planning-quote') return Promise.resolve({ cost: 7 });
      if (path.startsWith('/api/gen/short-drama/planning-job?')) return Promise.resolve({ job_id: null });
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

  assert.deepEqual(await client.getPlanningQuote(), { cost: 7 });
  await client.list();
  await client.get('project 1');
  await client.create({ title: '雨夜来客' });
  await client.update('project 1', 5, { revision: 99, title: '新标题' });
  await client.applyPlan('project 1', 6, 41);
  await client.confirm('project 1', 7, 'characters_review');
  await client.delete('project 1', 8);
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
    { path: '/api/gen/short-drama/planning-quote', options: undefined },
    { path: '/api/gen/short-drama/projects?page=1&page_size=20', options: undefined },
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
      path: '/api/gen/short-drama/project/delete',
      options: { method: 'POST', body: { project_id: 'project 1', revision: 8 } },
    },
    {
      path: '/api/gen/short-drama/planning-job?project_id=project-1', options: undefined,
    },
    {
      path: '/api/gen/copy',
      options: {
        method: 'POST', body: {
          format: 'short_drama', project_id: 'project-1', project_revision: 7,
          prompt: '陌生女孩敲开侦探的门', dur: '45s', ratio: '16:9',
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

async function testAvatarCandidateClientUsesCompatibilityFallback() {
  const calls = [];
  const api = {
    json(route) {
      calls.push(route);
      if (route.startsWith('/api/gen/short-drama/avatar-candidates?')) {
        const error = new Error('not found');
        error.status = 404;
        return Promise.reject(error);
      }
      return Promise.resolve({ items: [{ id: 7, name: '兼容形象' }] });
    },
  };
  const result = await shortDrama.createClient(api, () => Promise.resolve())
    .getAvatarCandidates('project 7', false);
  assert.deepEqual(result.items, [{ id: 7, name: '兼容形象' }]);
  assert.deepEqual(calls, [
    '/api/gen/short-drama/avatar-candidates?project_id=project%207',
    '/api/gen/short-drama/video-cast/avatars?project_id=project%207',
  ]);
}

async function testPaidPlanningRecoveryReusesJobWithoutAnotherCopyPost() {
  const calls = [];
  let revision = 8;
  const api = {
    json(path, options) {
      calls.push({ path, options });
      if (path === '/api/gen/short-drama/planning-job?project_id=project-1') {
        return Promise.resolve({ job_id: 77, status: 'done', cost: 11 });
      }
      if (path === '/api/gen/job/77') return Promise.resolve({
        status: 'done', result: JSON.stringify({ mode: 'short_drama', project_id: 'project-1' }),
      });
      if (path === '/api/gen/short-drama/apply-plan') {
        assert.equal(options.body.job_id, 77);
        assert.equal(options.body.revision, revision);
        return Promise.resolve({ id: 'project-1', revision: ++revision, stage: 'characters_review' });
      }
      if (path === '/api/gen/copy') throw new Error('recovery must not create another paid job');
      throw new Error(`unexpected recovery route ${path}`);
    },
  };
  const poll = (options) => options.request().then((job) => options.inspect(job).value);
  const result = await shortDrama.createClient(api, poll).generatePlan({
    id: 'project-1', revision, synopsis: '刷新后仍可恢复的故事梗概', ratio: '9:16',
    target_duration: 30, shot_count: 6, visual_style: '电影写实', target_platform: '抖音',
  });
  assert.equal(result.revision, 9);
  assert.equal(calls.filter((call) => call.path === '/api/gen/copy').length, 0);
  assert.deepEqual(calls.filter((call) => call.path.includes('/apply-plan'))[0].options.body, {
    project_id: 'project-1', revision: 8, job_id: 77,
  });
}

async function testTerminalJobFailureDoesNotApplyPlan() {
  let applyCalled = false;
  const api = {
    json(path) {
      if (path.startsWith('/api/gen/short-drama/planning-job?')) return Promise.resolve({ job_id: null });
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
      if (path.startsWith('/api/gen/short-drama/planning-job?')) return Promise.resolve({ job_id: null });
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
      if (path.startsWith('/api/gen/short-drama/planning-job?')) return Promise.resolve({ job_id: null });
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

async function testPlanningQuoteFailureDoesNotSubmit() {
  const quoteError = new Error('quote unavailable');
  let submitted = false;
  const project = workspaceProject({ stage: 'draft' });
  const workspace = shortDrama.createWorkspace({
    projectId: project.id, document: null, canEdit: true, confirm: () => true,
    client: {
      get() { return Promise.resolve(project); },
      update() { throw new Error('unexpected update'); },
      confirm() { throw new Error('unexpected confirm'); },
      getPlanningQuote() { return Promise.reject(quoteError); },
      generatePlan() { submitted = true; return Promise.resolve(project); },
    },
  });
  await workspace.ready;
  await assert.rejects(workspace.generatePlan(), (error) => error === quoteError);
  assert.equal(submitted, false, 'a failed quote must not submit /api/gen/copy');
  assert.equal(workspace.getState().busy, false);
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

async function testWorkspaceSourceAndRenderContract() {
  const root = path.join(__dirname, '..');
  const source = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.js'), 'utf8');
  const css = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-short-drama.css'), 'utf8');
  const app = fs.readFileSync(path.join(root, 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
  for (const text of [
    '项目设置', '角色确认', '剧本确认', '分镜确认', '按实时报价',
    '确认角色并继续', '确认剧本并继续', '确认分镜',
    '项目已在其他页面更新，请刷新后重试',
  ]) assert.ok(source.includes(text), `workspace source must include ${text}`);
  for (const endpoint of [
    '/api/gen/short-drama/project', '/api/gen/short-drama/confirm', '/api/gen/copy',
    '/api/gen/short-drama/apply-plan', '/api/gen/short-drama/planning-quote',
    '/api/gen/short-drama/avatar-candidates',
  ]) assert.ok(source.includes(endpoint), `workspace client must use ${endpoint}`);
  assert.match(
    source,
    /querySelectorAll\('\.nc-short-drama-dialogue\[data-dialogue-index\]'\)/,
    'script form reads dialogue cards only',
  );
  assert.doesNotMatch(
    source,
    /querySelectorAll\('\[data-dialogue-index\]'\)/,
    'copy/delete controls must never be parsed as empty dialogue lines',
  );
  assert.doesNotMatch(source, /3\s*点/, 'workspace must not hard-code the planning price as fact');
  assert.ok(css.includes('.nc-short-drama-workspace'));
  assert.ok(css.includes('.nc-short-drama-character-rail'));
  assert.ok(css.includes('.nc-short-drama-editor'));
  assert.ok(css.includes('.nc-short-drama-inspector'));
  assert.match(css, /\.nc-short-drama-production-slot\s*\{[^}]*flex:\s*1[^}]*min-height:\s*0/s,
    'production slot fills the remaining workspace below the shared topbar');
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
  assert.match(app, /payload\.board_id=currentBoardId/,
    'collaborative short-drama projects are server-associated with their board');
  assert.match(app, /boardId:currentBoardScope==='collab'\?currentBoardId:null/,
    'production API receives the trusted collaboration scope identifier');
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
    activeStage: 'settings', canEdit: true,
  }), /data-tab="stills_review"[^>]*>[\s\S]*画面确认/,
  'production has a labelled navigation tab');
  assert.doesNotMatch(shortDrama.renderWorkspace(workspaceProject({ stage: 'characters_review' }), {
    activeStage: 'stills_review', canEdit: true,
  }), /nc-short-drama-production|data-action="generate-current"/,
  'production controls cannot be opened before the server advances to stills_review');

  const stillCalls = [];
  const voiceCalls = [];
  const assemblyCalls = [];
  const delegatedOptions = [];
  const voiceOptions = [];
  const assemblyOptions = [];
  let stillDestroyCalls = 0;
  let voiceDestroyCalls = 0;
  let assemblyDestroyCalls = 0;
  const stillModule = {
    createWorkspace(options) {
      stillCalls.push(options.projectId);
      delegatedOptions.push(options);
      return {
        projectId: options.projectId,
        ready: Promise.resolve({ stage: 'stills_review' }),
        render() { return '<section class="nc-short-drama-production">production controls</section>'; },
        reload() { return Promise.resolve({ stage: 'stills_review' }); },
        destroy() { stillDestroyCalls += 1; },
      };
    },
  };
  const voiceModule = {
    createWorkspace(options) {
      voiceCalls.push(options.projectId);
      voiceOptions.push(options);
      return {
        ready: Promise.resolve(),
        render() { return '<section class="voice-workspace">voice workspace</section>'; },
        reload() { return Promise.resolve(); },
        destroy() { voiceDestroyCalls += 1; },
      };
    },
  };
  const assemblyModule = {
    createWorkspace(options) {
      assemblyCalls.push(options.projectId);
      assemblyOptions.push(options);
      return {
        ready: Promise.resolve(),
        render() { return '<section class="assembly-workspace">assembly workspace</section>'; },
        reload() { return Promise.resolve(); },
        destroy() { assemblyDestroyCalls += 1; },
      };
    },
  };
  const apiClient = { json() { throw new Error('fake production client is inspected, not called'); } };
  const confirmMessages = [];
  const confirm = (message) => { confirmMessages.push(message); return true; };
  const canvasSummaries = [];
  const onChange = (summary) => { canvasSummaries.push(summary); };
  const productionHost = { kind: 'production-host' };
  const body = {
    appendChild(node) { node.parentNode = body; },
    removeChild(node) { if (node.parentNode === body) node.parentNode = null; },
  };
  const host = {
    innerHTML: '', parentNode: null,
    addEventListener() {}, removeEventListener() {},
    querySelector(selector) { return selector === '[data-production-host]' ? productionHost : null; },
  };
  const document = { body, createElement() { return host; } };
  const stillsWorkspace = shortDrama.createWorkspace({
    projectId: 'project-stills', boardId: 'board-voice', document, canEdit: false,
    productionModule: stillModule, voiceModule, apiClient, confirm, onChange,
    client: {
      get() { return Promise.resolve(workspaceProject({ id: 'project-stills', stage: 'stills_review' })); },
      update() { throw new Error('phase-one update must not run'); },
      confirm() { throw new Error('phase-one confirm must not run'); },
      generatePlan() { throw new Error('phase-one planning must not run'); },
    },
  });
  await stillsWorkspace.ready;
  assert.equal(delegatedOptions.length, 1);
  assert.equal(delegatedOptions[0].projectId, 'project-stills');
  assert.strictEqual(delegatedOptions[0].client, apiClient);
  assert.equal(delegatedOptions[0].canEdit, false);
  assert.notStrictEqual(delegatedOptions[0].confirm, confirm,
    'wrapper adapts production quotes into user-facing confirmation messages');
  await delegatedOptions[0].confirm(24, { cost: 24, count: 2, shot_count: 1 }, {
    shot_id: 'shot-2', count: 2,
  });
  await delegatedOptions[0].confirm(30, {
    cost: 30, count: 4, kind: 'still-batch', shot_count: 2,
    quotes: [
      {
        shot_id: 'shot-4', base_prompt: '第四镜分镜提示词',
        user_direction: '第四镜补充要求', compiled_prompt: '第四镜最终提交提示词',
        source_prompt_hash: '4'.repeat(64),
      },
      {
        shot_id: 'shot-2', base_prompt: '第二镜分镜提示词',
        user_direction: '', compiled_prompt: '第二镜最终提交提示词',
        source_prompt_hash: '2'.repeat(64),
      },
    ],
  }, [
    { shot_id: 'shot-2', count: 2 }, { shot_id: 'shot-4', count: 2 },
  ]);
  assert.match(confirmMessages[0], /生成镜头 shot-2 的 2 张关键帧候选将消耗 24 点/);
  assert.match(confirmMessages[1], /批量生成 2 个镜头的关键帧[\s\S]*将消耗 30 点/);
  assert.match(confirmMessages[1],
    /镜头 1（shot-2）[\s\S]*第二镜分镜提示词[\s\S]*第二镜最终提交提示词/);
  assert.match(confirmMessages[1],
    /镜头 2（shot-4）[\s\S]*第四镜分镜提示词[\s\S]*第四镜补充要求[\s\S]*第四镜最终提交提示词/);
  assert.ok(confirmMessages[1].indexOf('第二镜最终提交提示词')<
    confirmMessages[1].indexOf('第四镜最终提交提示词'),
  'batch confirmation follows request order rather than quote response order');
  await assert.rejects(async () => delegatedOptions[0].confirm(30, {
    cost: 30, count: 4, kind: 'still-batch', shot_count: 2,
    quotes: [
      { shot_id: 'shot-2', base_prompt: '第二镜', user_direction: '',
        compiled_prompt: '', source_prompt_hash: '2'.repeat(64) },
      { shot_id: 'shot-4', base_prompt: '第四镜', user_direction: '',
        compiled_prompt: '第四镜最终提示词', source_prompt_hash: '4'.repeat(64) },
    ],
  }, [
    { shot_id: 'shot-2', count: 2 }, { shot_id: 'shot-4', count: 2 },
  ]), /批量报价缺少完整提示词/);
  assert.equal(confirmMessages.length, 2,
    'an incomplete batch quote fails before the paid confirmation hook');
  assert.notStrictEqual(delegatedOptions[0].onChange, onChange,
    'wrapper adapts production summaries before persisting them to the canvas');
  const voiceSummary = {
    project_id: 'project-stills', revision: 9, stage: 'voice_review', ratio: '16:9',
    spent_points: 24, point_budget: 100, reserved_points: 0,
    shots: [{ asset: { versions: [{ url: '/secret.png', job: 91 }] } }],
  };
  await delegatedOptions[0].onChange(voiceSummary);
  assert.equal(stillsWorkspace.getProject().stage, 'voice_review');
  assert.deepEqual(canvasSummaries, [{
    project_id: 'project-stills', title: '雨夜来客', ratio: '16:9', target_duration: 30,
    stage: 'voice_review', progress: 63, spent_points: 24, estimated_points: 12,
  }]);
  assert.doesNotMatch(JSON.stringify(canvasSummaries), /shot|asset|version|job|url/i);
  assert.equal(stillDestroyCalls, 1, 'advancing to voice destroys the still workspace exactly once');
  assert.deepEqual(voiceCalls, ['project-stills']);
  assert.equal(voiceOptions.length, 1);
  assert.equal(voiceOptions[0].projectId, 'project-stills');
  assert.equal(voiceOptions[0].boardId, 'board-voice');
  assert.strictEqual(voiceOptions[0].client, apiClient);
  assert.strictEqual(voiceOptions[0].host, productionHost);
  assert.equal(confirmMessages.length, 2, 'voice workspace does not invoke the still confirmation adapter');
  assert.match(stillsWorkspace.render(), /配音字幕[\s\S]*data-action="close"[\s\S]*voice-workspace/);

  await delegatedOptions[0].onChange(voiceSummary);
  await voiceOptions[0].onChange(voiceSummary);
  assert.equal(stillDestroyCalls, 1, 'repeated summaries do not destroy another still workspace');
  assert.equal(voiceCalls.length, 1, 'repeated summaries do not recreate the voice workspace');
  stillsWorkspace.destroy();
  assert.equal(stillDestroyCalls, 1);
  assert.equal(voiceDestroyCalls, 1, 'destroy cascades into the active voice workspace');

  const voiceWorkspace = shortDrama.createWorkspace({
    projectId: 'project-voice', document: null,
    productionModule: stillModule, voiceModule, apiClient,
    client: { get() { return Promise.resolve(workspaceProject({ id: 'project-voice', stage: 'voice_review' })); } },
  });
  await voiceWorkspace.ready;
  assert.deepEqual(stillCalls, ['project-stills']);
  assert.deepEqual(voiceCalls, ['project-stills', 'project-voice']);
  voiceWorkspace.destroy();
  assert.equal(voiceDestroyCalls, 2);

  let resolveSwitchStillReady;
  let switchStillOptions;
  let switchStillDestroys = 0;
  let switchVoiceCreates = 0;
  let switchVoiceDestroys = 0;
  const switching = shortDrama.createWorkspace({
    projectId: 'switching-project', document: null, apiClient,
    productionModule: {
      createWorkspace(options) {
        switchStillOptions = options;
        return {
          ready: new Promise((resolve) => { resolveSwitchStillReady = resolve; }),
          render() { return '<section>late still workspace</section>'; },
          reload() { return Promise.resolve(); },
          destroy() { switchStillDestroys += 1; },
        };
      },
    },
    voiceModule: {
      createWorkspace() {
        switchVoiceCreates += 1;
        return {
          ready: Promise.resolve(),
          render() { return '<section>switched voice workspace</section>'; },
          reload() { return Promise.resolve(); },
          destroy() { switchVoiceDestroys += 1; },
        };
      },
    },
    client: { get() { return Promise.resolve(workspaceProject({ id: 'switching-project', stage: 'stills_review' })); } },
  });
  for (let spin = 0; spin < 10 && !switchStillOptions; spin += 1) await Promise.resolve();
  const switchingReady = switching.ready;
  await switchStillOptions.onChange({ project_id: 'switching-project', stage: 'voice_review', revision: 10 });
  assert.equal(switchStillDestroys, 1);
  assert.equal(switchVoiceCreates, 1);
  resolveSwitchStillReady({ stage: 'stills_review' });
  assert.equal(await switchingReady, null, 'late still readiness cannot remount over voice');
  await switchStillOptions.onChange({ project_id: 'switching-project', stage: 'stills_review', revision: 8 });
  assert.equal(switchVoiceCreates, 1, 'late callbacks from the destroyed still workspace are ignored');
  assert.equal(switching.getProject().stage, 'voice_review');
  assert.match(switching.render(), /switched voice workspace/);
  switching.destroy();
  assert.equal(switchVoiceDestroys, 1);

  let synchronousVoiceDestroys = 0;
  let synchronousProductionCreates = 0;
  let synchronousProductionDestroys = 0;
  const synchronous = shortDrama.createWorkspace({
    projectId: 'synchronous-project', document: null, apiClient,
    productionModule: {
      createWorkspace() {
        synchronousProductionCreates += 1;
        return {
          ready: Promise.resolve(),
          render() { return '<section>synchronous production fallback</section>'; },
          reload() { return Promise.resolve(); },
          destroy() { synchronousProductionDestroys += 1; },
        };
      },
    },
    voiceModule: {
      createWorkspace(options) {
        options.onChange({ project_id: 'synchronous-project', stage: 'video_review', revision: 12 });
        return {
          ready: Promise.resolve(),
          render() { return '<section>synchronous voice workspace</section>'; },
          reload() { return Promise.resolve(); },
          destroy() { synchronousVoiceDestroys += 1; },
        };
      },
    },
    client: { get() { return Promise.resolve(workspaceProject({ id: 'synchronous-project', stage: 'voice_review' })); } },
  });
  await synchronous.ready;
  assert.equal(synchronous.getProject().stage, 'video_review',
    'a construction-time voice summary is applied after the delegate is installed');
  assert.equal(synchronousVoiceDestroys, 1);
  assert.equal(synchronousProductionCreates, 1);
  assert.match(synchronous.render(), /synchronous production fallback/);
  synchronous.destroy();
  assert.equal(synchronousProductionDestroys, 1);

  let resolveDelegateReady;
  let lateDestroyCalls = 0;
  const closing = shortDrama.createWorkspace({
    projectId: 'closing-production', document: null, apiClient,
    productionModule: {
      createWorkspace() {
        return {
          projectId: 'closing-production',
          ready: new Promise((resolve) => { resolveDelegateReady = resolve; }),
          render() { return '<section class="nc-short-drama-production">late</section>'; },
          destroy() { lateDestroyCalls += 1; },
        };
      },
    },
    client: { get() { return Promise.resolve(workspaceProject({ id: 'closing-production', stage: 'stills_review' })); } },
  });
  for (let spin = 0; spin < 10 && !resolveDelegateReady; spin += 1) await Promise.resolve();
  assert.equal(typeof resolveDelegateReady, 'function', 'test reaches delegated production readiness');
  closing.destroy();
  resolveDelegateReady({ stage: 'stills_review' });
  assert.equal(await closing.ready, null, 'late delegated readiness is ignored after destroy');
  assert.equal(lateDestroyCalls, 1);

  for (const stage of ['video_review', 'assembly_review', 'completed']) {
    const later = shortDrama.createWorkspace({
      projectId: `project-${stage}`, document: null,
      productionModule: stillModule, voiceModule, assemblyModule, apiClient,
      canEdit: true,
      client: { get() { return Promise.resolve(workspaceProject({ id: `project-${stage}`, stage })); } },
    });
    await later.ready;
    if (stage === 'video_review') {
      assert.equal(delegatedOptions.at(-1).projectId, `project-${stage}`,
        'video_review delegates to production');
    } else {
      assert.equal(assemblyOptions.at(-1).projectId, `project-${stage}`,
        `${stage} delegates to assembly`);
      assert.equal(
        assemblyOptions.at(-1).canEdit,
        stage !== 'completed',
        'completed assembly workspace is always read-only',
      );
    }
    later.destroy();
  }
  assert.deepEqual(assemblyCalls, ['project-assembly_review', 'project-completed']);
  assert.equal(assemblyDestroyCalls, 2);

  const planning = shortDrama.createWorkspace({
    projectId: 'planning', document: null, productionModule: stillModule, voiceModule, apiClient,
    client: { get() { return Promise.resolve(workspaceProject({ id: 'planning', stage: 'storyboard_review' })); } },
  });
  await planning.ready;
  assert.equal(delegatedOptions.some((options) => options.projectId === 'planning'), false,
    'pre-stills projects never instantiate production controls');
  assert.doesNotMatch(planning.render(), /nc-short-drama-production|data-action="generate-current"/);
  planning.destroy();

  const missing = shortDrama.createWorkspace({
    projectId: 'missing-production', document: null, productionModule: null, apiClient,
    client: { get() { return Promise.resolve(workspaceProject({ id: 'missing-production', stage: 'stills_review' })); } },
  });
  assert.equal(await missing.ready, null);
  assert.match(missing.render(), /生产工作区未加载[\s\S]*data-action="reload"[\s\S]*data-action="close"/,
    'a missing production module renders a recoverable error instead of crashing');
  missing.destroy();
}

function testScriptValidationReportsTheExactBrokenDialogue() {
  const project = workspaceProject({ stage: 'script_review' });
  const script = Object.assign({}, project.script_versions[0], {
    dialogue_lines: [
      project.script_versions[0].dialogue_lines[0],
      {
        id: '', client_token: '', character_key: 'missing-role', text: '',
      },
    ],
  });
  const errors = shortDrama.validateScript(script, project);
  assert.ok(errors.includes('台词 2：请填写台词内容'));
  assert.ok(errors.includes('台词 2：新增台词缺少客户端请求标识'));
  assert.ok(errors.includes('台词 2：引用了未知角色 missing-role'));
  assert.ok(errors.every((message) => !message.includes('台词 3')),
    'one broken dialogue must not be expanded into synthetic button rows');
}

async function testScriptSaveIgnoresDialogueActionControls() {
  const project = workspaceProject({ stage: 'script_review' });
  const script = project.script_versions[0];
  const field = (value) => ({ value });
  const formFields = Object.fromEntries(
    ['title', 'logline', 'hook', 'conflict_text', 'turn_text', 'ending']
      .map((name) => [`[data-field="${name}"]`, field(script[name])]),
  );
  const dialogueFields = {
    '[data-field="id"]': field('line-1'),
    '[data-field="character_key"]': field('visitor'),
    '[data-field="text"]': field('我只有五分钟。'),
  };
  const dialogueCard = {
    className: 'nc-short-drama-dialogue',
    getAttribute(name) { return name === 'data-dialogue-index' ? '0' : null; },
    querySelector(selector) { return dialogueFields[selector] || null; },
  };
  const copyButton = {
    className: 'copy-dialogue',
    getAttribute(name) {
      if (name === 'data-dialogue-index') return '0';
      if (name === 'data-action') return 'copy-dialogue';
      return null;
    },
    querySelector() { return null; },
  };
  const scriptForm = {
    querySelector(selector) { return formFields[selector] || null; },
    querySelectorAll(selector) {
      if (selector === '.nc-short-drama-dialogue[data-dialogue-index]') return [dialogueCard];
      if (selector === '[data-dialogue-index]') return [dialogueCard, copyButton];
      return [];
    },
  };
  let clickHandler = null;
  let savedPatch = null;
  let resolveSaved;
  const saved = new Promise((resolve) => { resolveSaved = resolve; });
  const body = {
    appendChild(node) { node.parentNode = body; },
    removeChild(node) { if (node.parentNode === body) node.parentNode = null; },
  };
  const host = {
    innerHTML: '', parentNode: null,
    addEventListener(type, handler) { if (type === 'click') clickHandler = handler; },
    removeEventListener() {},
    querySelector(selector) {
      return selector === '.nc-short-drama-script-form' ? scriptForm : null;
    },
  };
  const document = { body, createElement() { return host; } };
  const client = {
    get() { return Promise.resolve(project); },
    update(id, revision, patch) {
      savedPatch = patch;
      resolveSaved();
      return Promise.resolve(Object.assign({}, project, { revision: revision + 1 }));
    },
    confirm() { throw new Error('unexpected confirmation'); },
    generatePlan() { throw new Error('unexpected paid generation'); },
  };
  const workspace = shortDrama.createWorkspace({ projectId: project.id, client, document });
  await workspace.ready;
  const saveButton = {
    parentNode: host,
    getAttribute(name) { return name === 'data-action' ? 'save-script' : null; },
  };
  clickHandler({ target: saveButton });
  await saved;
  assert.equal(scriptForm.querySelectorAll('[data-dialogue-index]').length, 2,
    'fixture contains both a real dialogue card and an indexed action control');
  assert.deepEqual(savedPatch.script.dialogue_lines, [{
    id: 'line-1', character_key: 'visitor', text: '我只有五分钟。',
  }], 'saved payload contains only the real dialogue card');
  assert.deepEqual(Object.keys(savedPatch.script.dialogue_lines[0]).sort(),
    ['character_key', 'id', 'text'], 'saved payload keeps the existing backend contract');
  workspace.destroy();
}

async function testBrowserGlobalProductionModuleFallbacks() {
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'site', 'workbench', 'canvas', 'canvas-short-drama.js'),
    'utf8'
  );
  const stillCalls = [];
  const voiceCalls = [];
  function fallbackModule(calls, label) {
    return {
      createWorkspace(options) {
        calls.push(options.projectId);
        return {
          ready: Promise.resolve(),
          render() { return `<section>${label}</section>`; },
          reload() { return Promise.resolve(); },
          destroy() {},
        };
      },
    };
  }
  const window = { HQCanvas: {
    shortDramaProduction: fallbackModule(stillCalls, 'global still'),
    shortDramaVoice: fallbackModule(voiceCalls, 'global voice'),
  } };
  vm.runInNewContext(source, { window }, { filename: 'canvas-short-drama.js' });
  const browserShortDrama = window.HQCanvas.shortDrama;
  const apiClient = { json() { throw new Error('global fallback stub does not call API'); } };
  const stills = browserShortDrama.createWorkspace({
    projectId: 'global-stills', document: null, apiClient,
    client: { get() { return Promise.resolve(workspaceProject({ id: 'global-stills', stage: 'stills_review' })); } },
  });
  const voice = browserShortDrama.createWorkspace({
    projectId: 'global-voice', document: null, apiClient,
    client: { get() { return Promise.resolve(workspaceProject({ id: 'global-voice', stage: 'voice_review' })); } },
  });
  await Promise.all([stills.ready, voice.ready]);
  assert.deepEqual(stillCalls, ['global-stills']);
  assert.deepEqual(voiceCalls, ['global-voice']);
  stills.destroy();
  voice.destroy();
}

async function testProductionWorkspaceCanReturnToPhaseOneReview() {
  const project = workspaceProject({ stage: 'stills_review' });
  let creates = 0;
  let destroys = 0;
  const workspace = shortDrama.createWorkspace({
    projectId: project.id, document: null,
    apiClient: { json() { throw new Error('delegate stub does not call the API'); } },
    productionModule: {
      createWorkspace() {
        creates += 1;
        return {
          projectId: project.id, ready: Promise.resolve(),
          render() { return '<section class="nc-short-drama-production">production</section>'; },
          destroy() { destroys += 1; },
        };
      },
    },
    client: { get() { return Promise.resolve(project); } },
  });
  await workspace.ready;
  assert.equal(creates, 1);
  assert.equal(workspace.selectStage('storyboard_review'), true);
  assert.match(workspace.render(), /分镜确认[\s\S]*data-field="image_prompt"/);
  assert.doesNotMatch(workspace.render(), /class="nc-short-drama-production"/);
  assert.equal(destroys, 1);

  assert.equal(workspace.selectStage('stills_review'), true);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(creates, 2);
  assert.match(workspace.render(), /class="nc-short-drama-production"/);
  workspace.destroy();
  assert.equal(destroys, 2);
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
      reference_job_id: character.reference_job_id ? Number(character.reference_job_id) : null,
      reference_locked: character.reference_locked === true || character.reference_locked === 'true',
      voice_key: character.voice_key, voice_settings: character.voice_settings,
    })),
  });
  assert.equal(shortDrama.makeCharactersPatch([Object.assign({}, project.characters[0], {
    avatar_id: 'must-not-survive',
  })]).characters[0].avatar_id, null, 'AI roles never persist an avatar binding');

  const baseCharacters = project.characters.map((character) => Object.assign({}, character));
  const localCharacters = baseCharacters.map((character, index) => Object.assign(
    {}, character, index === 0 ? { name: '本地侦探' } : {},
  ));
  const remoteCharacters = baseCharacters.map((character, index) => Object.assign(
    {}, character, index === 0 ? { personality: '服务端更新' } : {},
  ));
  const merged = shortDrama.mergeCharacterDrafts(baseCharacters, remoteCharacters, localCharacters);
  assert.deepEqual(merged.conflicts, []);
  assert.equal(merged.characters[0].name, '本地侦探');
  assert.equal(merged.characters[0].personality, '服务端更新');
  const conflicting = shortDrama.mergeCharacterDrafts(
    baseCharacters,
    baseCharacters.map((character, index) => Object.assign({}, character, index === 0 ? { name: '远端名称' } : {})),
    localCharacters,
  );
  assert.equal(conflicting.characters[0].name, '本地侦探',
    'same-field conflicts retain the unsaved local draft for manual resolution');
  assert.deepEqual(conflicting.conflicts.map((item) => [item.character_key, item.field]),
    [['detective', 'name']]);
  const remotelyAdded = shortDrama.mergeCharacterDrafts(
    baseCharacters,
    remoteCharacters.concat([Object.assign({}, baseCharacters[0], {
      character_key: 'new-role', name: '远端新增角色',
    })]),
    localCharacters,
  );
  assert.equal(remotelyAdded.characters[0].name, '本地侦探');
  assert.equal(remotelyAdded.characters.at(-1).name, '远端新增角色',
    'revision recovery never drops a role added by another editor');
  assert.deepEqual(remotelyAdded.conflicts.at(-1),
    { character_key: 'new-role', field: 'character_key', reason: 'added' });
  const locallyRenamed = shortDrama.mergeCharacterDrafts(
    baseCharacters,
    remoteCharacters,
    baseCharacters.map((character, index) => Object.assign(
      {}, character, index === 0 ? {
        character_key: 'renamed-detective', name: '本地改名侦探',
      } : {},
    )),
  );
  assert.deepEqual(locallyRenamed.conflicts, []);
  assert.equal(locallyRenamed.characters[0].character_key, 'renamed-detective');
  assert.equal(locallyRenamed.characters[0].name, '本地改名侦探');
  assert.equal(locallyRenamed.characters[0].personality, remoteCharacters[0].personality,
    'a local key rename remains associated with remote edits to the original role');
  const bothRenamed = shortDrama.mergeCharacterDrafts(
    baseCharacters,
    baseCharacters.map((character, index) => Object.assign(
      {}, character, index === 0 ? { character_key: 'remote-detective' } : {},
    )),
    baseCharacters.map((character, index) => Object.assign(
      {}, character, index === 0 ? {
        character_key: 'local-detective', name: '完整本地草稿',
      } : {},
    )),
  );
  assert.equal(bothRenamed.characters[0].character_key, 'local-detective');
  assert.equal(bothRenamed.characters[0].name, '完整本地草稿');
  assert.ok(bothRenamed.conflicts.some((item) => (
    item.character_key === 'local-detective' &&
    item.field === 'character_key' &&
    item.reason === 'changed'
  )));
  const remotelyDeleted = shortDrama.mergeCharacterDrafts(
    baseCharacters,
    [remoteCharacters[1]],
    baseCharacters.map((character, index) => Object.assign(
      {}, character, index === 0 ? { name: '删除冲突中的本地草稿' } : {},
    )),
  );
  assert.equal(remotelyDeleted.characters[0].character_key, 'detective');
  assert.equal(remotelyDeleted.characters[0].name, '删除冲突中的本地草稿');
  assert.ok(remotelyDeleted.conflicts.some((item) => (
    item.character_key === 'detective' && item.reason === 'removed'
  )));
  const acceptedRemoteDelete = shortDrama.mergeCharacterDrafts(
    baseCharacters,
    [remoteCharacters[1]],
    baseCharacters.map((character, index) => Object.assign(
      {}, character, index === 1 ? { name: '本地访客草稿' } : {},
    )),
  );
  assert.deepEqual(acceptedRemoteDelete.conflicts, []);
  assert.equal(acceptedRemoteDelete.characters.length, 1);
  assert.equal(acceptedRemoteDelete.characters[0].character_key, 'visitor');
  assert.equal(acceptedRemoteDelete.characters[0].name, '本地访客草稿');

  const selectorHtml = shortDrama.renderWorkspace(project, {
    activeStage: 'characters_review', canEdit: true,
    avatarCandidates: {
      loaded: true, loading: false, error: '', canCreate: true,
      items: [{ id: 'avatar-2', name: '雨夜访客', image_url: '/assets/avatar-2.jpg', status: 'ready' }],
    },
  });
  assert.match(selectorHtml, /雨夜访客/);
  assert.match(selectorHtml, /data-action="select-character-avatar"/);
  assert.match(selectorHtml, /data-action="create-character-avatar"/);
  assert.doesNotMatch(selectorHtml, />Avatar ID</,
    'the role editor must not ask users to copy an internal avatar ID');
  assert.doesNotMatch(selectorHtml, /provider_avatar_id/,
    'supplier identifiers must never be rendered into the role editor');
  assert.deepEqual(shortDrama.makeScriptPatch(project.script_versions[0]).script.dialogue_lines,
    project.script_versions[0].dialogue_lines.map((line) => ({
      id: line.id, character_key: line.character_key, text: line.text,
    })));
  const scriptEditor = shortDrama.renderWorkspace(Object.assign({}, project, {
    stage: 'script_review',
  }), {
    activeStage: 'script_review', canEdit: true,
  });
  assert.match(scriptEditor, /说话角色/);
  assert.match(scriptEditor, /访客/);
  assert.match(scriptEditor, /data-action="add-dialogue"/);
  assert.match(scriptEditor, /data-action="copy-dialogue"/);
  assert.match(scriptEditor, /data-action="delete-dialogue"/);
  assert.match(scriptEditor, /data-field="id"[^>]*readonly/,
    'line ids must be visible but immutable');
  assert.doesNotMatch(scriptEditor, /data-field="character_key"[^>]*value="visitor"/,
    'speaker selection must not expose character keys as editable text');
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

async function testConfirmSavesChangedSectionThenUsesReturnedRevisionAndSkipsUnchangedScriptSave() {
  let project = workspaceProject({ stage: 'characters_review', revision: 7 });
  const calls = [];
  const client = {
    get() { return Promise.resolve(project); },
    update(id, revision, patch) {
      calls.push(['update', id, revision, patch]);
      project = Object.assign({}, project, { revision: revision + 1 });
      if (patch.characters) project.characters = patch.characters;
      if (patch.script) project.script_versions = project.script_versions.concat([
        Object.assign({ version: project.script_versions.length + 1 }, patch.script),
      ]);
      return Promise.resolve(project);
    },
    confirm(id, revision, stage) {
      calls.push(['confirm', id, revision, stage]);
      project = Object.assign({}, project, {
        revision: revision + 1,
        stage: stage === 'characters_review' ? 'script_review' : 'storyboard_review',
      });
      return Promise.resolve(project);
    },
    generatePlan() { throw new Error('unexpected paid generation'); },
  };
  const workspace = shortDrama.createWorkspace({ projectId: project.id, client, document: null });
  await workspace.ready;
  const changedCharacters = project.characters.map((character, index) => Object.assign(
    {}, character, index === 0 ? { name: '保存后确认的侦探' } : {},
  ));
  await workspace.confirm('characters_review', changedCharacters);
  const versionsBefore = workspace.getProject().script_versions.length;
  await workspace.confirm('script_review', workspace.getProject().script_versions.at(-1));
  assert.deepEqual(calls, [
    ['update', 'project-1', 7, shortDrama.makeCharactersPatch(changedCharacters)],
    ['confirm', 'project-1', 8, 'characters_review'],
    ['confirm', 'project-1', 9, 'script_review'],
  ]);
  assert.equal(workspace.getProject().script_versions.length, versionsBefore,
    'unchanged script confirmation must not append a version');
  workspace.destroy();
}

async function testStoryboardConfirmRejectsServerPromptDriftBeforeAdvancing() {
  let project = workspaceProject({ stage: 'storyboard_review', revision: 20 });
  let confirmations = 0;
  const editedShots = project.shots.map((shot, index) => Object.assign({}, shot,
    index === 0 ? { image_prompt: '用户刚保存的画面要求' } : {}));
  const client = {
    get() { return Promise.resolve(project); },
    update(id, revision) {
      project = Object.assign({}, project, {
        revision: revision + 1,
        shots: project.shots,
      });
      return Promise.resolve(project);
    },
    confirm() {
      confirmations += 1;
      return Promise.resolve(project);
    },
    generatePlan() { throw new Error('unexpected paid generation'); },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: project.id, client, document: null, canEdit: true,
  });
  await workspace.ready;

  await assert.rejects(
    workspace.confirm('storyboard_review', editedShots),
    /服务器保存后的分镜与当前编辑内容不一致/,
  );
  assert.equal(confirmations, 0, 'a mismatched server echo must never advance the stage');
  workspace.destroy();
}

async function testHistoricalScriptVersionCannotBeConfirmedOrResavedAsLatest() {
  const base = workspaceProject({ stage: 'script_review', revision: 9 });
  let project = Object.assign({}, base, {
    script_versions: [
      Object.assign({}, base.script_versions[0], { version: 1, title: '历史版本' }),
      Object.assign({}, base.script_versions[0], { version: 2, title: '最新版本' }),
    ],
  });
  let updates = 0;
  let confirmations = 0;
  const client = {
    get() { return Promise.resolve(project); },
    update() { updates += 1; return Promise.resolve(project); },
    confirm() { confirmations += 1; return Promise.resolve(project); },
    generatePlan() { throw new Error('unexpected paid generation'); },
  };
  const workspace = shortDrama.createWorkspace({ projectId: project.id, client, document: null });
  await workspace.ready;

  await assert.rejects(
    workspace.saveScript(project.script_versions[0]),
    /最新版本/,
  );
  await assert.rejects(
    workspace.confirm('script_review', project.script_versions[0]),
    /最新版本/,
  );
  assert.equal(updates, 0, 'historical script confirmation must not create a new latest version');
  assert.equal(confirmations, 0, 'historical script confirmation must not advance the stage');
  assert.equal(workspace.getProject().script_versions.length, 2);

  const historicalHtml = shortDrama.renderWorkspace(project, {
    activeStage: 'script_review', canEdit: true, busy: false, scriptVersion: 1, planning: {},
  });
  assert.match(historicalHtml, /data-confirm-stage="script_review" disabled/,
    'historical script versions must render the confirm action disabled');
  workspace.destroy();
}

async function testScriptSaveSelectsReturnedLatestVersion() {
  const base = workspaceProject({ stage: 'script_review', revision: 9 });
  let project = Object.assign({}, base, {
    script_versions: [
      Object.assign({}, base.script_versions[0], { version: 1, title: '历史版本' }),
      Object.assign({}, base.script_versions[0], { version: 2, title: '保存前最新版' }),
    ],
  });
  let clickHandler = null;
  const body = {
    appendChild(node) { node.parentNode = body; },
    removeChild(node) { if (node.parentNode === body) node.parentNode = null; },
  };
  const host = {
    innerHTML: '', parentNode: null,
    addEventListener(type, handler) { if (type === 'click') clickHandler = handler; },
    removeEventListener() {},
  };
  const document = { body, createElement() { return host; } };
  const client = {
    get() { return Promise.resolve(project); },
    update(id, revision, patch) {
      project = Object.assign({}, project, {
        revision: revision + 1,
        script_versions: project.script_versions.concat([
          Object.assign({ version: 3 }, patch.script),
        ]),
      });
      return Promise.resolve(project);
    },
    confirm() { throw new Error('unexpected confirmation'); },
    generatePlan() { throw new Error('unexpected paid generation'); },
  };
  const workspace = shortDrama.createWorkspace({ projectId: project.id, client, document });
  await workspace.ready;
  clickHandler({
    target: {
      parentNode: host,
      getAttribute(name) { return name === 'data-script-version' ? '2' : null; },
    },
  });
  assert.equal(workspace.getState().scriptVersion, 2, 'test setup explicitly selects the current latest version');

  await workspace.saveScript(Object.assign({}, project.script_versions[1], { title: '保存后的新版本' }));

  assert.equal(workspace.getState().scriptVersion, null,
    'a newly returned latest version must clear the stale explicit selection');
  assert.match(workspace.render(), /data-script-version="3" class="is-active"/,
    'the newly created latest version must render as active immediately');
  assert.match(workspace.render(), /data-action="save-script">保存为新版本/,
    'the save action must be enabled for the returned latest version');
  assert.match(workspace.render(), /data-confirm-stage="script_review">确认剧本并继续/,
    'the confirm action must be enabled for the returned latest version');
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

async function testWorkspaceLatestLoadWinsAndStaleFailuresAreIgnored() {
  function deferredClient() {
    const pending = [];
    return {
      pending,
      client: {
        get() {
          return new Promise((resolve, reject) => { pending.push({ resolve, reject }); });
        },
      },
    };
  }
  let delegateCreates = 0;
  let delegateDestroys = 0;
  const productionModule = {
    createWorkspace(options) {
      delegateCreates += 1;
      return {
        projectId: options.projectId, ready: Promise.resolve(),
        render() { return '<section>production</section>'; },
        destroy() { delegateDestroys += 1; },
      };
    },
  };
  const apiClient = { json() { throw new Error('not called by the delegate stub'); } };

  const race = deferredClient();
  const workspace = shortDrama.createWorkspace({
    projectId: 'project-1', document: null, client: race.client, apiClient, productionModule,
  });
  const firstLoad = workspace.ready;
  const secondLoad = workspace.reload();
  assert.equal(race.pending.length, 2);
  race.pending[1].resolve(workspaceProject({ title: 'B', revision: 20, stage: 'stills_review' }));
  await secondLoad;
  assert.equal(workspace.getProject().title, 'B');
  assert.equal(delegateCreates, 1);
  race.pending[0].resolve(workspaceProject({ title: 'A', revision: 10, stage: 'stills_review' }));
  assert.equal(await firstLoad, null, 'an older successful load settles without publishing stale state');
  assert.equal(workspace.getProject().title, 'B', 'older A cannot overwrite newer B');
  assert.equal(delegateCreates, 1, 'older A cannot create a replacement delegate');
  assert.equal(delegateDestroys, 0, 'older A cannot destroy the newer B delegate');
  workspace.destroy();
  assert.equal(delegateDestroys, 1);

  const failureRace = deferredClient();
  const failureWorkspace = shortDrama.createWorkspace({
    projectId: 'project-1', document: null, client: failureRace.client, apiClient, productionModule,
  });
  const staleLoad = failureWorkspace.ready;
  const newestLoad = failureWorkspace.reload();
  failureRace.pending[1].resolve(workspaceProject({ title: 'newest', revision: 22, stage: 'stills_review' }));
  await newestLoad;
  failureRace.pending[0].reject(Object.assign(new Error('stale failure'), { status: 500 }));
  assert.equal(await staleLoad, null);
  assert.equal(failureWorkspace.getProject().title, 'newest');
  assert.equal(failureWorkspace.getState().error, '', 'a stale failure cannot replace the newest render');
  failureWorkspace.destroy();
}

async function testProductionModuleCanBeInstalledAfterInitialLoadFailure() {
  let delegateCreates = 0;
  const project = workspaceProject({ stage: 'stills_review' });
  const options = {
    projectId: project.id, document: null, productionModule: null,
    apiClient: { json() { throw new Error('not called by the delegate stub'); } },
    client: { get() { return Promise.resolve(project); } },
  };
  const workspace = shortDrama.createWorkspace(options);
  assert.equal(await workspace.ready, null, 'a missing production module leaves a recoverable load error');
  assert.equal(workspace.getState().loadFailed, true);

  options.productionModule = {
    createWorkspace(delegateOptions) {
      delegateCreates += 1;
      return {
        projectId: delegateOptions.projectId, ready: Promise.resolve(),
        render() { return '<section>late production module</section>'; },
        destroy() {},
      };
    },
  };
  assert.equal((await workspace.reload()).id, project.id);
  assert.equal(delegateCreates, 1, 'reload resolves the production module again after late installation');
  assert.equal(workspace.getState().error, '');
  assert.match(workspace.render(), /late production module/);
  workspace.destroy();
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
      getPlanningQuote() { return Promise.resolve({ cost: 7 }); },
      generatePlan() {
        submits += 1;
        return new Promise((resolve) => { resolvePlan = resolve; });
      },
    },
  });
  await planning.ready;
  const first = planning.generatePlan();
  await assert.rejects(planning.generatePlan(), /busy/i);
  await Promise.resolve();
  await Promise.resolve();
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
      getPlanningQuote() { return Promise.resolve({ cost: 7 }); },
      generatePlan() { return new Promise((resolve) => { resolveLatePlan = resolve; }); },
    },
  });
  await closing.ready;
  const late = closing.generatePlan();
  await Promise.resolve();
  await Promise.resolve();
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
  let quoteCalls = 0;
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
    getPlanningQuote() { quoteCalls += 1; return Promise.resolve({ cost: 7 }); },
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
  assert.equal(updates, 2,
    'revision recovery retries once, while the read-only workspace submits no further update');
  assert.match(readonly.render(), /data-readonly="true"/);

  project = workspaceProject({ stage: 'draft' });
  const planning = shortDrama.createWorkspace({
    projectId: project.id, client, document: null, canEdit: true,
    confirm(message) { confirmMessages.push(message); return allowPaid; },
  });
  await planning.ready;
  assert.equal(await planning.generatePlan(), null);
  assert.equal(quoteCalls, 1, 'planning reads a fresh authenticated quote before confirmation');
  assert.equal(planningCalls, 0, 'rejecting the quoted price submits nothing');
  allowPaid = true;
  const pending = planning.generatePlan();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(quoteCalls, 2, 'a new paid attempt obtains a new quote');
  assert.equal(planningCalls, 1);
  assert.equal(planning.getState().planning.running, true);
  assert.ok(planning.getState().planning.percent > 0);
  assert.match(planning.render(), /正在生成策划/);
  project = workspaceProject({ revision: 9, spent_points: 6 });
  resolvePlan(project);
  await pending;
  assert.equal(planning.getState().planning.running, false);
  assert.equal(planning.getState().planning.percent, 100);
  assert.ok(confirmMessages.some((message) => message.includes('7') && message.includes('点')));

  const placeholder = shortDrama.createWorkspace({
    projectId: 'placeholder', document: null, canEdit: true, confirm: () => true,
    client: {
      get() { return Promise.resolve(workspaceProject({ id: 'placeholder', stage: 'draft', synopsis: shortDrama.PLACEHOLDER_SYNOPSIS })); },
      update(id, revision, patch) { return Promise.resolve(workspaceProject(Object.assign({ id, revision: revision + 1, stage: 'draft' }, patch))); },
      getPlanningQuote() { throw new Error('invalid synopsis must fail before quote'); },
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

async function testNoChargeFortyFiveSecondControllerAcceptance() {
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const planned = workspaceProject({
    revision: 3, stage: 'characters_review', title: '横屏雨夜来客',
    synopsis: '一名侦探在暴雨夜必须用四十五秒识破危险访客的谎言',
    ratio: '16:9', target_duration: 45, shot_count: 8, spent_points: 7,
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
      if (route === '/api/gen/short-drama/planning-quote') {
        return Promise.resolve({ cost: 7 });
      }
      if (route === `/api/gen/short-drama/planning-job?project_id=${encodeURIComponent(persisted.id)}`) {
        return Promise.resolve({ job_id: null });
      }
      if (route === '/api/gen/copy') {
        copySubmissions += 1;
        assert.deepEqual(options.body, {
          format: 'short_drama', project_id: persisted.id, project_revision: persisted.revision,
          prompt: persisted.synopsis, dur: '45s', ratio: '16:9',
          shot_count: 8, style: persisted.visual_style, platform: persisted.target_platform,
        });
        return Promise.resolve({ job_id: 4516, cost: 7, points_left: 93 });
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
  assert.ok(paidPrompts.some((message) => message.includes('7') && message.includes('点')));
  const quoteIndex = routeCalls.findIndex(({ route }) => route === '/api/gen/short-drama/planning-quote');
  const submitIndex = routeCalls.findIndex(({ route }) => route === '/api/gen/copy');
  assert.ok(quoteIndex >= 0 && quoteIndex < submitIndex, 'quote precedes confirmed copy submission');
  assert.equal(workspace.getState().planning.cost, 7);
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
  assert.equal(workspace.getProject().spent_points, 7, 'free saves and confirmations do not add to quoted planning cost');

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

async function testWorkspaceDeletesProjectWithRevision() {
  const project = {
    id: 'project-delete', revision: 4, stage: 'characters_review', title: '待删除短剧',
    synopsis: '这是一个准备删除的短剧项目', ratio: '9:16', target_duration: 30,
    shot_count: 6, visual_style: '电影写实', target_platform: '抖音',
    characters: [], script_versions: [], shots: [],
  };
  let deleted = null;
  let removed = null;
  const workspace = shortDrama.createWorkspace({
    projectId: project.id,
    document: null,
    client: {
      get() { return Promise.resolve(project); },
      delete(projectId, revision) {
        deleted = { projectId, revision };
        return Promise.resolve({ id: projectId, revision: revision + 1, deleted: true });
      },
    },
    confirmDelete() { return true; },
    onDelete(result) { removed = result; },
  });
  await workspace.ready;
  assert.equal(workspace.selectStage('settings'), true);
  assert.match(workspace.render(), /data-action="delete-project">删除项目<\/button>/,
    'owners can delete after planning while no mutation is active');
  const result = await workspace.deleteProject();
  assert.deepEqual(deleted, { projectId: 'project-delete', revision: 4 });
  assert.deepEqual(result, { id: 'project-delete', revision: 5, deleted: true });
  assert.deepEqual(removed, result);
  workspace.destroy();

  const blocked = shortDrama.createWorkspace({
    projectId: project.id, document: null,
    client: {
      get() { return Promise.resolve(project); },
      delete() {
        const error = new Error('项目存在尚未处理的付费策划任务');
        error.status = 409; error.code = 'short_drama_unapplied_paid_job';
        return Promise.reject(error);
      },
    },
    confirmDelete() { return true; },
  });
  await blocked.ready;
  await assert.rejects(blocked.deleteProject(), /尚未处理/);
  assert.equal(blocked.getState().stale, false, 'non-revision 409 does not force a stale reload state');
  assert.equal(blocked.getState().error, '项目存在尚未处理的付费策划任务');
  blocked.destroy();

  const scopedCalls = [];
  const scopedClient = shortDrama.createClient({
    json(path, options) {
      scopedCalls.push({ path, options });
      return Promise.resolve({ id: 'project-delete', revision: 5, deleted: true });
    },
  }, () => Promise.resolve(), 'board-delete');
  await scopedClient.delete('project-delete', 4);
  assert.deepEqual(scopedCalls, [{
    path: '/api/gen/short-drama/project/delete',
    options: {
      method: 'POST',
      body: { project_id: 'project-delete', revision: 4 },
      headers: { 'X-Canvas-Board-Id': 'board-delete' },
    },
  }], 'collaborative deletion must carry the scoped board header');
}

function testCharacterReferenceUsesDurableProjectEndpoint() {
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'site', 'workbench', 'canvas', 'canvas-short-drama.js'),
    'utf8',
  ).replace(/\r\n/g, '\n');
  const start = source.indexOf('generateCharacterReference:function(project,character)');
  const end = source.indexOf('\n      }\n    };', start);
  assert.ok(start >= 0 && end > start);
  const block = source.slice(start, end);
  assert.match(block, /\/api\/gen\/short-drama\/generate-character-reference/);
  assert.match(block, /project_id:project\.id/);
  assert.match(block, /revision:project\.revision/);
  assert.match(block, /character_key:character\.character_key/);
  assert.doesNotMatch(block, /\/api\/gen\/image/);
  assert.match(source, /saveSectionIfChanged\('characters_review',characters\)/);
  assert.match(source, /client\.generateCharacterReference\(savedProject,savedCharacter\)/);
  assert.match(source, /client\.get\(project\.id\)/);
}

async function testCharacterReferencePersistsEditedPromptsBeforeGeneration() {
  let project = workspaceProject({ stage: 'characters_review', revision: 7 });
  const calls = [];
  const client = {
    get() { calls.push(['get']); return Promise.resolve(project); },
    update(id, revision, patch) {
      calls.push(['update', id, revision, patch]);
      project = Object.assign({}, project, patch, { revision: revision + 1 });
      return Promise.resolve(project);
    },
    generateCharacterReference(currentProject, character) {
      calls.push([
        'generate', currentProject.revision, character.character_key,
        character.personality, character.appearance_prompt, character.wardrobe_prompt,
      ]);
      return Promise.resolve({ job_id: 99 });
    },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: project.id, client, document: null, confirm: () => true,
  });
  await workspace.ready;
  calls.length = 0;
  const edited = project.characters.map((character, index) => Object.assign(
    {}, character, index === 0 ? {
      personality: '谨慎但富有同理心',
      appearance_prompt: '短发、琥珀色眼睛、自然妆容',
      wardrobe_prompt: '深绿色风衣与棕色短靴',
    } : {},
  ));

  await workspace.generateCharacterReference(0, edited);

  assert.deepEqual(calls.slice(0, 2), [
    ['update', 'project-1', 7, shortDrama.makeCharactersPatch(edited)],
    ['generate', 8, edited[0].character_key, edited[0].personality,
      edited[0].appearance_prompt, edited[0].wardrobe_prompt],
  ]);
  workspace.destroy();
}

async function testCharacterReferenceLocksMountedFormWhileSaving() {
  let project = workspaceProject({ stage: 'characters_review', revision: 7 });
  let resolveUpdate;
  let updateStarted;
  const updateStartedPromise = new Promise((resolve) => { updateStarted = resolve; });
  const personality = { value: '谨慎但富有同理心', disabled: false };
  const appearance = { value: '短发、琥珀色眼睛、自然妆容', disabled: false };
  const wardrobe = { value: '深绿色风衣与棕色短靴', disabled: false };
  const permanentlyDisabled = { value: 'locked', disabled: true };
  const controls = [personality, appearance, wardrobe, permanentlyDisabled];
  const attributes = {};
  const host = {
    innerHTML: '', parentNode: null,
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll(selector) {
      assert.equal(selector, 'input,textarea,select,button');
      return controls;
    },
    setAttribute(name, value) { attributes[name] = value; },
    removeAttribute(name) { delete attributes[name]; },
  };
  const body = {
    appendChild(node) { node.parentNode = body; },
    removeChild(node) { if (node.parentNode === body) node.parentNode = null; },
  };
  const document = { body, createElement() { return host; } };
  const calls = [];
  const client = {
    get() { calls.push(['get']); return Promise.resolve(project); },
    update(id, revision, patch) {
      calls.push(['update', id, revision, patch]);
      updateStarted();
      return new Promise((resolve) => { resolveUpdate = resolve; });
    },
    generateCharacterReference(currentProject, character) {
      calls.push(['generate', currentProject.revision, character.character_key]);
      return Promise.resolve({ job_id: 99 });
    },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: project.id, client, document, confirm: () => true,
  });
  await workspace.ready;
  calls.length = 0;
  const edited = project.characters.map((character, index) => Object.assign(
    {}, character, index === 0 ? {
      personality: personality.value,
      appearance_prompt: appearance.value,
      wardrobe_prompt: wardrobe.value,
    } : {},
  ));

  const generation = workspace.generateCharacterReference(0, edited);
  await updateStartedPromise;

  assert.equal(attributes['aria-busy'], 'true');
  assert.ok(controls.every((control) => control.disabled),
    'all mounted controls must be locked while the save request is in flight');
  assert.equal(personality.value, edited[0].personality);
  assert.equal(appearance.value, edited[0].appearance_prompt);
  assert.equal(wardrobe.value, edited[0].wardrobe_prompt);

  project = Object.assign({}, project, shortDrama.makeCharactersPatch(edited), { revision: 8 });
  resolveUpdate(project);
  await generation;

  assert.equal(attributes['aria-busy'], undefined);
  assert.equal(personality.disabled, false);
  assert.equal(appearance.disabled, false);
  assert.equal(wardrobe.disabled, false);
  assert.equal(permanentlyDisabled.disabled, true,
    'unlocking must preserve controls that were already disabled');
  assert.deepEqual(calls.slice(0, 2).map((call) => call.slice(0, 3)), [
    ['update', 'project-1', 7],
    ['generate', 8, edited[0].character_key],
  ]);
  workspace.destroy();
}

function testPlanningFormatErrorsUseActionableMessage() {
  const legacy = new Error('短剧规划缺少字段: id');
  assert.equal(
    shortDrama.workspaceErrorMessage(legacy),
    'AI 返回的剧本格式不完整，系统自动修复失败。本次失败会自动退款，请重新生成',
  );
}

async function testCharacterDraftsRecoverAcrossRevisionConflict() {
  const base = workspaceProject({ stage: 'characters_review', revision: 7 });
  let persisted = base;
  let updates = 0;
  const client = {
    get() { return Promise.resolve(persisted); },
    update(id, revision, patch) {
      updates += 1;
      if (updates === 1) {
        persisted = Object.assign({}, persisted, {
          revision: 8,
          characters: persisted.characters.map((character, index) => Object.assign(
            {}, character, index === 0 ? { personality: '远端新性格' } : {},
          )),
        });
        const error = new Error('stale revision');
        error.status = 409;
        error.code = 'revision_conflict';
        return Promise.reject(error);
      }
      assert.equal(revision, 8, 'automatic retry uses the latest project revision');
      persisted = Object.assign({}, persisted, patch, { revision: 9 });
      return Promise.resolve(persisted);
    },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: base.id, client, document: null, canEdit: true,
  });
  await workspace.ready;
  const local = base.characters.map((character, index) => Object.assign(
    {}, character, index === 0 ? {
      character_key: 'local-detective', name: '本地新名称',
    } : {},
  ));
  await workspace.saveCharacters(local);
  assert.equal(updates, 2, 'a non-overlapping revision conflict is retried exactly once');
  assert.equal(workspace.getProject().characters[0].character_key, 'local-detective');
  assert.equal(workspace.getProject().characters[0].name, '本地新名称');
  assert.equal(workspace.getProject().characters[0].personality, '远端新性格');
  const state = workspace.getState();
  assert.equal(state.characterDraftDirty, false);
  assert.equal(state.characterDrafts[0].character_key, 'local-detective');
  assert.equal(state.characterDrafts[0].personality, '远端新性格');
  workspace.destroy();
}

async function testCharacterRenameConflictKeepsWorkspaceDrafts() {
  const base = workspaceProject({ stage: 'characters_review', revision: 7 });
  let persisted = base;
  let updates = 0;
  const local = base.characters.map((character, index) => Object.assign(
    {}, character, index === 0 ? {
      character_key: 'local-detective',
      name: '完整本地角色',
      identity_text: '完整本地身份',
    } : {},
  ));
  const client = {
    get() { return Promise.resolve(persisted); },
    update() {
      updates += 1;
      persisted = Object.assign({}, persisted, {
        revision: 8,
        characters: persisted.characters.map((character, index) => Object.assign(
          {}, character, index === 0 ? {
            character_key: 'remote-detective',
            personality: '远端性格',
          } : {},
        )),
      });
      const error = new Error('stale revision');
      error.status = 409;
      error.code = 'revision_conflict';
      return Promise.reject(error);
    },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: base.id, client, document: null, canEdit: true,
  });
  await workspace.ready;
  await assert.rejects(
    workspace.saveCharacters(local),
    (error) => error && error.code === 'character_merge_conflict',
  );
  const state = workspace.getState();
  assert.equal(updates, 1, 'ambiguous rename conflicts are never auto-submitted');
  assert.equal(state.characterDraftDirty, true);
  assert.equal(state.characterDrafts[0].character_key, 'local-detective');
  assert.equal(state.characterDrafts[0].name, '完整本地角色');
  assert.equal(state.characterDrafts[0].identity_text, '完整本地身份');
  assert.equal(state.characterDrafts[0].personality, '远端性格');
  assert.ok(state.characterConflicts.some((item) => (
    item.character_key === 'local-detective' &&
    item.field === 'character_key'
  )));
  workspace.destroy();
}

function testScriptThreeWayMergeRules() {
  const base = workspaceProject({ stage: 'script_review' }).script_versions[0];
  const remote = Object.assign({}, base, {
    hook: 'remote hook',
    dialogue_lines: base.dialogue_lines.map((line, index) => (
      Object.assign({}, line, index === 0 ? { text: 'remote line' } : {})
    )),
  });
  const local = Object.assign({}, base, { ending: 'local ending' });
  const merged = shortDrama.mergeScriptDrafts(base, remote, local);
  assert.equal(merged.conflicts.length, 0);
  assert.equal(merged.script.hook, 'remote hook');
  assert.equal(merged.script.ending, 'local ending');
  assert.equal(merged.script.dialogue_lines[0].text, 'remote line');

  const conflictingLocal = Object.assign({}, base, {
    dialogue_lines: base.dialogue_lines.map((line, index) => (
      Object.assign({}, line, index === 0 ? { text: 'local line' } : {})
    )),
  });
  const conflict = shortDrama.mergeScriptDrafts(base, remote, conflictingLocal);
  assert.equal(conflict.conflicts.length, 1);
  assert.equal(conflict.conflicts[0].key, 'id:line-1');
  assert.equal(conflict.conflicts[0].field, 'text');
  assert.equal(conflict.script.dialogue_lines[0].text, 'local line');

  const deletedLocal = Object.assign({}, base, {
    dialogue_lines: base.dialogue_lines.slice(1),
  });
  const deleteConflict = shortDrama.mergeScriptDrafts(base, remote, deletedLocal);
  assert.ok(deleteConflict.conflicts.some((item) => (
    item.key === 'id:line-1' && item.reason === 'removed_local'
  )));
}

function testScriptOrderedInsertionsAndReorderConflicts() {
  const base = Object.assign(
    {}, workspaceProject({ stage: 'script_review' }).script_versions[0],
    { dialogue_lines: [
      { id: 'line-1', character_key: 'visitor', text: 'one' },
      { id: 'line-2', character_key: 'detective', text: 'two' },
      { id: 'line-3', character_key: 'visitor', text: 'three' },
    ] },
  );
  const localCopy = {
    client_token: 'copy-token', character_key: 'visitor', text: 'one copy',
  };
  const local = Object.assign({}, base, {
    dialogue_lines: [base.dialogue_lines[0], localCopy].concat(base.dialogue_lines.slice(1)),
  });
  const remote = Object.assign({}, base, { hook: 'remote hook' });
  const merged = shortDrama.mergeScriptDrafts(base, remote, local);
  assert.deepEqual(
    merged.script.dialogue_lines.map((line) => line.id || line.client_token),
    ['line-1', 'copy-token', 'line-2', 'line-3'],
  );
  assert.equal(merged.conflicts.length, 0);

  const remoteAddition = { id: 'remote-new', character_key: 'detective', text: 'remote new' };
  const remoteWithAddition = Object.assign({}, base, {
    dialogue_lines: [base.dialogue_lines[0], remoteAddition].concat(base.dialogue_lines.slice(1)),
  });
  const simultaneous = shortDrama.mergeScriptDrafts(base, remoteWithAddition, local);
  assert.deepEqual(
    simultaneous.script.dialogue_lines.map((line) => line.id || line.client_token),
    ['line-1', 'remote-new', 'copy-token', 'line-2', 'line-3'],
    'remote then local is the deterministic order for additions in the same gap',
  );

  const localReorder = Object.assign({}, base, {
    dialogue_lines: [base.dialogue_lines[1], base.dialogue_lines[0], base.dialogue_lines[2]],
  });
  const remoteReorder = Object.assign({}, base, {
    dialogue_lines: [base.dialogue_lines[0], base.dialogue_lines[2], base.dialogue_lines[1]],
  });
  const reorderConflict = shortDrama.mergeScriptDrafts(base, remoteReorder, localReorder);
  assert.ok(reorderConflict.conflicts.some((item) => item.field === '__order__'));
  assert.deepEqual(
    reorderConflict.script.dialogue_lines.map((line) => line.id),
    ['line-2', 'line-1', 'line-3'],
    'local order remains visible until the user resolves the order conflict',
  );
}

function testScriptTokenReceiptReconciliation() {
  const base = Object.assign(
    {}, workspaceProject({ stage: 'script_review' }).script_versions[0],
    { dialogue_lines: [
      { id: 'line-1', character_key: 'visitor', text: 'one' },
      { id: 'line-2', character_key: 'detective', text: 'two' },
    ] },
  );
  const localLine = {
    client_token: 'lost-token', character_key: 'visitor', text: 'saved once',
    subtitle_enabled: true, voice_overrides: {},
  };
  const remoteLine = Object.assign({}, localLine, {
    id: 'line-generated', speaker_name_snapshot: 'Visitor',
  });
  delete remoteLine.client_token;
  const local = Object.assign({}, base, {
    dialogue_lines: [base.dialogue_lines[0], localLine, base.dialogue_lines[1]],
  });
  const remote = Object.assign({}, base, {
    dialogue_lines: [base.dialogue_lines[0], remoteLine, base.dialogue_lines[1]],
  });
  const receipts = { 'lost-token': 'line-generated' };
  const reconciled = shortDrama.mergeScriptDrafts(base, remote, local, receipts);
  assert.equal(reconciled.conflicts.length, 0);
  assert.deepEqual(
    reconciled.script.dialogue_lines.map((line) => line.id || line.client_token),
    ['line-1', 'line-generated', 'line-2'],
  );
  assert.equal(
    reconciled.script.dialogue_lines.filter((line) => (
      line.id === 'line-generated' || line.client_token === 'lost-token'
    )).length,
    1,
  );

  const conflictingLocal = Object.assign({}, local, {
    dialogue_lines: [
      base.dialogue_lines[0],
      Object.assign({}, localLine, { text: 'changed after loss' }),
      base.dialogue_lines[1],
    ],
  });
  const mismatch = shortDrama.mergeScriptDrafts(base, remote, conflictingLocal, receipts);
  assert.ok(mismatch.conflicts.some((item) => (
    item.key === 'token:lost-token' && item.field === 'text'
  )));
}

async function testLostScriptSaveResponseRetriesWithOfficialId() {
  const base = workspaceProject({ stage: 'script_review', revision: 7 });
  const original = base.script_versions[0];
  const tokenLine = {
    client_token: 'lost-response-token', character_key: 'visitor', text: 'saved once',
    subtitle_enabled: true, voice_overrides: {},
  };
  const local = Object.assign({}, original, {
    dialogue_lines: [original.dialogue_lines[0], tokenLine, original.dialogue_lines[1]],
  });
  let persisted = base;
  let updates = 0;
  const client = {
    get() { return Promise.resolve(persisted); },
    update(id, revision, patch) {
      updates += 1;
      if (updates === 1) {
        const officialLine = Object.assign({}, tokenLine, {
          id: 'line-generated', speaker_name_snapshot: 'Visitor',
        });
        delete officialLine.client_token;
        persisted = Object.assign({}, persisted, {
          revision: 8,
          dialogue_token_receipts: { 'lost-response-token': 'line-generated' },
          script_versions: persisted.script_versions.concat(Object.assign({}, original, {
            version: 2,
            dialogue_lines: [original.dialogue_lines[0], officialLine, original.dialogue_lines[1]],
          })),
        });
        const error = new Error('response lost after commit');
        error.code = 'revision_conflict';
        return Promise.reject(error);
      }
      assert.equal(revision, 8);
      assert.equal(patch.script.dialogue_lines.length, 3);
      assert.equal(patch.script.dialogue_lines[1].id, 'line-generated');
      assert.equal(patch.script.dialogue_lines[1].client_token, undefined);
      persisted = Object.assign({}, persisted, {
        revision: 9,
        script_versions: persisted.script_versions.concat(
          Object.assign({ version: 3 }, patch.script),
        ),
      });
      return Promise.resolve(persisted);
    },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: base.id, client, document: null, canEdit: true,
  });
  await workspace.ready;
  await workspace.saveScript(local);
  assert.equal(updates, 2);
  assert.equal(
    workspace.getProject().script_versions.slice(-1)[0].dialogue_lines.length, 3,
  );
  workspace.destroy();
}

async function testScriptDraftAutoMergesNonOverlappingRevisionConflict() {
  const base = workspaceProject({ stage: 'script_review', revision: 7 });
  let persisted = base;
  let updates = 0;
  const client = {
    get() { return Promise.resolve(persisted); },
    update(id, revision, patch) {
      updates += 1;
      if (updates === 1) {
        const remoteScript = Object.assign({}, persisted.script_versions[0], {
          hook: 'remote hook', version: 2,
        });
        persisted = Object.assign({}, persisted, {
          revision: 8, script_versions: persisted.script_versions.concat(remoteScript),
        });
        const error = new Error('stale revision');
        error.code = 'revision_conflict';
        return Promise.reject(error);
      }
      assert.equal(revision, 8);
      assert.equal(patch.script.hook, 'remote hook');
      assert.equal(patch.script.ending, 'local ending');
      persisted = Object.assign({}, persisted, {
        revision: 9,
        script_versions: persisted.script_versions.concat(
          Object.assign({ version: 3 }, patch.script),
        ),
      });
      return Promise.resolve(persisted);
    },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: base.id, client, document: null, canEdit: true,
  });
  await workspace.ready;
  const local = Object.assign({}, base.script_versions[0], { ending: 'local ending' });
  await workspace.saveScript(local);
  assert.equal(updates, 2);
  assert.equal(workspace.getProject().script_versions.slice(-1)[0].hook, 'remote hook');
  assert.equal(workspace.getProject().script_versions.slice(-1)[0].ending, 'local ending');
  assert.equal(workspace.getState().scriptDraftDirty, false);
  workspace.destroy();
}

async function testScriptDraftConflictBlocksSilentOverwrite() {
  const base = workspaceProject({ stage: 'script_review', revision: 7 });
  let persisted = base;
  let updates = 0;
  const client = {
    get() { return Promise.resolve(persisted); },
    update() {
      updates += 1;
      const remoteScript = Object.assign({}, persisted.script_versions[0], {
        ending: 'remote ending', version: 2,
      });
      persisted = Object.assign({}, persisted, {
        revision: 8, script_versions: persisted.script_versions.concat(remoteScript),
      });
      const error = new Error('stale revision');
      error.code = 'revision_conflict';
      return Promise.reject(error);
    },
  };
  const workspace = shortDrama.createWorkspace({
    projectId: base.id, client, document: null, canEdit: true,
  });
  await workspace.ready;
  const local = Object.assign({}, base.script_versions[0], { ending: 'local ending' });
  await assert.rejects(
    workspace.saveScript(local),
    (error) => error && error.code === 'script_merge_conflict',
  );
  const state = workspace.getState();
  assert.equal(updates, 1);
  assert.equal(state.scriptDraftDirty, true);
  assert.equal(state.scriptDraft.ending, 'local ending');
  assert.equal(state.scriptConflicts.length, 1);
  assert.equal(state.scriptConflicts[0].field, 'ending');
  await assert.rejects(workspace.saveScript(state.scriptDraft), /处理全部剧本冲突/);
  assert.equal(updates, 1, 'unresolved conflicts must never be submitted');
  workspace.destroy();
}

async function main() {
  testOpenApiContract();
  testCharacterReferenceUsesDurableProjectEndpoint();
  await testCharacterReferencePersistsEditedPromptsBeforeGeneration();
  await testCharacterReferenceLocksMountedFormWhileSaving();
  await testCharacterDraftsRecoverAcrossRevisionConflict();
  await testCharacterRenameConflictKeepsWorkspaceDrafts();
  testScriptThreeWayMergeRules();
  testScriptOrderedInsertionsAndReorderConflicts();
  testScriptTokenReceiptReconciliation();
  await testLostScriptSaveResponseRetriesWithOfficialId();
  await testScriptDraftAutoMergesNonOverlappingRevisionConflict();
  await testScriptDraftConflictBlocksSilentOverwrite();
  testPlanningFormatErrorsUseActionableMessage();
  testCanvasIntegration();
  testNodePersistenceHelpers();
  await testCreateProjectCoordinatorIsBoardScoped();
  await testCreateProjectCoordinatorPreservesConflictingLink();
  await testCreateProjectCoordinatorScopeCleanup();
  await testPureHelpers();
  await testProjectRoutesAndPlanningFlow();
  await testAvatarCandidateClientUsesCompatibilityFallback();
  await testPaidPlanningRecoveryReusesJobWithoutAnotherCopyPost();
  await testPlanningErrorsPropagateWithoutApplying();
  await testPlanningQuoteFailureDoesNotSubmit();
  await testTerminalJobFailureDoesNotApplyPlan();
  testMissingPollFailsClearly();
  await testWorkspaceSourceAndRenderContract();
  await testScriptSaveIgnoresDialogueActionControls();
  testScriptValidationReportsTheExactBrokenDialogue();
  await testBrowserGlobalProductionModuleFallbacks();
  await testProductionWorkspaceCanReturnToPhaseOneReview();
  testWorkspacePureStateAndPayloadHelpers();
  await testWorkspaceSavesUseExactRevisionedBodiesAndSummaries();
  await testConfirmSavesChangedSectionThenUsesReturnedRevisionAndSkipsUnchangedScriptSave();
  await testStoryboardConfirmRejectsServerPromptDriftBeforeAdvancing();
  await testHistoricalScriptVersionCannotBeConfirmedOrResavedAsLatest();
  await testScriptSaveSelectsReturnedLatestVersion();
  await testWorkspaceLoadRecoveryOwnerIsolationAndDestroy();
  await testWorkspaceLatestLoadWinsAndStaleFailuresAreIgnored();
  await testProductionModuleCanBeInstalledAfterInitialLoadFailure();
  await testCollaborationRoleDowngradeDestroysEditableWorkspaceOnly();
  await testWorkspaceLocksSettingsAndRejectsConcurrentPaidPlanning();
  await testWorkspaceOrderConflictReadonlyAndPlanning();
  await testNoChargeFortyFiveSecondControllerAcceptance();
  await testWorkspaceDeletesProjectWithRevision();
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
