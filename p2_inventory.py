#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读：枚举生产后台「渠道管理」页面实际展示的全部功能（唯一清单）。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import json
import admin_api
from content_domains import function_registry as fr, channel_capabilities as caps

ov = admin_api.channel_workspace_overview()
fm = ov.get("frontend_matrix") or {}
ops_by_id = {o["operation_id"]: o for o in (ov.get("all_operations") or [])}
ad = ov.get("adapters") or {}

print("=== 生产页面：分类 / 产品 / 模型 / 功能（唯一清单）===")
total = 0
lines = []
for p in (fm.get("pages") or []):
    print("\n▸ 分类：%s  (page=%s)" % (p.get("label"), p.get("page")))
    for pr in (p.get("products") or []):
        vis = "显示" if pr.get("visible") else "隐藏"
        print("   ├─ 产品：%s  [%s]" % (pr.get("label"), vis))
        for mo in (pr.get("models") or []):
            mv = "显示" if mo.get("visible") is not False else "隐藏"
            print("   │    ├─ 模型：%s  [%s]  actual=%s" % (mo.get("label"), mv, mo.get("actual_model")))
            for rt in (mo.get("routes") or []):
                oid = rt.get("operation_id")
                o = ops_by_id.get(oid) or {}
                elig = o.get("channel_eligible")
                kind = o.get("channel_kind") or "-"
                adps = o.get("channel_adapters") or []
                prim = (rt.get("primary") or {}).get("name") or "-"
                orig = (rt.get("original") or {}).get("name") or "-"
                total += 1
                print("   │    │    ├─ 功能 %-30s eligible=%-5s kind=%-13s 主渠道=%-22s 原厂=%s" % (
                    oid, elig, kind, prim[:22], orig[:20]))
                lines.append((p.get("page"), pr.get("label"), mo.get("label"), oid, elig, kind, adps))
print()
print("=== 统计 ===")
print("  页面功能总数（每条路由算一个）：%d" % total)
un = [l for l in lines if not l[4]]
print("  其中不可切换：%d" % len(un))
for l in un:
    o = ops_by_id.get(l[3]) or {}
    print("     %-32s kind=%-14s 原因=%s" % (l[3], l[5] or "-", str(o.get("channel_reason"))[:60]))
print()
print("=== 可用渠道按 kind ===")
kinds = {}
for it in (ov.get("items") or []):
    k = (ad.get(it.get("adapter")) or {}).get("kind")
    if k and it.get("enabled") and it.get("configured"):
        kinds.setdefault(k, []).append(it.get("name"))
for k in sorted(kinds):
    print("  %-14s %d 条: %s" % (k, len(kinds[k]), [x[:24] for x in kinds[k]]))
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
