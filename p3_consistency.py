#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核查：后台设定的第一位渠道 vs 实际生成时使用的渠道。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import json, sqlite3, time
from content_domains import channel_manager as cm, function_registry as fr

def ts(v):
    try: return time.strftime('%m-%d %H:%M', time.localtime(float(v)))
    except Exception: return '-'

print("=== 一、后台「渠道管理」里所有已发布的映射（第一位是谁）===")
mappings = {}
try:
    import admin_api
    ov = admin_api.channel_workspace_overview()
    for m in (ov.get('operation_mappings') or []):
        chans = m.get('channels') or []
        first = chans[0] if chans else ''
        mappings[m['operation_id']] = dict(state=m.get('state'), revision=m.get('revision'),
                                           channels=chans, first=first)
    for oid, m in mappings.items():
        names = []
        for cid in m['channels']:
            try:
                names.append(cm.version(cid).get('name'))
            except Exception:
                names.append(cid[:10])
        print("  %-30s state=%-9s r%-3s 第一位=%s" % (oid, m['state'], m['revision'], names[0] if names else '(无)'))
        if len(names) > 1:
            print("     后续: %s" % names[1:])
except Exception as e:
    print("  读取失败:", type(e).__name__, str(e)[:120])

print()
print("=== 二、每个功能最近的真实任务用的是哪条渠道 ===")
c = sqlite3.connect('file:/home/ubuntu/content-api/content_jobs.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row
rows = c.execute("""SELECT id,kind,username,status,cost,created_at,payload
                    FROM jobs WHERE deleted=0 ORDER BY id DESC LIMIT 400""").fetchall()
latest = {}
for r in rows:
    try: p = json.loads(r['payload'] or '{}')
    except Exception: continue
    clean = {k: v for k, v in p.items() if not k.startswith('_')}
    try: op = fr.classify_task(r['kind'], clean)
    except Exception: op = None
    if not op or op in latest: continue
    b = p.get('_channel_binding') or {}
    latest[op] = dict(job=r['id'], when=ts(r['created_at']), user=r['username'],
                      status=r['status'], channel=b.get('id'), name=b.get('name'),
                      version=b.get('version'), front=b.get('front') or b.get('model'))

print("  %-30s %-8s %-14s %-24s %s" % ("功能", "任务", "时间", "实际用的渠道", "状态"))
for oid, v in sorted(latest.items()):
    print("  %-30s %-8s %-14s %-24s %s" % (oid[:30], v['job'], v['when'],
          (v['name'] or '(无绑定/原厂)')[:24], v['status']))

print()
print("=== 三、对照：第一位 vs 实际使用 ===")
print("  %-30s %-24s %-24s %s" % ("功能", "后台第一位", "最近任务实际用的", "是否一致"))
for oid, m in mappings.items():
    v = latest.get(oid)
    first_name = ''
    if m['first']:
        try: first_name = cm.version(m['first']).get('name') or m['first'][:14]
        except Exception: first_name = m['first'][:14]
    else:
        first_name = '(未设主渠道→走原厂)'
    used = (v or {}).get('channel') or (v or {}).get('name')
    if not v:
        verdict = "无近期任务，无法对照"
    elif m['state'] == 'legacy':
        verdict = "一致（映射为 legacy，走原厂）" if not used else "⚠ 映射 legacy 但任务有绑定"
    elif used and m['first'] and str(used) == str(m['first']):
        verdict = "✅ 一致"
    elif used:
        verdict = "❌ 不一致"
    else:
        verdict = "⚠ 任务未绑定渠道（走了原厂）"
    print("  %-30s %-24s %-24s %s" % (oid[:30], first_name[:24], str(used or '-')[:24], verdict))

print()
print("=== 四、没有映射的功能（走原厂）===")
cat = fr.operation_catalog()
on = [o['operation_id'] for o in cat if o.get('channel_eligible')]
no_map = [o for o in on if o not in mappings]
print("  可切换功能 %d 个，其中已发布映射 %d 个，未发布 %d 个" % (len(on), len(mappings), len(no_map)))
for o in no_map[:12]:
    print("     %s" % o)
if len(no_map) > 12: print("     … 另有 %d 个" % (len(no_map)-12))
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
        print(e[-800:])
    c.close()


if __name__ == "__main__":
    main()
