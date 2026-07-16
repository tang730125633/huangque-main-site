const assert = require('node:assert/strict');
const exporter = require('../site/workbench/canvas/canvas-export.js');

function testTemplateRoundTrip() {
  const snapshot = { nodes: [{ id: 'n1', type: 'text' }], edges: [] };
  const text = exporter.serializeTemplate({ name: '演示模板', data: snapshot }, () => 1234);
  assert.deepEqual(JSON.parse(text), {
    version: 1,
    name: '演示模板',
    createdAt: 1234,
    data: snapshot,
  });
  assert.deepEqual(exporter.parseTemplate(text), { name: '演示模板', data: snapshot });
}

function testLegacyTemplateAndValidation() {
  const legacy = { nodes: [{ id: 'legacy' }], edges: [] };
  assert.deepEqual(exporter.parseTemplate(JSON.stringify(legacy), '旧模板'), {
    name: '旧模板',
    data: legacy,
  });
  const longName = '四'.repeat(50);
  assert.equal(exporter.parseTemplate(JSON.stringify({ name: longName, data: legacy })).name.length, 40);
  assert.throws(() => exporter.parseTemplate('{"edges":[]}'), /模板格式不正确/);
}

function testFilenameAndWrappedLines() {
  assert.equal(exporter.safeFilename('a\\b/c:d*e?f"g<h>i|j'), 'a-b-c-d-e-f-g-h-i-j');
  assert.deepEqual(exporter.wrappedLines((value) => value.length, '甲乙丙丁', 2, 1), ['甲乙']);
  assert.deepEqual(exporter.wrappedLines((value) => value.length, '甲乙\n丙丁', 3, 2), ['甲乙', '丙丁']);
}

function testNodeImageSource() {
  assert.equal(exporter.nodeImageSource({ type: 'image', image: 'direct.png', outputs: { image: 'output.png' } }), 'direct.png');
  assert.equal(exporter.nodeImageSource({ type: 'image', outputs: { image: 'output.png' } }), 'output.png');
  assert.equal(exporter.nodeImageSource({ type: 'gen', outputs: { image: 'generated.png' } }), 'generated.png');
  assert.equal(exporter.nodeImageSource({ type: 'text', outputs: { image: 'ignored.png' } }), '');
}

function fakeContext() {
  const calls = [];
  const context = { calls };
  for (const name of ['beginPath', 'moveTo', 'arcTo', 'closePath', 'save', 'clip', 'drawImage', 'restore', 'fill', 'stroke', 'lineTo', 'arc', 'setLineDash', 'fillText', 'fillRect', 'scale', 'translate', 'bezierCurveTo']) {
    context[name] = (...args) => calls.push([name, ...args]);
  }
  context.measureText = (value) => ({ width: String(value).length * 7 });
  return context;
}

async function testExportJpegLoadsImagesAndCleansUrl() {
  const context = fakeContext();
  const blob = { type: 'image/jpeg' };
  let quality;
  const canvas = {
    width: 0,
    height: 0,
    getContext: () => context,
    toBlob(callback, type, requestedQuality) {
      assert.equal(type, 'image/jpeg');
      quality = requestedQuality;
      callback(blob);
    },
  };
  const loaded = [];
  const downloads = [];
  const revoked = [];
  const result = await exporter.exportJpeg({
    bounds: { x: 10, y: 20, w: 300, h: 180 },
    nodes: [{ id: 'n1', type: 'image', x: 20, y: 30, width: 250, height: 160, collapsed: false, image: 'broken.png', params: {}, outputs: {} }],
    edges: [],
    theme: 'dark',
    portCenter: () => null,
    createCanvas: () => canvas,
    loadImage(src) { loaded.push(src); return Promise.reject(new Error('not available')); },
    createObjectURL(value) { assert.strictEqual(value, blob); return 'blob:download'; },
    revokeObjectURL(url) { revoked.push(url); },
    download(url, filename) { downloads.push({ url, filename }); },
    now: () => new Date('2026-07-16T08:09:10Z'),
  });

  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(loaded, ['broken.png'], 'image load failures resolve as null and do not abort export');
  assert.equal(quality, 0.92);
  assert.deepEqual(downloads, [{ url: 'blob:download', filename: 'canvas-preview-2026-07-16-08-09-10.jpg' }]);
  assert.deepEqual(revoked, ['blob:download']);
  assert.deepEqual(result, { filename: downloads[0].filename, blob });
  assert.ok(context.calls.some((call) => call[0] === 'fillRect'), 'background is drawn');
}

Promise.resolve()
  .then(testTemplateRoundTrip)
  .then(testLegacyTemplateAndValidation)
  .then(testFilenameAndWrappedLines)
  .then(testNodeImageSource)
  .then(testExportJpegLoadsImagesAndCleansUrl)
  .then(() => console.log('canvas export: pass'))
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
