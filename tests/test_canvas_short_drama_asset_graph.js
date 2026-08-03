const assert = require('assert');
const test = require('node:test');
const shortDrama = require('../site/workbench/canvas/canvas-short-drama.js');

test('storyboard exposes the asset graph controls and categories', () => {
  const project = {
    id: 'p1', revision: 4, stage: 'storyboard_review', title: '测试短剧',
    ratio: '16:9', target_duration: 30, shot_count: 6,
    characters: [], script_versions: [], shots: [{id: 's1', shot_key: 'shot_001'}],
    spent_points: 0, point_budget: 0,
  };
  const html = shortDrama.renderWorkspace(project, {
    activeStage: 'storyboard_review', canEdit: true, busy: false,
    assetGraph: {
      graph_revision: 2,
      entities: [{
        id: 'a1', name: '雨夜街道', asset_type: 'scene', current_version_id: null,
        versions: [{id: 'v1', version: 1, status: 'draft'}],
      }, {id: 'a2', name: '黑伞', asset_type: 'prop', current_version_id: null, versions: []}],
    },
  });
  assert.match(html, /短剧资产图谱/);
  assert.match(html, /场景 1/);
  assert.match(html, /data-action="sync-asset-graph"/);
  assert.match(html, /data-action="lock-asset-version"/);
  assert.match(html, /data-action="build-asset-snapshots"/);
  assert.match(html, /data-action="create-asset"/);
  assert.match(html, /资产名称，例如：黑色雨伞/);
  assert.match(html, /data-action="bind-asset"/);
});

test('asset graph client sends scoped mutation contracts', async () => {
  const calls = [];
  const apiClient = {
    json(path, options) {
      calls.push([path, options || {}]);
      return Promise.resolve({ok: true});
    },
    poll() { return Promise.resolve(null); },
  };
  const client = shortDrama.createClient(apiClient, apiClient.poll, 'board-1');
  await client.getAssetGraph('p1');
  await client.syncAssetGraph('p1', 3);
  await client.createAsset('p1', 4, {
    asset_key: 'prop:umbrella', asset_type: 'prop', name: '黑伞', description: '雨夜道具',
  });
  await client.bindAsset('p1', 5, {shot_id: 's1', entity_id: 'a2', relation_type: 'uses'});
  await client.lockAssetVersion('p1', 6, 'v1');
  await client.buildAssetSnapshot('p1', 7, 's1');
  assert.equal(calls[0][0], '/api/gen/short-drama/asset-graph?project_id=p1');
  assert.deepEqual(calls[1][1].body, {project_id: 'p1', graph_revision: 3});
  assert.equal(calls[1][1].headers['X-Canvas-Board-Id'], 'board-1');
  assert.deepEqual(calls[2][1].body, {
    project_id: 'p1', graph_revision: 4, asset_key: 'prop:umbrella',
    asset_type: 'prop', name: '黑伞', description: '雨夜道具',
  });
  assert.deepEqual(calls[3][1].body, {
    project_id: 'p1', graph_revision: 5, shot_id: 's1', entity_id: 'a2', relation_type: 'uses',
  });
  assert.deepEqual(calls[4][1].body, {
    project_id: 'p1', graph_revision: 6, version_id: 'v1',
  });
  assert.deepEqual(calls[5][1].body, {
    project_id: 'p1', graph_revision: 7, shot_id: 's1',
  });
});
