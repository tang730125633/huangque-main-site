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
for it in targets:
    for k in ("connection","full"):
        s,b=call("POST","/api/admin/channel-manager/test",{"id":it["id"],"kind":k},t=120)
        print("  %-26s v%s %-11s → HTTP %s %s" % (it["name"][:26], it["version"], k, s, b[:70]))
        time.sleep(4)
print("\n=== 60 秒后查结果 ===")
time.sleep(60)
d=json.loads(call("GET","/api/admin/channel-manager",t=200)[1])
for it in d["items"]:
    if it["adapter"] not in ("cosyvoice_tts","wavespeed_tryon"): continue
    p={r["kind"]:r for r in (it.get("checks") or [])}
    print("── %-26s v%s" % (it["name"][:26], it["version"]))
    for k in ("connection","full"):
        r_=p.get(k) or {}
        print("     %-11s %-9s %s" % (k, r_.get("state") or '-', str(r_.get("detail"))[:90]))
