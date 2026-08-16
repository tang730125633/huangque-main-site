const assert = require('node:assert/strict');
const graph = require('../site/workbench/canvas/canvas-graph.js');

const nodes = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
const chain = [
  { from: { node: 'a' }, to: { node: 'b' } },
  { from: { node: 'b' }, to: { node: 'c' } },
];
assert.deepEqual(graph.detectCycle(nodes, chain), []);
assert.deepEqual(graph.topologicalOrder(nodes, chain), ['a', 'b', 'c']);
assert.deepEqual(graph.detectCycle(nodes, chain.concat({ from: { node: 'c' }, to: { node: 'a' } })), ['a', 'b', 'c']);
assert.deepEqual(graph.computeAutoLayout(nodes, chain), {
  a: { x: 60, y: 60 }, b: { x: 370, y: 60 }, c: { x: 680, y: 60 },
});
assert.deepEqual(graph.contentBounds([{ id: 'a', x: 100, y: 100, width: 250, height: 160 }]), {
  x: 40, y: 40, w: 370, h: 280,
});
assert.deepEqual(graph.resizeNodeRect(
  { x: 100, y: 100, width: 250, height: 160 }, 'nw', -40, -30,
  { minWidth: 220, maxWidth: 520, minHeight: 80, maxHeight: 720 },
), { x: 60, y: 70, width: 290, height: 190 });
assert.deepEqual(graph.resizeNodeRect(
  { x: 100, y: 100, width: 250, height: 160 }, 'se', -100, -100,
  { minWidth: 220, maxWidth: 520, minHeight: 80, maxHeight: 720 },
), { x: 100, y: 100, width: 220, height: 80 });
assert.deepEqual(graph.alignmentGuides([
  { id: 'a', x: 100, y: 100, width: 250, height: 160 },
  { id: 'b', x: 400, y: 102, width: 250, height: 156 },
], ['a'], 6), { x: null, y: 180 });
assert.deepEqual(nodes, [{ id: 'a' }, { id: 'b' }, { id: 'c' }], 'inputs must not be mutated');
console.log('canvas graph: pass');
