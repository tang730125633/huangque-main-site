const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'site/workbench/banana.html'), 'utf8');

function test(name, fn) {
  try {
    fn();
    process.stdout.write(`PASS ${name}\n`);
  } catch (error) {
    process.stderr.write(`FAIL ${name}\n${error.stack}\n`);
    process.exitCode = 1;
  }
}

test('oversized converted reference is rejected before addRef', () => {
  const block = html.slice(html.indexOf('function refFromFile'), html.indexOf('if(upFile)'));
  assert.match(block, /dataUrlBytes\(ref\.dataUrl\)>\(fruit\?XIAOLE_REF_MAX_BYTES:REF_MAX_BYTES\)/);
  assert.match(block, /图片仍超过/);
  assert.ok(block.indexOf('dataUrlBytes(ref.dataUrl)') < block.indexOf('addRef('));
});

test('oversized reverse-prompt image is rejected before revSet', () => {
  const block = html.slice(html.indexOf('function revLoad'), html.indexOf('if(revFile)'));
  assert.match(block, /dataUrlBytes\(ref\.dataUrl\)>REF_MAX_BYTES/);
  assert.ok(block.indexOf('dataUrlBytes(ref.dataUrl)') < block.indexOf('revSet('));
});

test('image request uses xhr upload progress', () => {
  assert.match(html, /function requestJsonWithProgress\(endpoint,payload,onProgress\)/);
  assert.match(html, /new XMLHttpRequest\(\)/);
  assert.match(html, /xhr\.upload\.onprogress=function\(e\)/);
  assert.match(html, /Math\.round\(e\.loaded\/e\.total\*100\)/);
  assert.match(html, /上传素材 '\+pct\+'%/);
  assert.match(html, /payload\.image\s*\?\s*requestJsonWithProgress/);
});

test('failed upload preserves payload and exposes explicit retry', () => {
  assert.match(html, /var lastPayload=null,lastLabel='',lastEndpoint=''/);
  assert.match(html, /function showUploadRetry\(message\)/);
  assert.match(html, /id="uploadRetryBtn"/);
  assert.match(html, /submit\(lastPayload,lastLabel,lastEndpoint\)/);
  assert.match(html, /if\(payload\.image&&!error\.uploadComplete\) showUploadRetry/);
  assert.match(html, /上传已完成但响应中断，请先到最近作品确认，避免重复扣点/);
});

test('inline application script remains valid JavaScript', () => {
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)];
  assert.ok(scripts.length > 0, 'banana.html must contain an inline script');
  for (const script of scripts) new Function(script[1]);
});
