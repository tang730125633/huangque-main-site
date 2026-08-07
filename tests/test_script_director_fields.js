// 编导页 7 字段明细保留与多参考图上限的「行为」回归测试(非字符串断言):
// 从 script.html 抽取真实函数,注入假 DOM/假 FileReader 执行,验证 tang#984 打回的两个 P2 修复。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'site/workbench/script.html'), 'utf8');

function extractFn(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `${name} 必须存在于 script.html`);
  const brace = source.indexOf('{', start);
  let depth = 0, quote = null, escaped = false;
  for (let i = brace; i < source.length; i++) {
    const ch = source[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') quote = ch;
    else if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) return source.slice(start, i + 1); }
  }
  throw new Error(`unterminated function: ${name}`);
}

function load(name, scope) {
  const src = extractFn(html, name);
  const keys = Object.keys(scope);
  return new Function(...keys, `${src}\nreturn ${name};`)(...keys.map((k) => scope[k]));
}

function test(name, fn) {
  try {
    fn();
    process.stdout.write(`PASS ${name}\n`);
  } catch (error) {
    process.stderr.write(`FAIL ${name}\n${error.stack}\n`);
    process.exitCode = 1;
  }
}

// ---- 假 DOM:编辑态分镜卡 ----
function fakeCard(dur, scene, line) {
  return {
    querySelector(sel) {
      if (sel.includes('data-scene-dur')) return { value: dur };
      if (sel.includes('data-scene-text')) return { value: scene };
      if (sel.includes('data-scene-line')) return { value: line };
      return null;
    },
  };
}
function fakeScenesEl(cards) {
  return { querySelectorAll: (sel) => (sel === '.sc-card' ? cards : []) };
}

const FULL = {
  dur: '3s', scene: '原画面', line: '原口播',
  shot: '近景', camera: '前推', lighting: '暖光', audio: '安静', transition: '硬切',
};

test('编辑保存(write 模式)保留未编辑的 5 个明细字段', () => {
  const lastScenes = [{ ...FULL }, { ...FULL, scene: '第二镜' }];
  const cards = [fakeCard('4s', '改后画面', '改后口播'), fakeCard('3s', '第二镜', '原口播')];
  const readEditingScenes = load('readEditingScenes', {
    scenes: fakeScenesEl(cards), currentMode: 'write', lastBreakdown: null, lastScenes,
  });
  const out = readEditingScenes();
  assert.equal(out.length, 2);
  assert.equal(out[0].scene, '改后画面'); // 编辑值生效
  assert.equal(out[0].dur, '4s');
  for (const f of ['shot', 'camera', 'lighting', 'audio', 'transition']) {
    assert.equal(out[0][f], FULL[f], `字段 ${f} 必须保留`);
    assert.equal(out[1][f], FULL[f]);
  }
});

test('编辑保存(breakdown 模式)以 lastBreakdown.scenes 为底合并', () => {
  const lastBreakdown = { scenes: [{ ...FULL }] };
  const cards = [fakeCard('5s', '新画面', '')];
  const readEditingScenes = load('readEditingScenes', {
    scenes: fakeScenesEl(cards), currentMode: 'breakdown', lastBreakdown, lastScenes: [],
  });
  const out = readEditingScenes();
  assert.equal(out.length, 1);
  assert.equal(out[0].scene, '新画面');
  assert.equal(out[0].shot, '近景');
  assert.equal(out[0].transition, '硬切');
});

test('清空画面+口播的分镜被过滤且后续索引不错位', () => {
  const lastScenes = [{ ...FULL }, { ...FULL, scene: '第二镜', transition: '叠化' }];
  const cards = [fakeCard('3s', '', ''), fakeCard('3s', '第二镜改', '口播二')];
  const readEditingScenes = load('readEditingScenes', {
    scenes: fakeScenesEl(cards), currentMode: 'write', lastBreakdown: null, lastScenes,
  });
  const out = readEditingScenes();
  assert.equal(out.length, 1);
  assert.equal(out[0].scene, '第二镜改');
  assert.equal(out[0].transition, '叠化'); // 与第 2 个状态对象合并,不错位
});

