# -*- coding: utf-8 -*-
"""纳米香蕉2 三条候选：按前台真实流程测试（先取参数令牌，再下单）。"""
import http.cookiejar, json, os, ssl, time, urllib.error, urllib.request
BASE="https://huangquechuanmei.com"; USER="yuelei"; PASS=os.environ.get("HQ_TEST_PASS","")
OP="image.banana.nb2.text"; ctx=ssl.create_default_context()
cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
def call(method,path,body=None,timeout=180):
    r=urllib.request.Request(BASE+path,data=(json.dumps(body).encode() if body is not None else None),method=method)
    r.add_header("Content-Type","application/json"); r.add_header("User-Agent","hq-acc/1.0")
    try:
        with op.open(r,timeout=timeout) as resp: return resp.status, resp.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8","replace")
    except Exception as e: return None,"ERR %s: %s"%(type(e).__name__,e)
r=urllib.request.Request(BASE+"/api/auth/login",data=json.dumps({"username":USER,"password":PASS}).encode(),method="POST")
r.add_header("Content-Type","application/json"); op.open(r,timeout=60).read()
print("已登录",USER)
s,b=call("GET","/api/admin/channel-manager",timeout=180); d=json.loads(b)
m0=next(x for x in d["operation_mappings"] if x["operation_id"]==OP)
print("原始映射 r%s channels=%s" % (m0["revision"], m0["channels"])); rev=int(m0["revision"])

print("\n=== 前台参数接口返回什么 ===")
s,b=call("GET","/api/gen/channel-parameters",timeout=120)
print("  HTTP %s" % s)
d=json.loads(b) if s==200 else {}
items=d.get("items") or d.get("candidates") or []
print("  顶层键:", list(d.keys()))
print("  条目数:", len(items))
for it in items[:6]:
    ch=(it.get("choices") or it.get("combinations") or [])
    print("   front=%-18s revision=%-8s label=%-20s choices=%d" % (
        str(it.get("front"))[:18], str(it.get("revision"))[:8], str(it.get("label"))[:20], len(ch)))
    for c in ch[:2]:
        print("       %-8s %s  points=%s" % (c.get("id"), json.dumps(c.get("values") or {},ensure_ascii=False)[:60], c.get("points")))

rows=[]
for name,state,chans,front in [("乐创 GPT Image 2 生图","managed",["xlw-image-2"],"gpt-image-2"),
                               ("乐创 GPT Image 2.5 生图","managed",["xlw-image-25"],"gpt-image-2.5-flare")]:
    print("\n"+"="*70); print("候选:",name,"->",front)
    s,b=call("POST","/api/admin/channel-manager/operation-mapping",
             {"operation_id":OP,"state":state,"channels":chans,"expected_revision":rev},timeout=120)
    if s!=200: print("  发布失败 HTTP %s %s"%(s,b[:120])); rows.append([name,"发布失败","-","-","-"]); continue
    rev+=1
    s,b=call("GET","/api/gen/channel-parameters",timeout=120)
    items=json.loads(b).get("items") or []
    want=[x for x in items if x.get("front")==front]
    if not want: print("  参数接口无该 front（%s），现有: %s"%(front,[x.get('front') for x in items])); rows.append([name,"无参数项","-","-","-"]); continue
    it=want[0]; ch=(it.get("choices") or [])
    combo=ch[0] if ch else None
    payload={"prompt":"纯白背景中央一个红色圆点，极简海报，渠道验证用，无文字","count":1,"model":front,
             "parameter_selection":{"revision":it.get("revision"),"combination":combo.get("id") if combo else ""}}
    print("  下单体: model=%s revision=%s combination=%s points=%s" % (front,it.get("revision"),combo.get("id") if combo else None, combo.get("points") if combo else None))
    s,b=call("POST","/api/gen/banana",payload,timeout=240)
    print("  下单 HTTP %s %s"%(s,b[:180]))
    try: jd=json.loads(b)
    except Exception: jd={}
    jid=jd.get("job_id") or jd.get("id")
    if not jid: rows.append([name,"下单失败","-","-","-"]); continue
    st="?"
    for _ in range(50):
        s,b=call("GET","/api/gen/job/%s"%jid,timeout=60)
        try: dd=json.loads(b)
        except Exception: dd={}
        st=str(dd.get("status") or "")
        if st in ("done","error","failed","refunded"): break
        time.sleep(6)
    s,b=call("GET","/api/gen/job/%s"%jid,timeout=60); dd=json.loads(b); res=dd.get("result") or {}
    print("  任务 %s status=%s provider=%s model=%s" % (jid,dd.get("status"),res.get("provider"),res.get("model")))
    print("  成品=%s error=%s" % (str(res.get("file"))[:44], str(dd.get("error"))[:130]))
    rows.append([name,"HTTP %s"%s,jid,dd.get("status"),"%s / %s"%(res.get("provider"),res.get("model")),"OK" if res.get("file") else str(dd.get("error"))[:60]])
    time.sleep(3)
print("\n"+"="*70); print("恢复原始映射")
s,b=call("POST","/api/admin/channel-manager/operation-mapping",
         {"operation_id":OP,"state":m0["state"],"channels":m0["channels"],"expected_revision":rev},timeout=120)
print("  HTTP %s %s"%(s,b[:120]))
print("\n%-24s %-10s %-8s %-9s %-30s %s"%("候选","下单","任务","状态","实际供应商/模型","成品/错误"))
for r_ in rows: print("%-24s %-10s %-8s %-9s %-30s %s"%tuple(str(x)[:30] for x in r_))
