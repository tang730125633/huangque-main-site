const fs = require('fs');
const path = require('path');
const vm = require('vm');

class ClassList {
  constructor(node) { this.node = node; this.names = new Set(); }
  add(name) { this.names.add(name); }
  remove(name) { this.names.delete(name); }
  toggle(name, force) {
    const enabled = force === undefined ? !this.names.has(name) : Boolean(force);
    if (enabled) this.names.add(name); else this.names.delete(name);
    return enabled;
  }
  contains(name) { return this.names.has(name); }
}

class Element {
  constructor(tagName = 'div', id = '') {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.value = '';
    this._textContent = '';
    this.innerHTML = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.files = [];
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.className = '';
    this.classList = new ClassList(this);
  }
  get textContent() { return this._textContent; }
  set textContent(value) { this._textContent = String(value); if (value === '') this.children = []; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter((item) => item !== child); child.parentNode = null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  dispatch(name) {
    (this.listeners[name] || []).forEach((handler) => handler({target: this, type: name}));
    const property = this['on' + name];
    if (typeof property === 'function') property({target: this, type: name});
  }
  click() { if (!this.disabled && typeof this.onclick === 'function') this.onclick({target: this}); }
  focus() {}
  play() { return Promise.resolve(); }
  querySelectorAll(selector) {
    const descendants = [];
    const visit = (node) => node.children.forEach((child) => { descendants.push(child); visit(child); });
    visit(this);
    if (selector === 'button') return descendants.filter((node) => node.tagName === 'BUTTON');
    if (selector === '.tv-template') return descendants.filter((node) => node.className === 'tv-template');
    return [];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return {promise, resolve, reject};
}

function response(status, data) {
  return {status, ok: status >= 200 && status < 300, text: () => Promise.resolve(JSON.stringify(data))};
}

async function flush(turns = 8) {
  for (let index = 0; index < turns; index += 1) await new Promise((resolve) => setImmediate(resolve));
}

function createRuntime() {
  const root = path.resolve(__dirname, '..');
  const page = fs.readFileSync(path.join(root, 'site', 'workbench', 'text-video.html'), 'utf8');
  const scripts = [...page.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map((match) => match[1]).filter(Boolean);
  const source = scripts[scripts.length - 1];
  const elements = new Map();
  for (const match of page.matchAll(/<([a-z0-9-]+)[^>]*\sid="([^"]+)"[^>]*>/gi)) elements.set(match[2], new Element(match[1], match[2]));
  const get = (id) => elements.get(id) || (() => { const node = new Element('div', id); elements.set(id, node); return node; })();

  get('videoText').value = 'AI 培训如何提升团队效率';
  get('speechRate').value = '1';
  get('talkingRatio').value = '30';
  get('materialStyle').value = '';
  get('videoVoice').value = '';

  const modeGenerate = new Element('button'); modeGenerate.setAttribute('data-mode', 'generate');
  const modeFixed = new Element('button'); modeFixed.setAttribute('data-mode', 'fixed');
  const modeRoot = new Element('div'); modeRoot.appendChild(modeGenerate); modeRoot.appendChild(modeFixed);
  const kindIllustration = new Element('button'); kindIllustration.setAttribute('data-kind', 'illustration');
  const kindVideo = new Element('button'); kindVideo.setAttribute('data-kind', 'video');
  get('kindTabs').appendChild(kindIllustration); get('kindTabs').appendChild(kindVideo);
  const orientationPortrait = new Element('button'); orientationPortrait.setAttribute('data-orientation', 'portrait');
  const orientationLandscape = new Element('button'); orientationLandscape.setAttribute('data-orientation', 'landscape');
  get('orientationTabs').appendChild(orientationPortrait); get('orientationTabs').appendChild(orientationLandscape);

  const requests = {plans: [], avatars: [], paid: [], jobs: []};
  const revoked = [];
  let objectUrlCounter = 0;
  const fetch = (url, options = {}) => {
    if (url === '/api/gen/text-video/templates') return Promise.resolve(response(200, {templates: [
      {key: 'portrait-pro', name: '竖屏模板', kind: 'illustration', orientation: 'portrait', width: 1080, height: 1920, preview_url: '/template.png'},
      {key: 'landscape-pro', name: '横屏模板', kind: 'illustration', orientation: 'landscape', width: 1920, height: 1080, preview_url: '/landscape.png'},
    ]}));
    if (url === '/api/gen/text-video/styles') return Promise.resolve(response(200, {styles: [{key: 'business', name: '商务写实'}], default_style: 'business'}));
    if (url === '/api/gen/text-video/voices') return Promise.resolve(response(200, {voices: [{id: 'public:yun', name: '云健', scope: 'public'}], default_voice: 'public:yun'}));
    if (url === '/api/gen/text-video/plan') {
      const item = deferred(); item.url = url; item.options = options; requests.plans.push(item); return item.promise;
    }
    if (url === '/api/gen/text-video/avatar') {
      const item = deferred(); item.url = url; item.options = options; requests.avatars.push(item); return item.promise;
    }
    if (url === '/api/gen/script_to_video') {
      const item = deferred(); item.url = url; item.options = options; requests.paid.push(item); return item.promise;
    }
    if (String(url).startsWith('/api/gen/job/')) {
      const item = deferred(); item.url = url; item.options = options; requests.jobs.push(item); return item.promise;
    }
    throw new Error('Unexpected fetch: ' + url);
  };

  class MockFileReader {
    readAsDataURL(file) {
      this.result = file.data || 'data:image/png;base64,AAAA';
      queueMicrotask(() => { if (typeof this.onload === 'function') this.onload(); });
    }
    abort() { if (typeof this.onabort === 'function') this.onabort(); }
  }

  const document = {
    getElementById: get,
    createElement: (tag) => new Element(tag),
    querySelectorAll: (selector) => selector === '.tv-mode button' ? [modeGenerate, modeFixed] : [],
  };
  const storage = new Map();
  const sessionStorage = {
    getItem: (key) => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  const context = {
    console,
    document,
    fetch,
    FileReader: MockFileReader,
    sessionStorage,
    location: {href: ''},
    crypto: {randomUUID: () => 'runtime-key'},
    AbortController,
    URL: {
      createObjectURL: () => 'blob:avatar-' + (++objectUrlCounter),
      revokeObjectURL: (url) => revoked.push(url),
    },
    setTimeout: () => 1,
    clearTimeout: () => {},
    Date,
    Math,
    JSON,
    Promise,
    Array,
    Object,
    Number,
    String,
    Error,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context, {filename: 'text-video.inline.js'});
  return {get, requests, response, revoked, modeGenerate, modeFixed, kindIllustration, kindVideo, orientationPortrait, orientationLandscape};
}

async function upload(runtime, file, sceneId = '') {
  const input = runtime.get(sceneId ? 'talkingSceneAvatarInput' : 'talkingDefaultAvatar');
  if (sceneId) {
    const scene = runtime.get('talkingScenes').children.find((node) => node.getAttribute('data-scene-id') === sceneId);
    const replace = scene && scene.children[3] && scene.children[3].children[0];
    if (!replace) throw new Error('Scene replace button missing');
    replace.disabled = false;
    replace.click();
  }
  input.files = [file];
  input.dispatch('change');
  await flush();
}

async function readyTalking(runtime) {
  await flush();
  runtime.get('talkingMaterialEnabled').checked = true;
  runtime.get('talkingMaterialEnabled').dispatch('change');
  await upload(runtime, {type: 'image/png', size: 100, data: 'data:image/png;base64,AAAA'});
  runtime.requests.avatars[0].resolve(response(200, {asset_id: 'avatar-base', preview_url: '/avatar-base'}));
  await flush();
}

async function createPlan(runtime, index = 0, sceneCount = 1) {
  runtime.get('generateBtn').click();
  await flush();
  const scenes = Array.from({length: sceneCount}, (_, sceneIndex) => ({
    scene_id: 'scene_' + (sceneIndex + 1), text: '第' + (sceneIndex + 1) + '幕', estimated_duration: 6, talking_recommended: true,
  }));
  runtime.requests.plans[index].resolve(response(200, {plan_id: 'plan-' + index, source_hash: 'hash-' + index, scenes}));
  await flush();
}

async function scenarioLatePlan() {
  const runtime = createRuntime();
  await readyTalking(runtime);
  runtime.get('generateBtn').click();
  await flush();
  runtime.get('videoText').value = '修改后的文案';
  runtime.get('videoText').dispatch('input');
  runtime.requests.plans[0].resolve(response(200, {plan_id: 'stale', source_hash: 'stale-hash', scenes: [{scene_id: 'scene_1', text: '旧方案', estimated_duration: 6, talking_recommended: true}]}));
  await flush();
  return {button: runtime.get('generateBtn').textContent, status: runtime.get('statusText').textContent, scenes: runtime.get('talkingScenes').children.length};
}

async function scenarioAvatarRace() {
  const runtime = createRuntime();
  await readyTalking(runtime);
  await upload(runtime, {type: 'image/png', size: 100, data: 'data:image/png;base64,BBBB'});
  await upload(runtime, {type: 'image/png', size: 100, data: 'data:image/png;base64,CCCC'});
  const blockedWhilePending = runtime.get('generateBtn').disabled;
  runtime.get('generateBtn').onclick();
  const paidWhilePending = runtime.requests.paid.length;
  runtime.requests.avatars[2].resolve(response(200, {asset_id: 'avatar-new', preview_url: '/avatar-new'}));
  runtime.requests.avatars[1].resolve(response(200, {asset_id: 'avatar-old', preview_url: '/avatar-old'}));
  await flush();
  await createPlan(runtime, 0);
  runtime.get('generateBtn').click();
  await flush();
  return {
    blockedWhilePending,
    paidWhilePending,
    payload: JSON.parse(runtime.requests.paid[0].options.body),
    revoked: runtime.revoked,
  };
}

async function scenarioPhase() {
  const runtime = createRuntime();
  await readyTalking(runtime);
  await createPlan(runtime, 0);
  runtime.get('generateBtn').click();
  await flush();
  runtime.requests.paid[0].resolve(response(200, {job_id: 'job-1'}));
  await flush();
  runtime.requests.jobs[0].resolve(response(200, {status: 'running', phase: 'talking_render', stage: '普通素材阶段'}));
  await flush();
  return {status: runtime.get('statusText').textContent};
}

async function scenarioDisabledPath() {
  const runtime = createRuntime();
  await flush();
  runtime.get('generateBtn').click();
  await flush();
  return {payload: JSON.parse(runtime.requests.paid[0].options.body), planRequests: runtime.requests.plans.length};
}

async function scenarioPlanMutations() {
  const mutations = {
    text(runtime) { runtime.get('videoText').value = '新文案'; runtime.get('videoText').dispatch('input'); },
    mode(runtime) { runtime.modeFixed.click(); },
    voice(runtime) { runtime.get('videoVoice').value = 'public:other'; runtime.get('videoVoice').dispatch('change'); },
    speechRate(runtime) { runtime.get('speechRate').value = '1.3'; runtime.get('speechRate').dispatch('input'); },
    style(runtime) { runtime.get('materialStyle').value = 'other'; runtime.get('materialStyle').dispatch('change'); },
    ratio(runtime) { runtime.get('talkingRatio').value = '40'; runtime.get('talkingRatio').dispatch('input'); },
    template(runtime) { runtime.get('templateGrid').querySelector('.tv-template').click(); },
    kind(runtime) { runtime.kindVideo.click(); },
    orientation(runtime) { runtime.orientationLandscape.click(); },
    enabled(runtime) { runtime.get('talkingMaterialEnabled').checked = false; runtime.get('talkingMaterialEnabled').dispatch('change'); },
    defaultAvatar(runtime) {
      const input = runtime.get('talkingDefaultAvatar');
      input.files = [{type: 'image/png', size: 100, data: 'data:image/png;base64,DDDD'}];
      input.dispatch('change');
    },
  };
  const results = {};
  for (const [name, mutate] of Object.entries(mutations)) {
    const runtime = createRuntime();
    await readyTalking(runtime);
    runtime.get('generateBtn').click();
    await flush();
    mutate(runtime);
    await flush();
    runtime.requests.plans[0].resolve(response(200, {plan_id: 'stale-' + name, source_hash: 'stale', scenes: [{scene_id: 'scene_1', text: '旧方案', estimated_duration: 6, talking_recommended: true}]}));
    await flush();
    results[name] = runtime.get('generateBtn').textContent !== '确认并生成视频';
  }
  return results;
}

async function scenarioSceneAvatarRace() {
  const runtime = createRuntime();
  await readyTalking(runtime);
  await createPlan(runtime, 0);
  await upload(runtime, {type: 'image/png', size: 100, data: 'data:image/png;base64,EEEE'}, 'scene_1');
  await upload(runtime, {type: 'image/png', size: 100, data: 'data:image/png;base64,FFFF'}, 'scene_1');
  const blockedWhilePending = runtime.get('generateBtn').disabled;
  runtime.requests.avatars[2].resolve(response(200, {asset_id: 'scene-new', preview_url: '/scene-new'}));
  runtime.requests.avatars[1].resolve(response(200, {asset_id: 'scene-old', preview_url: '/scene-old'}));
  await flush();
  runtime.get('generateBtn').click();
  await flush();
  return {blockedWhilePending, payload: JSON.parse(runtime.requests.paid[0].options.body)};
}

async function main() {
  const scenario = process.argv[2];
  const handlers = {latePlan: scenarioLatePlan, avatarRace: scenarioAvatarRace, phase: scenarioPhase, disabledPath: scenarioDisabledPath, planMutations: scenarioPlanMutations, sceneAvatarRace: scenarioSceneAvatarRace};
  if (!handlers[scenario]) throw new Error('Unknown scenario: ' + scenario);
  process.stdout.write(JSON.stringify(await handlers[scenario]()));
}

main().catch((error) => { console.error(error && error.stack || error); process.exitCode = 1; });
