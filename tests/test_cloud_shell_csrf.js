const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const shell = fs.readFileSync(path.join(root, 'site/workbench/cloud-shell.js'), 'utf8');
const login = fs.readFileSync(path.join(root, 'site/login.html'), 'utf8');
const admin = fs.readFileSync(path.join(root, 'site/admin/index.html'), 'utf8');
const settings = fs.readFileSync(path.join(root, 'site/workbench/settings.html'), 'utf8');

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} must be defined`);
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  let quote = '';
  let escaped = false;
  for (let i = bodyStart; i < source.length; i += 1) {
    const char = source[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === quote) quote = '';
      continue;
    }
    if (char === "'" || char === '"' || char === '`') quote = char;
    else if (char === '{') depth += 1;
    else if (char === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`unterminated function ${name}`);
}

function loadHelpers(cookie = 'hq_csrf=csrf%2Btoken%2Fvalue%3D', source = shell) {
  const definitions = `${extractFunction(source, 'csrfToken')}\n${extractFunction(source, 'secureFetch')}`;
  const calls = [];
  const fakeFetch = (input, init) => {
    calls.push({input, init});
    return Promise.resolve({ok: true});
  };
  const document = {cookie};
  const location = {href: 'https://app.example/workbench/settings.html', origin: 'https://app.example'};
  const helpers = new Function('document', 'location', 'fetch', 'Headers', `${definitions}; return {csrfToken, secureFetch};`)(document, location, fakeFetch, Headers);
  return {...helpers, calls};
}

function loadSettingsApiFetch() {
  const calls = [];
  const secureFetch = (input, init) => {
    calls.push({input, init});
    return Promise.resolve({ok: true, status: 200, json: () => Promise.resolve({})});
  };
  const window = {HQ: {secureFetch}};
  const apiFetch = new Function(
    'window', 'fetch', 'clearLocalAuth', 'openLogin',
    `${extractFunction(settings, 'apiFetch')}; return apiFetch;`,
  )(window, () => { throw new Error('global fetch must not be used'); }, () => {}, () => {});
  return {apiFetch, calls};
}

test('csrfToken decodes the hq_csrf cookie', () => {
  const {csrfToken} = loadHelpers('theme=dark; hq_csrf=csrf%2Btoken%2Fvalue%3D; other=1');
  assert.equal(csrfToken(), 'csrf+token/value=');
});

test('malformed percent-encoded CSRF cookie returns empty without throwing', () => {
  const {csrfToken} = loadHelpers('hq_csrf=%E0%A4%A');
  assert.doesNotThrow(() => csrfToken());
  assert.equal(csrfToken(), '');
});

