# -*- coding: utf-8 -*-
import http.cookiejar, json, os, ssl, time, urllib.error, urllib.request
BASE="https://huangquechuanmei.com"; USER="yuelei"; PASS=os.environ.get("HQ_TEST_PASS","")
ctx=ssl.create_default_context(); cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
def call(m,p,b=None,t=200):
    r=urllib.request.Request(BASE+p,data=(json.dumps(b).encode() if b is not None else None),method=m)
    r.add_header("Content-Type","application/json"); r.add_header("User-Agent","hq-acc/1.0")
    try:
        with op.open(r,timeout=t) as x: return x.status, x.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8","replace")
    except Exception as e: return None,"ERR %s: %s"%(type(e).__name__,e)
r=urllib.request.Request(BASE+"/api/auth/login",data=json.dumps({"username":USER,"password":PASS}).encode(),method="POST")
r.add_header("Content-Type","application/json"); op.open(r,timeout=60).read()
d=json.loads(call("GET","/api/admin/channel-manager",t=200)[1])
targets=[it for it in d["items"] if it["adapter"] in ("cosyvoice_tts","wavespeed_tryon")]
print("=== 重跑完整生成 ===")
for it in targets:
    s,b=call("POST","/api/admin/channel-manager/test",{"id":it["id"],"kind":"full"},t=120)
    print("  %-28s v%s → HTTP %s %s" % (it["name"][:28], it["version"], s, b[:80]))
    time.sleep(5)
print("\n=== 等待（配音快，换装可能几分钟）===")
for i in range(30):
    time.sleep(15)
    d=json.loads(call("GET","/api/admin/channel-manager",t=200)[1])
    done=True
    for it in d["items"]:
        if it["adapter"] not in ("cosyvoice_tts","wavespeed_tryon"): continue
        p={r["kind"]:r["state"] for r in (it.get("checks") or [])}
        if p.get("full") in (None,"queued","running"): done=False
    print("  [%3ds] %s" % ((i+1)*15, "全部完成" if done else "进行中…"))
    if done: break
print("\n=== 结果 ===")
for it in d["items"]:
    p={r["kind"]:r for r in (it.get("checks") or [])}
    if it["adapter"] in ("cosyvoice_tts","wavespeed_tryon"):
        f=p.get("full") or {}
        print("  %-28s v%s  connection=%-8s full=%-8s" % (
            it["name"][:28], it["version"], (p.get("connection") or {}).get("state") or '-', f.get("state") or '-'))
        print("      full 详情: %s" % str(f.get("detail"))[:160])
