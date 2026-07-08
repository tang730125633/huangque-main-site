# -*- coding: utf-8 -*-
import hashlib

from .core import _collect_cos_play_url, re, tikhub

def gen_collect(payload):
    platform = (payload.get("platform") or "douyin").strip()
    raw = (payload.get("url") or payload.get("id") or "").strip()
    if not raw:
        raise ValueError("缺少链接或 id")
    note_type = payload.get("note_type") or "video"
    if payload.get("url") and not payload.get("id"):   # 贴链接：解析出平台+id+类型（短链也认）
        info = tikhub.parse_link(payload["url"])
        platform = info.get("platform") or platform
        ident = info.get("id")
        note_type = info.get("note_type")
        if not ident:
            raise ValueError("没解析出视频链接：若是抖音「口令」（如 x.xx CQ:/ … 复制打开抖音），"
                             "因平台风控无法直接解析，请在抖音里改用「复制链接」分享（含 v.douyin.com 链接）后再粘贴；"
                             "或改用关键词搜索。")
    else:
        ident = raw
    if platform not in tikhub.PLATFORMS:
        raise ValueError("未知平台")
    want = payload.get("want") or ["copy", "comments"]
    det = tikhub.detail(platform, ident, note_type=note_type)
    if not (det.get("title") or det.get("desc") or det.get("images")):
        # 内容全空 = TikHub 偶发限流/抽风 或 私密/已删 → 报错退点，让前端提示重试，别甩空卡片
        raise ValueError("内容获取失败（可能是上游限流或内容私密/已删），请重试")
    au = det.get("author") or {}
    # 视频号(channels)加密流不转存 COS——转存会存成加密数据且丢 decode_key；保持原 wxapp.tc.qq.com 直链由前端下载代理解密。
    play_url = det.get("play_url") if platform == "channels" else _collect_cos_play_url(platform, det.get("id") or ident, det.get("play_url"))
    out = {
        "type": "collect", "platform": platform, "source": det.get("url") or ident,
        "video": {"title": det.get("title"), "author": au.get("name"), "authorAvatar": None,
                  "profile_url": au.get("profile_url"),
                  "cover": det.get("cover"), "play_url": play_url, "url": det.get("url"),
                  "duration": det.get("duration"), "publish_time": det.get("publish_time"),
                  "stats": det.get("stats"),
                  "decode_key": det.get("decode_key")},  # 视频号加密流解密密钥；前端下载代理 /api/gen/dl?dk= 需它解密
        "copy": {"title": det.get("title"), "desc": det.get("desc"), "tags": det.get("tags")},
        "images": det.get("images") or [],   # 图文笔记的全部图片
        "transcript": None, "comments": [], "comments_more": False,
        "url": det.get("cover"), "prompt": det.get("title"),  # 给通用 history 用（封面+标题）
    }
    if "comments" in want:
        cm = tikhub.comments(platform, det.get("id") or ident, count=int(payload.get("comment_count") or 20))
        out["comments"] = cm["items"]; out["comments_more"] = bool(cm.get("has_more"))
    if "transcript" in want:
        try:
            out["transcript"] = tikhub.transcript(det)
        except tikhub.TikHubError as e:
            out["transcript"] = {"text": None, "error": str(e)[:120]}
    return out

# ============ 获客能力：关键词→搜视频→扒评论→意图过滤→客户名单 ============
# 意图规则镜像 scripts/leads_filter.py（调词两边同步）。
_SPAM = ["需要我推荐", "推荐给你", "先帮店做出业绩", "做出业绩再合作", "做出业绩再分润",
         "不需要店家出成本", "不需要我先出成本", "W的业绩", "万的业绩", "免费送模式",
         "0成本启动", "感兴趣的老板", "一起交流交流", "下店来打版"]
_HIGH = ["怎么拓客", "怎么收费", "怎么弄", "怎么做", "怎么操作", "怎么整", "怎么合作", "怎么矩阵",
         "多少钱", "价位", "求带", "带带", "带一带", "想学", "有偿", "预算", "求助", "求推荐",
         "靠谱的拓客", "有没有靠谱", "哪里下载", "谁能帮我", "我也想", "没开单", "怎么收费的",
         "想找", "教一下", "怎么回", "我该怎么", "到底", "求带带", "也想",
         "有效果吗", "效果怎么样", "会反弹", "反弹吗", "能瘦", "痛吗", "维持多久", "做一次",
         "几次", "安全吗", "在哪做", "怎么预约", "约一个", "想做", "想咨询", "哪家好",
         "怎么联系", "贵吗", "价格", "多少钱一次", "可以瘦吗", "有用吗", "求地址"]
