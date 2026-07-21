const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const css = fs.readFileSync(
  path.join(__dirname, '..', 'site', 'workbench', 'canvas', 'canvas.css'),
  'utf8'
);

function pixelValue(selector, property) {
  const block = css.match(new RegExp(
    selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'
  ));
  assert.ok(block, `missing CSS block: ${selector}`);
  const value = block[1].match(new RegExp(`${property}\\s*:\\s*(\\d+)px`));
  assert.ok(value, `missing pixel value for ${selector} ${property}`);
  return Number(value[1]);
}

const cardHeight = pixelValue('.nc-board-card', 'height');
const thumbHeight = pixelValue('.nc-board-thumb', 'height');

// Title, timestamp, badges, their margins, and vertical padding need 88px.
assert.ok(
  cardHeight >= thumbHeight + 88,
  `board card is too short for metadata: ${cardHeight}px < ${thumbHeight + 88}px`
);

console.log('canvas board card layout: pass');
