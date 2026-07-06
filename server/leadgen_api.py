#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄雀 · 采集获客后端 —— 故意独立于 content_api.py（Tang 负责这摊）。
背景：content_api.py 被多人共改(我的采集/获客 vs 同事的音频/豆包)反复互相覆盖。把采集/获客拆成
独立服务 + 独立端口(8100) + 独立 systemd 单元，nginx 把 /api/gen/collect、/api/gen/collect/search、
/api/gen/leads 精确路由过来；同事怎么改/重启 content_api 都碰不到这里。

共用基础设施(不重复造)：
- 任务库 content_jobs.db：仍写同一个库 → 前端轮询 /api/gen/job/{id} 和「资产/历史」由 content_api 读，照常工作。
- 点数 users.db、登录 auth(:8095)：和 content_api 共用同一套，点数统一。
- 清道夫 reaper：由 content_api 跑(同一个库)，这里不重复。
依赖 同目录 tikhub.py（抖音/小红书/视频号客户端，自带限流/重试）。systemd 加载同一份 content.env。
"""
import os, re, sqlite3, json, time, threading, urllib.request, urllib.parse, urllib.error
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tikhub

PORT      = int(os.environ.get("LEADGEN_API_PORT", "8100"))
AUTH_BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")
AUTH_DB   = os.environ.get("AUTH_DB", "/home/ubuntu/auth-service/users.db")
JOB_DB    = os.environ.get("CONTENT_JOB_DB", "/home/ubuntu/content-api/content_jobs.db")  # 共用 content_api 的任务库
COS_COLLECT = os.environ.get("COS_COLLECT", "1").strip().lower() not in ("0", "false", "no")  # 采集视频转存 COS 开关


# ============ 采集视频转存 COS（永久直链；未配置/失败/关闭时回退原 CDN 链接） ============
def public_url_from_remote(remote_url, rel_key, content_type=None):
    """远程 URL(如抖音 CDN 直链)字节 → COS 永久直链。
    COS 已启用且 remote_url 非空 → urllib 拉字节(带 UA/超时) → cos put → 返回直链；
    未配置 / COS_COLLECT=0 / 拉取失败 / 上传失败 → 返回原 remote_url（回退，绝不因转存失败中断采集）。"""
    remote_url = (remote_url or "").strip()
    if not remote_url or not COS_COLLECT:
        return remote_url
    try:
        from content_domains import cos
        if not cos.enabled():
            return remote_url
        # 采集视频可能较大，增加超时和大小限制；加简单重试避免偶发网络抖动导致回退过期链接
        # 注意：总时长需 < reaper 的 360s（collect 只有默认 6 分钟宽限），建议 ≤240s
        for attempt in range(2):
            try:
                data = tikhub._http_get(remote_url, timeout=120, max_bytes=100 * 1024 * 1024)
                if data:
                    return cos.put_bytes(data, str(rel_key), content_type)
            except Exception as e:
                if attempt == 1:
                    print("[cos] 采集转存失败(重试后), 回退原链接: %s -> %s" % (rel_key, e), flush=True)
                else:
                    print("[cos] 采集转存第1次失败，重试: %s" % e, flush=True)
        return remote_url
    except Exception as e:
        print("[cos] 采集转存失败，回退原链接: %s -> %s" % (rel_key, e), flush=True)
        return remote_url

def _collect_cos_play_url(platform, vid_id, play_url):
    """采集视频 play_url → COS 永久直链。图集/无 play_url 跳过、保持原样。
    对象键 collect/<platform>/<id>.mp4。转存失败/未配置回退原 play_url。
    注意：视频号(channels)是加密流，不能走这里直存——用 _collect_channels_play_url 先解密。"""
    if not play_url:
        return play_url
    ident = re.sub(r"[^A-Za-z0-9_.-]", "", str(vid_id or "")) or "v"
    key = "collect/%s/%s.mp4" % ((platform or "x"), ident)
    return public_url_from_remote(play_url, key, "video/mp4")


DECRYPT_API = os.environ.get("WXCH_DECRYPT_API", "http://127.0.0.1:3001/api/decrypt")  # 视频号 Isaac64 解密服务(与 dl_service 同一个)

def _collect_channels_play_url(vid_id, play_url, decode_key):
    """视频号：加密流先解密再存 COS，返回可播放的永久直链。
    视频号 CDN 直链是加密流(无 mp4 容器)，直接转存会得到打不开的乱码文件——
    必须先下加密流 → 本地 :3001 解密 → 存解密后的 mp4。
    缺 decode_key / 解密服务不可用 / 解密结果非法 / COS 未开 → 返回 None
    (绝不落地加密垃圾，也绝不因转存失败而中断采集：文案/评论照常返回)。"""
    play_url = (play_url or "").strip()
    dk = (decode_key or "").strip()
    if not play_url or not dk or not COS_COLLECT:
        return None
    enc_path = None
    try:
        import tempfile, subprocess
        from content_domains import cos
        if not cos.enabled():
            return None
        # 1) 下加密流(视频号 CDN 需带 Referer)
        req = urllib.request.Request(play_url, headers={
            "User-Agent": tikhub.UA, "Referer": "https://channels.weixin.qq.com/"})
        with urllib.request.urlopen(req, timeout=90) as r:
            enc = r.read(100 * 1024 * 1024)
        if not enc:
            return None
        with tempfile.NamedTemporaryFile(suffix=".enc", delete=False) as tf:
            tf.write(enc); enc_path = tf.name
        # 2) 调本地解密服务(multipart: decode_key + video 文件)
        proc = subprocess.run(
            ["curl", "-sS", "-X", "POST", DECRYPT_API,
             "-F", "decode_key=" + dk, "-F", "video=@" + enc_path, "-o", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        if proc.returncode != 0 or not proc.stdout:
            print("[cos] 视频号解密失败，跳过转存: %s" % (
                (proc.stderr or b"")[:120].decode("u8", "ignore") or "解密服务无响应"), flush=True)
            return None
        dec = proc.stdout
        # 3) 校验确实解成了 mp4(含 ftyp 盒)，杜绝再落地垃圾
        if b"ftyp" not in dec[:4096]:
            print("[cos] 视频号解密结果非合法 mp4，跳过转存", flush=True)
            return None
        ident = re.sub(r"[^A-Za-z0-9_.-]", "", str(vid_id or "")) or "v"
        return cos.put_bytes(dec, "collect/channels/%s.mp4" % ident, "video/mp4")
    except Exception as e:
        print("[cos] 视频号解密转存失败，跳过: %s" % e, flush=True)
        return None
    finally:
        if enc_path:
            try:
                os.unlink(enc_path)
            except Exception:
                pass


# ============ 共享管道：任务库 / 点数 / 鉴权 ============
def jdb():
    c = sqlite3.connect(JOB_DB, timeout=10); c.row_factory = sqlite3.Row; return c

def get_points(username):
    try:
        with closing(sqlite3.connect(AUTH_DB, timeout=10)) as c:
            r = c.execute("SELECT points FROM users WHERE username=?", (username,)).fetchone()
            return r[0] if r else 0
    except Exception:
        return 0

def add_points(username, delta):
    try:
        with closing(sqlite3.connect(AUTH_DB, timeout=10)) as c:
            c.execute("UPDATE users SET points = MAX(0, points + ?) WHERE username=?", (delta, username)); c.commit()
    except Exception:
        pass

def verify(token):
    if not token: return None
    try:
        req = urllib.request.Request(AUTH_BASE + "/api/auth/me", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read()).get("user")
    except Exception:
        return None

def cost_of(kind, body):
    if kind == "collect":
        return 3 + (3 if "transcript" in (body.get("want") or []) else 0)
    if kind == "leads":
        n = max(1, min(30, int(body.get("count") or 12)))
        p = max(1, min(3, int(body.get("pages") or 1)))
        return 6 + (n * p) // 4
    return 0


# ============ 采集能力：单条视频/图文 → 视频+文案+口播+评论 ============
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
        raise ValueError("内容获取失败（可能是上游限流或内容私密/已删），请重试")
    au = det.get("author") or {}
    if platform == "channels":   # 视频号是加密流：先解密再存 COS，否则存下来是打不开的乱码
        play_url = _collect_channels_play_url(det.get("id") or ident, det.get("play_url"), det.get("decode_key"))
    else:
        play_url = _collect_cos_play_url(platform, det.get("id") or ident, det.get("play_url"))
    out = {
        "type": "collect", "platform": platform, "source": det.get("url") or ident,
        "video": {"title": det.get("title"), "author": au.get("name"), "authorAvatar": None,
                  "profile_url": au.get("profile_url"),
                  "cover": det.get("cover"), "play_url": play_url, "url": det.get("url"),
                  "duration": det.get("duration"), "publish_time": det.get("publish_time"),
                  "stats": det.get("stats")},
        "copy": {"title": det.get("title"), "desc": det.get("desc"), "tags": det.get("tags")},
        "images": det.get("images") or [],
        "transcript": None, "comments": [], "comments_more": False,
        "url": det.get("cover"), "prompt": det.get("title"),   # 给通用 history 用
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

def gen_leads(payload):
    keyword   = (payload.get("keyword") or "").strip()
    platforms = payload.get("platforms") or ["douyin"]
    nvid      = max(1, min(30, int(payload.get("count") or 12)))
    pages     = max(1, min(3, int(payload.get("pages") or 2)))
    targets   = payload.get("channels_targets") or []
    raw = []

    def pull(platform, vid_id, title, video_url=None):
        for pg in range(pages):
            try:
                cm = tikhub.comments(platform, vid_id, cursor=(pg * 20 if platform == "douyin" else None), count=20)
            except tikhub.TikHubError:
                break
            for c in cm["items"]:
                raw.append({"content": c.get("text"), "user_id": c.get("user_id"), "nickname": c.get("user"),
                            "ip_location": c.get("ip"), "like_count": c.get("likes") or 0,
                            "profile_url": c.get("profile_url"), "platform": platform, "source": title,
                            "video_url": video_url, "time": c.get("time"), "red_id": c.get("red_id")})
            if not cm.get("has_more"):
                break

    for platform in platforms:
        if platform == "channels" or not keyword:
            continue
        # 按采集量翻页收集视频：原来只取搜索第1页(抖音每页约10个)再 [:nvid] 切片，
        # 采集量≥10时不同数量切到的都是同一页那~10个视频→结果完全相同(#227)。
        # 现按 nvid 翻页(search 已支持 page 且按页缓存)，最多5页(~50)覆盖 nvid≤30。
        # 搜索端点偶发400，每页重试1次(dy_search 本身无重试)。
        vids = []
        for _pg in range(1, 6):
            sr = None
            for _try in range(2):
                try:
                    sr = tikhub.search(platform, keyword, page=_pg); break
                except tikhub.TikHubError:
                    if _try == 0: time.sleep(1.0)
            if sr is None:
                break
            vids += (sr.get("items") or [])
            if len(vids) >= nvid or not sr.get("has_more"):
                break
        for v in vids[:nvid]:
            pull(platform, v["id"], v.get("title"), v.get("url"))

    if "channels" in platforms:
        for tgt in targets:
            tgt = (tgt or "").strip()
            if not tgt:
                continue
            try:
                if "@finder" in tgt:
                    uname = tgt
                elif tgt.startswith("http") or "weixin.qq.com" in tgt:
                    # 视频号视频/分享链接 → 解析出发布账号(盯号入口)
                    uname = ((tikhub.ch_detail(tgt) or {}).get("author") or {}).get("id")
                else:
                    uname = (tikhub.ch_id_to_username(tgt) or {}).get("username")
                if not uname:
                    continue
                for v in tikhub.ch_user_videos(uname)["items"][:nvid]:
                    pull("channels", v["id"], v.get("title"), v.get("url"))
            except tikhub.TikHubError:
                continue

    leads, spam, chat, seen = [], 0, 0, set()
    for c in raw:
        t = (c.get("content") or "").strip()
        if not t:
            continue
        if _is_spam(t):
            spam += 1; continue
        if len(re.sub(r"\[[^\]]+\]", "", t).strip()) < 2:
            chat += 1; continue
        if _is_high(t):
            k = (c.get("user_id"), t)
            if k in seen:
                continue
            seen.add(k); leads.append(c)
        else:
            chat += 1
    # 时间优先(抓最近用户)→ 再按评论长度 → 再点赞。新评论不再被埋。
    leads.sort(key=lambda c: (c.get("time") or 0, len(c.get("content", "")), c.get("like_count", 0)), reverse=True)
    out_leads = [{"nickname": c.get("nickname"), "user_unique_id": c.get("user_id"),
                  "ip_location": c.get("ip_location"), "content": c.get("content"),
                  "title": c.get("source"), "platform": c.get("platform"),
                  "profile_url": c.get("profile_url"), "video_url": c.get("video_url"),
                  "red_id": c.get("red_id")} for c in leads]
    return {"type": "leads", "keyword": keyword, "platforms": platforms,
            "leads_count": len(out_leads), "spam": spam, "chat": chat, "total": len(raw),
            "leads": out_leads, "url": None, "prompt": keyword}

HANDLERS = {"collect": gen_collect, "leads": gen_leads}


# ============ worker（失败退点；清道夫由 content_api 统一跑） ============
def run_job(job_id):
    with closing(jdb()) as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r: return
    kind = r["kind"]; payload = json.loads(r["payload"] or "{}")
    try:
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (int(time.time()), job_id)); c.commit()
        result = HANDLERS[kind](payload)
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=?",
                      (json.dumps(result, ensure_ascii=False), int(time.time()), job_id)); c.commit()
    except Exception as e:
        add_points(r["username"], r["cost"])
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                      (str(e)[:300], int(time.time()), job_id)); c.commit()


# ============ HTTP ============
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _token(self):
        a = self.headers.get("Authorization") or ""
        return a[7:].strip() if a.startswith("Bearer ") else ""
    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        p = self.path.split("?")[0]
        if p.startswith("/api/gen/") and p[9:] in HANDLERS:
            kind = p[9:]
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录或登录已过期"})
            body = self._json_body()
            cost = cost_of(kind, body)
            if get_points(user["username"]) < cost:
                return self._send(402, {"detail": "点数不足", "need": cost})
            add_points(user["username"], -cost)
            now = int(time.time())
            with closing(jdb()) as c:
                cur = c.execute("INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                (kind, user["username"], cost, json.dumps(body, ensure_ascii=False), now, now))
                c.commit(); jid = cur.lastrowid
            threading.Thread(target=run_job, args=(jid,), daemon=True).start()
            return self._send(200, {"job_id": jid, "cost": cost, "points_left": get_points(user["username"])})
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/gen/collect/search":   # 关键词搜（即时，扣 1 点）— 采集页选片用，含图文
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            platform = (q.get("platform", ["douyin"])[0]).strip()
            keyword  = (q.get("keyword", [""])[0]).strip()
            try: page = int(q.get("page", ["1"])[0] or 1)
            except Exception: page = 1
            if not keyword: return self._send(400, {"detail": "缺少关键词"})
            if get_points(user["username"]) < 1: return self._send(402, {"detail": "点数不足", "need": 1})
            try:
                r = tikhub.search(platform, keyword, page=page, video_only=False)
            except tikhub.TikHubError as e:
                return self._send(502, {"detail": str(e)[:160]})
            add_points(user["username"], -1)
            items = [{"id": it.get("id"), "platform": it.get("platform"), "title": it.get("title"),
                      "cover": it.get("cover"), "author": it.get("author"), "url": it.get("url"),
                      "note_type": it.get("note_type"),
                      "stats": {"like": it.get("like"), "comment": it.get("comment")}} for it in (r.get("items") or [])]
            return self._send(200, {"items": items, "cost": 1, "points_left": get_points(user["username"])})
        if p == "/api/gen/leadgen/health":
            return self._send(200, {"ok": True, "service": "huangque-leadgen", "caps": list(HANDLERS), "has_tikhub": bool(tikhub.KEY)})
        self._send(404, {"detail": "not found"})


if __name__ == "__main__":
    print("huangque-leadgen-api on 127.0.0.1:%d  caps=%s" % (PORT, list(HANDLERS)))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
