#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读核查第二批：映射修订历史（走 PG store）+ 真实用户任务证据。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import os, json, time
from content_domains import channel_manager as cm, channel_store

def ts(v):
    try: return time.strftime('%m-%d %H:%M', time.localtime(float(v)))
    except Exception: return '-'

print("=== K. 映射修订历史（channel_store，PG）===")
ops = ['image.banana.nb2.text','image.banana.pro.text','video.grok.text']
for op in ops:
    print("  -- %s --" % op)
    try:
        rows = channel_store.operation_mapping_versions(op) if hasattr(channel_store,'operation_mapping_versions') else None
    except Exception as e:
        rows = None
        print("    版本接口异常:", type(e).__name__, str(e)[:100])
    if rows:
        for r in rows[:8]:
            cfg = r.get('config') or {}
            ch = [x.get('id') if isinstance(x, dict) else x for x in (cfg.get('channels') or [])]
            print("     %s r%-3s %-9s channels=%-30s actor=%-12s" % (
                ts(r.get('created')), r.get('revision'), r.get('state'), str(ch)[:30], r.get('actor')))
    else:
        print("     （无版本行 / 接口不可用，改用 runs 反推）")

print()
print("=== L. 真实用户任务（非巡检）：取最近 40 条 runs 里 detail 不含巡检字样的 ===")
import admin_api
ov = admin_api.channel_workspace_overview()
runs = ov.get('runs') or []
print("  runs 总数:", len(runs))
seen = 0
for r in runs:
    d = str(r.get('detail') or '')
    if '巡检' in d or 'connection' in d.lower() or '健康' in d:
        continue
    seen += 1
    print("  %s job=%-8s op=%-28s channel=%-22s state=%-8s rev=%-4s provider_id=%s" % (
        ts(r.get('created') or r.get('updated')), r.get('job_id'), (r.get('operation_id') or '-')[:28],
        (r.get('channel') or '-')[:22], r.get('state'), r.get('mapping_revision'),
        str(r.get('provider_id'))[:18]))
    if seen >= 20: break
if not seen:
    print("  （近 100 条里没有真实用户任务，只有巡检）")

print()
print("=== M. 每个 kind 的巡检 vs 真实任务计数 ===")
from collections import Counter
c_all = Counter(); c_patrol = Counter()
for r in runs:
    k = r.get('kind') or '-'
    c_all[k] += 1
    d = str(r.get('detail') or '')
    if '巡检' in d or 'connection' in d.lower() or '健康' in d:
        c_patrol[k] += 1
for k in sorted(c_all):
    print("  %-14s 共 %-4s 其中巡检 %-4s 真实任务 %s" % (k, c_all[k], c_patrol[k], c_all[k]-c_patrol[k]))

print()
print("=== N. 现有映射的完整快照（用于回滚）===")
snap = {}
for op in ['image.banana.nb2.text','image.banana.pro.text','video.grok.text']:
    m = cm.operation_mapping(op)
    if m:
        snap[op] = {'state': m.get('state'), 'revision': m.get('revision'),
                    'channels': [c.get('id') if isinstance(c,dict) else c for c in (m.get('channels') or [])],
                    'display_order': m.get('display_order')}
print(json.dumps(snap, ensure_ascii=False, indent=2))
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
