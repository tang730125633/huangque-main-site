const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.resolve(__dirname, '../site/workbench/audio.html'), 'utf8');

function test(name, fn) {
  try {
    fn();
    process.stdout.write(`PASS ${name}\n`);
  } catch (error) {
    process.stderr.write(`FAIL ${name}\n${error.stack}\n`);
    process.exitCode = 1;
  }
}

function readMapper(online) {
  const match = html.match(/function audioNetworkMessage\(error,phase\)\{[\s\S]*?\n  \}/);
  assert.ok(match, 'audio.html must define audioNetworkMessage(error,phase)');
  return new Function('navigator', `${match[0]}; return audioNetworkMessage;`)({onLine: online});
}

test('offline submit gets Chinese retry guidance', () => {
  const message = readMapper(false)(new TypeError('Failed to fetch'), 'submit');
  assert.match(message, /网络已断开/);
  assert.match(message, /再次点击生成/);
  assert.doesNotMatch(message, /Failed to fetch/i);
});

test('uncertain submit tells user to verify before retrying', () => {
  const message = readMapper(true)(new TypeError('NetworkError when attempting to fetch resource.'), 'submit');
  assert.match(message, /先查看最近音频/);
  assert.match(message, /避免重复扣点/);
});

test('poll failure never advises creating another task', () => {
  const message = readMapper(true)(new TypeError('Load failed'), 'poll');
  assert.match(message, /任务可能仍在后台生成/);
  assert.doesNotMatch(message, /再次点击生成/);
});

test('business errors remain readable', () => {
  const message = readMapper(true)(new Error('点数不足'), 'submit');
  assert.equal(message, '点数不足');
});

test('catch branches do not expose raw browser error messages', () => {
  assert.doesNotMatch(html, /statusText'\)\.textContent=e\.message/);
  assert.match(html, /audioNetworkMessage\(e,'submit'\)/);
  assert.match(html, /audioNetworkMessage\(e,'poll'\)/);
});

test('inline application script remains valid JavaScript', () => {
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)];
  assert.ok(scripts.length > 0);
  for (const script of scripts) new Function(script[1]);
});

