# -*- coding: utf-8 -*-
"""配音渠道真实验收：把 audio.tts.public 切到新渠道，真实下单，取证，恢复。"""
import http.cookiejar, json, os, ssl, time, urllib.error, urllib.request
BASE="https://huangquechuanmei.com"; USER="yuelei"; PASS=os.environ.get("HQ_TEST_PASS","")
OP="audio.tts.public"; ctx=ssl.create_default_context(); cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
def call(m,p,b=None,t=240):
    r=urllib.request.Request(BASE+p,data=(json.dumps(b).encode() if b is not None else None),method=m)
    r.add_header("Content-Type","application/json"); r.add_header("User-Agent","hq-acc/1.0")
    try:
        with op.open(r,timeout=t) as x: return x.status, x.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8","replace")
    except Exception as e: return None,"ERR %s: %s"%(type(e).__name__,e)
r=urllib.request.Request(BASE+"/api/auth/login",data=json.dumps({"username":USER,"password":PASS}).encode(),method="POST")
r.add_header("Content-Type","application/json"); op.open(r,timeout=60).read()
print("已登录",USER)
# 找配音渠道 id
d=json.loads(call("GET","/api/admin/channel-manager")[1])
voice_ch=next(x for x in d["items"] if x["adapter"]=="cosyvoice_tts")
print("配音渠道: %s  id=%s  enabled=%s configured=%s" % (voice_ch["name"], voice_ch["id"], voice_ch["enabled"], voice_ch["configured"]))
# 音色列表
s,b=call("GET","/api/gen/audio/voices")
try: vs=json.loads(b)
except Exception: vs={}
pub=[v for v in (vs.get("items") or vs.get("voices") or []) if (v.get("scope")=="public")]
pick=pub[0] if pub else None
print("公共音色: %d 个，选用 voice_key=%s provider_voice=%s" % (len(pub), (pick or {}).get("voice_key"), (pick or {}).get("provider_voice")))
if not pick:
    print("没有公共音色，停止"); raise SystemExit(1)

# 记录原始映射
m0=None
for m in d["operation_mappings"]:
    if m["operation_id"]==OP: m0=m
print("原始映射:", m0)
rev = int((m0 or {}).get("revision") or 0)

print("\n=== 切换：把配音渠道设为第一位 ===")
s,b=call("POST","/api/admin/channel-manager/operation-mapping",
         {"operation_id":OP,"state":"managed","channels":[voice_ch["id"]],"expected_revision":rev},t=120)
print("  发布 HTTP %s %s" % (s, b[:150]))
if s==200: rev+=1

print("\n=== 真实下单 ===")
payload={"text":"黄雀配音渠道可用性验收测试","voice":pick.get("voice_key"),"speed":1.0,"pitch":0,"volume":0}
s,b=call("POST","/api/gen/audio",payload,t=240)
print("  下单 HTTP %s %s" % (s, b[:200]))
jid=(json.loads(b) if s==200 else {}).get("job_id")
st="?"
if jid:
    for _ in range(40):
        s,b=call("GET","/api/gen/job/%s"%jid,t=60)
        try: dd=json.loads(b)
        except Exception: dd={}
        st=str(dd.get("status") or "")
        if st in ("done","error","failed","refunded"): break
        time.sleep(5)
    dd=json.loads(call("GET","/api/gen/job/%s"%jid,t=60)[1]); res=dd.get("result") or {}
    print("  任务 %s status=%s" % (jid, dd.get("status")))
    print("  provider=%s model=%s voice=%s" % (res.get("provider"), res.get("model"), res.get("voice")))
    print("  成品=%s" % str(res.get("url") or res.get("file"))[:100])
    if dd.get("error"): print("  错误=%s" % str(dd.get("error"))[:200])

print("\n=== 恢复原始映射 ===")
if m0:
    s,b=call("POST","/api/admin/channel-manager/operation-mapping",
             {"operation_id":OP,"state":m0["state"],"channels":m0["channels"],"expected_revision":rev},t=120)
    print("  HTTP %s %s" % (s, b[:120]))
else:
    s,b=call("POST","/api/admin/channel-manager/operation-mapping",
             {"operation_id":OP,"state":"legacy","channels":[],"expected_revision":rev},t=120)
    print("  （原本无映射，恢复为 legacy）HTTP %s" % s)
