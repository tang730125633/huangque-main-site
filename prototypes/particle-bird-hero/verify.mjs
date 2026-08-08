import { readFile, stat } from 'node:fs/promises';

const root = new URL('./', import.meta.url);
const [html, css, script, asset] = await Promise.all([
  readFile(new URL('index.html', root), 'utf8'),
  readFile(new URL('style.css', root), 'utf8'),
  readFile(new URL('experience.js', root), 'utf8'),
  stat(new URL('public/assets/bird-points.bin', root)),
]);

if (!html.includes('data-particle-stage') || !css.includes('prefers-reduced-motion')) throw new Error('missing page contract');
if ((html.match(/<section class="story/g) || []).length !== 6 || !html.includes('final-action')) throw new Error('missing six-act story');
if (!script.includes('ShaderMaterial') || !script.includes('uPointerStrength')) throw new Error('missing particle interaction');
if (!html.includes('data-cursor-ripple') || !css.includes('cursor-wave') || !script.includes('cursorRipple')) throw new Error('missing cursor ripple');
if (!script.includes('role < .025')) throw new Error('feather shaft density regressed');
for (const target of ['aFeather', 'aFlow', 'aFlock', 'aLogo']) {
  if (!script.includes(target)) throw new Error(`missing morph target: ${target}`);
}
if (asset.size !== 65536 * 3 * 4) throw new Error(`unexpected point asset size: ${asset.size}`);
console.log('particle-bird-hero verify: ok (65,536 points)');
