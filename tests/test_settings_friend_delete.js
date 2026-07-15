const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(
  path.join(__dirname, '..', 'site', 'workbench', 'settings.html'),
  'utf8'
);

assert.match(html, /id="deleteFriendBtn"/);
assert.match(html, /id="friendDeleteConfirm"/);
assert.match(html, /id="confirmDeleteFriendBtn"/);
assert.match(html, /method\s*:\s*['"]DELETE['"]/);
assert.match(html, /\/api\/auth\/friends\//);
assert.match(html, /deleteFriendRemark\s*\(/);
assert.match(html, /activeFriendKey\s*=\s*['"]{2}/);
assert.match(html, /friendDeleteConfirm['"]\)\.contains\(e\.target\)/);

console.log('settings friend delete contract: ok');
