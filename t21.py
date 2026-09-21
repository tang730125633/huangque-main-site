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
it=next(x for x in d["items"] if x["adapter"]=="heygen_mcp_video")
print("渠道 v%s enabled=%s" % (it["version"], it["enabled"]))
for k in ("connection","full"):
    s,b=call("POST","/api/admin/channel-manager/test",{"id":it["id"],"kind":k},t=120)
    print("  %-11s → HTTP %s %s" % (k, s, b[:70])); time.sleep(4)
time.sleep(150)
d=json.loads(call("GET","/api/admin/channel-manager",t=200)[1])
it=next(x for x in d["items"] if x["adapter"]=="heygen_mcp_video")
for r_ in (it.get("checks") or []):
    print("  %-11s %-9s %s" % (r_.get("kind"), r_.get("state"), str(r_.get("detail"))[:120]))