def _is_spam(t): return any(k in t for k in _SPAM)
def _is_high(t): return any(k in t for k in _HIGH)

def _lead_text_key(text):
    return re.sub(r"\s+", "", re.sub(r"\[[^\]]+\]", "", text or "")).strip().lower()

def _lead_identity(comment):
    platform = comment.get("platform") or ""
    user_id = comment.get("user_id") or comment.get("nickname") or ""
    content_key = _lead_text_key(comment.get("content"))
    return "%s:%s:%s" % (platform, user_id, content_key)

def _lead_id(comment):
    raw = _lead_identity(comment)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

def _intent_label(text):
    t = text or ""
    if any(k in t for k in ("怎么联系", "怎么预约", "约一个", "想咨询", "求地址", "在哪做")):
        return "咨询"
    if any(k in t for k in ("多少钱", "价格", "贵吗", "多少钱一次", "价位")):
        return "价格敏感"
    return "高意向"

def gen_leads(payload):
    keyword   = (payload.get("keyword") or "").strip()
    platforms = payload.get("platforms") or ["douyin"]
    nvid      = max(1, min(30, int(payload.get("count") or 12)))
    pages     = max(1, min(3, int(payload.get("pages") or 1)))
    targets   = payload.get("channels_targets") or []   # 视频号盯号：sph 短号 / finder username 列表
    raw = []   # 评论汇总（字段对齐 _is_spam/_is_high 过滤）

    def pull(platform, vid_id, title):
        for pg in range(pages):
            try:
                cm = tikhub.comments(platform, vid_id, cursor=(pg * 20 if platform == "douyin" else None), count=20)
            except tikhub.TikHubError:
                break
            for c in cm["items"]:
                raw.append({"content": c.get("text"), "user_id": c.get("user_id"), "nickname": c.get("user"),
                            "ip_location": c.get("ip"), "like_count": c.get("likes") or 0,
                            "profile_url": c.get("profile_url"), "platform": platform, "source": title})
            if not cm.get("has_more"):
                break

    for platform in platforms:
        if platform == "channels":
            continue  # 视频号无全网搜，走下面盯号
        if not keyword:
            continue
        try:
            sr = tikhub.search(platform, keyword)
        except tikhub.TikHubError:
            continue
        for v in sr["items"][:nvid]:
            pull(platform, v["id"], v.get("title"))

    if "channels" in platforms:
        for tgt in targets:
            try:
                uname = tgt if "@finder" in tgt else (tikhub.ch_id_to_username(tgt).get("username"))
                if not uname:
                    continue
                for v in tikhub.ch_user_videos(uname)["items"][:nvid]:
                    pull("channels", v["id"], v.get("title"))
            except tikhub.TikHubError:
                continue

    leads, spam, chat, deduped, seen = [], 0, 0, 0, set()
    for c in raw:
        t = (c.get("content") or "").strip()
        if not t:
            continue
        if _is_spam(t):
            spam += 1; continue
        if len(re.sub(r"\[[^\]]+\]", "", t).strip()) < 2:
            chat += 1; continue
        if _is_high(t):
            k = _lead_identity(c)
            if k in seen:
                deduped += 1
                continue
            seen.add(k); leads.append(c)
        else:
            chat += 1
    leads.sort(key=lambda c: (len(c.get("content", "")), c.get("like_count", 0)), reverse=True)
    out_leads = [{"lead_id": _lead_id(c), "nickname": c.get("nickname"), "user_unique_id": c.get("user_id"),
                  "ip_location": c.get("ip_location"), "content": c.get("content"),
                  "title": c.get("source"), "platform": c.get("platform"),
                  "profile_url": c.get("profile_url"), "intent": _intent_label(c.get("content")),
                  "follow_status": "待跟进", "follow_note": ""} for c in leads]
    return {"type": "leads", "keyword": keyword, "platforms": platforms,
            "leads_count": len(out_leads), "spam": spam, "chat": chat, "deduped": deduped, "total": len(raw),
            "leads": out_leads, "url": None, "prompt": keyword}

HANDLERS = {"collect": gen_collect, "leads": gen_leads}
