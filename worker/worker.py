#!/usr/bin/env python3
"""
Mac 端 worker（混合架构）—— 在 Tang 的 Mac 上跑（住宅 IP 能爬通抖音搜索）。
轮询服务器领任务 → 跑 MediaCrawler 关键词搜索 → leads_filter 过滤 → 回传名单。

跑法（Mac）：
  python worker/worker.py
环境变量（可选）：
  LEADGEN_SERVER   服务器地址，默认 http://129.204.166.13:8090
  LEADGEN_WORKER_TOKEN  与服务器一致的 worker 令牌
  MEDIACRAWLER_DIR  MediaCrawler 路径，默认 ~/code/MediaCrawler
"""
import os
import re
import sys
import json
import time
import glob
import shutil
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.leads_filter import is_spam, is_high, load_jsonl  # 复用过滤核心
import re as _re

SERVER = os.environ.get("LEADGEN_SERVER", "http://129.204.166.13:8090")
TOKEN = os.environ.get("LEADGEN_WORKER_TOKEN", "")
MC_DIR = os.path.expanduser(os.environ.get("MEDIACRAWLER_DIR", "~/code/MediaCrawler"))
HEADLESS = True   # Mac 住宅 IP；若搜索返回空改 False(有头)
POLL = 10         # 轮询间隔(秒)

import urllib.request
import urllib.parse


def http_get(path):
    with urllib.request.urlopen(SERVER + path, timeout=20) as r:
        return json.loads(r.read())


def http_post(path, data):
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(urllib.request.Request(SERVER + path, data=body), timeout=20) as r:
        return json.loads(r.read())


def patch_config(keyword, count):
    """改 MediaCrawler 配置：平台/关键词/数量/搜索/无头/评论。"""
    cfg = os.path.join(MC_DIR, "config/base_config.py")
    src = open(cfg, encoding="utf-8").read()
    rules = {
        r'^PLATFORM\s*=.*$': 'PLATFORM = "dy"',
        r'CRAWLER_TYPE = \(\s*"[^"]+"': 'CRAWLER_TYPE = (\n    "search"',
        r'^KEYWORDS\s*=.*$': f'KEYWORDS = "{keyword}"',
        r'^CRAWLER_MAX_NOTES_COUNT\s*=.*$': f'CRAWLER_MAX_NOTES_COUNT = {count}',
        r'^HEADLESS\s*=.*$': f'HEADLESS = {HEADLESS}',
        r'^ENABLE_CDP_MODE\s*=.*$': 'ENABLE_CDP_MODE = False',
        r'^ENABLE_GET_COMMENTS\s*=.*$': 'ENABLE_GET_COMMENTS = True',
        r'^CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES\s*=.*$': 'CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 100',
        r'^ENABLE_GET_SUB_COMMENTS\s*=.*$': 'ENABLE_GET_SUB_COMMENTS = False',
        r'^CRAWLER_MAX_SLEEP_SEC\s*=.*$': 'CRAWLER_MAX_SLEEP_SEC = 3',
        r'^SAVE_DATA_OPTION\s*=.*$': 'SAVE_DATA_OPTION = "jsonl"',
    }
    for pat, rep in rules.items():
        src = re.sub(pat, rep, src, count=1, flags=re.M)
    open(cfg, "w", encoding="utf-8").write(src)


def run_crawl():
    """清旧数据→跑 MediaCrawler→返回输出 jsonl 路径。"""
    jsonl_dir = os.path.join(MC_DIR, "data/douyin/jsonl")
    if os.path.isdir(jsonl_dir):
        for f in glob.glob(os.path.join(jsonl_dir, "*.jsonl")):
            os.remove(f)
    subprocess.run(
        ["uv", "run", "main.py", "--platform", "dy", "--lt", "qrcode", "--type", "search"],
        cwd=MC_DIR, timeout=900, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonl_dir


# 爆款深挖（两阶段补抓）阈值，与 scripts/viral_hunter.py 默认一致
DEEP_LIKE_THRESH = 10000
DEEP_COMMENT_THRESH = 1000
DEEP_DETAIL_COMMENTS = 300


def _parse_count(v):
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or "").strip()
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        if "万" in s:
            try:
                return int(float(s.replace("万", "")) * 10000)
            except ValueError:
                return 0
        return 0


