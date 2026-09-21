# -*- coding: utf-8 -*-
"""把后台所有「异常/未验证」的渠道逐个验证（走后台同一接口）。"""
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
print("已登录", USER)

def state():
    d=json.loads(call("GET","/api/admin/channel-manager",t=200)[1])
    out={}
    for it in d.get("items") or []:
        parts={r.get("kind"):r.get("state") for r in (it.get("checks") or [])}
        out[it["id"]]=dict(name=it["name"], version=it["version"], enabled=it["enabled"], parts=parts)
    return d,out

d,st=state()
print("\n=== 验证前 ===")
for cid,v in st.items():
    p=v["parts"]
    print("  %-30s v%-3s conn=%-9s auth=%-9s full=%-9s" % (
        v["name"][:30], v["version"], p.get("connection") or '-', p.get("auth") or '-', p.get("full") or '-'))

# 逐条补齐缺的项；full 只在「完全没有记录」或 unknown 时跑
PLAN=[]
for cid,v in st.items():
    if not v["enabled"]: continue
    p=v["parts"]
    if not p.get("connection"): PLAN.append((cid,v["name"],"connection",False))
    if not p.get("auth"):       PLAN.append((cid,v["name"],"auth",False))
    if p.get("full") in (None,"unknown"): PLAN.append((cid,v["name"],"full",True))

paid=sum(1 for _,_,k,ch in PLAN if ch)
print("\n=== 计划执行 %d 项（其中 %d 项收费）===" % (len(PLAN), paid))
for cid,n,k,ch in PLAN:
    print("   %-30s %-11s %s" % (n[:30], k, "收费" if ch else "免费"))

print("\n=== 开始执行 ===")
for cid,n,k,ch in PLAN:
    s,b=call("POST","/api/admin/channel-manager/test",{"id":cid,"kind":k},t=120)
    print("  %-30s %-11s → HTTP %s %s" % (n[:30], k, s, b[:90]))
    time.sleep(8)
print("\n=== 等待执行完成 ===")
for i in range(24):
    time.sleep(15)
    d,cur=state()
    done=True
    for cid,n,k,ch in PLAN:
        p=cur.get(cid,{}).get("parts",{})
        if p.get(k) in (None,"queued","running"): done=False
    print("  [%3ds] 已完成 %s" % ((i+1)*15, "全部" if done else "进行中…"))
    if done: break

print("\n=== 验证后 ===")
for cid,v in cur.items():
    p=v["parts"]
    ok=all(p.get(x)=="passed" for x in ("connection","auth","full"))
    print("  %s %-30s v%-3s conn=%-9s auth=%-9s full=%-9s" % (
        "🟢" if ok else "🔴", v["name"][:30], v["version"],
        p.get("connection") or '-', p.get("auth") or '-', p.get("full") or '-'))
