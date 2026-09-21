#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读核查第六批：点数定价（用于估算测试预算）。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import json, os
print("=== X. 点数与金额的换算 ===")
try:
    from content_domains import pricing
    for n in dir(pricing):
        if n.isupper() and not n.startswith('_'):
            v = getattr(pricing, n)
            if isinstance(v, (int, float, str, dict, list)):
                print("  %-28s %s" % (n, str(v)[:120]))
except Exception as e:
    print("  pricing 读取失败:", type(e).__name__, str(e)[:120])

print()
print("=== Y. 各功能的点数成本（COST 表）===")
try:
    from content_domains import core
    for k in ('COST', 'VIDEO_COST', 'KIND_COST'):
        v = getattr(core, k, None)
        if v: print("  %s = %s" % (k, json.dumps(v, ensure_ascii=False)[:300]))
except Exception as e:
    print("  读取失败:", e)

print()
print("=== Z. 渠道自身的测试成本与日预算（用于估算）===")
import admin_api
ov = admin_api.channel_workspace_overview()
for it in (ov.get('items') or []):
    print("  %-30s test_cost=%-8s daily_limit=%-4s daily_budget=%-8s daily_test=%s" % (
        (it.get('name') or '')[:30], it.get('test_cost'), it.get('daily_limit'),
        it.get('daily_budget'), it.get('daily_test')))

print()
print("=== AA. 图片生成的默认点数（从最近任务看）===")
import sqlite3
c = sqlite3.connect('file:/home/ubuntu/content-api/content_jobs.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row
for r in c.execute("SELECT kind, cost, COUNT(*) n FROM jobs WHERE deleted=0 GROUP BY kind, cost ORDER BY kind, cost"):
    print("  %-24s cost=%-6s %d 单" % (r['kind'], r['cost'], r['n']))
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
        print(e[-1200:])
    c.close()


if __name__ == "__main__":
    main()
