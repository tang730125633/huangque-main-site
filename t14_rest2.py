# -*- coding: utf-8 -*-
import base64, http.cookiejar, json, os, ssl, struct, time, urllib.error, urllib.request, zlib
BASE="https://huangquechuanmei.com"; USER="yuelei"; PASS=os.environ.get("HQ_TEST_PASS","")
ctx=ssl.create_default_context(); cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
def call(m,p,b=None,t=240):
    r=urllib.request.Request(BASE+p,data=(json.dumps(b).encode() if b is not None else None),method=m)
    r.add_header("Content-Type","application/json"); r.add_header("User-Agent","hq-acc/1.0")
    try:
        with op.open(r,timeout=t) as x: return x.status, x.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8","replace")
    except Exception as e: return None,"ERR %s: %s"%(type(e).__name__,e)
def png(w=768,h=1024,rgb=(150,158,170)):
    raw=b"".join(b"\x00"+bytes(rgb)*w for _ in range(h))
    def ck(t,d):
        c=t+d; return struct.pack(">I",len(d))+c+struct.pack(">I",zlib.crc32(c)&0xffffffff)
    b=(b"\x89PNG\r\n\x1a\n"+ck(b"IHDR",struct.pack(">IIBBBBB",w,h,8,2,0,0,0))+ck(b"IDAT",zlib.compress(raw,6))+ck(b"IEND",b""))
    return "data:image/png;base64,"+base64.b64encode(b).decode()
r=urllib.request.Request(BASE+"/api/auth/login",data=json.dumps({"username":USER,"password":PASS}).encode(),method="POST")
r.add_header("Content-Type","application/json"); op.open(r,timeout=60).read()

def cur(opid):
    d=json.loads(call("GET","/api/admin/channel-manager",t=180)[1])
    m=next((x for x in d["operation_mappings"] if x["operation_id"]==opid),None)
    return d,(m or {}),int((m or {}).get("revision") or 0)

def run(label,opid,adapter,endpoint,build):
    d,m0,rev=cur(opid)
    ch=next((x for x in d["items"] if x["adapter"]==adapter),None)
    if not ch: print("\n%s: 找不到 adapter=%s 的渠道"%(label,adapter)); return
    print("\n"+"="*74); print("%s → %s (id=%s)"%(label,ch["name"],ch["id"]))
    s,b=call("POST","/api/admin/channel-manager/operation-mapping",
             {"operation_id":opid,"state":"managed","channels":[ch["id"]],"expected_revision":rev},t=120)
    print("  切换 HTTP %s %s"%(s,b[:100]))
    if s!=200: return
    rev+=1
    body=build()
    s,b=call("POST",endpoint,body,t=300)
    print("  下单 HTTP %s %s"%(s,b[:150]))
    try: jd=json.loads(b)
    except Exception: jd={}
    jid=jd.get("job_id") or jd.get("id")
    if jid:
        st="?"
        for _ in range(80):
            try: dd=json.loads(call("GET","/api/gen/job/%s"%jid,t=60)[1])
            except Exception: dd={}
            st=str(dd.get("status") or "")
            if st in ("done","error","failed","refunded"): break
            time.sleep(8)
        dd=json.loads(call("GET","/api/gen/job/%s"%jid,t=60)[1]); res=dd.get("result") or {}
        print("  任务 %s status=%s provider=%s model=%s"%(jid,dd.get("status"),res.get("provider"),res.get("model")))
        print("  成品=%s"%str(res.get("url") or res.get("file"))[:100])
        if dd.get("error"): print("  错误=%s"%str(dd.get("error"))[:220])
    s,b=call("POST","/api/admin/channel-manager/operation-mapping",
             {"operation_id":opid,"state":m0.get("state","legacy"),"channels":m0.get("channels") or [],
              "expected_revision":rev},t=120)
    print("  恢复 HTTP %s %s"%(s,b[:80]))

def b_grok15():
    its=json.loads(call("GET","/api/gen/channel-parameters",t=120)[1]).get("items") or []
    it=next(x for x in its if x.get("front")=="grok15"); c=(it.get("combinations") or [])[0]
    print("  参数 front=grok15 revision=%s combination=%s points=%s"%(it.get("revision"),c.get("id"),c.get("points")))
    return {"channel":"grok15","prompt":"一只橘猫在窗台上伸懒腰，柔和阳光，短视频素材",
            "parameter_selection":{"revision":it.get("revision"),"combination":c.get("id")}}
run("乐创 Grok 视频 1.5","video.grok.text","lechuang_video","/api/gen/xiaole_video",b_grok15)

def b_tryon():
    return {"person_image_data":png(),"clothes_data":png(768,1024,(40,90,170)),"seconds":5,"line":"2"}
run("换装 WaveSpeed 线路二","video.tryon.fast","wavespeed_tryon","/api/gen/tryon",b_tryon)
