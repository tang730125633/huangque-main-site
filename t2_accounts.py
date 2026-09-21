#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：查清计费机制与可用测试账号（点数余额）。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import json, sqlite3, glob, os

print("=== 1. yuelei 的账号状态（含计费开关）===")
for db in glob.glob('/home/ubuntu/auth-service/*.db') + glob.glob('/home/ubuntu/auth-service/data/*.db'):
    try:
        c = sqlite3.connect('file:%s?mode=ro' % db, uri=True); c.row_factory = sqlite3.Row
        tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tabs:
            if 'user' not in t.lower() and 'account' not in t.lower(): continue
            cols = [d[1] for d in c.execute('PRAGMA table_info(%s)' % t)]
            if 'username' not in cols: continue
            pick = [x for x in ('username','points','role','status','membership_tier','points_billing_enabled','balance') if x in cols]
            row = c.execute('SELECT %s FROM %s WHERE username=?' % (','.join(pick), t), ('yuelei',)).fetchone()
            if row:
                print("  %s :: %s" % (os.path.basename(db), t))
                print("   ", json.dumps(dict(row), ensure_ascii=False))
        c.close()
    except Exception as e:
        pass

print()
print("=== 2. 有正数点数的账号（前 15）===")
for db in glob.glob('/home/ubuntu/auth-service/*.db'):
    try:
        c = sqlite3.connect('file:%s?mode=ro' % db, uri=True); c.row_factory = sqlite3.Row
        tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tabs:
            cols = [d[1] for d in c.execute('PRAGMA table_info(%s)' % t)]
            if 'username' not in cols or 'points' not in cols: continue
            rows = c.execute('SELECT username, points, role FROM %s WHERE points > 0 ORDER BY points DESC LIMIT 15' % t).fetchall()
            if rows:
                print("  %s :: %s" % (os.path.basename(db), t))
                for r in rows:
                    print("    %-22s points=%-8s role=%s" % (r['username'], r['points'], r['role']))
        c.close()
    except Exception as e:
        pass

print()
print("=== 3. yuelei 最近的扣费记录（确认他平时怎么下单）===")
c = sqlite3.connect('file:/home/ubuntu/content-api/content_jobs.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row
for r in c.execute("SELECT id,kind,status,cost,created_at FROM jobs WHERE username='yuelei' AND deleted=0 ORDER BY id DESC LIMIT 10"):
    print("  job=%-6s kind=%-10s status=%-8s cost=%-5s" % (r['id'], r['kind'], r['status'], r['cost']))
c.close()

print()
print("=== 4. 后台是否按用户维度隔离渠道（有没有测试账号专用路由）===")
from content_domains import channel_manager as cm
import inspect
src = inspect.getsource(cm)
for kw in ('username', 'user_scope', 'test_user', 'allow_users'):
    hits = [l.strip()[:90] for l in src.split('\n') if kw in l and 'def ' not in l]
    print("  %-14s 出现 %d 次 %s" % (kw, len(hits), hits[:2]))
PYEOF
'''


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("129.204.166.13", username="ubuntu", password="@Tzl2004", timeout=30,
              look_for_keys=False, allow_agent=False, banner_timeout=30)
    _in, out, err = c.exec_command(REMOTE, timeout=300)
    print(out.read().decode("utf-8", "replace"))
    e = err.read().decode("utf-8", "replace")
    if e.strip():
        print("--- stderr ---")
        print(e[-1200:])
    c.close()


if __name__ == "__main__":
    main()
