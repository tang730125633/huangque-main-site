#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读核查第四批：从真实任务的 payload 里取出 _channel_binding（决定性证据）。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import json, sqlite3, time
def ts(v):
    try: return time.strftime('%m-%d %H:%M', time.localtime(float(v)))
    except Exception: return '-'

db = '/home/ubuntu/content-api/content_jobs.db'
c = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
c.row_factory = sqlite3.Row

print("=== R. 最近有渠道绑定的真实任务（image / 决定性证据）===")
rows = c.execute("""
    SELECT id, kind, username, status, cost, created_at, payload, result
    FROM jobs
    WHERE kind='image' AND deleted=0
    ORDER BY id DESC LIMIT 40
""").fetchall()
n = 0
for r in rows:
    try: p = json.loads(r['payload'] or '{}')
    except Exception: continue
    b = p.get('_channel_binding') or {}
    if not b: continue
    n += 1
    print("  任务 %-6s %s user=%-12s status=%-6s cost=%-4s" % (
        r['id'], ts(r['created_at']), str(r['username'])[:12], r['status'], r['cost']))
    print("        绑定渠道 = %-22s revision=%-4s 来源=%-10s" % (
        str(b.get('id'))[:22], b.get('mapping_revision'), b.get('invocation_source')))
    print("        渠道名   = %s" % str(b.get('name'))[:40])
    print("        接入点   = %s   模型=%s" % (str(b.get('base_url'))[:44], str(b.get('model'))[:26]))
    print("        功能 ID  = %s" % b.get('operation_id'))
    if n >= 12: break
if not n:
    print("  （最近 40 个 image 任务里没有带 _channel_binding 的）")

print()
print("=== S. 各 kind 最近的渠道绑定统计（近 200 个任务）===")
rows = c.execute("SELECT id, kind, payload, created_at, status FROM jobs WHERE deleted=0 ORDER BY id DESC LIMIT 200").fetchall()
from collections import Counter, defaultdict
agg = defaultdict(Counter)
latest = {}
for r in rows:
    try: p = json.loads(r['payload'] or '{}')
    except Exception: continue
    b = p.get('_channel_binding') or {}
    k = r['kind']
    if b:
        agg[k][str(b.get('id'))] += 1
        if k not in latest:
            latest[k] = (r['id'], ts(r['created_at']), b.get('id'), b.get('operation_id'), b.get('mapping_revision'), r['status'])
    else:
        agg[k]['(无绑定/原厂)'] += 1
for k in sorted(agg):
    print("  %-22s %s" % (k, dict(agg[k])))
print()
print("  各 kind 最近一条带绑定的任务:")
for k, v in sorted(latest.items()):
    print("    %-22s job=%-6s %s channel=%-20s op=%-26s rev=%-4s %s" % (k, v[0], v[1], str(v[2])[:20], str(v[3])[:26], v[4], v[5]))
c.close()
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
