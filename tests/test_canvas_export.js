const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const exporter = require('../site/workbench/canvas/canvas-export.js');
const apiModule = require('../site/workbench/canvas/canvas-api.js');

function blobResponse(options) {
  options = options || {};
  return {
    ok: options.ok === undefined ? true : options.ok,
    status: options.status === undefined ? 200 : options.status,
    blob: () => Promise.resolve(options.blob),
  };
}

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
  for (const property of ['fillStyle', 'strokeStyle', 'lineWidth', 'font', 'textBaseline', 'textAlign']) {
    Object.defineProperty(context, property, {
      set(value) { calls.push([`set:${property}`, value]); },
    });
  }
  return context;
}

function fakeCanvas(context, blob, qualityLog) {
  return {
    width: 0,
    height: 0,
    getContext: () => context,
    toBlob(callback, type, requestedQuality) {
      assert.equal(type, 'image/jpeg');
      qualityLog.push(requestedQuality);
      callback(blob);
    },
  };
}

function exportOptions(overrides) {
  const context = fakeContext();
  const blob = { type: 'image/jpeg' };
  const quality = [];
  const canvas = fakeCanvas(context, blob, quality);
  const loaded = [];
  const downloads = [];
  const revoked = [];
  const scheduled = [];
  const options = {
    bounds: { x: 10, y: 20, w: 300, h: 180 },
    nodes: [{ id: 'n1', type: 'image', typeName: '素材', typeColor: '#46b4ff', x: 20, y: 30, width: 250, height: 160, collapsed: false, image: 'broken.png', params: {}, outputs: {} }],
    edges: [{ from: { x: 21, y: 31 }, to: { x: 201, y: 131 } }],
    theme: 'light',
    createCanvas: () => canvas,
    loadImage(src) { loaded.push(src); return Promise.reject(new Error('not available')); },
    createObjectURL(value) { assert.strictEqual(value, blob); return 'blob:download'; },
    revokeObjectURL(url) { revoked.push(url); },
    download(url, filename) { downloads.push({ url, filename }); },
    setTimeoutImpl(fn, delay) { scheduled.push({ fn, delay }); return scheduled.length; },
    now: () => new Date('2026-07-16T08:09:10Z'),
  };
  return { context, blob, quality, canvas, loaded, downloads, revoked, scheduled, options: Object.assign(options, overrides || {}) };
}

async function testExportJpegUsesExplicitGeometryAndDrawingConstants() {
  const fixture = exportOptions();
  const result = await exporter.exportJpeg(fixture.options);

  assert.deepEqual(fixture.loaded, ['broken.png'], 'image load failures resolve as null and do not abort export');
  assert.deepEqual(fixture.quality, [0.92]);
  assert.equal(fixture.canvas.width, 600);
  assert.equal(fixture.canvas.height, 360);
  assert.deepEqual(fixture.downloads, [{ url: 'blob:download', filename: 'canvas-preview-2026-07-16-08-09-10.jpg' }]);
  assert.deepEqual(fixture.revoked, [], 'download URL must remain valid until delayed cleanup runs');
  assert.equal(fixture.scheduled.length, 1);
  assert.equal(fixture.scheduled[0].delay, 1500);
  fixture.scheduled[0].fn();
  assert.deepEqual(fixture.revoked, ['blob:download']);
  assert.deepEqual(result, { filename: fixture.downloads[0].filename, blob: fixture.blob });
  assert.ok(fixture.context.calls.some((call) => JSON.stringify(call) === JSON.stringify(['scale', 2, 2])), 'pixel scaling is preserved');
  assert.ok(fixture.context.calls.some((call) => JSON.stringify(call) === JSON.stringify(['set:fillStyle', '#f5f8fc'])), 'light background palette is preserved');
  assert.ok(fixture.context.calls.some((call) => JSON.stringify(call) === JSON.stringify(['fillRect', 12, 12, 1, 1])), '24 pixel background grid is preserved');
  assert.ok(fixture.context.calls.some((call) => JSON.stringify(call) === JSON.stringify(['bezierCurveTo', 111, 31, 111, 131, 201, 131])), 'edge uses explicit endpoint geometry');
  assert.ok(fixture.context.calls.some((call) => call[0] === 'fillText' && call[1] === '素材'), 'node title is drawn from plain node data');
}

