#!/usr/bin/env python3
"""
爬取桥脚本 —— 给飞书 Bot（小秋）/ 命令行调用。
提交关键词 → 等后端+worker 跑完 → 打印精准客户名单 + 存 CSV。

小秋用法（识别到"爬客户"意图时）：
  python3 crawl_cli.py "美业获客" --count 10
然后把输出做成飞书卡片回复，并把 CSV 当文件发出去。

环境变量：
  LEADGEN_SERVER    后端地址，默认 http://129.204.166.13:8090
  LEADGEN_PASSWORD  提交口令（必填，不再有硬编码默认值）
"""
import os
import sys
import csv
import json
import time
import argparse
import urllib.request
import urllib.parse

SERVER = os.environ.get("LEADGEN_SERVER", "http://129.204.166.13:8090")
PASSWORD = os.environ.get("LEADGEN_PASSWORD", "")


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(urllib.request.Request(SERVER + path, data=body), timeout=20) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(SERVER + path, timeout=20) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword", help="关键词，如 美业获客")
    ap.add_argument("--count", type=int, default=10, help="爬取视频数 1-30")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非文本")
    ap.add_argument("--timeout", type=int, default=600, help="最长等待秒数")
    args = ap.parse_args()

    try:
        sub = post("/api/submit", {"password": PASSWORD, "keyword": args.keyword, "count": args.count})
    except urllib.error.HTTPError as e:
        print(f"❌ 提交失败：{e.code} {e.read().decode()[:120]}"); sys.exit(1)
    jid = sub["job_id"]

    deadline = time.time() + args.timeout
    result = None
    while time.time() < deadline:
        j = get(f"/api/job/{jid}")
        if j["status"] == "done":
            result = j["result"]; break
        if j["status"] == "error":
            print(f"❌ 爬取出错：{j.get('error')}"); sys.exit(1)
        time.sleep(8)
    if result is None:
        print("❌ 超时未完成"); sys.exit(1)

    # 存 CSV
    csv_path = f"/tmp/leads_{args.keyword}_{jid}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["昵称", "抖音号", "主页链接", "属地", "需求原文", "来源视频"])
        for c in result["leads"]:
            w.writerow([c["nickname"], c.get("douyin_id", ""), c.get("profile", ""),
                        c["ip"], c["content"], c.get("source", "")])

    if args.json:
        print(json.dumps({**result, "csv_path": csv_path}, ensure_ascii=False))
        return

    # 文本/markdown 输出（给小秋做卡片）
    print(f"关键词「{args.keyword}」客户线索")
    print(f"总评论 {result['total']} → 🔥精准客户 {result['leads_count']} | 🗑️中介噪音 {result['spam']} | 💬闲聊 {result['chat']}")
    print(f"CSV: {csv_path}\n")
    for i, c in enumerate(result["leads"][:15], 1):
        did = f" | 抖音号:{c['douyin_id']}" if c.get("douyin_id") else ""
        print(f"{i}. 【{c['nickname']}】{c['ip']}{did}\n   需求：{c['content']}")
    if result["leads_count"] > 15:
        print(f"\n…共 {result['leads_count']} 个，完整见 CSV")


if __name__ == "__main__":
    main()
