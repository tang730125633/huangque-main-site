#!/usr/bin/env python3
"""
关键词库验证器 —— 抽样跑关键词，确认「关键词→视频」「视频→评论」都成立。
每个词只爬 1 条视频 + 其评论（轻负载），带延时避免触发风控。

用法：python validate_keywords.py 关键词1 关键词2 ...
"""
import os
import re
import sys
import glob
import time
import json
import subprocess

MC_DIR = os.path.expanduser(os.environ.get("MEDIACRAWLER_DIR", "~/code/MediaCrawler"))
CFG = os.path.join(MC_DIR, "config/base_config.py")
JSONL = os.path.join(MC_DIR, "data/douyin/jsonl")


def patch(keyword):
    src = open(CFG, encoding="utf-8").read()
    rules = {
        r'^PLATFORM\s*=.*$': 'PLATFORM = "dy"',
        r'^KEYWORDS\s*=.*$': f'KEYWORDS = "{keyword}"',
        r'^CRAWLER_TYPE\s*=\s*\($': 'CRAWLER_TYPE = (',
        r'^CRAWLER_MAX_NOTES_COUNT\s*=.*$': 'CRAWLER_MAX_NOTES_COUNT = 1',
        r'^HEADLESS\s*=.*$': 'HEADLESS = True',
        r'^ENABLE_CDP_MODE\s*=.*$': 'ENABLE_CDP_MODE = False',
        r'^ENABLE_GET_COMMENTS\s*=.*$': 'ENABLE_GET_COMMENTS = True',
    }
    for pat, rep in rules.items():
        src = re.sub(pat, rep, src, count=1, flags=re.M)
    open(CFG, "w", encoding="utf-8").write(src)


def count_lines(pattern):
    n = 0
    for p in glob.glob(pattern):
        with open(p) as f:
            n += sum(1 for line in f if line.strip())
    return n


def run_one(keyword):
    for p in glob.glob(os.path.join(JSONL, "*.jsonl")):
        os.remove(p)
    patch(keyword)
    try:
        subprocess.run(["uv", "run", "main.py", "--platform", "dy", "--lt", "qrcode", "--type", "search"],
                       cwd=MC_DIR, timeout=300, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return {"keyword": keyword, "videos": 0, "comments": 0, "ok": False, "err": str(e)[:60]}
    v = count_lines(os.path.join(JSONL, "search_contents_*.jsonl"))
    c = count_lines(os.path.join(JSONL, "search_comments_*.jsonl"))
    return {"keyword": keyword, "videos": v, "comments": c, "ok": v > 0}


def main():
    kws = sys.argv[1:]
    results = []
    for i, kw in enumerate(kws, 1):
        print(f"[{i}/{len(kws)}] 测「{kw}」…", flush=True)
        r = run_one(kw)
        flag = "✅" if r["ok"] else "❌"
        print(f"   {flag} 视频 {r['videos']} | 评论 {r['comments']}"
              + (f" | {r.get('err')}" if not r["ok"] else ""), flush=True)
        results.append(r)
        if i < len(kws):
            time.sleep(12)  # 延时降风控
    ok = sum(1 for r in results if r["ok"])
    print(f"\n===== 汇总：{ok}/{len(results)} 个关键词成功找到视频+评论 =====")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
