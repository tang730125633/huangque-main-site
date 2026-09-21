#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读核查第五批：为什么最近的真实任务没有渠道绑定。"""
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

c = sqlite3.connect('file:/home/ubuntu/content-api/content_jobs.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row

print("=== T. 最近 8 个 image 任务的 payload 关键字段 ===")
rows = c.execute("SELECT id, kind, username, status, created_at, payload FROM jobs WHERE kind='image' AND deleted=0 ORDER BY id DESC LIMIT 8").fetchall()
for r in rows:
    try: p = json.loads(r['payload'] or '{}')
    except Exception: p = {}
    keys = [k for k in p.keys() if not k.startswith('__')]
    print("  任务 %s %s user=%s status=%s" % (r['id'], ts(r['created_at']), r['username'], r['status']))
    print("     payload 键: %s" % keys)
    for k in ('model','provider','source_page','mode','want','operation','image','size','resolution','_channel_binding'):
        if k in p:
            v = p[k]
            print("       %-16s = %s" % (k, str(v)[:70]))

print()
print("=== U. classify_task 会把它们归到哪个功能（决定有没有映射可查）===")
from content_domains import function_registry as fr, channel_manager as cm
for r in rows[:8]:
    try: p = json.loads(r['payload'] or '{}')
    except Exception: continue
    clean = {k: v for k, v in p.items() if not k.startswith('_')}
    try:
        op = fr.classify_task('image', clean)
    except Exception as e:
        op = 'ERR:%s' % type(e).__name__
    m = cm.operation_mapping(op) if op else None
    print("  任务 %-6s → op=%-30s 映射=%s" % (
        r['id'], str(op)[:30],
        ("%s r%s %s" % (m.get('state'), m.get('revision'), [x.get('id') if isinstance(x,dict) else x for x in (m.get('channels') or [])])) if m else "无"))

print()
print("=== V. 任务 9493（唯一带绑定的）为什么有绑定 ===")
r = c.execute("SELECT id,payload FROM jobs WHERE id=9493").fetchone()
if r:
    p = json.loads(r['payload'] or '{}')
    b = p.get('_channel_binding') or {}
    print("  payload 键:", [k for k in p.keys()])
    print("  _channel_binding:", json.dumps(b, ensure_ascii=False)[:400])

print()
print("=== W. 该功能映射的最新修订（r6）是什么时候、什么状态 ===")
for op in ('image.banana.nb2.text','image.banana.nb2.reference','image.openai.text','image.xiaole.text'):
    m = cm.operation_mapping(op)
    if m:
        print("  %-30s %s r%s channels=%s updated=%s" % (
            op, m.get('state'), m.get('revision'),
            [x.get('id') if isinstance(x,dict) else x for x in (m.get('channels') or [])], ts(m.get('updated'))))
    else:
        print("  %-30s 无映射" % op)
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
