const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const voice = require('../site/workbench/canvas/canvas-short-drama-voice.js');

function snapshot(overrides = {}) {
  return Object.assign({
    project_id: 'project-1', revision: 8, stage: 'voice_review',
    ratio: '9:16', target_duration: 30,
    point_budget: 100, spent_points: 12, reserved_points: 0,
    shots: [
      {
        id: 'shot-1', shot_key: '第一镜', sort_order: 0, duration: 5,
        locked: false, timeline_revision: 1, status: 'pending',
        lines: [{
          id: 'voice-1', dialogue_line_id: 'line-1', line_type: 'dialogue',
          sort_order: 0, character_key: 'detective',
          character_name: '林<script>探长', source_text: '谁在那里？',
          speech_text: '谁在那里？', subtitle_text: '<b>谁在那里？</b>',
          subtitle_visible: true, voice_key: 'longwan',
          speed: 1.2, pitch: 1, volume: 4,
          current_version: null, start_ms: null, end_ms: null,
          versions: [], job: null,
        }],
      },
      {
        id: 'shot-2', shot_key: '第二镜', sort_order: 1, duration: 5,
        locked: false, timeline_revision: 1, status: 'silent', lines: [],
      },
    ],
  }, overrides);
}

const voices = [
  { voice_key: 'longwan', display_name: '龙婉', preview_url: '/voice.mp3' },
  { voice_key: 'longcheng', display_name: '龙城', preview_url: '' },
];

async function testNormalizeAndRender() {
  assert.deepEqual(
    Object.keys(voice).sort(),
    ['createWorkspace', 'normalizeState', 'renderWorkspace']
  );
  const normalized = voice.normalizeState(snapshot(), voices, {});
  assert.equal(normalized.selectedShotId, 'shot-1');
  assert.equal(normalized.shots[0].lines[0].voice_name, '龙婉');
  const html = voice.renderWorkspace(snapshot(), { voices });
  assert.match(html, /镜头列表[\s\S]*台词与字幕[\s\S]*配音控制台/);
  assert.match(html, /第一镜[\s\S]*第二镜/);
  assert.match(html, /龙婉/);
  assert.match(html, /谁在那里？/);
  assert.doesNotMatch(html, /<script>|<b>/);
  assert.match(html, /林&lt;script&gt;探长/);
  assert.match(html, /&lt;b&gt;谁在那里？&lt;\/b&gt;/);
  assert.match(html, /data-action="generate-line"[^>]*disabled/);
  assert.match(html, /data-action="save-timeline"[^>]*disabled/);
}

async function testWorkspaceLoadsBothResourcesAndDestroysCleanly() {
  const calls = [];
  const client = {
    json(route) {
      calls.push(route);
      if (route.startsWith('/api/gen/short-drama/voice?')) {
        return Promise.resolve(snapshot());
      }
      if (route === '/api/gen/audio/voices') {
        return Promise.resolve({ items: voices });
      }
      throw new Error(`unexpected route ${route}`);
    },
  };
  const workspace = voice.createWorkspace({
    projectId: 'project-1', client, document: null,
  });
  await workspace.ready;
  assert.deepEqual(calls, [
    '/api/gen/short-drama/voice?project_id=project-1',
    '/api/gen/audio/voices',
  ]);
  assert.match(workspace.render(), /龙婉/);
  assert.equal(workspace.selectShot('shot-2'), true);
  assert.match(workspace.render(), /当前镜头没有台词/);
  assert.equal(workspace.selectShot('missing'), false);
  workspace.destroy();
  assert.equal(await workspace.reload(), null);
}

async function main() {
  await testNormalizeAndRender();
  await testWorkspaceLoadsBothResourcesAndDestroysCleanly();
  const css = fs.readFileSync(path.join(
    __dirname, '../site/workbench/canvas/canvas-short-drama-voice.css'
  ), 'utf8');
  assert.match(css, /grid-template-columns:\s*260px\s+minmax\(0,\s*1fr\)\s+300px/);
  console.log('canvas short drama voice: pass');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
