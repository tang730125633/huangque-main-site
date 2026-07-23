#!/usr/bin/env python3
"""黄雀渠道日报 → 飞书多维表格 服务器版同步（每5分钟同步当天，00:10 终算前一天）。
口径与 2026-07-12 驾驶舱一致；幂等：先删该日旧记录再写入。
用法: python3 hq_bitable_sync_server.py [YYYY-MM-DD]   # 缺省=今天
凭据: /home/ubuntu/.hq_feishu.env (600)
"""
import json, sqlite3, sys, datetime, collections, urllib.request

APP = "WUBRbMYN0awpMhsYuQNcLCW1nyc"
TABLE = "tbl1XM63cxFuXSha"
DB = "/home/ubuntu/content-api/content_jobs.db"
ENV = "/home/ubuntu/.hq_feishu.env"
BASE = "https://open.feishu.cn/open-apis"

def env():
    d = {}
    for line in open(ENV):
        if "=" in line:
            k, v = line.strip().split("=", 1); d[k] = v
    return d

def token():
    e = env()
    req = urllib.request.Request(BASE + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": e["FEISHU_APP_ID"], "app_secret": e["FEISHU_APP_SECRET"]}).encode(),
        headers={"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=15))
    if d.get("code") != 0:
        raise RuntimeError("取token失败: %s" % d)
    return d["tenant_access_token"]

def api(tok, method, path, body=None):
    req = urllib.request.Request(BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
        method=method)
    d = json.load(urllib.request.urlopen(req, timeout=30))
    if d.get("code") != 0:
        raise RuntimeError("%s %s 失败: %s" % (method, path, str(d)[:200]))
    return d

def aggregate(day):
    d0 = datetime.datetime.strptime(day, "%Y-%m-%d")
    t0, t1 = d0.timestamp(), (d0 + datetime.timedelta(days=1)).timestamp()
    db = sqlite3.connect(DB)
    rows = db.execute("SELECT kind,payload,status,COALESCE(cost,0),COALESCE(refunded,0) FROM jobs "
                      "WHERE created_at>=? AND created_at<? AND status IN ('done','error')", (t0, t1)).fetchall()
    out = collections.defaultdict(lambda: [0, 0, 0.0])
    IMG = {"nb2": "NanoBanana2", "pro": "Pro高清", "zelong": "泽龙Ai", "zelong2": "泽龙2号池",
           "xiaole": "果肉生图", "seedream": "Seedream"}
    for k, p, s, cost, ref in rows:
        try: pl = json.loads(p or "{}")
        except Exception: pl = {}
        cat = ch = None
        if k == "xiaole_video":
            cat, ch = "视频", {"grok": "果肉", "micro": "豆姐", "omni": "欧米"}.get(pl.get("channel", "?"), "小乐其他")
        elif k == "video":
            cat = "视频"
            m = str(pl.get("mode") or "")
            if m == "motion":
                ch = "动作模仿·线一HeyGen" if str(pl.get("line") or "1") == "1" else "动作模仿·线二Wave"
            elif m in ("text", "audio"):
                ch = "口播-" + ("文案" if m == "text" else "音频")
            else:
                ch = "视频其他"
        elif k == "tryon":
            cat = "视频"
            ch = "换装·线一HeyGen" if str(pl.get("line") or "1") == "1" else "换装·线二Wave"
        elif k == "cinematic":
            cat = "视频"
            cm = str(pl.get("cine_mode") or "motion")
            ch = {"duo": "双人动作模仿", "open": "开放式生成"}.get(cm, "动作模仿")
        elif k == "image":
            cat = "作图"
            e = str(pl.get("model") or pl.get("provider") or "openai")
            ch = IMG.get(e, "OpenAI官方" if e == "openai" else e)
        if not ch: continue
        cell = out[(cat, ch)]
        cell[1 if s == "error" else 0] += 1
        # 已退款的失败单不计入点数消耗(退了=没真花)
        if not (s == "error" and ref):
            try: cell[2] += float(cost or 0)
            except Exception: pass
    return [[c, ch, ok, bad, round(cost)] for (c, ch), (ok, bad, cost) in out.items()]

def main():
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
    rows = aggregate(day)
    ts = int(datetime.datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)
    tok = token()

    # 幂等删除该日旧记录
    ids, page = [], ""
    while True:
        d = api(tok, "GET", f"/bitable/v1/apps/{APP}/tables/{TABLE}/records?page_size=500"
                + (f"&page_token={page}" if page else ""))["data"]
        for it in d.get("items") or []:
            if (it.get("fields") or {}).get("日期") == ts:
                ids.append(it["record_id"])
        if not d.get("has_more"): break
        page = d.get("page_token", "")
    if ids:
        api(tok, "POST", f"/bitable/v1/apps/{APP}/tables/{TABLE}/records/batch_delete", {"records": ids})

    if not rows:
        print(day, "无数据"); return
    recs = [{"fields": {"日期": ts, "大类": cat, "渠道": ch, "成功": ok, "失败": bad,
                        "总数": ok + bad, "成功率": round(ok / (ok + bad), 4), "点数消耗": cost}}
            for cat, ch, ok, bad, cost in rows]
    api(tok, "POST", f"/bitable/v1/apps/{APP}/tables/{TABLE}/records/batch_create", {"records": recs})
    print(day, "同步", len(recs), "条")

if __name__ == "__main__":
    main()
