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
assert.doesNotMatch(html, /昵称已保存在当前浏览器/);
assert.doesNotMatch(html, /好友申请已发送到本地预览/);
assert.match(html, /好友服务没有返回有效结果/);

console.log('settings friend delete contract: ok');
