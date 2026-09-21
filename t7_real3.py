# -*- coding: utf-8 -*-
"""纳米香蕉2 三条候选渠道真实验收：发布映射→下单→轮询→取证。结束恢复原映射。"""
import http.cookiejar, json, os, ssl, sys, time, urllib.error, urllib.request
BASE="https://huangquechuanmei.com"; USER="yuelei"; PASS=os.environ.get("HQ_TEST_PASS","")
OP="image.banana.nb2.text"; ctx=ssl.create_default_context()
cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
def call(method, path, body=None, timeout=180):
    r=urllib.request.Request(BASE+path, data=(json.dumps(body).encode() if body is not None else None), method=method)
    r.add_header("Content-Type","application/json"); r.add_header("User-Agent","hq-acc/1.0")
    try:
        with op.open(r, timeout=timeout) as resp: return resp.status, resp.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8","replace")
    except Exception as e: return None, "ERR %s: %s" % (type(e).__name__, e)
r=urllib.request.Request(BASE+"/api/auth/login", data=json.dumps({"username":USER,"password":PASS}).encode(), method="POST")
r.add_header("Content-Type","application/json"); op.open(r, timeout=60).read()
print("已登录", USER)
s,b=call("GET","/api/admin/channel-manager", timeout=180); d=json.loads(b)
m0=next(x for x in d["operation_mappings"] if x["operation_id"]==OP)
print("原始映射: state=%s r%s channels=%s" % (m0["state"], m0["revision"], m0["channels"]))
rev=int(m0["revision"])
cases=[("乐创 GPT Image 2 生图","managed",["xlw-image-2"]),
       ("乐创 GPT Image 2.5 生图","managed",["xlw-image-25"]),
       ("官方 Gemini 原厂线路","legacy",[])]
rows=[]
for name,state,chans in cases:
    print("\n"+"="*70); print("候选: %s  state=%s channels=%s" % (name,state,chans))
    s,b=call("POST","/api/admin/channel-manager/operation-mapping",
             {"operation_id":OP,"state":state,"channels":chans,"expected_revision":rev}, timeout=120)
    print("  发布映射 HTTP %s %s" % (s, b[:140]))
    if s!=200: rows.append([name,"发布失败","-","-","-","-"]); continue
    rev+=1
    s,b=call("GET","/api/admin/channel-manager", timeout=180); d=json.loads(b)
    mc=next(x for x in d["operation_mappings"] if x["operation_id"]==OP)
    print("  服务端确认 r%s state=%s channels=%s" % (mc["revision"], mc["state"], mc["channels"]))
    payload={"provider":"banana","model":"nb2","prompt":"纯白背景中央一个红色圆点，极简海报风，用于渠道可用性验证，无文字无logo",
             "ratio":"1:1","quality":"std","count":1,"source_page":"banana"}
    s,b=call("POST","/api/gen/banana", payload, timeout=240)
    print("  下单 HTTP %s %s" % (s, b[:200]))
    try: jd=json.loads(b)
    except Exception: jd={}
    jid=jd.get("job_id") or jd.get("id")
    if not jid: rows.append([name,"下单失败","-","-","-","-"]); continue
    st="?"
    for _ in range(50):
        s,b=call("GET","/api/gen/job/%s"%jid, timeout=60)
        try: dd=json.loads(b)
        except Exception: dd={}
        st=str(dd.get("status") or "")
        if st in ("done","error","failed","refunded"): break
        time.sleep(6)
    s,b=call("GET","/api/gen/job/%s"%jid, timeout=60); dd=json.loads(b)
    res=dd.get("result") or {}
    print("  任务 %s status=%s provider=%s model=%s" % (jid, dd.get("status"), res.get("provider"), res.get("model")))
    print("  成品=%s  error=%s" % (str(res.get("file"))[:40], str(dd.get("error"))[:120]))
    rows.append([name, "HTTP %s"%s, jid, dd.get("status"), "%s / %s"%(res.get("provider"),res.get("model")), str(res.get("file") or dd.get("error"))[:44]])
    time.sleep(3)
print("\n"+"="*70); print("恢复原始映射")
s,b=call("POST","/api/admin/channel-manager/operation-mapping",
         {"operation_id":OP,"state":m0["state"],"channels":m0["channels"],"expected_revision":rev}, timeout=120)
print("  HTTP %s %s" % (s, b[:140]))
print("\n"+"="*70)
print("%-26s %-10s %-8s %-9s %-34s %s" % ("候选","下单","任务","状态","实际供应商/模型","成品或错误"))
for r_ in rows: print("%-26s %-10s %-8s %-9s %-34s %s" % tuple(str(x)[:34] for x in r_))
