const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const canvasApi = require('../site/workbench/canvas/canvas-api.js');
const video = require('../site/workbench/canvas/canvas-short-drama-video.js');

function snapshot(overrides = {}) {
  return Object.assign({
    project_id: 'project-c3', revision: 9, stage: 'video_review', ratio: '9:16',
    point_budget: 200, spent_points: 40, reserved_points: 10,
    unlocked_shot_count: 1, handoff_blocked: true, handoff_blockers: [],
    cast_characters: [{
      character_key: 'detective', name: '侦探', reference_url: '/detective.png',
      avatar_id: 12, avatar_name: '侦探化身', binding_source: 'video_cast',
      valid: true, blocker: null, shot_count: 1,
    }],
    shots: [{
      id: 'shot-1', shot_key: '第一镜', sort_order: 0, duration: 5,
      video_prompt: '<夜景>', video_revision: 2, current_version: 1,
      locked: false, status: 'done', lockable: true, lock_blockers: [],
      avatar_ids: ['12'], voice_tracks: [{
        start_ms: 0, end_ms: 1500, subtitle_text: '<开场>', subtitle_visible: true,
      }],
      versions: [{
        version: 1, status: 'done', url: '/api/gen/file/movie.mp4',
        duration_ms: 5000, ratio: '9:16', prompt: '<夜景>', cost: 20,
      }],
      job: null,
    }],
  }, overrides);
}

