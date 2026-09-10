const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const shell = fs.readFileSync(path.join(__dirname, '../site/workbench/cloud-shell.js'), 'utf8');
const authBlock = shell.match(/  \/\/ ===== 服务端验证身份状态 =====[\s\S]*?\n\n  \/\/ ===== 点数消费明细/);
assert.ok(authBlock, 'cloud-shell.js must keep the server-verified auth block testable');

function response(status, data, jsonError) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json() { return jsonError ? Promise.reject(jsonError) : Promise.resolve(data); },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function createRuntime(fetchImpl) {
  const values = new Map();
  const localStorage = {
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) { values.set(key, String(value)); },
    removeItem(key) { values.delete(key); },
  };
  const events = [];
  const rootClasses = new Set(['hq-points-ui-disabled']);
  const document = {
    documentElement: { classList: { toggle(name, on) { on ? rootClasses.add(name) : rootClasses.delete(name); } } },
    getElementById() { return null; },
  };
  const window = { dispatchEvent(event) { events.push(event.detail); } };
  function CustomEvent(name, options) { this.type = name; this.detail = options.detail; }
  const source = authBlock[0].replace(/\n\n  \/\/ ===== 点数消费明细$/, '');
  return new Function('fetch', 'localStorage', 'window', 'CustomEvent', 'document', 'events', 'rootClasses', `
    var _accountAvatar='';
    var renderCount=0,closeCount=0,loginCount=0;
    function authHeaders(){return {}}
    function closeAccountMenu(){closeCount++}
    function renderUser(){renderCount++}
    function requireLogin(){
      localStorage.removeItem('hq_user');
      invalidateAuthenticatedUi();
      loginCount++;
    }
    ${source}
    return {
      refreshPoints,
      handleAuthStorageChange,
      getUser:verifiedCurrentUser,
      getEpoch:()=>_authVerificationEpoch,
      getLoginCount:()=>loginCount,
      events,
      localStorage,
      pointsUiHidden:()=>rootClasses.has('hq-points-ui-disabled'),
    };
  `)(fetchImpl, localStorage, window, CustomEvent, document, events, rootClasses);
}

test('verification failures invalidate an already verified account', async () => {
  let next = Promise.resolve(response(200, { user: { username: 'alice', role: 'admin' } }));
  const runtime = createRuntime(() => next);
  await runtime.refreshPoints();
  assert.equal(runtime.getUser().role, 'admin');

  for (const failure of [
    Promise.reject(new Error('offline')),
    Promise.resolve(response(200, null, new Error('invalid json'))),
    Promise.resolve(response(200, {})),
    Promise.resolve(response(503, { detail: 'unavailable' })),
  ]) {
    next = failure;
    await runtime.refreshPoints();
    assert.equal(runtime.getUser(), null);
    next = Promise.resolve(response(200, { user: { username: 'alice', role: 'admin' } }));
    await runtime.refreshPoints();
  }
});

test('points UI follows the server billing switch', async () => {
  let enabled = false;
  const runtime = createRuntime(() => Promise.resolve(response(200, {
    user: { username: 'alice', role: 'member', points_billing_enabled: enabled },
  })));
  await runtime.refreshPoints();
  assert.equal(runtime.pointsUiHidden(), true);
  enabled = true;
  await runtime.refreshPoints();
  assert.equal(runtime.pointsUiHidden(), false);
});

test('a stale success cannot overwrite a newer unauthorized result', async () => {
  const first = deferred();
  let calls = 0;
  const runtime = createRuntime(() => calls++ === 0 ? first.promise : Promise.resolve(response(401, {})));
  const stale = runtime.refreshPoints();
  await runtime.refreshPoints();
  assert.equal(runtime.getLoginCount(), 1);
  first.resolve(response(200, { user: { username: 'stale-admin', role: 'admin' } }));
  await stale;
  assert.equal(runtime.getUser(), null);
  assert.equal(runtime.localStorage.getItem('hq_user'), null);
});

test('verified user is an isolated server snapshot and storage changes fail closed', async () => {
  const serverUser = { username: 'alice', role: 'member' };
  const runtime = createRuntime(() => Promise.resolve(response(200, { user: serverUser })));
  await runtime.refreshPoints();
  runtime.localStorage.setItem('hq_user', JSON.stringify({ username: 'alice', role: 'admin' }));
  assert.equal(runtime.getUser().role, 'member');
  const exposed = runtime.getUser();
  exposed.role = 'admin';
  assert.equal(runtime.getUser().role, 'member');

  runtime.handleAuthStorageChange({ key: 'hq_user', newValue: null });
  assert.equal(runtime.getUser(), null);
});
