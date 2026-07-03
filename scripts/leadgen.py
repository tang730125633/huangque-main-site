#!/usr/bin/env python3
"""抖音评论区获客 CLI —— 小冬用这个去抖音爬精准客户。
后端队列在本机 127.0.0.1:8090，实际爬取由 Mac worker（住宅IP）执行。

用法:
  python3 leadgen.py "关键词" [--count N]        # 关键词→搜视频→扒评论→精准客户名单
  python3 leadgen.py "主页链接" --mode account   # 账号深扒（画像+作品+评论）

输出为给群友看的纯文本，小冬据此整理成飞书消息。
"""
import os
import sys
import time
import json
import argparse
import urllib.request
import urllib.parse

BASE = "http://127.0.0.1:8090"
# 口令从环境变量读，不再硬编码（旧默认值已作废）。
PASSWORD = os.environ.get("LEADGEN_PASSWORD", "")


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode(), strict=False)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=25) as r:
        return json.loads(r.read().decode(), strict=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword", help="关键词，或 account 模式下的主页链接")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--mode", default="search", choices=["search", "account"])
    ap.add_argument("--wait", type=int, default=420, help="最长等待秒数")
    a = ap.parse_args()

    try:
        r = post("/api/submit", {"password": PASSWORD, "keyword": a.keyword,
                                 "count": a.count, "mode": a.mode})
    except Exception as e:
        print("提交失败（队列服务没起？）: %s" % e)
        sys.exit(1)
    jid = r.get("job_id")
    if not jid:
        print("提交被拒: %s" % r)
        sys.exit(1)
    print("已提交任务 #%s（%s / %s / 目标%s），爬取中（约1-3分钟）…"
          % (jid, a.mode, a.keyword, a.count))

    deadline = time.time() + a.wait
    j, last = {}, ""
    while time.time() < deadline:
        time.sleep(8)
        try:
            j = get("/api/job/%s" % jid)
        except Exception:
            continue
        st = j.get("status")
        if st and st != last:
            print("   …%s" % st)
            last = st
        if st in ("done", "failed", "error"):
            break

    if j.get("status") != "done":
        print("任务未完成（status=%s）。可能爬虫登录态失效或被风控。 %s"
              % (j.get("status"), str(j.get("error"))[:200]))
        sys.exit(2)

    res = j.get("result")
    if isinstance(res, str):
        try:
            res = json.loads(res, strict=False)
        except Exception:
            res = {}
    res = res or {}

    if a.mode == "search":
        leads = res.get("leads", [])
        print("\n关键词「%s」精准客户 %d 个（共扫 %s 条评论，已剔除同行广告 %s 条）\n"
              % (a.keyword, len(leads), res.get("total", "?"), res.get("spam", "?")))
        for i, l in enumerate(leads, 1):
            print("%d. %s  抖音号:%s  [%s]"
                  % (i, l.get("nickname", ""), l.get("douyin_id", ""), l.get("ip", "")))
            print("   需求原话: %s" % str(l.get("content", "")).replace("\n", " ")[:90])
            if l.get("profile"):
                print("   主页: %s" % l.get("profile"))
        kw = res.get("related_keywords", [])
        if kw:
            print("\n顺带发现的高潜词: %s" % " / ".join(kw[:10]))
    else:
        print("\n账号: %s  抖音号:%s  [%s]"
              % (res.get("nickname", ""), res.get("douyin_id", ""), res.get("ip", "")))
        print("   粉丝:%s  获赞:%s  关注:%s  作品:%s 条  抓到评论:%s 条"
              % (res.get("fans", "?"), res.get("likes", "?"), res.get("follows", "?"),
                 res.get("works_count", "?"), res.get("comments_count", "?")))
        if res.get("desc"):
            print("   简介: %s" % str(res.get("desc")).replace("\n", " ")[:80])
        if res.get("profile"):
            print("   主页: %s" % res.get("profile"))
        works = res.get("works", [])
        if works:
            print("\n   热门作品 TOP5（按点赞）:")
            top = sorted(works, key=lambda x: x.get("likes") or 0, reverse=True)[:5]
            for w in top:
                print("   - [赞%s/评%s] %s"
                      % (w.get("likes"), w.get("comments"),
                         str(w.get("title", "")).replace("\n", " ")[:42]))
        print("\n   完整数据（画像+全部作品+评论）已生成 xlsx 并自动发到飞书群。")


if __name__ == "__main__":
    main()
