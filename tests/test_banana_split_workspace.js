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

test('workspace header aligns its divider and result tabs with both panes', () => {
  const headerStart = html.indexOf('<header class="banana-workspace-head">');
  const headerEnd = html.indexOf('</header>', headerStart);
  const tabsStart = html.indexOf('<div class="result-tabs"', headerStart);
  assert.ok(headerStart >= 0 && headerEnd > headerStart, 'workspace header must exist');
  assert.ok(tabsStart > headerStart && tabsStart < headerEnd, 'result tabs must live in the workspace header');
  assert.match(html, /class="workspace-head-divider"/);
  assert.match(html, /\.banana-workspace-head\{[^}]*grid-template-columns:minmax\(0,var\(--left-pane\)\) 1px minmax\(0,1fr\)/);
  assert.match(html, /\.workspace-head-divider\{width:1px;background:var\(--hq-border\)\}/);
  assert.match(html, /document\.querySelector\('\.banana-workspace'\)/);
});

test('workspace header has no horizontal frame lines', () => {
  assert.match(html, /\.banana-workspace-head\{[^}]*border-bottom:0/);
  assert.match(html, /\.result-tabs\{[^}]*border-bottom:0/);
  assert.match(html, /\.result-tab\.on\{[^}]*border-bottom-color:#e7b24c/);
});

test('desktop divider is an accessible separator', () => {
  assert.match(html, /id="workspaceDivider"/);
  assert.match(html, /role="separator"/);
  assert.match(html, /aria-orientation="vertical"/);
  assert.match(html, /aria-valuemin="35"/);
  assert.match(html, /aria-valuemax="65"/);
  assert.match(html, /\.banana-workspace-body\{[^}]*grid-template-columns:minmax\(0,var\(--left-pane\)\) 1px minmax\(0,1fr\)/);
  assert.match(html, /\.workspace-divider\{[^}]*width:9px[^}]*margin-left:-4px[^}]*margin-right:-4px[^}]*background:transparent[^}]*border:0/);
  assert.match(html, /\.workspace-divider:before\{[^}]*left:4px[^}]*top:0[^}]*width:1px[^}]*height:100%/);
});

test('workspace header omits the split instruction copy', () => {
  assert.doesNotMatch(html, /默认 50 \/ 50 · 可拖动调整/);
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

test('flush workspace keeps shared theme surfaces without an outer card border', () => {
  assert.match(html, /\.banana-workspace\{[^}]*border:0[^}]*border-radius:0[^}]*background:var\(--hq-surface\)[^}]*box-shadow:none/);
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