test('same-origin mutations receive the CSRF header and preserve caller headers', async () => {
  const {secureFetch, calls} = loadHelpers();
  await secureFetch('/api/auth/profile', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Trace': 'one'}});
  const headers = new Headers(calls[0].init.headers);
  assert.equal(headers.get('X-CSRF-Token'), 'csrf+token/value=');
  assert.equal(headers.get('Content-Type'), 'application/json');
  assert.equal(headers.get('X-Trace'), 'one');
});

test('an existing CSRF header is not overwritten', async () => {
  const {secureFetch, calls} = loadHelpers();
  await secureFetch('/api/auth/logout', {method: 'DELETE', headers: {'x-csrf-token': 'caller-token'}});
  assert.equal(new Headers(calls[0].init.headers).get('X-CSRF-Token'), 'caller-token');
});

test('GET and cross-origin requests never receive the CSRF header', async () => {
  const {secureFetch, calls} = loadHelpers();
  await secureFetch('/api/auth/me', {method: 'GET', headers: {'X-CSRF-Token': 'must-remove'}});
  await secureFetch('https://evil.example/collect', {method: 'POST', headers: {'X-CSRF-Token': 'must-remove'}});
  for (const call of calls) assert.equal(new Headers(call.init.headers).has('X-CSRF-Token'), false);
});

test('protocol-relative cross-origin URL never receives the CSRF header', async () => {
  const {secureFetch, calls} = loadHelpers();
  await secureFetch('//evil.example/collect', {method: 'DELETE', headers: {'X-CSRF-Token': 'must-remove'}});
  assert.equal(new Headers(calls[0].init.headers).has('X-CSRF-Token'), false);
});

test('Request input preserves its method and headers while applying same-origin CSRF', async () => {
  const {secureFetch, calls} = loadHelpers();
  const request = new Request('https://app.example/api/auth/profile', {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json', 'X-Request-Header': 'kept'},
    body: '{}',
  });
  await secureFetch(request);
  assert.equal(calls[0].input, request);
  assert.equal(calls[0].input.method, 'PATCH');
  const headers = new Headers(calls[0].init.headers);
  assert.equal(headers.get('Content-Type'), 'application/json');
  assert.equal(headers.get('X-Request-Header'), 'kept');
  assert.equal(headers.get('X-CSRF-Token'), 'csrf+token/value=');
});

test('explicit Bearer requests never receive the CSRF header', async () => {
  const {secureFetch, calls} = loadHelpers();
  await secureFetch('/api/auth/profile', {method: 'PATCH', headers: {Authorization: 'Bearer mini-app-token', 'X-CSRF-Token': 'must-remove'}});
  const headers = new Headers(calls[0].init.headers);
  assert.equal(headers.get('Authorization'), 'Bearer mini-app-token');
  assert.equal(headers.has('X-CSRF-Token'), false);
});

test('standalone wrappers mirror the no-leak mutation behavior', async () => {
  for (const [name, source] of [['login', login], ['admin', admin]]) {
    const {secureFetch, calls} = loadHelpers('hq_csrf=standalone%2Btoken', source);
    await secureFetch('/api/change', {method: 'PUT', headers: {'X-Trace': name}});
    await secureFetch('https://evil.example/change', {method: 'PUT', headers: {'X-CSRF-Token': 'must-remove'}});
    await secureFetch('/api/change', {method: 'POST', headers: {Authorization: 'Bearer app-token', 'X-CSRF-Token': 'must-remove'}});
    assert.equal(new Headers(calls[0].init.headers).get('X-CSRF-Token'), 'standalone+token', name);
    assert.equal(new Headers(calls[0].init.headers).get('X-Trace'), name, name);
    assert.equal(new Headers(calls[1].init.headers).has('X-CSRF-Token'), false, name);
    assert.equal(new Headers(calls[2].init.headers).has('X-CSRF-Token'), false, name);
  }
});

test('workbench auth and profile mutations use secureFetch', () => {
  assert.match(shell, /secureFetch\('\/api\/auth\/login'/);
  assert.match(shell, /secureFetch\('\/api\/auth\/register'/);
  assert.match(shell, /secureFetch\('\/api\/auth\/logout'/);
  assert.match(settings, /typeof window\.HQ\.secureFetch==='function'/);
  assert.match(settings, /apiFetch\('\/api\/auth\/profile'/);
  assert.doesNotMatch(shell, /window\.fetch\s*=/);
});

test('settings adapter preserves a caller Headers instance for secureFetch', async () => {
  const {apiFetch, calls} = loadSettingsApiFetch();
  const callerHeaders = new Headers([['Content-Type', 'application/json'], ['X-Caller', 'kept']]);
  await apiFetch('/api/auth/profile', {method: 'POST', headers: callerHeaders, body: '{}'});
  const forwarded = new Headers(calls[0].init.headers);
  assert.equal(forwarded.get('Content-Type'), 'application/json');
  assert.equal(forwarded.get('X-Caller'), 'kept');
});

test('logout remains a legal JSON mutation under the server gate', () => {
  assert.match(shell, /secureFetch\('\/api\/auth\/logout',\{method:'POST',credentials:'same-origin',headers:\{'Content-Type':'application\/json'\},body:'\{\}'\}\)/);
});

test('standalone login and admin mutations enforce the same CSRF rules', () => {
  assert.match(login, /function secureFetch\(/);
  assert.match(login, /secureFetch\("\/api\/auth\/(?:login|register)"/);
  assert.match(admin, /function secureFetch\(/);
  assert.match(admin, /return secureFetch\(path,opt\)/);
  assert.doesNotMatch(admin, /[?&](?:token|admin_token|csrf)=/i);
});
