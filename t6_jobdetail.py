import http.cookiejar, json, os, ssl, urllib.request
BASE="https://huangquechuanmei.com"; USER="yuelei"; PASS=os.environ.get("HQ_TEST_PASS","")
ctx=ssl.create_default_context()
cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
r=urllib.request.Request(BASE+"/api/auth/login", data=json.dumps({"username":USER,"password":PASS}).encode(), method="POST")
r.add_header("Content-Type","application/json")
op.open(r, timeout=60).read()
r=urllib.request.Request(BASE+"/api/gen/job/9719"); r.add_header("User-Agent","hq-acc/1.0")
d=json.loads(op.open(r, timeout=60).read().decode())
print("顶层字段:", sorted(d.keys()))
for k in ("id","kind","cost","status","created_at","updated_at"):
    print("  %-12s = %s" % (k, d.get(k)))
res=d.get("result") or {}
print("result 字段:", sorted(res.keys()))
print("result:", json.dumps(res, ensure_ascii=False)[:600])
for k in ("channel","provider","model","provider_id","channel_binding","_channel_binding"):
    if k in d: print("  job.%-18s = %s" % (k, str(d[k])[:200]))
    if k in res: print("  result.%-15s = %s" % (k, str(res[k])[:200]))