test('sceneText 复制/导出包含全部 7 字段', () => {
  const sceneText = load('sceneText', {});
  const txt = sceneText({ ...FULL }, 0);
  for (const frag of ['镜号01', '（3s）', '画面：原画面', '口播：原口播', '景别：近景', '运镜：前推', '光线：暖光', '音效：安静', '转场：硬切']) {
    assert.ok(txt.includes(frag), `sceneText 缺少 ${frag}`);
  }
});

test('sceneText 对旧三字段数据不输出明细行(向后兼容)', () => {
  const sceneText = load('sceneText', {});
  const txt = sceneText({ dur: '3s', scene: '画面', line: '口播' }, 0);
  assert.ok(!txt.includes('景别：'));
  assert.ok(!txt.includes('转场：'));
});

test('normalizeBreakdownScenes 保留扩展字段', () => {
  const normalizeBreakdownScenes = load('normalizeBreakdownScenes', {});
  const out = normalizeBreakdownScenes([{ ...FULL }]);
  assert.equal(out.length, 1);
  for (const f of ['shot', 'camera', 'lighting', 'audio', 'transition']) {
    assert.equal(out[0][f], FULL[f], `字段 ${f} 必须保留`);
  }
});

// ---- 假 FileReader + 假 window.HQ:多参考图上限 ----
function uploadHarness(maxDataUrl = 7 * 1024 * 1024) {
  const env = {
    refImages: [],
    REF_IMAGE_MAX: 4,
    REF_DATAURL_MAX: maxDataUrl,
    toasts: [],
    syncs: 0,
    reads: [],
  };
  env.FileReader = class {
    readAsDataURL(f) {
      env.reads.push(f);
      this.result = 'data:image/png;base64,' + 'A'.repeat(f.dataUrlLen == null ? 32 : f.dataUrlLen);
      if (this.onload) this.onload(); // 同步触发,行为可测
    }
  };
  env.syncRefImages = () => { env.syncs++; };
  env.HQ = { toast: (m) => env.toasts.push(m) };
  env.window = { HQ: env.HQ };
  env.acceptRefFiles = load('acceptRefFiles', {
    refImages: env.refImages,
    REF_IMAGE_MAX: env.REF_IMAGE_MAX,
    REF_DATAURL_MAX: env.REF_DATAURL_MAX,
    FileReader: env.FileReader,
    syncRefImages: env.syncRefImages,
    window: env.window,
    HQ: env.HQ,
  });
  return env;
}

const file = (name, dataUrlLen) => ({ name, dataUrlLen });

test('一次多选 5 张只受理 4 张(tang#984 P2 回归)', () => {
  const env = uploadHarness();
  env.acceptRefFiles([file('a'), file('b'), file('c'), file('d'), file('e')]);
  assert.equal(env.refImages.length, 4);
  assert.equal(env.reads.length, 4, '启动读取前必须按剩余名额截断');
  assert.ok(env.toasts.some((m) => m.includes('最多 4 张')));
});

test('连续快速选择 3+3 张总量不超过 4 张(异步竞态回归)', () => {
  const env = uploadHarness();
  env.acceptRefFiles([file('a'), file('b'), file('c')]);
  env.acceptRefFiles([file('d'), file('e'), file('f')]);
  assert.equal(env.refImages.length, 4);
  assert.ok(env.toasts.some((m) => m.includes('最多 4 张')));
});

test('单张超 5MB 拒绝且不占用名额', () => {
  const env = uploadHarness(64); // 缩小阈值便于行为验证
  env.acceptRefFiles([file('big', 128), file('ok', 32)]);
  assert.equal(env.refImages.length, 1);
  assert.ok(env.toasts.some((m) => m.includes('不能超过 5MB')));
});

test('inline application script remains valid JavaScript', () => {
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)];
  assert.ok(scripts.length > 0, 'script.html must contain an inline script');
  for (const script of scripts) new Function(script[1]);
});
