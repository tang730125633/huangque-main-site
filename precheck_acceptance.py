#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读核查：生产渠道切换验收的前置清单。不做任何写操作。"""
import sys
sys.path.insert(0, "server")

import paramiko

REMOTE = r'''
cd /home/ubuntu/content-api
set -a; . ./content.env 2>/dev/null; set +a
python3 - <<'PYEOF'
import os, json, time, subprocess
from content_domains import channel_manager as cm, function_registry as fr, channel_capabilities as caps

def ts(v):
    try: return time.strftime('%m-%d %H:%M', time.localtime(float(v)))
    except Exception: return '-'

print("=== A. 代码版本 ===")
for name, path in (("channel_capabilities.py","content_domains/channel_capabilities.py"),
                   ("channel_manager.py","content_domains/channel_manager.py"),
                   ("channel_runtime.py","content_domains/channel_runtime.py"),
                   ("frontend_channel_matrix.py","content_domains/frontend_channel_matrix.py"),
                   ("video.py","content_domains/video.py")):
    try:
        r = subprocess.run(["md5sum", path], capture_output=True, text=True, timeout=20)
        print("  %-30s %s" % (name, r.stdout.split()[0][:16] if r.stdout else "-"))
    except Exception as e:
        print("  %-30s ERR %s" % (name, e))

print()
print("=== B. imggen 是否已加载 HeyGen/音频页代码（关键：配音入口）===")
import admin_api
ov = admin_api.channel_workspace_overview()
fm = ov.get("frontend_matrix") or {}
pages = [p.get("page") for p in (fm.get("pages") or [])]
print("  前台矩阵页:", pages)
print("  audio 页存在:", "audio" in pages)

print()
print("=== C. 渠道清单（含可用性）===")
ad = ov.get("adapters") or {}
for it in (ov.get("items") or []):
    kind = (ad.get(it.get("adapter")) or {}).get("kind")
    print("  %-30s %-16s kind=%-13s enabled=%-5s configured=%-5s v%s" % (
        (it.get("name") or "")[:30], it.get("adapter"), kind,
        it.get("enabled"), it.get("configured"), it.get("version")))

print()
print("=== D. 按 kind 统计可用渠道（决定哪些功能能拖）===")
kinds = {}
for it in (ov.get("items") or []):
    k = (ad.get(it.get("adapter")) or {}).get("kind")
    if k and it.get("enabled") and it.get("configured"):
        kinds.setdefault(k, []).append(it.get("name"))
for k in sorted(kinds):
    print("  %-14s %d 条: %s" % (k, len(kinds[k]), [x[:22] for x in kinds[k]]))

print()
print("=== E. 可切换功能（channel_eligible）===")
cat = fr.operation_catalog()
on = [o for o in cat if o["channel_eligible"]]
print("  可切换 %d / %d" % (len(on), len(cat)))
for o in on:
    print("    %-32s kind=%-12s adapters=%s" % (o["operation_id"], o.get("channel_kind"), o.get("channel_adapters")))

print()
print("=== F. 现有生产映射（原始状态）===")
for o in on:
    m = cm.operation_mapping(o["operation_id"])
    if m:
        ch = [c.get("id") if isinstance(c, dict) else c for c in (m.get("channels") or [])]
        print("  %-32s state=%-9s r%-3s channels=%s" % (o["operation_id"], m.get("state"), m.get("revision"), ch))
        print("      display_order=%s  updated=%s" % (m.get("display_order"), ts(m.get("updated"))))

print()
print("=== G. 映射修订历史（最近 20 条）===")
try:
    import contextlib
    with contextlib.closing(cm.db()) as c:
        rows = c.execute("SELECT operation_id,revision,state,config,actor,created FROM operation_mapping_versions ORDER BY created DESC LIMIT 20").fetchall()
    for r in rows:
        cfg = json.loads(r["config"])
        ch = [x.get("id") if isinstance(x, dict) else x for x in (cfg.get("channels") or [])]
        print("  %s r%-3s %-9s %-30s %-24s %s" % (
            ts(r["created"]), r["revision"], r["state"], r["operation_id"][:30], str(ch)[:24], r["actor"]))
except Exception as e:
    print("  读取失败:", type(e).__name__, str(e)[:120])

print()
print("=== H. 最近任务实际用了哪条渠道 ===")
runs = ov.get("runs") or []
if not runs:
    print("  overview 未返回 runs")
for r in runs[:25]:
    print("  %s op=%-28s channel=%-24s state=%-8s rev=%s" % (
        ts(r.get("created") or r.get("updated")), (r.get("operation_id") or "-")[:28],
        (r.get("channel") or "-")[:24], r.get("state"), r.get("mapping_revision")))

print()
print("=== I. 证据可查性：任务→渠道绑定→供应商请求 ID ===")
print("  runs 字段:", sorted((runs[0] or {}).keys()) if runs else "-")

print()
print("=== J. PR #1630（音频页）是否已上线 ===")
print("  audio 页在矩阵里:", "audio" in pages)
tts = [o for o in on if o["operation_id"].startswith("audio.tts")]
for o in tts:
    m = cm.operation_mapping(o["operation_id"])
    print("  %-24s eligible=%s  mapping=%s" % (o["operation_id"], o["channel_eligible"], (m or {}).get("state", "无")))
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
        print(e[-2000:])
    c.close()


if __name__ == "__main__":
    main()
