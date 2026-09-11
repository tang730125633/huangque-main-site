const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../site/workbench/banana.html'), 'utf8');
const start = source.indexOf('var LECHUANG_FRONT=');
const end = source.indexOf('function lechuangDefaultPoints(', start);
assert.ok(start > 0 && end > start, '未找到乐创线路纯逻辑块');

function helpers() {
  const context = { window: {}, Array, String, Number };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  return context.window.HQBananaLechuang;
}

const catalog = {
  items: [
    { kind: 'xiaole_video', front: 'grok', label: 'Grok 视频 1.0', revision: 'xlw-grok-video:6' },
    { kind: 'image', front: 'gemini-3-pro-image', label: '别的生图', revision: 'other:1' },
    { kind: 'image', front: 'gpt-image-2', label: 'GPT Image 2（乐创）', revision: 'xlw-image-2:8' },
  ],
};

test('pickItem 只选生图类且优先精确匹配前台标识', () => {
  const h = helpers();
  assert.equal(h.pickItem(catalog.items).front, 'gpt-image-2');
  assert.equal(h.pickItem(catalog.items).revision, 'xlw-image-2:8');
  const renamed = { items: [{ kind: 'image', front: 'custom-front', label: '乐创 · Image 2', revision: 'c:3' }] };
  assert.equal(h.pickItem(renamed.items).front, 'custom-front', '前台标识改名后按标签含「乐创」兜底');
  assert.equal(h.pickItem([{ kind: 'xiaole_video', front: 'grok', label: '乐创视频' }]), null, '视频类不能当生图线路');
  assert.equal(h.pickItem([]), null);
  assert.equal(h.pickItem(null), null);
});

test('buildPayload 提交组合编号、锁单张、参考图按需带上', () => {
  const h = helpers();
  const item = h.pickItem(catalog.items);
  const choice = { id: 'c4', values: { size: '1024x1024', quality: 'medium', background: 'transparent' }, points: 16 };
  const textOnly = h.buildPayload(item, choice, '一枚戒指', []);
  assert.equal(textOnly.model, 'gpt-image-2');
  assert.equal(textOnly.count, 1);
  assert.equal(textOnly.prompt, '一枚戒指');
  assert.equal(JSON.stringify(textOnly.parameter_selection), JSON.stringify({ revision: 'xlw-image-2:8', combination: 'c4' }));
  assert.equal('reference_images' in textOnly, false, '无参考图时不带该字段');

  const withRefs = h.buildPayload(item, choice, '改背景', ['data:image/png;base64,AAA']);
  assert.equal(JSON.stringify(withRefs.reference_images), JSON.stringify(['data:image/png;base64,AAA']));
  const blank = h.buildPayload(item, null, 'x', []);
  assert.equal(blank.parameter_selection.combination, '');
});

test('生图页承载乐创卡片：默认隐藏、参数区互斥、点数来自契约', () => {
  assert.ok(/data-engine="lechuang"[^>]*aria-hidden="true"/.test(source), '乐创卡片应在引擎行且默认隐藏');
  assert.ok(/id="lechuangParamRows" hidden/.test(source), '乐创参数区默认隐藏');
  assert.ok(/id="legacyParamRows"/.test(source), '老参数行需要独立容器以便互斥切换');
  assert.ok(source.includes("legacyRows.hidden = isLechuang"), '选中乐创时要隐藏老参数行');
  assert.ok(source.includes("lcRows.hidden = !isLechuang"), '切回老引擎时要隐藏乐创参数区');
  assert.ok(source.includes("if(engine==='lechuang') return lechuangItem"), '参考图上限要按契约引用');
  assert.ok(source.includes("data-engine-cost')==='lechuang') return"), '乐创卡片点数不能被老定价表覆盖');
  assert.ok(source.includes('window.ChannelParameterControls.mount'), '复用共享参数控件渲染乐创选项');
});

test('提交走托管渠道且不被平台面板重定向', () => {
  assert.ok(source.includes("if(!(opts&&opts.managed)&&window.PublishedChannelParameters?.redirect"), '托管提交需跳过重定向');
  assert.ok(source.includes("submit(lechuangPayload,'乐创 · Image 2 "), '乐创引擎提交到 /api/gen/image');
  assert.ok(source.includes("'/api/gen/image',{managed:true}"), '乐创提交须标记 managed');
  assert.ok(source.includes("setInterval(loadLechuangLine,15000)"), '参数契约变更需 15 秒内自动同步');
  assert.ok(source.includes("if(!ok&&engine==='lechuang') selectEngine('gpt')"), '线路下线时要退回可用引擎');
});
