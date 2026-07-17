const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const storageModule = require('../site/workbench/canvas/canvas-storage.js');

function createFakeStorage() {
  const values = new Map();
  const calls = [];
  return {
    calls,
    values,
    getItem(key) {
      calls.push(['getItem', key]);
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      calls.push(['setItem', key, value]);
      values.set(key, String(value));
    },
    removeItem(key) {
      calls.push(['removeItem', key]);
      values.delete(key);
    },
  };
}

assert.deepEqual(storageModule.DEFAULT_KEYS, {
  draft: 'hq_canvas_draft_v2',
  templates: 'hq_canvas_templates_v2',
  boards: 'hq_canvas_boards_v1',
  activeBoard: 'hq_canvas_active_id',
});

{
  const fake = createFakeStorage();
  const storage = storageModule.createStorage({ storage: fake });
  const draft = { nodes: [{ id: 'n1', params: { text: 'hello' } }], edges: [] };

  assert.deepEqual(storage.saveDraft(draft), { ok: true, value: draft });
  assert.equal(fake.calls[0][1], 'hq_canvas_draft_v2');
  const loaded = storage.loadDraft();
  assert.deepEqual(loaded, { ok: true, value: draft });
  assert.notStrictEqual(loaded.value, draft, 'loaded drafts must be defensive copies');
  loaded.value.nodes[0].params.text = 'changed';
  assert.equal(storage.loadDraft().value.nodes[0].params.text, 'hello');

  assert.deepEqual(storage.removeDraft(), { ok: true, value: null });
  assert.deepEqual(storage.loadDraft(), { ok: true, value: null });
}

{
  const fake = createFakeStorage();
  fake.values.set('hq_canvas_draft_v2', '{broken');
  const loaded = storageModule.createStorage({ storage: fake }).loadDraft();
  assert.equal(loaded.ok, false);
  assert.equal(loaded.error.code, 'corrupt_json');
}

{
  const fake = createFakeStorage();
  const storage = storageModule.createStorage({ storage: fake });
  assert.deepEqual(storage.loadTemplates(), { ok: true, value: [] });
  assert.deepEqual(storage.loadBoards(), { ok: true, value: [] });
  assert.equal(fake.calls[0][1], 'hq_canvas_templates_v2');
  assert.equal(fake.calls[1][1], 'hq_canvas_boards_v1');

  const templates = [{ id: 't1' }];
  const boards = [{ id: 'b1', data: { nodes: [] } }];
  storage.saveTemplates(templates);
  storage.saveBoards(boards);
  const loadedTemplates = storage.loadTemplates();
  const loadedBoards = storage.loadBoards();
  assert.deepEqual(loadedTemplates.value, templates);
  assert.deepEqual(loadedBoards.value, boards);
  assert.notStrictEqual(loadedTemplates.value, templates);
  assert.notStrictEqual(loadedBoards.value, boards);
}

{
  const fake = createFakeStorage();
  const storage = storageModule.createStorage({ storage: fake });
  assert.deepEqual(storage.loadActiveBoard(), { ok: true, value: '' });
  assert.deepEqual(storage.saveActiveBoard('board-7'), { ok: true, value: 'board-7' });
  assert.equal(fake.values.get('hq_canvas_active_id'), 'board-7');
  assert.deepEqual(storage.loadActiveBoard(), { ok: true, value: 'board-7' });
  assert.deepEqual(storage.saveActiveBoard(''), { ok: true, value: '' });
  assert.equal(fake.values.has('hq_canvas_active_id'), false);
}

