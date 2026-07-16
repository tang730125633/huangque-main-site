#!/usr/bin/env python3
"""
评论区意图过滤器 —— 抖音评论区获客系统核心脚本

输入 MediaCrawler 抓取的评论/视频 jsonl，输出干净的精准客户名单（Markdown 表格）。
逻辑：剔除同行中介引流话术，保留"求助/问价/求方法/报预算"等真实需求信号。

用法：
  python leads_filter.py \
    --comments path/to/search_comments_*.jsonl \
    --contents path/to/search_contents_*.jsonl \
    --out leads.md

成果（2026-06-15「美业获客」）：140 评论 → 42 精准客户 / 剔除 12 中介噪音 / 86 闲聊。
"""
import json
import re
import glob
import argparse

# 同行 / 拓客中介的引流话术模板（噪音，剔除）——不过滤则名单全是同行广告
SPAM = [
    "需要我推荐", "推荐给你", "先帮店做出业绩", "做出业绩再合作", "做出业绩再分润",
    "不需要店家出成本", "不需要我先出成本", "W的业绩", "万的业绩", "免费送模式",
    "0成本启动", "感兴趣的老板", "一起交流交流", "下店来打版",
]
# 精准客户信号
HIGH = [
    # —— B端（门店老板：求助/问价/求方法/报预算）——
    "怎么拓客", "怎么收费", "怎么弄", "怎么做", "怎么操作", "怎么整", "怎么合作", "怎么矩阵",
    "多少钱", "价位", "求带", "带带", "带一带", "想学", "有偿", "预算", "求助", "求推荐",
    "靠谱的拓客", "有没有靠谱", "哪里下载", "谁能帮我", "我也想", "没开单", "怎么收费的",
    "想找", "教一下", "怎么回", "我该怎么", "到底", "求带带", "也想",
    # —— C端（消费者/求美者：问效果/价格/预约/担忧）——
    "有效果吗", "效果怎么样", "会反弹", "反弹吗", "能瘦", "痛吗", "维持多久", "做一次",
    "几次", "安全吗", "在哪做", "怎么预约", "约一个", "想做", "想咨询", "哪家好",
    "怎么联系", "贵吗", "价格", "多少钱一次", "可以瘦吗", "有用吗", "求地址",
]


def is_spam(t):
    return any(k in t for k in SPAM)


def is_high(t):
    return any(k in t for k in HIGH)


def load_jsonl(pattern):
    rows = []
    for path in glob.glob(pattern):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--comments", required=True, help="评论 jsonl 路径（支持通配）")
    ap.add_argument("--contents", default="", help="视频 jsonl 路径（用于映射来源视频标题）")
    ap.add_argument("--out", default="leads.md", help="输出 Markdown 名单路径")
    args = ap.parse_args()

    comments = load_jsonl(args.comments)
    titles = {}
    if args.contents:
        for d in load_jsonl(args.contents):
            titles[d.get("aweme_id")] = (d.get("title") or "")[:24]

    leads, spam, chat = [], 0, 0
    seen = set()
    for c in comments:
        t = (c.get("content") or "").strip()
        if not t:
            continue
        if is_spam(t):
            spam += 1
            continue
        plain = re.sub(r"\[[^\]]+\]", "", t).strip()  # 去表情
        if len(plain) < 2:
            chat += 1
            continue
        if is_high(t):
            key = (c.get("user_id"), t)
            if key in seen:
                continue
            seen.add(key)
            leads.append(c)
        else:
            chat += 1

    # 排序：需求越具体（越长）越优先，其次点赞数
    leads.sort(key=lambda c: (len(c.get("content", "")), c.get("like_count", 0)), reverse=True)

    with open(args.out, "w") as f:
        f.write("# 抖音评论区客户线索\n\n")
        f.write(f"**总评论 {len(comments)} | 🔥精准客户 {len(leads)} "
                f"| 🗑️中介噪音 {spam} | 💬闲聊 {chat}**\n\n")
        f.write("| # | 昵称 | 抖音号 | 属地 | 需求原文 | 来源视频 |\n")
        f.write("|---|---|---|---|---|---|\n")
        for i, c in enumerate(leads, 1):
            ct = (c.get("content") or "").replace("|", "/").replace("\n", " ")
            f.write(f"| {i} | {c.get('nickname')} | {c.get('user_unique_id') or ''} "
                    f"| {c.get('ip_location')} | {ct} | {titles.get(c.get('aweme_id'), '')} |\n")

    print(f"总评论 {len(comments)} → 🔥精准客户 {len(leads)} "
          f"| 🗑️中介噪音 {spam} | 💬闲聊 {chat}")
    print(f"✅ 名单已存: {args.out}")


if __name__ == "__main__":
    main()
