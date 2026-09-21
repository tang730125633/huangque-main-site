#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决定性核查：现在建单，运行时会把任务绑到哪条渠道（与后台第一位比对）。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import json
from content_domains import channel_manager as cm

import admin_api
ov = admin_api.channel_workspace_overview()
items = {it['id']: it for it in (ov.get('items') or [])}
mappings = {m['operation_id']: m for m in (ov.get('operation_mappings') or [])}

def name_of(cid):
    if not cid: return '(原厂)'
    it = items.get(cid)
    return (it['name'] if it else cid[:14])

print("=== 后台第一位  vs  运行时解析出的主渠道 ===")
print("  %-30s %-26s %-26s %s" % ("功能", "后台第一位", "运行时主渠道", "一致"))
for oid, m in sorted(mappings.items()):
    first = (m.get('channels') or [''])[0]
    route = cm.operation_mapping(oid) or {}
    if route.get('state') == 'legacy':
        resolved = ''
    elif route.get('state') == 'paused':
        resolved = '__paused__'
    else:
        resolved = route.get('channel') or ''
    ok = ('✅' if str(resolved) == str(first) and route.get('state') == m.get('state')
          else '❌')
    note = ' (已暂停)' if resolved == '__paused__' else ''
    print("  %-30s %-26s %-26s %s%s" % (
        oid[:30], name_of(first)[:26], name_of(resolved if resolved != '__paused__' else '')[:26], ok, note))

print()
print("=== 每个「有映射」的功能：运行时选中的渠道明细 ===")
for oid, m in sorted(mappings.items()):
    route = cm.operation_mapping(oid) or {}
    cid = route.get('channel') or ''
    print("  %s" % oid)
    print("     映射状态 : %s  r%s" % (route.get('state'), route.get('revision')))
    print("     channels : %s" % [name_of(c) for c in (route.get('channels') or [])])
    print("     display  : %s" % (route.get('display_order') or '(未设)'))
    if cid:
        v = cm.version(cid)
        print("     运行时主渠道: %s   adapter=%s  model=%s  base=%s" % (
            v.get('name'), v.get('adapter'), v.get('model'), (v.get('base_url') or '')[:44]))
    else:
        print("     运行时主渠道: (原厂线路)")
    print()

print("=== 没有映射的可切换功能（建单必然走原厂）===")
on = [o['operation_id'] for o in (ov.get('all_operations') or []) if o.get('channel_eligible')]
no_map = [o for o in on if o not in mappings]
print("  可切换 %d 个：有映射 %d，无映射 %d" % (len(on), len(mappings), len(no_map)))
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