{
  const original = {
    nodes: [
      { id: 'gen', type: 'gen', outputs: { image: 'large-image', prompt: 'keep' } },
      { id: 'video', type: 'video', outputs: { video: 'large-video', video_url: 'large-url', prompt: 'keep' } },
      { id: 'image', type: 'image', outputs: { image: 'keep-image' } },
    ],
  };
  const stripped = storageModule.stripHeavyOutputs(original);
  assert.notStrictEqual(stripped, original);
  assert.equal(stripped.nodes[0].outputs.image, undefined);
  assert.equal(stripped.nodes[0].outputs.prompt, 'keep');
  assert.equal(stripped.nodes[1].outputs.video, undefined);
  assert.equal(stripped.nodes[1].outputs.video_url, undefined);
  assert.equal(stripped.nodes[1].outputs.prompt, 'keep');
  assert.equal(stripped.nodes[2].outputs.image, 'keep-image');
  assert.equal(original.nodes[0].outputs.image, 'large-image', 'stripping must not mutate the caller snapshot');
}

{
  const fake = createFakeStorage();
  fake.values.set('hq_canvas_draft_v2', JSON.stringify({ nodes: [{ id: 'old' }] }));
  let writeCalls = 0;
  fake.setItem = function setItem() {
    writeCalls += 1;
    const error = new Error('full');
    error.name = 'QuotaExceededError';
    throw error;
  };
  const saved = storageModule.createStorage({ storage: fake }).saveDraft({ nodes: [{ id: 'new' }] });
  assert.equal(saved.ok, false);
  assert.equal(saved.error.code, 'quota_exceeded');
  assert.equal(writeCalls, 1, 'a failed write must not be retried');
  assert.deepEqual(JSON.parse(fake.values.get('hq_canvas_draft_v2')), { nodes: [{ id: 'old' }] });
}

{
  const accessError = new Error('blocked by browser policy');
  accessError.name = 'SecurityError';
  let accesses = 0;
  let storage;
  assert.doesNotThrow(() => {
    storage = storageModule.createStorage({
      storage() {
        accesses += 1;
        throw accessError;
      },
    });
  }, 'creating the adapter must not eagerly access browser storage');
  assert.equal(accesses, 0);
  const loaded = storage.loadDraft();
  assert.equal(accesses, 1);
  assert.equal(loaded.ok, false);
  assert.equal(loaded.error.code, 'storage_unavailable');
  assert.equal(loaded.error.message, 'blocked by browser policy');

  const appSource = fs.readFileSync(path.join(__dirname, '..', 'site', 'workbench', 'canvas', 'canvas-app.js'), 'utf8');
  assert.match(appSource, /createStorage\(\{storage:function\(\)\{return window\.localStorage;\}\}\)/);
  assert.doesNotMatch(appSource, /createStorage\(\{storage:window\.localStorage\}\)/);
}

{
  const accessError = new Error('method access denied');
  accessError.name = 'SecurityError';
  const readStorage = {};
  Object.defineProperty(readStorage, 'getItem', { get() { throw accessError; } });
  const loaded = storageModule.createStorage({ storage: readStorage }).loadDraft();
  assert.equal(loaded.ok, false);
  assert.equal(loaded.error.code, 'storage_unavailable');

  const writeStorage = {};
  Object.defineProperty(writeStorage, 'setItem', { get() { throw accessError; } });
  const saved = storageModule.createStorage({ storage: writeStorage }).saveDraft({ nodes: [] });
  assert.equal(saved.ok, false);
  assert.equal(saved.error.code, 'storage_unavailable');
}

for (const code of [22, 1014]) {
  const fake = createFakeStorage();
  fake.setItem = function setItem() {
    const error = new Error(`quota code ${code}`);
    error.code = code;
    throw error;
  };
  const saved = storageModule.createStorage({ storage: fake }).saveDraft({ nodes: [] });
  assert.equal(saved.ok, false);
  assert.equal(saved.error.code, 'quota_exceeded');
}

{
  const fake = createFakeStorage();
  let writes = 0;
  fake.setItem = function setItem() { writes += 1; };
  const circular = { nodes: [] };
  circular.self = circular;
  const storage = storageModule.createStorage({ storage: fake });
  let saved;
  assert.doesNotThrow(() => { saved = storage.saveDraft(circular); });
  assert.equal(saved.ok, false);
  assert.equal(saved.error.code, 'serialization_failed');
  assert.equal(writes, 0, 'serialization failures must not attempt browser storage writes');
}

console.log('canvas storage: pass');
