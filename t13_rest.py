# -*- coding: utf-8 -*-
"""剩余渠道实测：Grok 视频 1.0 / 1.5 / 换装 WaveSpeed。
每条：发布映射→取参数→真实下单→轮询→取证→恢复。
"""
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

def png_data_url(w=768,h=1024,rgb=(150,158,170)):
    raw=b""
    for y in range(h):
        raw+=b"\x00"+bytes(rgb)*w
    def chunk(t,d):
        c=t+d; return struct.pack(">I",len(d))+c+struct.pack(">I",zlib.crc32(c)&0xffffffff)
    png=(b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",w,h,8,2,0,0,0))
         +chunk(b"IDAT",zlib.compress(raw,6))+chunk(b"IEND",b""))
    return "data:image/png;base64,"+base64.b64encode(png).decode()

r=urllib.request.Request(BASE+"/api/auth/login",data=json.dumps({"username":USER,"password":PASS}).encode(),method="POST")
r.add_header("Content-Type","application/json"); op.open(r,timeout=60).read()
d=json.loads(call("GET","/api/admin/channel-manager",t=180)[1])
items={x["id"]:x for x in d["items"]}
rows=[]

def run(label, opid, chid, endpoint, build, param_front=None):
    m0=next((x for x in d["operation_mappings"] if x["operation_id"]==opid),None)
    rev=int((m0 or {}).get("revision") or 0)
    print("\n"+"="*74); print("%s → 渠道 %s" % (label, (items.get(chid) or {}).get("name")))
    s,b=call("POST","/api/admin/channel-manager/operation-mapping",
             {"operation_id":opid,"state":"managed","channels":[chid],"expected_revision":rev},t=120)
    print("  切换 HTTP %s %s" % (s,b[:110]))
    if s!=200: rows.append([label,"切换失败","-","-","-"]); return
    rev+=1
    body=build(param_front)
    s,b=call("POST",endpoint,body,t=240)
    print("  下单 HTTP %s %s" % (s,b[:170]))
    try: jd=json.loads(b)
    except Exception: jd={}
    jid=jd.get("job_id") or jd.get("id")
    if not jid:
        rows.append([label,"下单失败",str(s),"-",b[:60]])
    else:
        st="?"
        for _ in range(70):
            try: dd=json.loads(call("GET","/api/gen/job/%s"%jid,t=60)[1])
            except Exception: dd={}
            st=str(dd.get("status") or "")
            if st in ("done","error","failed","refunded"): break
            time.sleep(8)
        dd=json.loads(call("GET","/api/gen/job/%s"%jid,t=60)[1]); res=dd.get("result") or {}
        print("  任务 %s status=%s provider=%s model=%s" % (jid,dd.get("status"),res.get("provider"),res.get("model")))
        print("  成品=%s" % str(res.get("url") or res.get("file"))[:100])
        if dd.get("error"): print("  错误=%s" % str(dd.get("error"))[:220])
        rows.append([label,"HTTP %s"%s,jid,dd.get("status"),"%s/%s"%(res.get("provider"),res.get("model")),"OK" if res.get("file") else str(dd.get("error"))[:64]])
    s,b=call("POST","/api/admin/channel-manager/operation-mapping",
             {"operation_id":opid,"state":(m0 or {}).get("state","legacy"),
              "channels":(m0 or {}).get("channels") or [],"expected_revision":rev},t=120)
    print("  恢复 HTTP %s" % s)

# ① Grok 视频 1.0
def b_grok10(front):
    its=json.loads(call("GET","/api/gen/channel-parameters",t=120)[1]).get("items") or []
    it=next((x for x in its if x.get("front")=="grok"),None)
    c=(it.get("combinations") or [])[0]
    print("  参数: front=grok revision=%s combination=%s points=%s" % (it.get("revision"),c.get("id"),c.get("points")))
    return {"channel":"grok","prompt":"一只橘猫在窗台上伸懒腰，柔和阳光，短视频素材",
            "parameter_selection":{"revision":it.get("revision"),"combination":c.get("id")}}
run("乐创 Grok 视频 1.0","video.grok.text","xlw-grok-video","/api/gen/xiaole_video",b_grok10)

# ② Grok 视频 1.5
def b_grok15(front):
    its=json.loads(call("GET","/api/gen/channel-parameters",t=120)[1]).get("items") or []
    it=next((x for x in its if x.get("front")=="grok15"),None)
    c=(it.get("combinations") or [])[0]
    print("  参数: front=grok15 revision=%s combination=%s points=%s" % (it.get("revision"),c.get("id"),c.get("points")))
    return {"channel":"grok15","prompt":"一只橘猫在窗台上伸懒腰，柔和阳光，短视频素材",
            "parameter_selection":{"revision":it.get("revision"),"combination":c.get("id")}}
run("乐创 Grok 视频 1.5","video.grok.text","xlw-grok-video-15","/api/gen/xiaole_video",b_grok15)

# ③ 换装 WaveSpeed（走迁移后的接口）
def b_tryon(front):
    return {"person_image_data":png_data_url(),"clothes_data":png_data_url(768,1024,(40,90,170)),
            "seconds":5,"line":"2"}
run("换装 WaveSpeed 线路二","video.tryon.fast","f91bab44058c","/api/gen/tryon",b_tryon)

print("\n"+"="*74)
print("%-24s %-10s %-7s %-8s %-26s %s"%("渠道","下单","任务","状态","实际供应商/模型","结果"))
for r_ in rows: print("%-24s %-10s %-7s %-8s %-26s %s"%tuple((str(x)[:26] for x in r_)))
