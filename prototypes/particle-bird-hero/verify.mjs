import { readFile, stat } from 'node:fs/promises';

const root = new URL('./', import.meta.url);
const [html, css, script, asset] = await Promise.all([
  readFile(new URL('index.html', root), 'utf8'),
  readFile(new URL('style.css', root), 'utf8'),
  readFile(new URL('experience.js', root), 'utf8'),
  stat(new URL('public/assets/bird-points.bin', root)),
]);

if (!html.includes('data-particle-stage') || !css.includes('prefers-reduced-motion')) throw new Error('missing page contract');
if (!script.includes('ShaderMaterial') || !script.includes('uPointerStrength')) throw new Error('missing particle interaction');
if (asset.size !== 65536 * 3 * 4) throw new Error(`unexpected point asset size: ${asset.size}`);
console.log('particle-bird-hero verify: ok (65,536 points)');
