const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

global.window = {};
global.document = {
  createElement() { return {}; },
  head: { appendChild() {} },
};
vm.runInThisContext(fs.readFileSync('site/workbench/image-mentions.js', 'utf8'));

const mentions = window.HQImageMentions;
assert.deepEqual(mentions.trigger('描述@', 3), { start: 2, end: 3 });
assert.equal(mentions.serialize({ childNodes: [{ nodeType: 1, tagName: 'BR' }] }), '');
assert.deepEqual(
  mentions.move('A@图片1B@图片2C', 1, 5, 11),
  { value: 'AB@图片2C@图片1', cursor: 11 },
);
assert.deepEqual(
  mentions.move('A@图片1B@图片2C', 6, 10, 1),
  { value: 'A@图片2@图片1BC', cursor: 5 },
);
assert.match(fs.readFileSync('site/workbench/image-mentions.js', 'utf8'), /is-selected/);
assert.match(fs.readFileSync('site/workbench/image-mentions.js', 'utf8'), /is-removing/);
assert.match(fs.readFileSync('site/workbench/image-mentions.js', 'utf8'), /mousemove/);
