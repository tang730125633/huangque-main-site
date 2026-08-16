const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const shell = fs.readFileSync(path.join(root, 'site/workbench/cloud-shell.js'), 'utf8');
const workflow = fs.readFileSync(path.join(root, '.github/workflows/ci.yml'), 'utf8');
const banana = fs.readFileSync(path.join(root, 'site/workbench/banana.html'), 'utf8');
const video = fs.readFileSync(path.join(root, 'site/workbench/video.html'), 'utf8');
const audio = fs.readFileSync(path.join(root, 'site/workbench/audio.html'), 'utf8');

function readNavDisplayMode() {
  const match = shell.match(/function navDisplayMode\(active,narrow\)\{[^}]+\}/);
  assert.ok(match, 'cloud-shell.js must define navDisplayMode(active,narrow)');
  return new Function(`${match[0]}; return navDisplayMode;`)();
}

function readUsesFlushWorkspace() {
  const match = shell.match(/function usesFlushWorkspace\(active\)\{[^}]+\}/);
  assert.ok(match, 'cloud-shell.js must define usesFlushWorkspace(active)');
  return new Function(`${match[0]}; return usesFlushWorkspace;`)();
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

test('shared navigation animates from pointer position with reduced-motion fallback', () => {
  assert.match(shell, /function bindNavMotion\(aside\)/);
  assert.match(shell, /--hq-nav-x/);
  assert.match(shell, /pointermove/);
  assert.match(shell, /hq-nav-draw/);
  assert.match(shell, /hq-nav-seed-a/);
  assert.match(shell, /bindNavMotion\(aside\)/);
  assert.match(shell, /prefers-reduced-motion:reduce/);
});

test('compact and expanded account cards open the shared account menu', () => {
  const navDisplayMode = readNavDisplayMode();
  assert.equal((shell.match(/data-account-menu-trigger="1"/g) || []).length, 2);
  assert.match(shell, /class="danger" data-logout="1" role="menuitem"/);
  assert.equal(navDisplayMode('inspiration', false), 'expanded');
  assert.equal(navDisplayMode('canvas', true), 'expanded');
  assert.match(shell, /\.hq-aside-compact \.hq-user-copy,\.hq-aside-compact \.hq-user-logout\{display:none!important/);
  assert.match(shell, /function openAccountMenu\(anchor\)/);
});

test('signed-in users display their concrete membership tier', () => {
  assert.match(shell, /function membershipRoleName\(user\)/);
  assert.match(shell, /experience:'体验官',partner:'合伙人',initiator:'发起人'/);
  assert.match(shell, /var role=membershipRoleName\(u\)/);
  assert.doesNotMatch(shell, /\?'管理员':'会员'/);
});

test('point prices are visible and refresh on open pages', () => {
  assert.match(shell, /\{k:'pricing',l:'点数价格'/);
  assert.match(shell, /fetch\('\/api\/gen\/pricing'/);
  assert.match(shell, /pricingListeners\.forEach/);
  assert.match(shell, /,30000\)/);
});

test('generation routes alone use the flush workspace shell', () => {
  const usesFlushWorkspace = readUsesFlushWorkspace();
  for (const route of ['banana', 'video', 'audio']) assert.equal(usesFlushWorkspace(route), true, route);
  for (const route of ['canvas', 'settings', 'inspiration']) assert.equal(usesFlushWorkspace(route), false, route);
  assert.match(shell, /hq-main-scroll-flush/);
  assert.match(shell, /\.hq-main-scroll-flush\{overflow:hidden;padding:0\}/);
  assert.match(shell, /\.hq-topbar-flush\{border-bottom:0!important\}/);
  assert.match(shell, /header\.className='hq-topbar'\+\(usesFlushWorkspace\(active\)\?' hq-topbar-flush':''\)/);
  assert.match(shell, /@media\(max-width:1100px\)\{\.hq-main-scroll-flush\{overflow-y:auto\}\}/);
});

test('image video and audio workspaces fill the shell without breaking narrow layouts', () => {
  assert.match(banana, /\.hq-content\[data-active="banana"\]\{height:100%;min-height:0\}/);
  assert.match(banana, /\.banana-workspace\{[^}]*height:100%[^}]*border:0[^}]*border-radius:0[^}]*box-shadow:none/);
  assert.match(video, /\.hq-content\[data-active="video"\]\{height:100%;min-height:0\}/);
  assert.match(video, /\.gVid\{[^}]*height:100%[^}]*overflow:hidden/);
  assert.match(audio, /\.hq-content\[data-active="audio"\]\{height:100%;min-height:0\}/);
  assert.match(audio, /\.gAud\{[^}]*height:100%[^}]*overflow:hidden/);
  assert.match(video, /@media \(max-width:1100px\)\{\.gVid\{height:auto/);
  assert.match(audio, /@media \(max-width:1100px\)\{\.gAud\{height:auto/);
});

test('CI runs the compact sidebar regression suite', () => {
  assert.match(workflow, /node tests\/test_cloud_shell_sidebar\.js/);
});
