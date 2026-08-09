#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄雀 AI · TikHub 客户端 —— 抖音 / 小红书 / 视频号 的「采集 + 评论区获客」统一出口。
=====================================================================
被 content_api.py 的 collect / leads 能力调用。零第三方依赖（只用 stdlib + 现有 OpenAI 代理跑 whisper）。

字段路径全部来自 2026-06-27 的 live 实测（见 scratchpad/tikhub-probe/*.md），不是文档臆测。
三平台各自的坑已在下方封死：
  抖音   search=POST+字符串参数；按 duration>0 滤真视频；下载用 play_addr 不用 download_addr；评论带 ip_label。
  小红书 走 app_v2（web_v3 全挂）；详情/评论只要 note_id 不要 xsec_token；视频笔记白送 .srt 字幕；评论带 ip_location。
  视频号 无全网关键词搜（盯号型）；下载地址加密(decode_key)→口播 v1 先跳过；评论带评论者 finder username + ip_region。

环境变量：TIKHUB_KEY（必填）、TIKHUB_BASE（默认 api.tikhub.io；大陆服务器改 api.tikhub.dev）、
         OPENAI_API_KEY / OPENAI_BASE（口播 ASR 用，与 content_api 同源）。
"""
import os, re, json, time, threading, sqlite3, subprocess, tempfile, urllib.request, urllib.parse, urllib.error
from contextlib import closing

KEY  = os.environ.get("TIKHUB_KEY", "")
BASE = os.environ.get("TIKHUB_BASE", "https://api.tikhub.io").rstrip("/")
UA   = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
OPENAI_KEY  = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com")
# 口播转写模型：默认 gpt-4o-mini-transcribe（中文口播更准、更便宜），可 env 回退 whisper-1
TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
# 采集口播转写并发限制：多任务同时挤 OpenAI ASR 通道 + 抢下载带宽会互相拖垮(单次11s→并发几分钟甚至超时)。
# 限同时转写数(默认1)，排队一个个来，不再让并发请求互相拖到分钟级。env TRANSCRIBE_MAX_CONCURRENCY 可调。
_TRANSCRIBE_SEM = threading.BoundedSemaphore(max(1, int(os.environ.get("TRANSCRIBE_MAX_CONCURRENCY", "1") or "1")))
TRANSCRIBE_TIMEOUT = max(20, int(os.environ.get("OPENAI_TRANSCRIBE_TIMEOUT", "75") or "75"))

PLATFORMS = ("douyin", "xhs", "channels")

# 直连 opener：服务器 content.env 设了 HTTPS_PROXY(给 OpenAI 用)，但该代理转 TikHub 的 Cloudflare
# 会 SSL EOF；TikHub API + CDN 下载都强制绕过代理直连（已实测 .io/.dev 直连均 200）。
# 仅 OpenAI whisper 仍走默认 urlopen（吃环境代理）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ============ 缓存：同一条链接/关键词别人爬过就直接给，省 TikHub 调用(限流+花钱) ============
# 内容(详情)发布即固定→缓存靠谱；评论/搜索会变→短存；play_url 带时效→跟详情 1h 内安全。
_CACHE_DB = os.environ.get("TIKHUB_CACHE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tikhub_cache.db"))
_CACHE_LOCK = threading.Lock()
def _cache_conn():
    c = sqlite3.connect(_CACHE_DB, timeout=5)
    c.execute("CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, v TEXT, exp INTEGER)")
    return c
def _cache_get(key):
    try:
        with _CACHE_LOCK, closing(_cache_conn()) as c:
            r = c.execute("SELECT v, exp FROM cache WHERE k=?", (key,)).fetchone()
            if r and r[1] > time.time():
                return json.loads(r[0])
    except Exception:
        pass
    return None
def _cache_set(key, val, ttl):
    try:
        with _CACHE_LOCK, closing(_cache_conn()) as c:
            c.execute("INSERT OR REPLACE INTO cache(k, v, exp) VALUES(?,?,?)",
                      (key, json.dumps(val, ensure_ascii=False), int(time.time()) + ttl))
            c.execute("DELETE FROM cache WHERE exp < ?", (int(time.time()),))  # 顺手清过期
            c.commit()
    except Exception:
        pass


class TikHubError(Exception):
    pass


# ============ 全局限流：TikHub QPS 10/s，跨线程排队稳在 ~7/s，避免突发被限流返回错笔记/空 ============
_RL_LOCK = threading.Lock()
_RL_LAST = [0.0]
def _ratelimit(min_gap=0.14):
    with _RL_LOCK:
        wait = min_gap - (time.time() - _RL_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _RL_LAST[0] = time.time()


# ============ HTTP（统一信封 {code,message,data}；带浏览器 UA 防 Cloudflare 1010）============
def _call(method, path, query=None, body=None, timeout=45):
    if not KEY:
        raise TikHubError("TIKHUB_KEY 未配置")
    _ratelimit()
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + KEY, "User-Agent": UA}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:  # 直连，绕过环境代理
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise TikHubError("HTTP %s %s :: %s" % (e.code, path.rsplit("/", 1)[-1], e.read()[:160].decode("u8", "ignore")))
    except Exception as e:
        raise TikHubError("REQ %s :: %s" % (path.rsplit("/", 1)[-1], str(e)[:160]))
    if not isinstance(d, dict) or ("data" not in d and d.get("code") not in (200, 0, None)):
        raise TikHubError("API %s :: %s" % (path.rsplit("/", 1)[-1], str(d)[:160]))
    return d.get("data") or {}

def _g(path, **query): return _call("GET", path, query=query)
def _p(path, **body):  return _call("POST", path, body=body)


# ---- 小工具 ----
def _first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", []):
            return d[k]
    return default

def _url0(node):
    """抖音/小红书常见的 {url_list:[...]} 结构取第一个。"""
    if isinstance(node, dict):
        ul = node.get("url_list") or node.get("urlList") or []
        return ul[0] if ul else None
    if isinstance(node, list):
        return node[0] if node else None
    return node

def _urls(node):
    """返回播放地址的全部 CDN 候选并去重，供下载失败时切换线路。"""
    if isinstance(node, dict):
        values = node.get("url_list") or node.get("urlList") or []
    elif isinstance(node, list):
        values = node
    else:
        values = [node] if node else []
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))

def _tags_from_text(text):
    return re.findall(r"#([^#\s]{1,20})", text or "")

def _xhs_img(im):
    # 小红书图片字典是 .url 直给（不是抖音那种 url_list）
    if isinstance(im, dict):
        return im.get("url") or im.get("url_size_large") or im.get("url_default") or _url0(im)
    return im

def _profile_url(platform, user_id):
    # 用户/评论者主页链接（视频号 finder 号无公开网页主页 → None）
    if not user_id:
        return None
    if platform == "douyin":
        return "https://www.douyin.com/user/%s" % user_id
    if platform == "xhs":
        return "https://www.xiaohongshu.com/user/profile/%s" % user_id
    return None


# ====================================================================
# 抖音 Douyin
# ====================================================================
DY = "/api/v1/douyin"

def dy_aweme_id(s):
    """从分享链/纯 id 里抠 aweme_id。"""
    s = (s or "").strip()
    m = re.search(r"/video/(\d+)", s) or re.search(r"(\d{15,21})", s)
    return m.group(1) if m else s

def _dy_item(a):
    vid = a.get("video") or {}
    stat = a.get("statistics") or {}
    au = a.get("author") or {}
    desc = a.get("desc") or ""
    imgs = [u for u in (_url0(im) for im in (a.get("images") or [])) if u]
    is_img = bool(imgs) or a.get("aweme_type") in (68, 2)
    return {
        "platform": "douyin",
        "id": a.get("aweme_id"),
        "url": "https://www.douyin.com/video/%s" % a.get("aweme_id"),
        "title": desc,
        "cover": (imgs[0] if is_img and imgs else _url0(vid.get("cover") or vid.get("origin_cover"))),
        "author": _first(au, "nickname", default=""),
        "author_id": au.get("sec_uid") or au.get("uid"),
        "like": stat.get("digg_count"),
        "comment": stat.get("comment_count"),
        "duration": vid.get("duration"),
        "note_type": "image" if is_img else "video",
    }

def dy_search(keyword, cursor=0, video_only=True):
    # sort_type="0"=综合:实测它返回的视频评论本就含当天最新(sort_type="1" 反而更旧)。
    # 抓"最近用户"靠 leadgen 按评论 time 排序,不靠搜索排序。
    d = _p(DY + "/search/fetch_general_search_v1",
           keyword=keyword, cursor=int(cursor), sort_type="0",
           publish_time="0", filter_duration="0", content_type=("1" if video_only else "0"))
    items = []
    for it in (d.get("data") or []):
        if it.get("type") != 1:
            continue
        a = it.get("aweme_info") or {}
        if not a.get("aweme_id"):
            continue
        if video_only and not ((a.get("video") or {}).get("duration")):
            continue  # 只要视频时滤掉图文/图集；video_only=False 时连图文一起返回
        items.append(_dy_item(a))
    return {"items": items, "cursor": d.get("cursor"), "has_more": d.get("has_more")}

def dy_detail(id_or_url):
    aid = dy_aweme_id(id_or_url)
    a = {}
    for att in range(4):  # 偶发返回空，重试(带间隔)通常即恢复
        if att:
            time.sleep(0.5)
        a = (_g(DY + "/web/fetch_one_video", aweme_id=aid) or {}).get("aweme_detail") or {}
        if a:
            break
    vid = a.get("video") or {}
    stat = a.get("statistics") or {}
    au = a.get("author") or {}
    desc = a.get("desc") or ""
    images = [u for u in (_url0(im) for im in (a.get("images") or [])) if u]
    is_img = bool(images) or a.get("aweme_type") in (68, 2)
    play_node = vid.get("play_addr") or vid.get("play_addr_h264")
    play_urls = _urls(play_node)
    sec = au.get("sec_uid") or au.get("uid")
    return {
        "platform": "douyin", "id": aid,
        "url": "https://www.douyin.com/video/%s" % aid,
        "title": desc, "desc": desc, "tags": _tags_from_text(desc),
        "author": {"name": au.get("nickname"), "id": sec,
                   "fans": au.get("follower_count"), "ip": au.get("ip_location"),
                   "signature": au.get("signature"), "profile_url": _profile_url("douyin", sec)},
        "stats": {"like": stat.get("digg_count"), "comment": stat.get("comment_count"),
                  "share": stat.get("share_count"), "collect": stat.get("collect_count")},
        "cover": (images[0] if is_img and images else _url0(vid.get("cover"))),
        "images": images,
        "play_url": None if is_img else (play_urls[0] if play_urls else None),
        "play_urls": [] if is_img else play_urls,
        "subtitle_url": None, "decode_key": None,
        "duration": vid.get("duration"), "publish_time": a.get("create_time"),
        "note_type": "image" if is_img else "video",
    }

def dy_comments(id_or_url, cursor=0, count=20):
    aid = dy_aweme_id(id_or_url)
    d = _g(DY + "/web/fetch_video_comments", aweme_id=aid, cursor=int(cursor), count=int(count))
    items = []
    for c in (d.get("comments") or []):
        u = c.get("user") or {}
        uid = u.get("sec_uid") or u.get("uid")
        items.append({"text": c.get("text"), "ip": c.get("ip_label"),
                      "likes": c.get("digg_count"), "time": c.get("create_time"),
                      "user": u.get("nickname"), "user_id": uid,
                      "avatar": _url0(u.get("avatar_thumb")), "profile_url": _profile_url("douyin", uid),
                      "cid": c.get("cid"), "replies": c.get("reply_comment_total")})
    return {"items": items, "cursor": d.get("cursor"), "has_more": d.get("has_more"), "total": d.get("total")}


# ====================================================================
# 小红书 Xiaohongshu（统一走 app_v2）
# ====================================================================
XHS = "/api/v1/xiaohongshu/app_v2"

def xhs_search(keyword, page=1, note_type=""):
    # note_type 可选：不限 / 视频笔记 / 普通笔记 / 直播笔记
    d = _g(XHS + "/search_notes", keyword=keyword, page=page, note_type=note_type or None)
    items = []
    for it in ((d.get("data") or {}).get("items") or []):
        n = it.get("note") or {}
        if not n.get("id"):
            continue
        items.append({
            "platform": "xhs", "id": n.get("id"),
            "url": "https://www.xiaohongshu.com/explore/%s" % n.get("id"),
            "title": n.get("title") or (n.get("desc") or "")[:30],
            "cover": _xhs_img((n.get("images_list") or [{}])[0]),
            "author": (n.get("user") or {}).get("nickname"),
            "author_id": (n.get("user") or {}).get("userid"),
            "like": n.get("liked_count"), "comment": n.get("comments_count"),
            "note_type": "video" if n.get("type") == "video" else "image",
        })
    return {"items": items, "next_page": d.get("next_page")}

def _xhs_fetch(note_id, kind):
    if kind == "image":
        root = (_g(XHS + "/get_image_note_detail", note_id=note_id) or {}).get("data") or []
        return ((root[0] if root else {}).get("note_list") or [{}])[0]
    root = (_g(XHS + "/get_video_note_detail", note_id=note_id) or {}).get("data") or []
    return root[0] if root else {}

def xhs_detail(note_id, note_type="video"):
    # 贴链接时未必知道图文还是视频。坑：① 对错类型的 note_id 调详情，TikHub 会返回**无关随机笔记**
    # （有标题骗过"有内容"判断）→ 必须校验 .id==note_id；② 偶发对正确请求也返回错笔记（间歇性）
    # → id 不匹配就重试，通常一次即恢复。命中即停，正常只 1 次调用。
    order = ["image", "video"] if note_type == "image" else ["video", "image"]
    n = {}
    for att in range(4):
        if att:
            time.sleep(0.5)  # 给 TikHub 喘口气，transient 错通常即恢复
        for kind in order:
            try:
                cand = _xhs_fetch(note_id, kind) or {}
            except TikHubError:
                cand = {}
            if cand and str(cand.get("id") or "") == str(note_id):
                n = cand
                note_type = kind
                break
        if n:
            break
    desc = n.get("desc") or ""
    tags = [t.get("name") or t.get("link") for t in (n.get("hash_tag") or []) if isinstance(t, dict)] or _tags_from_text(desc)
    sub = None
    zh = (((n.get("video_info_v2") or {}).get("media") or {}).get("video") or {}).get("subtitles") or {}
    if zh.get("zh-CN"):
        sub = _first(zh["zh-CN"][0], "url") if isinstance(zh["zh-CN"][0], dict) else None
    play = None
    stream = (((n.get("video_info_v2") or {}).get("media") or {}).get("stream") or {})
    for codec in ("h264", "h265", "h266", "av1"):
        if stream.get(codec):
            play = (stream[codec][0] or {}).get("master_url")
            if play:
                break
    imgs = [u for u in (_xhs_img(im) for im in (n.get("images_list") or [])) if u]
    vimg = ((n.get("video_info_v2") or {}).get("image") or {})
    cover = vimg.get("first_frame") or vimg.get("thumbnail") or (imgs[0] if imgs else None)
    return {
        "platform": "xhs", "id": note_id,
        "url": "https://www.xiaohongshu.com/explore/%s" % note_id,
        "title": n.get("title") or desc[:30], "desc": desc, "tags": tags,
        "author": {"name": (n.get("user") or {}).get("nickname"),
                   "id": (n.get("user") or {}).get("userid"),
                   "fans": None, "ip": n.get("ip_location"), "signature": None,
                   "profile_url": _profile_url("xhs", (n.get("user") or {}).get("userid"))},
        "stats": {"like": n.get("liked_count"), "comment": n.get("comments_count"),
                  "share": n.get("shared_count"), "collect": n.get("collected_count")},
        "cover": cover, "images": (imgs if note_type == "image" else []),  # 画廊只给图文笔记(其type=normal，用循环匹配到的kind门控)
        "play_url": play, "subtitle_url": sub, "decode_key": None,
        "duration": None, "publish_time": n.get("time"),
        "note_type": note_type,
    }

def xhs_comments(note_id, cursor=None, count=20):
    d = (_g(XHS + "/get_note_comments", note_id=note_id) or {}).get("data") or {}
    items = []
    for c in (d.get("comments") or []):
        u = c.get("user") or {}
        uid = u.get("userid")
        items.append({"text": c.get("content"), "ip": c.get("ip_location"),
                      "likes": c.get("like_count"), "time": c.get("time"),
                      "user": u.get("nickname"), "user_id": uid, "red_id": u.get("red_id"),
                      "avatar": _xhs_img(u.get("images")), "profile_url": _profile_url("xhs", uid),
                      "cid": c.get("id"), "replies": c.get("sub_comment_count")})
    return {"items": items, "cursor": d.get("cursor"), "has_more": d.get("has_more"), "total": d.get("comment_count")}


# ====================================================================
# 视频号 WeChat Channels（盯号型：无全网搜，全 POST + raw=false）
# ====================================================================
CH = "/api/v1/wechat_channels/v2"

def ch_id_to_username(channel_id):
    """sph 短号 / channel_id → finder username（冷启动拿号入口）。"""
    d = _p(CH + "/fetch_channel_id_to_username", channel_id=channel_id, raw=False)
    return {"username": d.get("username"), "nickname": d.get("nickname"), "desc": d.get("desc")}

def ch_user_videos(username, last_buffer=""):
    d = _p(CH + "/fetch_user_videos", username=username, raw=True, last_buffer=last_buffer)  # raw=False 裁掉 objectDesc.media(无播放地址/decodeKey)，视频号下载必须 raw=True
    items = []
    for v in (d.get("videos") or []):
        # 视频号列表项的视频信息藏在 objectDesc.media[0]（与 detail 同构）；个别字段在顶层。
        od = v.get("objectDesc") or {}
        m = (od.get("media") or [{}])[0] if od.get("media") else (v.get("media") or {})
        title = od.get("description") or v.get("title") or ""
        items.append({
            "platform": "channels", "id": v.get("id") or od.get("id"),
            "url": None, "title": title,
            "cover": m.get("coverUrl") or m.get("cover_url") or m.get("thumbUrl"),
            "author": d.get("nickname"), "author_id": d.get("username"),
            "like": v.get("like_count") or od.get("likeCount"),
            "comment": v.get("comment_count") or od.get("commentCount"),
            # 视频号下载：url + urlToken 拼接才是带签名 token 的可下载直链(单 url 缺 token 会 400)。
            "play_url": _ch_play_url(m),
            "decode_key": str(m.get("decodeKey") or ""),  # 视频号流加密的 Isaac64 解密密钥，下载代理需用它解密才能播放
            "duration": m.get("videoPlayLen") or m.get("duration"), "note_type": "video",
        })
    return {"items": items, "nickname": d.get("nickname"), "username": d.get("username"),
            "last_buffer": d.get("last_buffer"), "has_more": d.get("up_continue")}

def _ch_play_url(media):
    """视频号可下载直链 = url + urlToken 拼接。TikHub 把直链拆两段：url(缺 token，单用 400) + urlToken('&token=...&sign=...')。
    优先 nonWatermarkUrl(无水印)，其次 url。地址带 encfilekey+token 时效，详情 1h 内安全。"""
    base = media.get("nonWatermarkUrl") or media.get("url") or media.get("fullUrl") or ""
    return (base + (media.get("urlToken") or "")) if base else None

def _ch_cover_url(media):
    """视频号封面直链 = coverUrl + coverUrlToken 拼接。封面本身不加密(普通 JPEG)，
    但 coverUrl 缺 token 单用报 400（同 play_url 的 url/urlToken 两段式）；带 token 后可直连加载。
    token 有时效（~1h），采集侧再转存 COS 保永久。"""
    base = media.get("coverUrl") or media.get("fullCoverUrl") or ""
    return (base + (media.get("coverUrlToken") or media.get("fullCoverUrlToken") or "")) if base else None

_CH_DECRYPT_API = os.environ.get("CH_DECRYPT_API", "http://127.0.0.1:3001/api/decrypt")  # Isaac64 WASM 解密服务(同下载代理 dl_service)
_CH_REFERER = "https://channels.weixin.qq.com/"

def _ch_download_decrypt(play_url, decode_key, dest_path, deadline_ts, max_bytes=100_000_000):
    """视频号：下载 encfilekey 加密流(带微信 Referer) → 调 :3001 Isaac64 解密 → 落盘可播 mp4。
    play_url = url+urlToken；decode_key = media.decodeKey。成功返回 dest_path。"""
    fd, enc = tempfile.mkstemp(suffix=".enc", prefix="hqch-"); os.close(fd)
    try:
        remain = deadline_ts - time.time()
        if remain <= 0:
            raise TimeoutError("下载预算已耗尽")
        req = urllib.request.Request(play_url, headers={"User-Agent": UA, "Referer": _CH_REFERER})
        got = 0
        with _OPENER.open(req, timeout=min(30, remain)) as r, open(enc, "wb") as f:
            while True:
                if time.time() >= deadline_ts:
                    raise TimeoutError("视频号下载超预算（已下 %.1fMB）" % (got / 1048576.0))
                block = r.read(262144)
                if not block:
                    break
                got += len(block)
                if got > max_bytes:
                    raise ValueError("视频号文件超上限 %.0fMB" % (max_bytes / 1048576.0))
                f.write(block)
        p = subprocess.run(["curl", "-sS", "-X", "POST", _CH_DECRYPT_API,
                            "-F", "decode_key=" + str(decode_key), "-F", "video=@" + enc, "-o", dest_path],
                           capture_output=True, timeout=180)
        if p.returncode != 0 or not os.path.exists(dest_path) or not os.path.getsize(dest_path):
            raise TikHubError("视频号解密失败：" + (p.stderr[-120:].decode("u8", "ignore") if p.stderr else "空输出"))
        return dest_path
    finally:
        try: os.unlink(enc)
        except OSError: pass

def ch_detail(object_id):
    s = str(object_id)
    loc = {"share_url": s} if ("://" in s or "weixin" in s) else {"object_id": s}
    d = _p(CH + "/fetch_video_detail", raw=True, **loc)  # raw=False 会裁掉 objectDesc.media(无播放地址)，视频号下载必须 raw=True
    obj = d.get("objectDesc") or {}
    media = (obj.get("media") or [{}])[0] or {}  # 视频号真实字段都在 objectDesc.media[0]
    title = obj.get("description") or obj.get("shortTitle") or ""
    play = _ch_play_url(media)
    # ponytail: TikHub 偶发返回缺播放地址或解密密钥的不完整 media，重取一次即可。
    if not play or not media.get("decodeKey"):
        d = _p(CH + "/fetch_video_detail", raw=True, **loc)
        obj = d.get("objectDesc") or {}
        media = (obj.get("media") or [{}])[0] or {}
        title = obj.get("description") or obj.get("shortTitle") or title
        play = _ch_play_url(media)
    return {
        "platform": "channels", "id": d.get("id") or object_id, "url": None,
        "title": title, "desc": title, "tags": _tags_from_text(title),
        "author": {"name": d.get("nickname"), "id": d.get("username"),
                   "fans": None, "ip": None, "signature": None},
        "stats": {"like": d.get("likeCount"), "comment": d.get("commentCount"),
                  "share": d.get("forwardCount"), "collect": d.get("favCount")},
        "cover": _ch_cover_url(media),
        "play_url": play,  # 视频号下载直链(有时效，详情 1h 内安全)；None 时前端不显示下载按钮
        "subtitle_url": None, "decode_key": media.get("decodeKey"),
        "duration": media.get("videoPlayLen") or media.get("duration"),
        "publish_time": d.get("createtime"),
        "note_type": "video",
    }

def ch_comments(object_id, last_buffer=""):
    d = _p(CH + "/fetch_video_comments", object_id=str(object_id), raw=False, last_buffer=last_buffer)
    items = []
    for c in (d.get("comments") or []):
        items.append({"text": c.get("content"), "ip": c.get("ip_region"),
                      "likes": c.get("like_count"), "time": c.get("create_time"),
                      "user": c.get("nickname"), "user_id": c.get("username"),
                      "avatar": c.get("head_url"), "profile_url": None,  # 视频号 finder 号无网页主页
                      "cid": c.get("comment_id"), "replies": c.get("reply_count")})
    return {"items": items, "cursor": d.get("last_buffer"), "has_more": d.get("down_continue"), "total": None}


# ====================================================================
# 链接解析：贴任意分享链/短链 → {platform, id, note_type}
# ====================================================================
# 从分享文案里抠 URL：只吃合法 URL 字符（RFC3986），天然在中文/省略号/全角标点处断开。
# 抖音分享常见「…https://v.douyin.com/xxx/…复制此链接，打开抖音」——链接后直接粘中文，
# 旧的 https?://[^\s]+ 会把后面的中文一并吞进来 → 短链带脏字 → get_aweme_id 400 失败。
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_DY_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")
_DY_HOSTS = {"v.douyin.com", "douyinvod.com"}
_XHS_HOST_SUFFIXES = ("xiaohongshu.com", "xhslink.com", "xhslink.cn")

def _is_xhs_url(url):
    parsed = urllib.parse.urlparse(url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in ("http", "https") and any(host == suffix or host.endswith("." + suffix) for suffix in _XHS_HOST_SUFFIXES)

class _XhsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_xhs_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "unsafe Xiaohongshu redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_XHS_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _XhsRedirectHandler())

def _extract_url(text):
    """从分享文案里提取第一个干净 URL；没有则返回 None（口令式分享无 URL）。"""
    m = _URL_RE.search(text or "")
    return m.group(0) if m else None

def _is_douyin_url(url):
    """只按 hostname 判断抖音链接，避免 notdouyin.com 这类子串误判。"""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower().rstrip(".")
    return host in _DY_HOSTS or any(host == suffix or host.endswith("." + suffix) for suffix in _DY_HOST_SUFFIXES)

def _xhs_note_id(url):
    m = re.search(r"(?:explore|discovery/item|item)/([0-9a-fA-F]+)", url or "")
    if m:
        return m.group(1)
    if not _is_xhs_url(url):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with _XHS_OPENER.open(req, timeout=10) as r:
            final_url = r.geturl()
        if not _is_xhs_url(final_url):
            return None
        m = re.search(r"(?:explore|discovery/item|item)/([0-9a-fA-F]+)", final_url)
        return m.group(1) if m else None
    except (OSError, urllib.error.URLError):
        return None

def dy_share_code(text):
    """抖音「口令式」无链接分享 → aweme_id（best-effort）。
    ⚠ 抖音风控(shark)会拦截服务端口令解析，实测多数返回 invalid_command/shark_fail，
    故这是兜底：主路径永远是从文案里提取 v.douyin.com/iesdouyin 链接再解。"""
    try:
        r = _g("/api/v1/douyin/app/v3/fetch_share_info_by_share_code", share_code=(text or "").strip())
    except TikHubError:
        return None
    if not isinstance(r, dict) or r.get("invalid_command"):
        return None
    blob = json.dumps(r)   # 成功时在返回里找 aweme_id / schema / 链接
    m = re.search(r"/video/(\d+)", blob) or re.search(r"(\d{15,21})", blob)
    return m.group(1) if m else None

def dy_resolve(url):
    """抖音链接/短链/纯 id → aweme_id。非抖音链接返回 None（不再把杂串瞎丢给上游）。"""
    url = (url or "").strip()
    m = re.fullmatch(r"\d{15,21}", url)
    if m:
        return url
    if _is_douyin_url(url):
        m = re.search(r"/video/(\d{15,21})", url)
        if m:
            return m.group(1)
        # v.douyin.com 短链 / iesdouyin 分享链 → 解出 aweme_id
        r = _g("/api/v1/douyin/web/get_aweme_id", url=url)
        return r if isinstance(r, str) else (r.get("aweme_id") if isinstance(r, dict) else None)
    return None

def parse_link(text):
    text = text or ""
    url = _extract_url(text)         # 干净 URL（截断粘连的中文）；口令式分享无 URL → None
    probe = (url or text).strip()
    low = probe.lower()
    if _is_xhs_url(probe):
        return {"platform": "xhs", "id": _xhs_note_id(probe), "note_type": None}
    if "weixin.qq.com" in low or "/sph" in low or "channels" in low or "finder" in low:
        return {"platform": "channels", "id": probe, "note_type": "video"}
    # 抖音：优先链接解析；纯口令(无链接)退回 best-effort 口令解析
    aid = dy_resolve(probe)
    if not aid and url is None:
        aid = dy_share_code(text)
    return {"platform": "douyin", "id": aid, "note_type": "video"}


# ====================================================================
# 统一调度（content_api 用这层，不直接碰平台函数）
# ====================================================================
def _search(platform, keyword, page=1, video_only=True):
    if platform == "douyin": return dy_search(keyword, cursor=(page - 1) * 10, video_only=video_only)
    if platform == "xhs":    return xhs_search(keyword, page=page, note_type="视频笔记" if video_only else "")
    if platform == "channels":
        raise TikHubError("视频号无全网关键词搜索，请用 sph 短号/finder 走盯号采集")
    raise TikHubError("未知平台 " + str(platform))

def _detail(platform, id_or_url, note_type="video"):
    if platform == "douyin":   return dy_detail(id_or_url)
    if platform == "xhs":      return xhs_detail(id_or_url, note_type=note_type)
    if platform == "channels": return ch_detail(id_or_url)
    raise TikHubError("未知平台 " + str(platform))

def _comments(platform, id_or_url, cursor=None, count=20):
    if platform == "douyin":   return dy_comments(id_or_url, cursor=cursor or 0, count=count)
    if platform == "xhs":      return xhs_comments(id_or_url)
    if platform == "channels": return ch_comments(id_or_url, last_buffer=cursor or "")
    raise TikHubError("未知平台 " + str(platform))

# 带缓存的对外入口：内容(详情)发布即固定→存 1h；评论会增→存 1h；搜索会变→存 30min。
# 任一函数传 fresh=True 可绕过缓存强制重取（给"刷新最新"用）。
def search(platform, keyword, page=1, video_only=True, fresh=False):
    key = "srch:%s:%s:%s:%d" % (platform, keyword, page, int(video_only))
    if not fresh:
        hit = _cache_get(key)
        if hit is not None: return hit
    r = _search(platform, keyword, page=page, video_only=video_only)
    _cache_set(key, r, 1800)
    return r

def detail(platform, id_or_url, note_type="video", fresh=False):
    key = "det:%s:%s" % (platform, id_or_url)
    # ponytail: 视频号不缓存 detail(读/写都不)——play_url(url+urlToken) 带时效且需新鲜直链解密，
    # 旧缓存(play_url=None)会让下载按钮永远不显示；抖音/小红书 detail 固定，照常缓存 1h。
    if not fresh and platform != "channels":
        hit = _cache_get(key)
        if hit is not None: return hit
    r = _detail(platform, id_or_url, note_type=note_type)
    if platform != "channels":
        _cache_set(key, r, 3600)
    return r

def comments(platform, id_or_url, cursor=None, count=20, fresh=False):
    key = "cmt:%s:%s:%s:%s" % (platform, id_or_url, cursor, count)
    if not fresh:
        hit = _cache_get(key)
        if hit is not None: return hit
    r = _comments(platform, id_or_url, cursor=cursor, count=count)
    _cache_set(key, r, 3600)
    return r


# ====================================================================
# 口播文案：小红书优先白嫖 .srt 字幕；抖音下载 mp4 跑 whisper；视频号 v1 跳过
# ====================================================================
def _http_get(url, max_bytes=26_000_000, timeout=60):
    """⚠ timeout 只管单次 socket 读：慢 CDN 每次都在 timeout 内吐一点数据就能无限续命，
    总耗时不受控。要硬上限请用 http_get_budgeted()。此函数保留给小文件(字幕等)。"""
    req = urllib.request.Request(url, headers=cdn_headers(url))
    with _OPENER.open(req, timeout=timeout) as r:  # CDN 直连，绕过环境代理
        return r.read(max_bytes)

# ASR 下载预算：下载顶过 reaper 判死线会导致「判死退点 → worker 又写回 done」的双发事故
ASR_DL_DEADLINE = int(os.environ.get("ASR_DOWNLOAD_DEADLINE", "120"))

DOUYIN_CDN_SUFFIXES = (
    "zjcdn.com", "douyinvod.com", "douyinstatic.com", "douyinpic.com", "amemv.com",
    "bytecdn.cn", "ixigua.com", "pstatp.com", "snssdk.com", "byteimg.com",
    "bytedance.net", "lf-douyin.com", "365yg.com",
)

def cdn_headers(url):
    headers = {"User-Agent": UA}
    if (urllib.parse.urlparse(url).hostname or "").lower().endswith(DOUYIN_CDN_SUFFIXES):
        headers["Referer"] = "https://www.douyin.com/"
    return headers

def download_to_file(url, deadline_ts, dest_path, max_bytes=26_000_000, read_timeout=30):
    """流式下载到文件，内存恒定（一次只驻留 256KB）。返回落盘字节数。

    采集视频最大 100MB，原来先在内存里攒成 bytes 再 b"".join（拼接瞬间双份），
    而 leadgen 的 do_POST 每个请求直接起一个不限流的线程 —— 并发一高就是 OOM。
    落盘之后 ffmpeg 直接读这个文件、COS 也从文件上传，反而少一次写。
    """
    remain = deadline_ts - time.time()
    if remain <= 0:
        raise TimeoutError("下载预算已耗尽")
    req = urllib.request.Request(url, headers=cdn_headers(url))
    got = 0
    with _OPENER.open(req, timeout=min(read_timeout, remain)) as r, open(dest_path, "wb") as f:
        declared = r.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("文件 %.1fMB 超过上限 %.0fMB" % (int(declared) / 1048576.0, max_bytes / 1048576.0))
        while True:
            if time.time() >= deadline_ts:
                raise TimeoutError("下载超过预算（已下载 %.1fMB）" % (got / 1048576.0))
            block = r.read(262144)
            if not block:
                break
            got += len(block)
            if got > max_bytes:
                raise ValueError("文件超过上限 %.0fMB" % (max_bytes / 1048576.0))
            f.write(block)
    return got


def http_get_budgeted(url, deadline_ts, max_bytes=26_000_000, read_timeout=30):
    """流式拉取，总耗时受 deadline_ts 约束（绝对时间戳）。

    分块读 + 每块检查预算，把总耗时钉死；Content-Length 预检可在下载前否掉超大文件。
    超预算/超限抛异常，绝不静默截断——旧的 r.read(max_bytes) 会把超大文件截断成
    残缺数据却当成功返回。
    """
    remain = deadline_ts - time.time()
    if remain <= 0:
        raise TimeoutError("下载预算已耗尽")
    req = urllib.request.Request(url, headers=cdn_headers(url))
    with _OPENER.open(req, timeout=min(read_timeout, remain)) as r:  # CDN 直连，绕过环境代理
        declared = r.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise ValueError("文件 %.1fMB 超过上限 %.0fMB" % (int(declared) / 1048576.0, max_bytes / 1048576.0))
        chunks, got = [], 0
        while True:
            if time.time() >= deadline_ts:
                raise TimeoutError("下载超过预算（已下载 %.1fMB）" % (got / 1048576.0))
            block = r.read(262144)
            if not block:
                break
            got += len(block)
            if got > max_bytes:
                raise ValueError("文件超过上限 %.0fMB" % (max_bytes / 1048576.0))
            chunks.append(block)
    return b"".join(chunks)

def _log_asr_step(step, start, **extra):
    fields = ["%s=%s" % (k, v) for k, v in sorted(extra.items()) if v is not None]
    suffix = (" " + " ".join(fields)) if fields else ""
    print("[asr] %s %.2fs%s" % (step, time.time() - start, suffix), flush=True)


def _openai_url(path):
    base = str(OPENAI_BASE or "https://api.openai.com").strip().rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base + "/" + str(path or "").lstrip("/")

def _srt_to_text(srt):
    out = []
    for line in srt.splitlines():
        line = line.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        out.append(line)
    return " ".join(out)

def _extract_audio(mp4_path):
    """ffmpeg 抽音轨 → 低码率 mp3（16kHz 单声道 64k）。音频体积仅 mp4 的 1/10~1/20，
    规避 OpenAI 转写端点 25MB 上限（长/高清视频也能转）、上传更快。失败抛异常由上层兜底。

    入参是【文件路径】：视频本来就是流式落盘的，ffmpeg 直接读它，不必先读回内存。
    """
    p = subprocess.run(
        ["ffmpeg", "-y", "-i", mp4_path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", "-f", "mp3", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if p.returncode != 0 or not p.stdout:
        raise TikHubError("ffmpeg 抽音轨失败：" + (p.stderr[-160:].decode("u8", "ignore") if p.stderr else "无输出"))
    return p.stdout

def _whisper(mp4_path, filename="v.mp4"):
    """入参是落盘的 mp4 路径。抽出来的音轨只有原视频的 1/10~1/20，才需要读进内存上传。"""
    if not OPENAI_KEY:
        raise TikHubError("OPENAI_API_KEY 未配置，无法 ASR")
    with _TRANSCRIBE_SEM:                     # 限并发转写：多任务同挤 OpenAI ASR 会互相拖垮，排队一个个来
        t0 = time.time()
        video_bytes = os.path.getsize(mp4_path)
        try:                                  # 优先抽音轨转 mp3（小、快、不撞 25MB）
            t_extract = time.time()
            audio, aname, ctype = _extract_audio(mp4_path), "a.mp3", "audio/mpeg"
            _log_asr_step("extract_audio", t_extract, input_bytes=video_bytes, audio_bytes=len(audio))
        except Exception as e:                # ffmpeg 出问题兜底：直接传原 mp4（老行为，赌 <25MB）
            with open(mp4_path, "rb") as f:   # 只有这条罕见兜底路径才把整个视频读进内存
                audio = f.read()
            aname, ctype = filename, "video/mp4"
            _log_asr_step("extract_audio_fallback", t_extract, input_bytes=video_bytes, reason=str(e)[:80])
        b = "----hqtikhub7e3f"
        parts = [("--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n%s\r\n" % (b, TRANSCRIBE_MODEL)).encode(),
                 ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (b, aname, ctype)).encode(),
                 audio, b"\r\n", ("--%s--\r\n" % b).encode()]
        body = b"".join(parts)
        req = urllib.request.Request(_openai_url("audio/transcriptions"), data=body,
                                     headers={"Authorization": "Bearer " + OPENAI_KEY,
                                              "Content-Type": "multipart/form-data; boundary=" + b}, method="POST")
        try:
            t_openai = time.time()
            with urllib.request.urlopen(req, timeout=TRANSCRIBE_TIMEOUT) as r:
                text = json.loads(r.read()).get("text", "").strip()
            _log_asr_step("openai_transcribe", t_openai, model=TRANSCRIBE_MODEL, upload_bytes=len(body), text_chars=len(text))
            _log_asr_step("total", t0)
            return text
        except TimeoutError:
            raise TikHubError("OpenAI ASR 超时(%ss)，请稍后重试" % TRANSCRIBE_TIMEOUT)
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), TimeoutError):
                raise TikHubError("OpenAI ASR 超时(%ss)，请稍后重试" % TRANSCRIBE_TIMEOUT)
            raise

def transcript(det, video_path=None):
    """det = detail() 的返回。返回 {text, source} 或 None。

    video_path：调用方已经下好的 mp4 文件路径，传进来就不用再下一遍（文件归调用方删）。
    采集流程里 COS 转存刚下过同一个 play_url —— 线上 job 1354 实测同一个 5.1MB 文件
    被下了两次，第一次(转存)耗时 130s 且超时失败，第二次(ASR)只花 20.5s。
    """
    if det.get("platform") == "channels":
        # 视频号是 Isaac64 加密流，whisper 直接解不了；先下载→:3001 解密成可播 mp4→再 ASR。
        pu, dk = det.get("play_url"), str(det.get("decode_key") or "")
        if not pu or not dk:
            return None  # 不完整 media（缺播放地址/解密密钥）→ 放弃 ASR，不报错
        fd, dec = tempfile.mkstemp(suffix=".mp4", prefix="hqchdec-"); os.close(fd)
        try:
            _ch_download_decrypt(pu, dk, dec, time.time() + ASR_DL_DEADLINE)
            return {"text": _whisper(dec), "source": "asr"}
        except Exception as e:
            raise TikHubError("视频号 ASR 失败：" + str(e)[:120])
        finally:
            try: os.unlink(dec)
            except OSError: pass
    if det.get("subtitle_url"):  # 小红书视频笔记白送逐字稿
        try:
            return {"text": _srt_to_text(_http_get(det["subtitle_url"]).decode("u8", "ignore")), "source": "subtitle"}
        except Exception:
            pass
    if video_path:   # 复用调用方下好的文件，省掉一次完整下载
        try:
            _log_asr_step("reuse_video", time.time(), bytes=os.path.getsize(video_path))
            return {"text": _whisper(video_path), "source": "asr"}
        except Exception as e:
            raise TikHubError("ASR 失败：" + str(e)[:120])
    if det.get("play_url"):  # 抖音：下载无水印 mp4 → whisper（短视频普遍 <25MB）
        fd, path = tempfile.mkstemp(suffix=".mp4", prefix="hqasr-")
        os.close(fd)
        try:
            t_download = time.time()
            # 带总预算：慢 CDN 下 _http_get 的 timeout 会被反复续命，把 collect 任务顶过
            # reaper 判死线，导致「判死退点 → worker 又写回 done」(线上 job 1118)
            n = download_to_file(det["play_url"], time.time() + ASR_DL_DEADLINE, path)
            _log_asr_step("download_video", t_download, bytes=n)
            return {"text": _whisper(path), "source": "asr"}
        except Exception as e:
            raise TikHubError("ASR 失败：" + str(e)[:120])
        finally:
            try: os.unlink(path)
            except OSError: pass
    return None


# ====================================================================
# selftest：live 验三平台（复诊铁律——真调，贴结果）
# ====================================================================
def _selftest():
    bal = _g("/api/v1/tikhub/user/get_user_info")
    print("✓ user_info  余额:", (bal.get("user_data") or {}).get("balance") if isinstance(bal, dict) else bal)
    r = dy_search("美甲加盟")
    assert r["items"], "抖音搜索空"
    print("✓ douyin.search  %d 条，样例: %s" % (len(r["items"]), (r["items"][0]["title"] or "")[:24]))
    aid = r["items"][0]["id"]
    cm = dy_comments(aid, count=5)
    assert cm["items"], "抖音评论空"
    print("✓ douyin.comments  %d 条，ip 样例: %s" % (len(cm["items"]), cm["items"][0].get("ip")))
    x = xhs_search("美甲", note_type="视频笔记")
    assert x["items"], "小红书搜索空"
    print("✓ xhs.search  %d 条，样例: %s" % (len(x["items"]), (x["items"][0]["title"] or "")[:24]))
    xd = xhs_detail(x["items"][0]["id"], note_type="video")
    print("✓ xhs.detail  有字幕:%s 有正文:%s" % (bool(xd.get("subtitle_url")), bool(xd.get("desc"))))
    xc = xhs_comments(x["items"][0]["id"])
    print("✓ xhs.comments  %d 条，ip 样例: %s" % (len(xc["items"]), (xc["items"][0].get("ip") if xc["items"] else None)))
    u = ch_id_to_username("sphi9BjV8GK0Zsl")  # 人民日报 sph 短号 → finder
    assert u.get("username"), "视频号 id→username 空"
    print("✓ channels.id_to_username  %s (%s)" % (u.get("nickname"), (u.get("username") or "")[:18] + "…"))
    print("\n全部 live 通过 ✅  base=%s" % BASE)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("tikhub client. platforms=%s  base=%s  key=%s" % (PLATFORMS, BASE, "set" if KEY else "MISSING"))
