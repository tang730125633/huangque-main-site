const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../site/admin/channel-manager.js'), 'utf8');
const start = source.indexOf('const secretField=');
const end = source.indexOf('const workspace=', start);
assert.ok(start > 0 && end > start, 'secretField/revealSecret 代码块未找到');

function setup() {
  const box = { hidden: false, textContent: '' };
  const nodes = { cmSecretReveal: box };
  const toasts = [];
  const requests = [];
  let clipboardValue = null;
  const context = {
    el: (id) => nodes[id],
    toast: (m) => toasts.push(m),
    confirm: () => true,
    api: (p, o) => { requests.push({ path: p, body: JSON.parse(o.body) }); return Promise.resolve({ secret: 'sk-live-secret', expires_in: 5 }); },
    navigator: { clipboard: { writeText: (v) => { clipboardValue = v; return Promise.resolve(); } } },
    Promise, console,
    setTimeout: (...a) => globalThis.setTimeout(...a),
    clearTimeout: (...a) => globalThis.clearTimeout(...a),
  };
  vm.createContext(context);
  vm.runInContext(
    'let editing=null, secretTimer=null;\n' + source.slice(start, end) +
    '\nglobalThis.__t={revealSecret,setEditing:(v)=>{editing=v},getBox:()=>el("cmSecretReveal")};',
    context,
  );
  return { c: context.__t, box, toasts, requests, getClipboard: () => clipboardValue };
}

test('view reveals secret with confirm, toast and 5s auto-hide', async (t) => {
  const { c, box, toasts, requests } = setup();
  c.setEditing({ id: 'xlw-image-2', name: 'GPT Image 2 生图（乐创）' });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  c.revealSecret('view');
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(requests, [{ path: '/api/admin/channel-manager/secret-reveal', body: { id: 'xlw-image-2' } }]);
  assert.equal(box.hidden, false);
  assert.equal(box.textContent, 'sk-live-secret');
  assert.ok(toasts.some((m) => m.includes('已显示，5 秒后隐藏')));
  t.mock.timers.tick(5000);
  assert.equal(box.hidden, true);
  assert.equal(box.textContent, '');
});

test('copy writes clipboard and hides after expiry', async (t) => {
  const { c, box, toasts, getClipboard } = setup();
  c.setEditing({ id: 'xlw-grok-video' });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  c.revealSecret('copy');
  await new Promise((r) => setImmediate(r));
  assert.equal(getClipboard(), 'sk-live-secret');
  assert.ok(toasts.some((m) => m.includes('密钥已复制，5 秒后隐藏')));
  t.mock.timers.tick(5000);
  assert.equal(box.hidden, true);
});

test('cancel skips request; unsaved channel is rejected without request', () => {
  const toasts = [];
  const requests = [];
  const cancelled = {
    el: () => ({ hidden: true, textContent: '' }), toast: (m) => toasts.push(m),
    confirm: () => false,
    api: (p) => { requests.push({ path: p }); return Promise.resolve({}); },
    navigator: {}, Promise, console,
    setTimeout: (...a) => globalThis.setTimeout(...a),
    clearTimeout: (...a) => globalThis.clearTimeout(...a),
  };
  const vc = vm.createContext(cancelled);
  vm.runInContext('let editing={id:"xlw-image-2"}, secretTimer=null;\n' + source.slice(start, end) + '\nglobalThis.__t={revealSecret};', vc);
  vc.__t.revealSecret('view');
  await0();
  assert.equal(requests.length, 0);
  assert.equal(toasts.length, 0);

  const fresh = setup();
  fresh.c.setEditing(null);
  fresh.c.revealSecret('view');
  assert.equal(fresh.requests.length, 0);
  assert.ok(fresh.toasts.some((m) => m.includes('请先保存渠道')));
});

function await0() {}