def pick_viral_ids(jsonl_dir):
    """从 search_contents 里筛爆款 aweme_id(点赞>1万 或 评论>1000)。"""
    ids, seen = [], set()
    for d in load_jsonl(os.path.join(jsonl_dir, "search_contents_*.jsonl")):
        aid = d.get("aweme_id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        if (_parse_count(d.get("liked_count")) >= DEEP_LIKE_THRESH
                or _parse_count(d.get("comment_count")) >= DEEP_COMMENT_THRESH):
            ids.append(aid)
    return ids


def run_detail_deep_dig(jsonl_dir):
    """两阶段补抓：对爆款视频用 detail 模式高量补抓评论(不清 search 数据，追加 detail_*.jsonl)。"""
    ids = pick_viral_ids(jsonl_dir)
    if not ids:
        print("  [deep_dig] 无爆款视频，跳过补抓")
        return 0
    print(f"  [deep_dig] 爆款 {len(ids)} 个 → detail 补抓 {DEEP_DETAIL_COMMENTS} 条/视频")
    # detail 模式不清旧数据(保留 search 结果)，结果写入 detail_*.jsonl
    subprocess.run(
        ["uv", "run", "main.py", "--platform", "dy", "--lt", "qrcode", "--type", "detail",
         "--specified_id", ",".join(ids),
         "--max_comments_count_singlenotes", str(DEEP_DETAIL_COMMENTS)],
        cwd=MC_DIR, timeout=1800, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return len(ids)


def build_result(jsonl_dir):
    # 合并 search + detail(爆款深挖补抓) 评论，按 comment_id 去重
    _cmt_map = {}
    for c in load_jsonl(os.path.join(jsonl_dir, "search_comments_*.jsonl")):
        cid = c.get("comment_id")
        _cmt_map[cid if cid else id(c)] = c
    for c in load_jsonl(os.path.join(jsonl_dir, "detail_comments_*.jsonl")):
        cid = c.get("comment_id")
        _cmt_map[cid if cid else id(c)] = c
    comments = list(_cmt_map.values())
    titles = {}
    hashtags = {}
    videos = []
    vid_seen = set()
    for d in load_jsonl(os.path.join(jsonl_dir, "search_contents_*.jsonl")):
        aid = d.get("aweme_id")
        title = d.get("title") or ""
        if aid:
            titles[aid] = title[:24]
        # 从标题话题标签里挖相关关键词（#美容院获客 #美业老板…）
        for tag in _re.findall(r"#([^#\s\n]+)", title):
            tag = tag.strip()
            if 1 < len(tag) <= 12:
                hashtags[tag] = hashtags.get(tag, 0) + 1
        # 收集视频列表（供网页「按视频看」展示）
        if aid and aid not in vid_seen:
            vid_seen.add(aid)
            videos.append({
                "aweme_id": aid,
                "title": title,
                "nickname": d.get("nickname") or "",   # 博主
                "likes": d.get("liked_count", 0),
                "comment_count": d.get("comment_count", 0),
                "url": d.get("aweme_url") or f"https://www.douyin.com/video/{aid}",
                "cover": d.get("cover_url") or "",
                "ip": d.get("ip_location") or "",
                "leads_count": 0,
            })
    related = [t for t, _ in sorted(hashtags.items(), key=lambda x: -x[1])][:24]
    leads, spam, chat, seen = [], 0, 0, set()
    for c in comments:
        t = (c.get("content") or "").strip()
        if not t:
            continue
        if is_spam(t):
            spam += 1; continue
        if len(_re.sub(r"\[[^\]]+\]", "", t).strip()) < 2:
            chat += 1; continue
        if is_high(t):
            key = (c.get("user_id"), t)
            if key in seen:
                continue
            seen.add(key)
            sec = c.get("sec_uid") or ""
            aid = c.get("aweme_id") or ""
            leads.append({
                "nickname": c.get("nickname"),
                "douyin_id": c.get("user_unique_id") or "",
                "sec_uid": sec,
                "profile": f"https://www.douyin.com/user/{sec}" if sec else "",
                "ip": c.get("ip_location"),
                "content": c.get("content"),
                "source": titles.get(aid, ""),
                "aweme_id": aid,   # 精确关联到具体视频
                "like": c.get("like_count", 0),
            })
        else:
            chat += 1
    leads.sort(key=lambda x: (len(x["content"]), x["like"]), reverse=True)
    # 统计每条视频扒出了几个精准客户，产出多的排前面
    lead_cnt = {}
    for l in leads:
        if l["aweme_id"]:
            lead_cnt[l["aweme_id"]] = lead_cnt.get(l["aweme_id"], 0) + 1
    for v in videos:
        v["leads_count"] = lead_cnt.get(v["aweme_id"], 0)
    videos.sort(key=lambda v: v["leads_count"], reverse=True)
    return {"total": len(comments), "leads_count": len(leads),
            "spam": spam, "chat": chat, "leads": leads,
            "videos": videos,
            "related_keywords": related}


DY_CFG = os.path.join(MC_DIR, "config/dy_config.py")
EXPORT_ACCOUNT = os.path.join(os.path.dirname(__file__), "..", "scripts", "export_account_xlsx.py")
FEISHU_CHAT = os.environ.get("LEADGEN_FEISHU_CHAT", "oc_5663a8c602094c48a850a7da7ad9f70d")


def patch_config_account(creator_input):
    """账号模式：creator 模式 + 设要爬的账号 + 开评论。"""
    cfg = os.path.join(MC_DIR, "config/base_config.py")
    s = open(cfg, encoding="utf-8").read()
    rules = {
        r'^PLATFORM\s*=.*$': 'PLATFORM = "dy"',
        r'CRAWLER_TYPE = \(\s*"[^"]+"': 'CRAWLER_TYPE = (\n    "creator"',
        r'^HEADLESS\s*=.*$': 'HEADLESS = True',
        r'^ENABLE_CDP_MODE\s*=.*$': 'ENABLE_CDP_MODE = False',
        r'^ENABLE_GET_COMMENTS\s*=.*$': 'ENABLE_GET_COMMENTS = True',
        r'^CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES\s*=.*$': 'CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 100',
        r'^ENABLE_GET_SUB_COMMENTS\s*=.*$': 'ENABLE_GET_SUB_COMMENTS = False',
        r'^CRAWLER_MAX_SLEEP_SEC\s*=.*$': 'CRAWLER_MAX_SLEEP_SEC = 3',
        r'^SAVE_DATA_OPTION\s*=.*$': 'SAVE_DATA_OPTION = "jsonl"',
    }
    for pat, rep in rules.items():
        s = re.sub(pat, rep, s, count=1, flags=re.M)
    open(cfg, "w", encoding="utf-8").write(s)
    safe = (creator_input or "").replace('"', '').strip()
    d = open(DY_CFG, encoding="utf-8").read()
    d = re.sub(r'DY_CREATOR_ID_LIST = \[.*?\]', f'DY_CREATOR_ID_LIST = [\n    "{safe}"\n]',
               d, count=1, flags=re.S)
    open(DY_CFG, "w", encoding="utf-8").write(d)


def run_crawl_creator():
    jsonl_dir = os.path.join(MC_DIR, "data/douyin/jsonl")
    if os.path.isdir(jsonl_dir):
        for f in glob.glob(os.path.join(jsonl_dir, "*.jsonl")):
            os.remove(f)
    subprocess.run(["uv", "run", "main.py", "--platform", "dy", "--lt", "qrcode", "--type", "creator"],
                   cwd=MC_DIR, timeout=1800, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonl_dir


def build_account_result(jsonl_dir):
    creators = load_jsonl(os.path.join(jsonl_dir, "creator_creators_*.jsonl"))
    works = load_jsonl(os.path.join(jsonl_dir, "creator_contents_*.jsonl"))
    comments = load_jsonl(os.path.join(jsonl_dir, "creator_comments_*.jsonl"))
    c = creators[0] if creators else {}
    sec = works[0].get("sec_uid") if works else ""
    works_out = [{
        "title": w.get("title") or w.get("desc") or "",
        "cover": w.get("cover_url") or "",
        "likes": w.get("liked_count"), "comments": w.get("comment_count"),
        "shares": w.get("share_count"), "collects": w.get("collected_count"),
        "create_time": w.get("create_time"),
        "url": w.get("aweme_url") or "",
    } for w in works[:200]]
    return {
        "type": "account", "nickname": c.get("nickname"),
        "douyin_id": works[0].get("user_unique_id") if works else "",
        "fans": c.get("fans"), "likes": c.get("interaction"),
        "works_count": len(works), "comments_count": len(comments),
        "follows": c.get("follows"),
        "ip": (c.get("ip_location") or "").replace("IP属地：", ""),
        "desc": c.get("desc") or "",
        "avatar": c.get("avatar") or "",
        "profile": f"https://www.douyin.com/user/{sec}" if sec else "",
        "works": works_out,
    }


def feishu_send_file(path, caption):
    folder, fn = os.path.dirname(path), os.path.basename(path)
    base = ["lark-cli", "im", "+messages-send", "--profile", "xiaoqiu", "--as", "bot",
            "--chat-id", FEISHU_CHAT]
    subprocess.run(base + ["--text", caption], cwd=folder, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(base + ["--file", "./" + fn], cwd=folder, timeout=180,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def handle_account(job):
    jid, target = job["id"], job["keyword"]
    print(f"[job {jid}] 账号爬取「{target[:40]}」…")
    patch_config_account(target)
    jsonl_dir = run_crawl_creator()
    res = build_account_result(jsonl_dir)
    nick = (res.get("nickname") or "account").replace("/", "_")[:20]
    xlsx = f"/tmp/账号_{nick}_{jid}.xlsx"
    subprocess.run(["uv", "run", "python", EXPORT_ACCOUNT, jsonl_dir, xlsx],
                   cwd=MC_DIR, timeout=120, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    feishu_send_file(xlsx, f"👤 账号「{res.get('nickname')}」数据：粉丝{res.get('fans')}/"
                           f"获赞{res.get('likes')}/作品{res.get('works_count')}，含画像+作品+评论 ↓")
    res["sent_feishu"] = True
    http_post("/api/complete", {"token": TOKEN, "job_id": jid,
                                "result": json.dumps(res, ensure_ascii=False)})
    print(f"[job {jid}] ✅ 账号「{res.get('nickname')}」已发飞书")


def _deep_dig_on(job):
    """从 job.payload(JSON) 读爆款深挖开关。"""
    raw = job.get("payload")
    if not raw:
        return False
    try:
        return bool(json.loads(raw).get("deep_dig"))
    except Exception:
        return False


def handle_search(job):
    jid, kw, cnt = job["id"], job["keyword"], job["count"]
    deep = _deep_dig_on(job)
    print(f"[job {jid}] 关键词「{kw}」x{cnt}{' [爆款深挖]' if deep else ''} 开始爬取…")
    patch_config(kw, cnt)
    jsonl_dir = run_crawl()
    if deep:
        try:
            n = run_detail_deep_dig(jsonl_dir)
            print(f"[job {jid}] 爆款深挖补抓完成: {n} 个视频")
        except Exception as e:
            print(f"[job {jid}] ⚠️ 爆款深挖失败(不影响主结果): {e}")
    result = build_result(jsonl_dir)
    result["deep_dig"] = deep
    http_post("/api/complete", {"token": TOKEN, "job_id": jid,
                                "result": json.dumps(result, ensure_ascii=False)})
    print(f"[job {jid}] ✅ 完成：{result['leads_count']} 精准客户")


def handle(job):
    jid = job["id"]
    try:
        if job.get("mode") == "account":
            handle_account(job)
        else:
            handle_search(job)
    except subprocess.TimeoutExpired:
        http_post("/api/fail", {"token": TOKEN, "job_id": jid, "error": "爬取超时"})
        print(f"[job {jid}] ❌ 超时")
    except Exception as e:
        http_post("/api/fail", {"token": TOKEN, "job_id": jid, "error": str(e)[:300]})
        print(f"[job {jid}] ❌ {e}")


def main():
    print(f"worker 启动，连接 {SERVER}，MediaCrawler={MC_DIR}")
    while True:
        try:
            res = http_get(f"/api/claim?token={TOKEN}")
            job = res.get("job")
            if job:
                handle(job)
            else:
                time.sleep(POLL)
        except KeyboardInterrupt:
            print("退出"); break
        except Exception as e:
            print("轮询出错(稍后重试):", e); time.sleep(POLL)


if __name__ == "__main__":
    main()
