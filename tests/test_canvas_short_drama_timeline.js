const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const timeline = require('../site/workbench/canvas/canvas-short-drama-timeline.js');
const voice = require('../site/workbench/canvas/canvas-short-drama-voice.js');

function snapshot() {
  return {
    project_id: 'project-1', project_revision: 9, timeline_revision: 2,
    stage: 'voice_review', status: 'blocked', duration_ms: 5000,
    capabilities: { rebuild: true, save: true, confirm: false, view_history: true },
    characters: [
      { character_key: 'host', name: '主持人' },
      { character_key: 'guest', name: '嘉宾' },
    ],
    blockers: [],
    current_version: {
      id: 'timeline-2', version: 2, status: 'blocked',
      effective_status: 'blocked', timeline_hash: 'a'.repeat(64),
      blockers: [{
        code: 'timeline_missing_face_target',
        line_id: 'dialogue-1',
        segment_id: 'segment-1',
        message: '画面内说话必须绑定可见角色',
      }],
      segments: [{
        id: 'segment-1', shot_id: 'shot-1', line_id: 'dialogue-1',
        character_key: 'host', voice_asset_id: 'voice-1',
        start_ms: 100, end_ms: 1200, speaking_mode: 'visible',
        face_target: null,
      }],
      subtitle_cues: [],
    },
    versions: [{
      id: 'timeline-2', version: 2, status: 'blocked',
      effective_status: 'blocked', timeline_hash: 'a'.repeat(64),
    }],
  };
}

function testDraftAndChanges() {
  const source = snapshot();
  const draft = timeline.createDraft(source);
  timeline.updateDraft(draft, 'segment-1', 'face_target', 'host');
  assert.deepEqual(draft.segments[0].face_target, {
    type: 'character', value: 'host',
  });
  const changed = timeline.changes(source, draft);
  assert.equal(changed.length, 1);
  assert.equal(changed[0].id, 'segment-1');
  assert.deepEqual(changed[0].face_target, {
    type: 'character', value: 'host',
  });
}

function testModeClearsFaceTarget() {
  const source = snapshot();
  source.current_version.segments[0].face_target = {
    type: 'character', value: 'host',
  };
  const draft = timeline.createDraft(source);
  timeline.updateDraft(draft, 'segment-1', 'speaking_mode', 'offscreen');
  assert.equal(draft.segments[0].face_target, null);
}

function testRendererEscapesAndExposesActions() {
  const source = snapshot();
  source.characters[0].name = '<img src=x onerror=alert(1)>';
  const html = timeline.renderPanel(source, null, {
    busy: false, canEdit: true, conflictFrozen: false,
  });
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /data-action="rebuild-master-timeline"/);
  assert.match(html, /data-action="save-master-timeline"/);
  assert.match(html, /待补充/);
  assert.match(
    html,
    /dialogue-1 \/ &lt;img src=x onerror=alert\(1\)&gt;/
  );
  assert.match(html, /版本历史/);
}

function testReadonlySpeakerMigrationCanBeExplicitlyConfirmed() {
  const source = snapshot();
  source.stage = 'assembly_review';
  source.capabilities = {
    rebuild: false, save: false, confirm: true,
    confirm_speaker_migration: true, view_history: true,
  };
  source.current_version.blockers = [{
    code: 'timeline_speaker_identity_unverified',
    message: '历史时间轴缺少镜头角色快照，请核对迁移后的说话模式并确认',
  }];
  const html = timeline.renderPanel(source, null, {
    busy: false, canEdit: true, conflictFrozen: false,
  });
  assert.match(
    html,
    /data-action="confirm-master-timeline"[^>]*>确认角色映射迁移</,
  );
  assert.doesNotMatch(
    html,
    /data-action="confirm-master-timeline" disabled/,
  );
}

function testVoiceWorkspaceMountsTimelineAndGatesHandoff() {
  const input = {
    project_id: 'project-1', revision: 9, stage: 'voice_review',
    point_budget: 0, spent_points: 0, reserved_points: 0,
    shots: [], handoff_blocked: false, handoff_blockers: [],
    alignment: {
      handoff: { ready: true, required: true, blockers: [] },
      actions: {}, readiness: { ready: true, blockers: [] },
    },
    master_timeline: snapshot(),
  };
  const html = voice.renderWorkspace(input, { canEdit: true });
  assert.match(html, /PR-C 主时间轴/);
  assert.match(html, /data-action="confirm-voice-stage" disabled/);
  input.master_timeline.current_version.status = 'ready';
  input.master_timeline.current_version.effective_status = 'ready';
  input.master_timeline.status = 'ready';
  input.master_timeline.capabilities.confirm = false;
  const readyHtml = voice.renderWorkspace(input, { canEdit: true });
  assert.doesNotMatch(
    readyHtml,
    /data-action="confirm-voice-stage" disabled/
  );
}

function main() {
  testDraftAndChanges();
  testModeClearsFaceTarget();
  testRendererEscapesAndExposesActions();
  testReadonlySpeakerMigrationCanBeExplicitlyConfirmed();
  testVoiceWorkspaceMountsTimelineAndGatesHandoff();
  const css = fs.readFileSync(path.join(
    __dirname,
    '../site/workbench/canvas/canvas-short-drama-timeline.css'
  ), 'utf8');
  assert.match(css, /\.nc-sdt-segments/);
  console.log('canvas short drama timeline: pass');
}

main();
