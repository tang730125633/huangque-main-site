const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'site/workbench/banana.html'), 'utf8');

function readSplitPercent() {
  const match = html.match(/function splitPercent\(clientX,left,width\)\{[^}]+\}/);
  assert.ok(match, 'banana.html must define splitPercent(clientX,left,width)');
  return new Function(`${match[0]}; return splitPercent;`)();
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

test('split math starts at an exact half', () => {
  const splitPercent = readSplitPercent();
  assert.equal(splitPercent(600, 100, 1000), 50);
});

test('split math clamps pointer movement to 35 and 65 percent', () => {
  const splitPercent = readSplitPercent();
  assert.equal(splitPercent(-100, 100, 1000), 35);
  assert.equal(splitPercent(2000, 100, 1000), 65);
});

test('workspace is one frame with semantic left and right panes', () => {
  assert.match(html, /class="banana-workspace"/);
  assert.match(html, /class="banana-workspace-body"/);
  assert.match(html, /class="banana-settings-pane"/);
  assert.match(html, /class="[^"]*\bbanana-results-pane\b[^"]*"/);
  assert.match(html, /--left-pane:50%/);
});

test('desktop divider is an accessible separator', () => {
  assert.match(html, /id="workspaceDivider"/);
  assert.match(html, /role="separator"/);
  assert.match(html, /aria-orientation="vertical"/);
  assert.match(html, /aria-valuemin="35"/);
  assert.match(html, /aria-valuemax="65"/);
});

test('results pane offers accessible recent generated and recent works tabs', () => {
  assert.match(html, /id="recentGeneratedTab"[^>]*role="tab"[^>]*>最近生成<\/button>/);
  assert.match(html, /id="recentWorksTab"[^>]*role="tab"[^>]*>最近作品<\/button>/);
  assert.match(html, /id="recentGeneratedView"[^>]*role="tabpanel"/);
  assert.match(html, /id="recentWorksView"[^>]*role="tabpanel"/);
  assert.match(html, /function setResultView\(name\)/);
});

test('recent works requests and renders at most nine historical images', () => {
  assert.match(html, /\/api\/gen\/history\?limit=9/);
  assert.match(html, /d\.items\.slice\(0,9\)/);
});

test('divider supports pointer dragging and keyboard adjustment', () => {
  assert.match(html, /function initWorkspaceDivider\(\)/);
  assert.match(html, /addEventListener\('pointerdown'/);
  assert.match(html, /addEventListener\('pointermove'/);
  assert.match(html, /addEventListener\('pointerup'/);
  assert.match(html, /e\.key==='ArrowLeft'/);
  assert.match(html, /e\.key==='ArrowRight'/);
  assert.match(html, /e\.key==='Home'/);
});

test('narrow layouts stack panes and hide the divider', () => {
  assert.match(html, /@media \(max-width:1100px\)/);
  assert.match(html, /\.workspace-divider\{display:none\}/);
});

test('workspace and preview surfaces use shared theme tokens', () => {
  assert.match(html, /\.banana-workspace\{[^}]*border:1px solid var\(--hq-border\)[^}]*background:var\(--hq-surface\)/);
  assert.match(html, /\.result-preview\{[^}]*background:var\(--hq-surface-soft\)/);
});

test('light theme keeps the nested result pane inside the unified workspace', () => {
  assert.match(
    html,
    /html\[data-theme="light"\] \.hq-content\[data-active="banana"\] \.banana-results-pane\s*\{[^}]*background:\s*transparent\s*!important;[^}]*box-shadow:\s*none\s*!important;/
  );
});

test('banana-only theme fix does not require a shared stylesheet cache change', () => {
  assert.match(html, /theme\.css\?v=5873a2f4/);
});
