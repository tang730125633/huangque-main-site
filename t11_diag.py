import http.cookiejar, json, os, ssl, urllib.request, urllib.error
BASE="https://huangquechuanmei.com"; USER="yuelei"; PASS=os.environ.get("HQ_TEST_PASS","")
ctx=ssl.create_default_context(); cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
def call(m,p,b=None,t=180):
    r=urllib.request.Request(BASE+p,data=(json.dumps(b).encode() if b is not None else None),method=m)
    r.add_header("Content-Type","application/json"); r.add_header("User-Agent","hq-acc/1.0")
    try:
        with op.open(r,timeout=t) as x: return x.status, x.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8","replace")
r=urllib.request.Request(BASE+"/api/auth/login",data=json.dumps({"username":USER,"password":PASS}).encode(),method="POST")
r.add_header("Content-Type","application/json"); op.open(r,timeout=60).read()
d=json.loads(call("GET","/api/admin/channel-manager")[1])
for m in d["operation_mappings"]:
    print("映射 %-24s state=%-8s r%-3s channels=%s" % (m["operation_id"],m["state"],m["revision"],m["channels"]))
print()
s,b=call("GET","/api/gen/channel-parameters")
d=json.loads(b)
print("顶层键:", list(d.keys()))
for it in (d.get("items") or []):
    print("── front=%s  label=%s" % (it.get("front"), it.get("label")))
    print("   全部键: %s" % sorted(it.keys()))
    print("   revision=%s  choices=%s  choice_count=%s" % (it.get("revision"), type(it.get("choices")).__name__, len(it.get("choices") or [])))
    print("   原始: %s" % json.dumps(it, ensure_ascii=False)[:600])
    print()
