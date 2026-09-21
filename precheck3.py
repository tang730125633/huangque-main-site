#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读核查第三批：真实任务的渠道绑定证据来源（run_snapshots / jobs）。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import os, json, time, sqlite3
def ts(v):
    try: return time.strftime('%m-%d %H:%M', time.localtime(float(v)))
    except Exception: return '-'

print("=== O. 渠道库里的表（PG routing schema）===")
from content_domains import channel_store
try:
    with channel_store._connect() as c:
        rows = c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='routing' ORDER BY table_name").fetchall()
    print("  ", [r[0] if not isinstance(r,dict) else list(r.values())[0] for r in rows])
except Exception as e:
    print("  连接失败:", type(e).__name__, str(e)[:200])
    print("  暴露的公开接口:", [x for x in dir(channel_store) if not x.startswith('__')][:40])

print()
print("=== P. run_snapshots：任务→渠道绑定（最近 20 条）===")
try:
    with channel_store._connect() as c:
        rows = c.execute("SELECT run_id, operation_id, channel, mapping_revision, invocation_source, created FROM routing.run_snapshots ORDER BY created DESC LIMIT 20").fetchall()
    for r in rows:
        d = dict(r) if not isinstance(r, tuple) else {}
        print("  %s %s" % (ts(d.get('created')), json.dumps({k:str(v)[:26] for k,v in d.items()}, ensure_ascii=False)))
except Exception as e:
    print("  查询失败:", type(e).__name__, str(e)[:200])

print()
print("=== Q. 任务库里的真实任务（content_jobs.db）===")
try:
    db = '/home/ubuntu/content-api/content_jobs.db'
    c = sqlite3.connect('file:%s?mode=ro' % db, uri=True); c.row_factory = sqlite3.Row
    tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tabs:
        if 'job' not in t.lower(): continue
        cols = [d[1] for d in c.execute('PRAGMA table_info(%s)' % t)]
        n = c.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
        print("  %-24s %6d 行  列=%s" % (t, n, cols[:14]))
    print()
    print("  -- 最近 15 个任务（含渠道/供应商字段）--")
    t = 'jobs' if 'jobs' in tabs else ('content_jobs' if 'content_jobs' in tabs else None)
    if t:
        cols = [d[1] for d in c.execute('PRAGMA table_info(%s)' % t)]
        pick = [x for x in ('id','job_id','kind','username','status','state','operation_id','channel','mapping_revision','provider_id','provider_task_id','created_at','updated_at','cost') if x in cols]
        q = 'SELECT %s FROM %s ORDER BY id DESC LIMIT 15' % (','.join(pick), t)
        for r in c.execute(q):
            d = dict(r)
            tstr = d.get('created_at') or d.get('updated_at')
            tshow = ts(tstr) if isinstance(tstr,(int,float)) else str(tstr)[:16]
            print("  %s " % tshow + " ".join("%s=%s" % (k, str(d.get(k))[:20]) for k in pick if k not in ('created_at','updated_at')))
    c.close()
except Exception as e:
    print("  读取失败:", type(e).__name__, str(e)[:200])
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
        print(e[-1500:])
    c.close()


if __name__ == "__main__":
    main()
