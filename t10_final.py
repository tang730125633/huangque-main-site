# -*- coding: utf-8 -*-
"""乐创两条渠道真实验收（走前台真实接口 /api/gen/image）。"""
import http.cookiejar, json, os, ssl, time, urllib.error, urllib.request
BASE="https://huangquechuanmei.com"; USER="yuelei"; PASS=os.environ.get("HQ_TEST_PASS","")
OP="image.banana.nb2.text"; ctx=ssl.create_default_context()
cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
def call(method,path,body=None,timeout=240):
    r=urllib.request.Request(BASE+path,data=(json.dumps(body).encode() if body is not None else None),method=method)
    r.add_header("Content-Type","application/json"); r.add_header("User-Agent","hq-acc/1.0")
    try:
        with op.open(r,timeout=timeout) as resp: return resp.status, resp.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8","replace")
    except Exception as e: return None,"ERR %s: %s"%(type(e).__name__,e)
r=urllib.request.Request(BASE+"/api/auth/login",data=json.dumps({"username":USER,"password":PASS}).encode(),method="POST")
r.add_header("Content-Type","application/json"); op.open(r,timeout=60).read()
d=json.loads(call("GET","/api/admin/channel-manager",timeout=180)[1])
m0=next(x for x in d["operation_mappings"] if x["operation_id"]==OP); rev=int(m0["revision"])
print("已登录 %s ｜ 原始映射 r%s channels=%s" % (USER, rev, m0["channels"]))
rows=[]
for name,chans,front in [("乐创 GPT Image 2 生图",["xlw-image-2"],"gpt-image-2"),
                         ("乐创 GPT Image 2.5 生图",["xlw-image-25"],"gpt-image-2.5-flare")]:
    print("\n"+"="*72); print("候选: %s  →  front=%s" % (name, front))
    s,b=call("POST","/api/admin/channel-manager/operation-mapping",
             {"operation_id":OP,"state":"managed","channels":chans,"expected_revision":rev},timeout=120)
    if s!=200: print("  发布失败 HTTP %s %s"%(s,b[:120])); rows.append([name,"发布失败","-","-","-"]); continue
    rev+=1
    items=json.loads(call("GET","/api/gen/channel-parameters",timeout=120)[1]).get("items") or []
    it=next((x for x in items if x.get("front")==front),None)
    if not it: print("  参数接口无 front=%s"%front); rows.append([name,"无参数项","-","-","-"]); continue
    ch=(it.get("combinations") or [])[0]
    payload={"prompt":"纯白背景中央一个红色圆点，极简海报，渠道可用性验证，无文字无logo","count":1,
             "model":front,"parameter_selection":{"revision":it.get("revision"),"combination":ch.get("id")}}
    print("  下单体: model=%s revision=%s combination=%s points=%s" % (front,it.get("revision"),ch.get("id"),ch.get("points")))
    s,b=call("POST","/api/gen/image",payload,timeout=240)
    print("  下单 HTTP %s %s"%(s,b[:200]))
    try: jd=json.loads(b)
    except Exception: jd={}
    jid=jd.get("job_id") or jd.get("id")
    if not jid: rows.append([name,"下单失败",str(s),"-",b[:60]]); continue
    st="?"
    for _ in range(50):
        s,b=call("GET","/api/gen/job/%s"%jid,timeout=60)
        try: dd=json.loads(b)
        except Exception: dd={}
        st=str(dd.get("status") or "")
        if st in ("done","error","failed","refunded"): break
        time.sleep(6)
    dd=json.loads(call("GET","/api/gen/job/%s"%jid,timeout=60)[1]); res=dd.get("result") or {}
    print("  任务 %s status=%s provider=%s model=%s" % (jid,dd.get("status"),res.get("provider"),res.get("model")))
    print("  成品=%s" % str(res.get("url") or res.get("file"))[:90])
    if dd.get("error"): print("  错误=%s" % str(dd.get("error"))[:200])
    rows.append([name,"HTTP %s"%s,jid,dd.get("status"),"%s / %s"%(res.get("provider"),res.get("model")),"OK" if res.get("file") else str(dd.get("error"))[:70]])
    time.sleep(3)
s,b=call("POST","/api/admin/channel-manager/operation-mapping",
         {"operation_id":OP,"state":m0["state"],"channels":m0["channels"],"expected_revision":rev},timeout=120)
print("\n恢复原始映射: HTTP %s"%s)
print("\n%-26s %-10s %-7s %-8s %-32s %s"%("候选","下单","任务","状态","实际供应商 / 模型","结果"))
for r_ in rows: print("%-26s %-10s %-7s %-8s %-32s %s"%tuple((str(x)[:32] for x in r_)))
