const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const completion = require(
  '../site/workbench/canvas/canvas-short-drama-completion.js',
);
const apiModule = require('../site/workbench/canvas/canvas-short-drama-api.js');


function readiness(overrides = {}) {
  return Object.assign({
    project_id: 'project-d6',
    revision: 21,
    stage: 'assembly_review',
    feature_enabled: true,
    ready: true,
    blockers: [],
    delivery_hash: 'a'.repeat(64),
    final_version: { id: 'final-version-4', version: 4 },
    asset: { id: 'final-asset-4' },
    billing: { spent_points: 36, reserved_points: 0 },
  }, overrides);
}


function testReadinessAndAcknowledgementRender() {
  const report = readiness();
  let html = completion.render(
    report,
    { title: '夜航', ratio: '9:16', target_duration: 30 },
    {},
    true,
    false,
  );
  assert.match(html, /交付门禁已通过/);
  assert.match(html, /确认完成并锁定项目/);
  assert.doesNotMatch(html, /role="dialog"/);

  html = completion.render(
    report,
    { title: '夜航', ratio: '9:16', target_duration: 30 },
    { completionDialog: true, completionAcknowledged: false },
    true,
    false,
  );
  assert.match(html, /role="dialog"/);
  assert.match(html, /aria-checked="false"/);
  assert.match(html, /data-action="submit-completion" disabled/);
  assert.match(html, /36 点/);
  assert.match(html, /final-asset-4/);

  html = completion.render(
    report,
    { title: '夜航', ratio: '9:16', target_duration: 30 },
    { completionDialog: true, completionAcknowledged: true },
    true,
    false,
  );
  assert.match(html, /aria-checked="true"/);
  assert.doesNotMatch(html, /data-action="submit-completion" disabled/);
}


function testBlockersAndCompletedSummary() {
  const blocked = completion.render(
    readiness({
      ready: false,
      blockers: [{
        code: 'billing_unsettled',
        domain: 'billing',
        entity_id: 'project-d6',
        message: '点数尚未结清',
        recommended_action: '等待退款恢复',
      }],
    }),
    {},
    {},
    true,
    false,
  );
  assert.match(blocked, /data-code="billing_unsettled"/);
  assert.match(blocked, /等待退款恢复/);
  assert.match(blocked, /data-action="open-completion" disabled/);

  const completed = completion.render({
    completion: {
      completion_id: 'completion-1',
      asset_id: 'asset-1',
      completed_by: 'alice',
      completed_at: 1720000000,
    },
  }, {}, {}, false, false);
  assert.match(completed, /项目已确认交付/);
  assert.match(completed, /completion-1/);
  assert.match(completed, /完成后永久只读/);
  assert.doesNotMatch(completed, /open-completion/);
}


function testRequestUsesServerIssuedDeliveryIdentity() {
  assert.deepEqual(completion.request(readiness()), {
    project_id: 'project-d6',
    revision: 21,
    final_version_id: 'final-version-4',
    asset_id: 'final-asset-4',
    delivery_hash: 'a'.repeat(64),
    acknowledged: true,
  });
}


async function testCompletionApiUsesBoardScopeAndStableKey() {
  const calls = [];
  const api = apiModule.createApi({
    boardId: 'board-d6',
    client: {
      json(url, options) {
        calls.push({ url, options });
        return Promise.resolve({ completion_id: 'completion-1' });
      },
    },
  });
  await api.completionReadiness('project-d6');
  await api.confirmCompletion(
    completion.request(readiness()),
    'd6-stable-key',
  );
  await api.completion('project-d6');
  assert.equal(
    calls[0].url,
    '/api/gen/short-drama/completion/readiness?project_id=project-d6',
  );
  assert.equal(calls[1].url, '/api/gen/short-drama/completion/confirm');
  assert.equal(calls[1].options.headers['Idempotency-Key'], 'd6-stable-key');
  assert.equal(calls[1].options.headers['X-Canvas-Board-Id'], 'board-d6');
  assert.equal(
    calls[2].url,
    '/api/gen/short-drama/completion?project_id=project-d6',
  );
  api.destroy();
}


function testWorkspaceAndAssetsAreIntegrated() {
  const workspace = fs.readFileSync(
    path.join(
      __dirname,
      '../site/workbench/canvas/canvas-short-drama-workspace.js',
    ),
    'utf8',
  );
  const html = fs.readFileSync(
    path.join(__dirname, '../site/workbench/canvas.html'),
    'utf8',
  );
  assert.match(workspace, /completionDialog:true/);
  assert.match(workspace, /completionAcknowledged:false/);
  assert.match(workspace, /api\.confirmCompletion/);
  assert.match(workspace, /pendingKeys\.completion/);
  assert.match(
    html,
    /canvas-short-drama-completion\.js\?v=[0-9a-f]{8}/,
  );
  assert.ok(
    html.indexOf('canvas-short-drama-completion.js') <
      html.indexOf('canvas-short-drama-workspace.js'),
  );
}


async function main() {
  testReadinessAndAcknowledgementRender();
  testBlockersAndCompletedSummary();
  testRequestUsesServerIssuedDeliveryIdentity();
  await testCompletionApiUsesBoardScopeAndStableKey();
  testWorkspaceAndAssetsAreIntegrated();
  console.log('short drama D-6 completion canvas tests passed');
}


main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
