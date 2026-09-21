#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：定位纳米香蕉2的真实下单入口与参数。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import json
from content_domains import function_registry as fr

print("=== 1. image.banana.nb2.text 的入口 ===")
for p in fr.FUNCTION_REGISTRY:
    for f in (p.get('functions') or []):
        for m in (f.get('modes') or []):
            if m['key'] in ('image.banana.nb2.text','image.banana.pro.text','image.banana.nb2.reference'):
                print("  %s" % m['key'])
                for e in (m.get('entrypoints') or []):
                    print("     %-6s %s" % (e.get('method'), e.get('path')))
                print("     smoke_inputs: %s" % (m.get('smoke_inputs') or [])[:3])
                v = m.get('validation') or {}
                print("     prefill: %s" % json.dumps(v.get('prefill') or {}, ensure_ascii=False)[:300])
                print()

print("=== 2. 前台实际调用路径（从 nginx 与 imggen 反查）===")
print("  /api/gen/banana      → imggen(8101)  文生图/图生图")
print("  /api/gen/banana/health → 健康")
print("  /api/gen/job/{id}    → 任务查询（content 8096）")

print()
print("=== 3. imggen_api 的请求体字段（从源码提取）===")
import re
src = open('/home/ubuntu/content-api/imggen_api.py', encoding='utf-8').read()
i = src.find('def do_POST')
seg = src[i:i+4000]
for m in re.finditer(r'(?:body|payload|data)\.get\(["\'](\w+)["\']', seg):
    pass
keys = sorted(set(re.findall(r'\.get\(["\'](\w+)["\']', seg)))
print("  读到的字段:", keys[:40])
j = src.find('MODELS = ')
print("  ", src[j:j+120].split('\n')[0])
k = src.find('BASE_COST')
print("  ", src[k:k+200].split('\n')[0])
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