async function testExportJpegRejectsMissingContextAndBlob() {
  const missingContext = exportOptions({ createCanvas: () => ({ getContext: () => null }) });
  await assert.rejects(exporter.exportJpeg(missingContext.options), /canvas context unavailable/);

  const missingBlob = exportOptions();
  missingBlob.canvas.toBlob = (callback) => callback(null);
  await assert.rejects(exporter.exportJpeg(missingBlob.options), /canvas blob unavailable/);
}

async function testDownloadFailureStillCleansUrl() {
  const fixture = exportOptions({ download() { throw new Error('download blocked'); } });
  await assert.rejects(exporter.exportJpeg(fixture.options), /download blocked/);
  assert.deepEqual(fixture.revoked, []);
  assert.equal(fixture.scheduled.length, 1);
  assert.equal(fixture.scheduled[0].delay, 1500);
  fixture.scheduled[0].fn();
  assert.deepEqual(fixture.revoked, ['blob:download']);
}

async function testBlobImageUrlIsRevokedOnSynchronousImageErrors() {
  for (const createImage of [
    () => { throw new Error('constructor failed'); },
    () => ({ set src(value) { throw new Error(`src failed: ${value}`); } }),
  ]) {
    const revoked = [];
    const image = await exporter.loadExportImage('/asset.png', {
      fetchBlob: () => Promise.resolve({ bytes: 1 }),
      createObjectURL: () => 'blob:image',
      revokeObjectURL: (url) => revoked.push(url),
      createImage,
    });
    assert.equal(image, null);
    assert.deepEqual(revoked, ['blob:image']);
  }
}

async function testExportImageUsesProtectedAndPublicAssetPolicies() {
  const calls = [];
  const revoked = [];
  const client = apiModule.createClient({
    fetchImpl(url, options) {
      calls.push({ url, options });
      return Promise.resolve(blobResponse({ blob: { url } }));
    },
    tokenProvider: () => '__cookie__',
  });
  const common = {
    fetchBlob: (url) => client.asset(url),
    createObjectURL: (blob) => `blob:${blob.url}`,
    revokeObjectURL: (url) => revoked.push(url),
    createImage: () => ({ set src(value) { this.onload(); } }),
  };

  await exporter.loadExportImage('/api/gen/file/protected.png', common);
  await exporter.loadExportImage('https://cdn.example.com/public.png', common);

  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer __cookie__');
  assert.equal(calls[0].options.headers.Accept, 'application/json');
  assert.equal(calls[1].options.credentials, 'include');
  assert.deepEqual(calls[1].options.headers, {});
  assert.deepEqual(revoked, [
    'blob:/api/gen/file/protected.png',
    'blob:https://cdn.example.com/public.png',
  ]);
}

async function testExportImageSwallowsExternalHttpErrorsWithoutLeakingUrls() {
  let objectUrlCalls = 0;
  const client = apiModule.createClient({
    fetchImpl: () => Promise.resolve(blobResponse({ ok: false, status: 404 })),
  });
  const image = await exporter.loadExportImage('https://cdn.example.com/missing.png', {
    fetchBlob: (url) => client.asset(url),
    createObjectURL() { objectUrlCalls += 1; return 'blob:unexpected'; },
    revokeObjectURL() { throw new Error('nothing should be revoked'); },
    createImage: () => ({}),
  });

  assert.equal(image, null);
  assert.equal(objectUrlCalls, 0);
}

function testModuleHasNoDomAccess() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'site', 'workbench', 'canvas', 'canvas-export.js'), 'utf8');
  assert.doesNotMatch(source, /\b(?:document|window)\b/);
  assert.doesNotMatch(source, /portCenter/);
}

Promise.resolve()
  .then(testTemplateRoundTrip)
  .then(testLegacyTemplateAndValidation)
  .then(testFilenameAndWrappedLines)
  .then(testNodeImageSource)
  .then(testExportJpegUsesExplicitGeometryAndDrawingConstants)
  .then(testExportJpegRejectsMissingContextAndBlob)
  .then(testDownloadFailureStillCleansUrl)
  .then(testBlobImageUrlIsRevokedOnSynchronousImageErrors)
  .then(testExportImageUsesProtectedAndPublicAssetPolicies)
  .then(testExportImageSwallowsExternalHttpErrorsWithoutLeakingUrls)
  .then(testModuleHasNoDomAccess)
  .then(() => console.log('canvas export: pass'))
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