function testNormalizeAndRender() {
  const state = video.normalizeState(snapshot(), {});
  assert.equal(state.shots[0].current.version, 1);
  assert.equal(state.shots[0].voice_tracks.length, 1);
  const html = video.renderWorkspace(snapshot(), {});
  assert.match(html, /C-3 镜头队列/);
  assert.match(html, /data-video-player/);
  assert.match(html, /<video controls playsinline muted data-video-player/);
  assert.match(html, /&lt;夜景&gt;/);
  assert.match(html, /&lt;开场&gt;/);
  assert.doesNotMatch(html, /<夜景>/);
  assert.match(html, /角色选角/);
  assert.match(html, /侦探化身/);
  assert.match(html, /保存角色绑定/);
  assert.match(html, /data-reference-image/);
  const fileOnlyHtml = video.renderWorkspace(snapshot({
    cast_characters: [{
      ...snapshot().cast_characters[0],
      reference_url: '',
      reference_file: 'server-only/avatar.jpg',
    }],
  }), {});
  assert.doesNotMatch(fileOnlyHtml, /server-only\/avatar\.jpg/);
  assert.match(fileOnlyHtml, /nc-sdv-cast-reference"><span>/);
  const publicCandidateState = video.normalizeState(snapshot(), {
    avatars: [{ id: 12, name: '公开候选契约', status: 'ready' }],
  });
  assert.equal(publicCandidateState.avatars.length, 1);
  assert.equal(publicCandidateState.avatars[0].name, '公开候选契约');
}

function testCanvasAssets() {
  const root = path.join(__dirname, '..', 'site', 'workbench');
  const html = fs.readFileSync(path.join(root, 'canvas.html'), 'utf8');
  const videoHtml = fs.readFileSync(path.join(root, 'video.html'), 'utf8');
  for (const asset of [
    'canvas/canvas-short-drama-video.js',
    'canvas/canvas-short-drama-video.css',
  ]) {
    const source = fs.readFileSync(path.join(root, asset), 'utf8').replace(/\r\n/g, '\n');
    const stamp = crypto.createHash('md5').update(source).digest('hex').slice(0, 8);
    assert.ok(html.includes(`${asset}?v=${stamp}`), `${asset} must use its MD5 stamp`);
  }
  assert.match(videoHtml, /q\.get\('function'\)!=='cinematic'/);
  assert.match(videoHtml, /q\.get\('action'\)!=='create-avatar'/);
  assert.match(videoHtml, /updateFunction\('cinematic'\)/);
  assert.match(videoHtml, /cineNewAvatarDrop/);
}

async function testReloadAndDestroy() {
  const listeners = {};
  const host = {
    innerHTML: '',
    addEventListener(type, callback) { listeners[type] = callback; },
    removeEventListener(type) { delete listeners[type]; },
  };
  const calls = [];
  const workspace = video.createWorkspace({
    projectId: 'project-c3', host,
    client: {
      json(path, options) {
        calls.push({ path, options });
        if (path === '/api/gen/short-drama/video-cast/avatars?project_id=project-c3') {
          return Promise.resolve({ items: [{
            id: 12, name: '侦探化身', image_url: '/avatar-12.png',
            status: 'ready',
          }], can_create_avatar: true });
        }
        return Promise.resolve(snapshot());
      },
    },
  });
  await workspace.ready;
  assert.equal(calls[0].path, '/api/gen/short-drama/video?project_id=project-c3');
  assert.equal(calls[1].path, '/api/gen/short-drama/video-cast/avatars?project_id=project-c3');
  assert.match(host.innerHTML, /电影化身视频/);
  assert.match(host.innerHTML, /侦探化身/);
  workspace.destroy();
  assert.equal(Object.keys(listeners).length, 0);
}

async function testSaveBindingsUsesRevisionAndBoardScope() {
  const listeners = {};
  const host = {
    innerHTML: '',
    addEventListener(type, callback) { listeners[type] = callback; },
    removeEventListener(type) { delete listeners[type]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  const calls = [];
  const workspace = video.createWorkspace({
    projectId: 'project-c3', boardId: 'board-7', host,
    client: {
      json(path, options) {
        calls.push({ path, options });
        if (path === '/api/gen/short-drama/video-cast/avatars?project_id=project-c3') {
          return Promise.resolve({ items: [{
            id: 12, name: '侦探化身', image_url: '/avatar-12.png',
            status: 'ready',
          }, {
            id: 13, name: '备用化身', image_url: '/avatar-13.png',
            status: 'ready',
          }], can_create_avatar: true });
        }
        return Promise.resolve(snapshot());
      },
    },
  });
  await workspace.ready;
  const candidates = calls.find((item) => item.path.includes('/video-cast/avatars?'));
  assert.equal(candidates.options.headers['X-Canvas-Board-Id'], 'board-7');
  workspace.setCastSelection('detective', 13);
  await workspace.saveCast();
  const save = calls.find((item) => item.path === '/api/gen/short-drama/video-cast');
  assert.ok(save);
  assert.equal(save.options.headers['X-Canvas-Board-Id'], 'board-7');
  assert.deepEqual(save.options.body, {
    project_id: 'project-c3', revision: 9,
    bindings: [{ character_key: 'detective', avatar_id: 13 }],
  });
  workspace.destroy();
}

async function testBindingsAreSerializedExactlyOnceByTheRealClient() {
  const host = {
    innerHTML: '',
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  const requests = [];
  const client = canvasApi.createClient({
    tokenProvider() { return 'token'; },
    fetchImpl(path, options) {
      requests.push({ path, options });
      const payload = path.includes('/video-cast/avatars?')
        ? { items: [{ id: 13, name: '备用化身', status: 'ready' }], can_create_avatar: true }
        : snapshot();
      return Promise.resolve({
        ok: true,
        status: 200,
        text() { return Promise.resolve(JSON.stringify(payload)); },
      });
    },
  });
  const workspace = video.createWorkspace({
    projectId: 'project-c3', boardId: 'board-7', host, client,
  });
  await workspace.ready;
  workspace.setCastSelection('detective', 13);
  await workspace.saveCast();
  const request = requests.find((item) => (
    item.path === '/api/gen/short-drama/video-cast'
  ));
  assert.ok(request);
  const wireBody = JSON.parse(request.options.body);
  assert.equal(typeof wireBody, 'object');
  assert.deepEqual(wireBody, {
    project_id: 'project-c3', revision: 9,
    bindings: [{ character_key: 'detective', avatar_id: 13 }],
  });
  workspace.destroy();
}

async function testPollingPreservesDirtyCastAndDetectsConflict() {
  const listeners = {};
  const host = {
    innerHTML: '',
    addEventListener(type, callback) { listeners[type] = callback; },
    removeEventListener(type) { delete listeners[type]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  let remote = snapshot();
  const calls = [];
  const workspace = video.createWorkspace({
    projectId: 'project-c3', boardId: 'board-7', host,
    client: {
      json(path, options) {
        calls.push({ path, options });
        if (path.includes('/video-cast/avatars?')) {
          return Promise.resolve({
            items: [
              { id: 12, name: 'A', status: 'ready' },
              { id: 13, name: 'B', status: 'ready' },
              { id: 14, name: 'C', status: 'ready' },
            ],
            can_create_avatar: false,
          });
        }
        if (path === '/api/gen/short-drama/video-cast') return Promise.resolve(remote);
        return Promise.resolve(remote);
      },
    },
  });
  await workspace.ready;
  workspace.setCastSelection('detective', 13);

  remote = snapshot({ revision: 10 });
  await workspace.reload({ quiet: true });
  assert.equal(workspace.getState().castSelections.detective, 13);
  assert.equal(workspace.getState().castDirty, true);
  assert.deepEqual(workspace.getState().castConflicts, {});

  remote = snapshot({
    revision: 11,
    cast_characters: [{
      ...snapshot().cast_characters[0],
      avatar_id: 14, avatar_name: 'C',
    }],
  });
  await workspace.reload({ quiet: true });
  assert.equal(workspace.getState().castSelections.detective, 13);
  assert.equal(workspace.getState().castConflicts.detective, true);
  assert.match(host.innerHTML, /采用最新绑定/);

  workspace.keepLocalCast();
  assert.deepEqual(workspace.getState().castConflicts, {});
  await workspace.saveCast();
  const save = calls.filter((item) => item.path === '/api/gen/short-drama/video-cast').pop();
  assert.equal(save.options.body.revision, 11);

  workspace.setCastSelection('detective', 13);
  remote = snapshot({
    revision: 12,
    cast_characters: [{
      ...snapshot().cast_characters[0],
      avatar_id: 14, avatar_name: 'C',
    }],
  });
  await workspace.reload({ quiet: true });
  workspace.reloadCast();
  assert.equal(workspace.getState().castSelections.detective, 14);
  assert.equal(workspace.getState().castDirty, false);
  workspace.destroy();
}

async function testCreateAvatarUsesSupportedDirectAction() {
  const listeners = {};
  const host = {
    innerHTML: '',
    addEventListener(type, callback) { listeners[type] = callback; },
    removeEventListener(type) { delete listeners[type]; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  let opened = '';
  const workspace = video.createWorkspace({
    projectId: 'project-c3', host,
    openAvatarCreator(url) { opened = url; },
    client: {
      json(path) {
        if (path.includes('/video-cast/avatars?')) {
          return Promise.resolve({ items: [], can_create_avatar: true });
        }
        return Promise.resolve(snapshot());
      },
    },
  });
  await workspace.ready;
  const target = {
    parentNode: host,
    getAttribute(name) { return name === 'data-action' ? 'create-avatar' : null; },
  };
  listeners.click({ target });
  assert.equal(opened, '/workbench/video.html?function=cinematic&action=create-avatar');
  workspace.destroy();
}

async function testViewerDoesNotEnumerateOwnerAvatarLibrary() {
  const host = {
    innerHTML: '',
    addEventListener() {},
    removeEventListener() {},
  };
  const calls = [];
  const workspace = video.createWorkspace({
    projectId: 'project-c3', canEdit: false, host,
    client: {
      json(path) {
        calls.push(path);
        return Promise.resolve(snapshot());
      },
    },
  });
  await workspace.ready;
  assert.deepEqual(calls, ['/api/gen/short-drama/video?project_id=project-c3']);
  assert.match(host.innerHTML, /data-action="refresh-avatars" disabled/);
  assert.match(host.innerHTML, /data-action="create-avatar" disabled/);
  workspace.destroy();
}

(async function main() {
  testNormalizeAndRender();
  testCanvasAssets();
  await testReloadAndDestroy();
  await testSaveBindingsUsesRevisionAndBoardScope();
  await testBindingsAreSerializedExactlyOnceByTheRealClient();
  await testPollingPreservesDirtyCastAndDetectsConflict();
  await testCreateAvatarUsesSupportedDirectAction();
  await testViewerDoesNotEnumerateOwnerAvatarLibrary();
  console.log('canvas short drama video: pass');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
