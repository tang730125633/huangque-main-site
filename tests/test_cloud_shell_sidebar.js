const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const shell = fs.readFileSync(path.join(root, 'site/workbench/cloud-shell.js'), 'utf8');
const workflow = fs.readFileSync(path.join(root, '.github/workflows/ci.yml'), 'utf8');

function readNavDisplayMode() {
  const match = shell.match(/function navDisplayMode\(active,narrow\)\{[^}]+\}/);
  assert.ok(match, 'cloud-shell.js must define navDisplayMode(active,narrow)');
  return new Function(`${match[0]}; return navDisplayMode;`)();
}

test('desktop Inspiration keeps the sidebar expanded', () => {
  const navDisplayMode = readNavDisplayMode();
  assert.equal(navDisplayMode('inspiration', false), 'expanded');
});

test('other desktop routes use the compact icon rail', () => {
  const navDisplayMode = readNavDisplayMode();
  for (const route of ['leads', 'banana', 'canvas', 'settings']) {
    assert.equal(navDisplayMode(route, false), 'compact', route);
  }
});

test('narrow viewports keep the full drawer on every route', () => {
  const navDisplayMode = readNavDisplayMode();
  assert.equal(navDisplayMode('inspiration', true), 'expanded');
  assert.equal(navDisplayMode('canvas', true), 'expanded');
});

test('generated navigation keeps semantic labels for compact mode', () => {
  assert.match(shell, /class="hq-nav-label"/);
  assert.match(shell, /data-nav-label=/);
  assert.match(shell, /aria-label=/);
});

test('compact shell styles cover the rail, footer, and reduced motion', () => {
  assert.match(shell, /function ensureNavStyles\(\)/);
  assert.match(shell, /\.hq-aside-compact/);
  assert.match(shell, /\.hq-side-points/);
  assert.match(shell, /prefers-reduced-motion/);
});

test('compact labels are bound to a floating hover and focus tooltip', () => {
  assert.match(shell, /function bindNavTooltips\(aside\)/);
  assert.match(shell, /hq-nav-tooltip/);
  assert.match(shell, /mouseenter/);
  assert.match(shell, /focusin/);
  assert.match(shell, /bindNavTooltips\(aside\)/);
});

test('compact mode keeps the logout control reachable', () => {
  assert.match(shell, /class="hq-user-logout" data-logout="1"/);
  assert.match(shell, /\.hq-aside-compact button\.hq-user-logout\{display:flex!important/);
});

test('CI runs the compact sidebar regression suite', () => {
  assert.match(workflow, /node tests\/test_cloud_shell_sidebar\.js/);
});
