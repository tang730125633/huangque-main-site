# -*- coding: utf-8 -*-
"""核实：生产上 env.legacy() 的数据（服务类分类是否有内容）"""
import http.cookiejar, json, os, ssl, urllib.error, urllib.request
BASE="https://huangquechuanmei.com"; USER="yuelei"; PASS=os.environ.get("HQ_TEST_PASS","")
ctx=ssl.create_default_context(); cj=http.cookiejar.CookieJar()
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
r=urllib.request.Request(BASE+"/api/auth/login",data=json.dumps({"username":USER,"password":PASS}).encode(),method="POST")
r.add_header("Content-Type","application/json"); op.open(r,timeout=60).read()
def get(p,t=120):
    r=urllib.request.Request(BASE+p); r.add_header("User-Agent","hq-acc/1.0")
    try:
        with op.open(r,timeout=t) as x: return x.status, x.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8","replace")
s,b=get("/api/admin/keys")
print("/api/admin/keys → HTTP %s" % s)
if s==200:
    d=json.loads(b)
    items=d.get("items") or []
    print("服务条目数: %d" % len(items))
    # 模拟前端 classify
    import re
    RULES=[('image',r'图片|生图|绘图'),('video',r'视频生成'),('avatar',r'数字人|数字化 IP|数字化IP|形象|口播'),
           ('audio',r'音频|配音|语音|声音|音乐'),('text',r'文本|文案|助手|语言|提示词'),
           ('collect',r'采集|解析|搜索|抓取'),('process',r'视频处理|成片|剪辑|合成|字幕|换装|背景')]
    def classify(t):
        f=[k for k,ru in RULES if re.search(ru,t or '')]
        return f or ['other']
    from collections import Counter
    cnt=Counter()
    for it in items:
        cats=classify(it.get("category") or "")
        for c in cats: cnt[c]+=1
        print("  %-14s category=%-22s → %s" % (it.get("key"), (it.get("category") or "")[:22], cats))
    print()
    print("按分类统计：", dict(cnt))
    print()
    print("采集(collect) 分类下的服务:", [it.get("name") for it in items if 'collect' in classify(it.get("category") or "")])
    print("音频(audio)  分类下的服务:", [it.get("name") for it in items if 'audio' in classify(it.get("category") or "")])
