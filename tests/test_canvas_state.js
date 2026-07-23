const assert = require('node:assert/strict');
const state = require('../site/workbench/canvas/canvas-state.js');
const original = { nodes: [{ id: 'a', params: { text: 'one' } }], edges: [] };
const cloned = state.cloneSnapshot(original);
cloned.nodes[0].params.text = 'changed';
assert.equal(original.nodes[0].params.text, 'one');

const history = state.createHistory({ limit: 2 });
history.push({ value: 1 }); history.push({ value: 2 }); history.push({ value: 3 });
assert.equal(history.canUndo(), true);
assert.deepEqual(history.undo({ value: 4 }), { value: 3 });
assert.deepEqual(history.undo({ value: 3 }), { value: 2 });
assert.equal(history.canUndo(), false);
assert.deepEqual(history.redo({ value: 2 }), { value: 3 });
history.push({ value: 9 });
assert.equal(history.canRedo(), false, 'new edits clear redo');
history.clear();
assert.equal(history.canUndo(), false);
console.log('canvas state: pass');
