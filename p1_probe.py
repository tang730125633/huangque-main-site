#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第一阶段只读核查：验证记录版本绑定 / 任务详情可见性 / 渠道增改能力。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import json, inspect, time
from content_domains import channel_manager as cm, channel_store

print("=== 1. 验证记录是怎么写进去的（有没有带 version）===")
src = inspect.getsource(cm)
for kw in ('def note_check', 'def record_check', 'def _check', "checks'", '"checks"'):
    i = src.find(kw)
    if i < 0: continue
    seg = src[max(0,i-100):i+700]
    if 'INSERT' in seg or 'checks' in seg or 'def ' in seg[:120]:
        print("── %s ──" % kw)
        print(seg[:600])
        print()
        break

print("=== 2. channel_manager 里所有与 checks 有关的函数 ===")
for n in dir(cm):
    if 'check' in n.lower() and not n.startswith('__'):
        try:
            print("   %-34s %s" % (n, str(inspect.signature(getattr(cm,n)))[:70]))
        except Exception:
            print("   %-34s (非函数)" % n)

print()
print("=== 3. 写 checks 的语句（找 version 是否参与）===")
for m in [l.strip() for l in src.split('\n') if 'checks' in l and ('INSERT' in l or 'UPDATE' in l or 'json.dumps' in l)]:
    print("   %s" % m[:170])

print()
print("=== 4. channel_store 里对应的实现 ===")
src2 = inspect.getsource(channel_store)
for m in [l.strip() for l in src2.split('\n') if 'check' in l.lower() and ('def ' in l or 'version' in l)][:12]:
    print("   %s" % m[:170])

print()
print("=== 5. 后台任务详情的数据来源（管理员怎么看实际渠道）===")
import admin_api
srca = inspect.getsource(admin_api)
hits = [l.strip()[:150] for l in srca.split('\n') if 'run_snapshot' in l or '_channel_binding' in l or 'task_detail' in l]
for h in hits[:12]:
    print("   %s" % h)
PYEOF
'''


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect("129.204.166.13", username="ubuntu", password="@Tzl2004", timeout=30,
              look_for_keys=False, allow_agent=False, banner_timeout=30)
    _in, out, err = c.exec_command(REMOTE, timeout=240)
    print(out.read().decode("utf-8", "replace"))
    e = err.read().decode("utf-8", "replace")
    if e.strip():
        print("--- stderr ---")
        print(e[-800:])
    c.close()


if __name__ == "__main__":
    main()
