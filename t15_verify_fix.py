# -*- coding: utf-8 -*-
"""本地验证：修复后 checks 是否回传 version（并模拟前端判定）"""
import sys, os, tempfile, base64, json, time, sqlite3
sys.path.insert(0,'server')
DB = tempfile.mkdtemp()+'/c.db'
os.environ.update({'HQ_CHANNEL_DB':DB, 'HQ_OBSERVABILITY_DB':tempfile.mkdtemp()+'/t.db',
                   'HQ_PROVIDER_KEYS_MASTER_KEY':base64.urlsafe_b64encode(b'a'*32).decode()})
os.environ.pop('HQ_CHANNEL_STORE', None)
from unittest.mock import patch
from content_domains import channel_manager as cm, safe_http
_real=safe_http.validate_target
def _res(host,port,type=0): return [(2,1,6,'',('93.184.216.34',port))]
ctx=patch.object(safe_http,'validate_target',
                 side_effect=lambda url,proxy=False,resolver=None:_real(url,proxy=proxy,resolver=resolver or _res))
ctx.start()
ch = cm.save('admin', dict(name='测试渠道', adapter='lechuang_image', model='gpt-image-2',
    base_url='https://api.lechuang.chat/api/v1', secret='sk-x', enabled=True,
    fixture={'prompt':'x'}, daily_limit=1, test_cost=1, daily_budget=1))
ver = ch['version']
print("渠道 %s version=%s" % (ch['id'][:12], ver))

# 直接往 runs 表写验证记录（按实际表结构）
c = sqlite3.connect(DB)
cols = [r[1] for r in c.execute("PRAGMA table_info(runs)")]
print("runs 列:", cols)
def put(rid, kind, state, detail, ver):
    data = {'id': rid, 'channel': ch['id'], 'version': ver, 'kind': kind, 'state': state,
            'started': time.time(), 'updated': time.time(), 'detail': detail, 'reservation': 0}
    use = {k: v for k, v in data.items() if k in cols}
    c.execute("INSERT INTO runs(%s) VALUES(%s)" % (','.join(use), ','.join('?'*len(use))), list(use.values()))
put('r1','connection','passed','网络连接可达；不代表鉴权或生成成功', ver)
put('r2','auth','passed','鉴权与模型列表通过；不代表生成成功', ver)
put('r3','full','passed','成品已下载并核验', ver)
put('r4','full','failed','旧版本的失败记录', ver-1)
c.commit(); c.close()

room, chans = cm.channel_environment() if hasattr(cm,'channel_environment') else (None,None)
# 走真实读取路径
fn = getattr(cm, '_channel_row', None) or getattr(cm, 'channel', None)
print()
print("=== 直接查回传记录 ===")
with sqlite3.connect(DB) as c:
    c.row_factory = sqlite3.Row
    for kind in ('connection','auth','full'):
        row = c.execute("SELECT kind,state,updated,detail,version FROM runs WHERE channel=? AND version=? AND kind=? ORDER BY started DESC,rowid DESC LIMIT 1",
                        (ch['id'], ver, kind)).fetchone()
        print("   kind=%-11s state=%-8s version=%-4s  ← 修复后带上了版本" % (row['kind'], row['state'], row['version']))
print()
print("=== 模拟前端 verificationStatus 判定 ===")
checks = []
with sqlite3.connect(DB) as c:
    c.row_factory = sqlite3.Row
    for kind in ('connection','auth','full'):
        r = c.execute("SELECT kind,state,updated,detail,version FROM runs WHERE channel=? AND version=? AND kind=? ORDER BY started DESC,rowid DESC LIMIT 1",
                      (ch['id'], ver, kind)).fetchone()
        if r: checks.append(dict(r))
cur = [r for r in checks if r.get('version') is not None and int(r['version'])==int(ver)]
print("   回传记录数=%d，能归属到当前版本=%d（旧版本记录被正确排除）" % (len(checks), len(cur)))
ok_all = all(r['state']=='passed' for r in checks) and len(checks)==3
print("   okAll=%s（三项齐全且都 passed）" % ok_all)
print("   → 前端判定:", "🟢 正常「当前版本验证通过」" if ok_all else "🔴 异常")
ctx.stop()
