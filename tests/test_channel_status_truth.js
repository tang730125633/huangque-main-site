// 渠道状态真实性：绿灯必须有明确证据，红灯必须有真实失败原因，
// 没测 / 过期 / 配置变更 / 无法归属 / 不适用各自显示清楚，不能互相冒充。
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const src = fs.readFileSync(require('node:path').join(__dirname, '../site/admin/channel-catalog.js'), 'utf8');
// channel-catalog.js 把实现包在闭包里，按既有测试的做法切片取出需要的部分。
const _from = src.indexOf('  const VALIDITY_WINDOW');
const _to = src.indexOf('  // 配置维度');
assert.ok(_from > 0 && _to > _from, '取不到 resolveCheck/verificationStatus 片段');
const ctx = {Date: Date, JSON: JSON, Number: Number, String: String, Array: Array, Object: Object, Math: Math, console: console};
vm.createContext(ctx);
vm.runInContext(src.slice(_from, _to) + ';this.verificationStatus=verificationStatus;this.num=num;', ctx);
const vs = ctx.verificationStatus;
const NOW = 1800000000;

const chk = (kind, state, version, ageSec) => ({
  kind: kind, state: state, version: version,
  updated: NOW - (ageSec === undefined ? 60 : ageSec), detail: kind + ':' + state,
});
const chan = (version, checks, verification) => ({
  version: version, checks: checks, verification: verification, adapter: 'x',
});

test('三项全过 → 当前配置验证通过', () => {
  const c = chan(3, [chk('connection', 'passed', 3), chk('auth', 'passed', 3), chk('full', 'passed', 3)],
    ['connection', 'auth', 'full']);
  assert.equal(vs(c, NOW).overall.state, 'ok');
  assert.equal(vs(c, NOW).overall.label, '当前配置验证通过');
});

test('只过连接和鉴权 → 未完成生成验证（不是绿）', () => {
  const c = chan(3, [chk('connection', 'passed', 3), chk('auth', 'passed', 3)],
    ['connection', 'auth', 'full']);
  const o = vs(c, NOW).overall;
  assert.notEqual(o.state, 'ok');
  assert.equal(o.kind, 'full');
});

test('只有旧版本通过 → 配置已变更待验证（不是绿也不是失败）', () => {
  const c = chan(4, [chk('connection', 'passed', 3), chk('auth', 'passed', 3), chk('full', 'passed', 3)],
    ['connection', 'auth', 'full']);
  const o = vs(c, NOW).overall;
  assert.equal(o.state, 'stale-version');
  assert.notEqual(o.state, 'failed');
});

test('版本缺失或空值 → 无法归属，不能当通过', () => {
  [null, undefined, '', '  ', 'abc'].forEach((bad) => {
    const c = chan(3, [chk('connection', 'passed', bad), chk('auth', 'passed', bad), chk('full', 'passed', bad)],
      ['connection', 'auth', 'full']);
    const o = vs(c, NOW).overall;
    assert.notEqual(o.state, 'ok', '版本 ' + JSON.stringify(bad) + ' 不该判通过');
    assert.equal(o.state, 'unattributed');
  });
});

test('新的完整生成失败、旧的记录成功 → 显示失败', () => {
  const c = chan(3, [chk('connection', 'passed', 3), chk('auth', 'passed', 3),
    chk('full', 'passed', 3, 3600), chk('full', 'failed', 3, 10)],
  ['connection', 'auth', 'full']);
  const o = vs(c, NOW).overall;
  assert.equal(o.state, 'failed');
  assert.equal(o.kind, 'full');
});

test('验证过程中改了配置 → 结果归入被测版本', () => {
  const c = chan(9, [chk('connection', 'passed', 8), chk('auth', 'passed', 8), chk('full', 'passed', 8)],
    ['connection', 'auth', 'full']);
  assert.equal(vs(c, NOW).overall.state, 'stale-version');
});

test('证据过期 → 不判绿', () => {
  const c = chan(3, [chk('connection', 'passed', 3, 999999), chk('auth', 'passed', 3, 999999),
    chk('full', 'passed', 3, 999999)], ['connection', 'auth', 'full']);
  assert.notEqual(vs(c, NOW).overall.state, 'ok');
});

test('不适用的检测项单独说明，不参与判定也不伪造通过', () => {
  // HeyGen 场景：协议只要求 full，但历史里留了一条 connection failed
  const c = chan(5, [chk('full', 'passed', 5), chk('connection', 'failed', 5, 10)], ['full']);
  const v = vs(c, NOW);
  assert.equal(v.overall.state, 'ok', '不要求的连接失败不该把渠道判成异常');
  assert.equal(v.parts.connection.state, 'failed', '但那一项的事实仍然展示');
});

test('不适用项的新失败不能被当成「存在较新异常」', () => {
  const c = chan(5, [chk('full', 'passed', 5, 600), chk('connection', 'failed', 5, 1)], ['full']);
  assert.equal(vs(c, NOW).overall.state, 'ok');
});

test('某项正在验证 → 不是绿，也不是失败', () => {
  const c = chan(3, [chk('connection', 'passed', 3), chk('auth', 'passed', 3), chk('full', 'running', 3)],
    ['connection', 'auth', 'full']);
  const o = vs(c, NOW).overall;
  assert.notEqual(o.state, 'ok');
  assert.notEqual(o.state, 'failed');
  assert.ok(['running', 'queued'].indexOf(o.state) >= 0);
});

test('要求为空 → 未验证，不默认通过', () => {
  const c = chan(3, [chk('full', 'passed', 3)], []);
  assert.notEqual(vs(c, NOW).overall.state, 'ok');
});

test('num 不把空值或非数字转成有效版本', () => {
  const num = ctx.num;
  [null, undefined, '', '  ', 'abc', {}, [], true].forEach((bad) => {
    assert.equal(num(bad), null, JSON.stringify(bad) + ' 应为 null');
  });
  assert.equal(num(0), 0);
  assert.equal(num('2'), 2);
  assert.equal(num(2), 2);
});

test('协议不支持的项显示不适用；支持但仍会记录事实', () => {
  // HeyGen：协议只支持 full，历史里那条 connection failed 仍要能看到
  const c = chan(5, [chk('full', 'passed', 5), chk('connection', 'failed', 5, 10)], ['full']);
  const v = vs(c, NOW);
  assert.equal(v.parts.connection.state, 'failed');
  assert.equal(v.overall.state, 'ok');
});

test('规则校验：缺失/无效/协议未登记 → 不可用，不默认通过', () => {
  const cases = [
    {rules_known: true, verification: undefined},
    {rules_known: true, verification: []},
    {rules_known: true, verification: ['bogus']},
    {rules_known: false, verification: ['full']},
  ];
  cases.forEach((extra, i) => {
    const c = Object.assign(chan(3, [chk('full', 'passed', 3)], ['full']), {source: 'managed'}, extra);
    const o = vs(c, NOW).overall;
    assert.notEqual(o.state, 'ok', '第 ' + i + ' 种不该判通过');
    assert.equal(o.state, 'rules-unavailable');
  });
});
