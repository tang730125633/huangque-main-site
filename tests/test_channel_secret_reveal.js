const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../site/admin/channel-manager.js'), 'utf8');
const start = source.indexOf('const secretField=');
const end = source.indexOf('const workspace=', start);
assert.ok(start > 0 && end > start, 'secretField/revealSecret 代码块未找到');

function legacyDocument(execResult = true) {
  const created = [];
  return {
    created,
    createElement() {
      const node = {
        value: '', style: {}, selected: false, removed: false,
        setAttribute() {}, select() { this.selected = true; }, remove() { this.removed = true; }, focus() {},
      };
      created.push(node);
      return node;
    },
    body: { appendChild() {} },
    execCommand(cmd) { created.lastCommand = cmd; return execResult; },
    createRange() { return { selectNodeContents() {} }; },
  };
}

function setup({ clipboard = 'resolve', doc } = {}) {
  const box = { hidden: false, textContent: '', focus() {} };
  const nodes = { cmSecretReveal: box };
  const toasts = [];
  const requests = [];
  let clipboardValue = null;
  const navigator = {};
  if (clipboard === 'resolve') navigator.clipboard = { writeText: (v) => { clipboardValue = v; return Promise.resolve(); } };
  else if (clipboard === 'reject') navigator.clipboard = { writeText: () => Promise.reject(new Error('not allowed')) };
  const context = {
    el: (id) => nodes[id],
    toast: (m) => toasts.push(m),
    confirm: () => true,
    api: (p, o) => { requests.push({ path: p, body: JSON.parse(o.body) }); return Promise.resolve({ secret: 'ak_xlw_secret', expires_in: 5 }); },
    navigator,
    window: { isSecureContext: true, getSelection: () => ({ removeAllRanges() {}, addRange() {} }) },
    document: doc || legacyDocument(),
    Promise, console,
    setTimeout: (...a) => globalThis.setTimeout(...a),
    clearTimeout: (...a) => globalThis.clearTimeout(...a),
  };
  vm.createContext(context);
  vm.runInContext(
    'let editing=null, secretTimer=null;\n' + source.slice(start, end) +
    '\nglobalThis.__t={revealSecret,setEditing:(v)=>{editing=v}};',
    context,
  );
  return { c: context.__t, box, toasts, requests, getClipboard: () => clipboardValue, doc: context.document };
}

const flush = () => new Promise((r) => setImmediate(r));

test('view reveals secret with confirm, toast and 5s auto-hide', async (t) => {
  const { c, box, toasts, requests } = setup();
  c.setEditing({ id: 'xlw-image-2', name: 'GPT Image 2 生图（乐创）' });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  c.revealSecret('view');
  await flush();
  assert.deepEqual(requests, [{ path: '/api/admin/channel-manager/secret-reveal', body: { id: 'xlw-image-2' } }]);
  assert.equal(box.hidden, false);
  assert.equal(box.textContent, 'ak_xlw_secret');
  assert.ok(toasts.some((m) => m.includes('已显示，5 秒后隐藏')));
  t.mock.timers.tick(5000);
  assert.equal(box.hidden, true);
  assert.equal(box.textContent, '');
});

test('copy uses clipboard API and hides after expiry', async (t) => {
  const { c, box, toasts, getClipboard } = setup();
  c.setEditing({ id: 'xlw-grok-video' });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  c.revealSecret('copy');
  await flush();
  assert.equal(getClipboard(), 'ak_xlw_secret');
  assert.ok(toasts.some((m) => m.includes('密钥已复制，5 秒后隐藏')));
  t.mock.timers.tick(5000);
  assert.equal(box.hidden, true);
});

test('copy falls back to textarea+execCommand when clipboard API rejects', async () => {
  const doc = legacyDocument(true);
  const { c, toasts } = setup({ clipboard: 'reject', doc });
  c.setEditing({ id: 'xlw-image-2' });
  c.revealSecret('copy');
  await flush();
  await flush();
  assert.equal(doc.created.length, 1, '应创建一个临时 textarea');
  assert.equal(doc.created[0].value, 'ak_xlw_secret');
  assert.equal(doc.created[0].selected, true);
  assert.equal(doc.created[0].removed, true);
  assert.equal(doc.created.lastCommand, 'copy');
  assert.ok(toasts.some((m) => m.includes('密钥已复制')));
});

test('copy falls back when clipboard API is unavailable at all', async () => {
  const doc = legacyDocument(false);
  const { c, toasts } = setup({ clipboard: 'none', doc });
  c.setEditing({ id: 'xlw-image-2' });
  c.revealSecret('copy');
  await flush();
  await flush();
  assert.equal(doc.created.lastCommand, 'copy');
  assert.ok(toasts.some((m) => m.includes('明文已选中，请按 Ctrl+C')));
});

test('reveal buttons live outside the label so label activation cannot swallow clicks', () => {
  const markup = source.slice(source.indexOf('const secretField='), start === -1 ? undefined : source.indexOf('function selectReveal', source.indexOf('const secretField=')));
  const labelEnd = markup.indexOf('</label>');
  assert.ok(labelEnd > 0, '应存在 label 结束标签');
  assert.ok(markup.indexOf('data-cm-secret="view"') > labelEnd, '显示按钮应在 label 之外');
  assert.ok(markup.indexOf('data-cm-secret="copy"') > labelEnd, '复制按钮应在 label 之外');
});

test('cancel skips request; unsaved channel is rejected without request', () => {
  const toasts = [];
  const requests = [];
  const cancelled = {
    el: () => ({ hidden: true, textContent: '' }), toast: (m) => toasts.push(m),
    confirm: () => false,
    api: (p) => { requests.push({ path: p }); return Promise.resolve({}); },
    navigator: {}, window: {}, document: legacyDocument(), Promise, console,
    setTimeout: (...a) => globalThis.setTimeout(...a),
    clearTimeout: (...a) => globalThis.clearTimeout(...a),
  };
  const vc = vm.createContext(cancelled);
  vm.runInContext('let editing={id:"xlw-image-2"}, secretTimer=null;\n' + source.slice(start, end) + '\nglobalThis.__t={revealSecret};', vc);
  vc.__t.revealSecret('view');
  assert.equal(requests.length, 0);
  assert.equal(toasts.length, 0);

  const fresh = setup();
  fresh.c.setEditing(null);
  fresh.c.revealSecret('view');
  assert.equal(fresh.requests.length, 0);
  assert.ok(fresh.toasts.some((m) => m.includes('请先保存渠道')));
});
