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

test('契约推导：比例分主次、清晰度与背景分组、尺寸反查比例', () => {
  const h = helpers();
  const item = { combinations: [
    { id: 'c1', values: { size: '1024x1024', quality: 'medium', background: 'opaque' }, points: 12 },
    { id: 'c2', values: { size: '1024x1024', quality: 'high', background: 'opaque' }, points: 15 },
    { id: 'c4', values: { size: '1024x1024', quality: 'medium', background: 'transparent' }, points: 16 },
    { id: 'c6', values: { size: '720x1280', quality: 'medium', background: 'opaque' }, points: 12 },
    { id: 'c9', values: { size: '1024x1280', quality: 'medium', background: 'opaque' }, points: 12 },
  ] };
  const f = h.facets(item);
  assert.equal(JSON.stringify(f.primary), JSON.stringify(['9:16', '1:1']), '老界面常用比例在前，按 9:16/1:1/16:9/3:4 排序');
  assert.equal(JSON.stringify(f.extra), JSON.stringify(['4:5']), '其余尺寸进「更多比例」');
  assert.equal(JSON.stringify(f.qualities), JSON.stringify(['medium', 'high']), '清晰度按 auto/low/medium/high 排序');
  assert.equal(JSON.stringify(f.backgrounds), JSON.stringify(['opaque', 'transparent']), '背景按 auto/opaque/transparent 排序');
  assert.equal(h.ratioOfSize('720x1280'), '9:16');
  assert.equal(h.ratioOfSize('1536x1024'), '3:2');
  assert.equal(h.facets(null).primary.length, 0, '没有契约时不产生选项');
});

test('choose 解析最近的合法组合（含无解回退）', () => {
  const h = helpers();
  const item = { combinations: [
    { id: 'c1', values: { size: '1024x1024', quality: 'medium', background: 'opaque' }, points: 12 },
    { id: 'c4', values: { size: '1024x1024', quality: 'medium', background: 'transparent' }, points: 16 },
    { id: 'c6', values: { size: '720x1280', quality: 'medium', background: 'opaque' }, points: 12 },
  ] };
  assert.equal(h.choose(item, { size: '1024x1024', quality: 'medium', background: 'transparent' }).id, 'c4', '完全匹配优先');
  const fallback = h.choose(item, { size: '720x1280', quality: 'medium', background: 'transparent' });
  assert.equal(fallback.id, 'c4', '透明底 9:16 无组合时回退到命中项最多的组合');
  assert.equal(JSON.stringify(h.wantedFromChoice(fallback)),
    JSON.stringify({ size: '1024x1024', quality: 'medium', background: 'transparent' }), '回退后选择项要跟着收敛');
  assert.equal(h.choose({ combinations: [] }, {}), null);
});

test('生图页承载乐创卡片：默认隐藏、参数区互斥、点数来自契约', () => {
  assert.ok(/data-engine="lechuang"[^>]*aria-hidden="true"/.test(source), '乐创卡片应在引擎行且默认隐藏');
  assert.ok(/id="lechuangParamRows" hidden/.test(source), '乐创参数区默认隐藏');
  assert.ok(/id="legacyParamRows"/.test(source), '老参数行需要独立容器以便互斥切换');
  assert.ok(source.includes("legacyRows.hidden = isLechuang"), '选中乐创时要隐藏老参数行');
  assert.ok(source.includes("lcRows.hidden = !isLechuang"), '切回老引擎时要隐藏乐创参数区');
  assert.ok(source.includes("if(engine==='lechuang') return lechuangItem"), '参考图上限要按契约引用');
  assert.ok(source.includes("data-engine-cost')==='lechuang') return"), '乐创卡片点数不能被老定价表覆盖');
});

test('乐创参数区用与其他引擎同款的卡片行布局', () => {
  assert.ok(/id="lechuangRatioRow"[^>]*repeat\(4,1fr\)/.test(source), '比例行与老引擎一样 4 列卡片');
  assert.ok(/id="lechuangQualityRow"[^>]*repeat\(2,1fr\)/.test(source), '清晰度行与老引擎一样两列卡片');
  assert.ok(/id="lechuangBackgroundRow"/.test(source), '背景行（透明底）同为卡片行');
  assert.ok(/id="lechuangCountRow"/.test(source), '生成数量行保留，固定一张与老布局对齐');
  assert.ok(source.includes('data-lc-ratio'), '比例卡片可点选');
  assert.ok(source.includes('data-lc-quality'), '清晰度卡片可点选');
  assert.ok(source.includes('data-lc-background'), '背景卡片可点选');
  assert.equal(source.includes('window.ChannelParameterControls.mount'), false, '乐创参数区不再使用下拉框控件');
  assert.ok(source.includes('lechuangControls={value:function(){return lechuangChoice;}}'), '提交路径仍通过 lechuangControls 取当前组合');
});

test('提交走托管渠道且不被平台面板重定向', () => {
  assert.ok(source.includes("if(!(opts&&opts.managed)&&window.PublishedChannelParameters?.redirect"), '托管提交需跳过重定向');
  assert.ok(source.includes("submit(lechuangPayload,'乐创 · Image 2 "), '乐创引擎提交到 /api/gen/image');
  assert.ok(source.includes("'/api/gen/image',{managed:true}"), '乐创提交须标记 managed');
  assert.ok(source.includes("setInterval(loadLechuangLine,15000)"), '参数契约变更需 15 秒内自动同步');
  assert.ok(source.includes("if(engine==='lechuang') selectEngine('gpt')"), '线路下线时要退回可用引擎');
});
