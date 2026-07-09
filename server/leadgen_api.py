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
import os, re, sqlite3, json, time, threading, tempfile, urllib.request, urllib.parse, urllib.error
from contextlib import closing
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tikhub

PORT      = int(os.environ.get("LEADGEN_API_PORT", "8100"))
AUTH_BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")
AUTH_COOKIE_NAME = os.environ.get("HQ_AUTH_COOKIE_NAME", "hq_session")
AUTH_DB   = os.environ.get("AUTH_DB", "/home/ubuntu/auth-service/users.db")
INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")   # 调 auth 内部点数接口用；来自 auth.env
JOB_DB    = os.environ.get("CONTENT_JOB_DB", "/home/ubuntu/content-api/content_jobs.db")  # 共用 content_api 的任务库
COS_COLLECT = os.environ.get("COS_COLLECT", "1").strip().lower() not in ("0", "false", "no")  # 采集视频转存 COS 开关
# 转存预算：线上 23 次转存失败全部是 "The read operation timed out"。原实现 timeout=120 且盲目重试 2 次，
# 最坏在转存上耗 240s+，把整个 collect 任务顶过 reaper 判死线(当时 360s)→ 判死退点、worker 又写回 done
# (jobs 1118/1161/1164/1170/1182 就是这么来的)。改为总预算 + 流式读，超预算立即放弃、不再盲目重试。
COS_FETCH_DEADLINE   = int(os.environ.get("COS_COLLECT_DEADLINE", "180"))            # 单次采集用于转存的总秒数
COS_FETCH_READ_TIMEO = int(os.environ.get("COS_COLLECT_READ_TIMEOUT", "30"))         # 单次 socket 读超时
COS_FETCH_MAX_BYTES  = int(os.environ.get("COS_COLLECT_MAX_BYTES", str(100 * 1024 * 1024)))
# 同时最多几个采集在下视频。每个最坏占 2×MAX_BYTES 内存(chunks + join)，默认 2 → 峰值约 400MB
_COS_FETCH_GATE = threading.BoundedSemaphore(int(os.environ.get("COS_COLLECT_MAX_CONCURRENCY", "2")))


# ============ 采集视频转存 COS（永久直链；未配置/失败/关闭时回退原 CDN 链接） ============
def _request_token(headers):
    auth = headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token and token != "__cookie__":
            return token
    try:
        jar = cookies.SimpleCookie()
        jar.load(headers.get("Cookie") or "")
        morsel = jar.get(AUTH_COOKIE_NAME)
        return morsel.value.strip() if morsel and morsel.value else ""
    except Exception:
        return ""

def _download_with_retry(remote_url, rel_key, dest_path):
    """流式下载远程视频到 dest_path，受总预算约束。返回字节数；失败返回 0。

    绝不抛异常 —— 采集流程不能因为转存失败而中断。
    并发闸：do_POST 每个请求直接起一个不受限的线程（本文件 threading.Thread(target=run_job)），
    落盘后内存虽已恒定，但带宽和磁盘仍需限流。
    """
    deadline = time.time() + COS_FETCH_DEADLINE
    for attempt in (1, 2):
        try:
            with _COS_FETCH_GATE:
                n = tikhub.download_to_file(remote_url, deadline, dest_path,
                                            max_bytes=COS_FETCH_MAX_BYTES,
                                            read_timeout=COS_FETCH_READ_TIMEO)
            if n:
                return n
            raise ValueError("拉取到 0 字节")
        except Exception as e:
            # 预算耗尽就别再重试了——多等一轮只会把整个 collect 任务顶过 reaper 判死线
            if attempt == 2 or time.time() >= deadline:
                print("[cos] 视频下载失败(%d次尝试): %s -> %s: %s"
                      % (attempt, rel_key, type(e).__name__, e), flush=True)
                return 0
            print("[cos] 视频下载第%d次失败，剩余预算 %.0fs，重试: %s"
                  % (attempt, deadline - time.time(), e), flush=True)
    return 0


def store_video_file(path, rel_key, content_type=None):
    """已落盘的视频 → COS 永久直链。未配置 / 上传失败 → 返回 None，由调用方回退原链接。"""
    try:
        from content_domains import cos
        if not cos.enabled():
            return None
        return cos.put_file(path, str(rel_key), content_type)
    except Exception as e:
        print("[cos] 上传失败: %s -> %s" % (rel_key, e), flush=True)
        return None


def fetch_and_store(remote_url, rel_key, content_type=None, keep_file=False):
    """下载一次到临时文件，同一份既用于 COS 转存、也可交给调用方复用（ASR）。

    返回 (可用的 url, 视频文件路径 或 None)。keep_file=True 时【文件归调用方删】。
    转存失败时 url 回退成会过期的第三方 CDN 直链 —— 静默降级，资产库据此标为「非永久」。

    为什么要落盘而不是留在内存：视频最大 100MB，原来 b"".join 拼接瞬间双份，
    而 leadgen 每个请求起一个不限流的线程，并发一高就 OOM。落盘后 ffmpeg 直接读文件、
    COS 也从文件流式上传，内存恒定，反而少一次写。
    """
    remote_url = (remote_url or "").strip()
    if not remote_url:
        return remote_url, None
    want_cos = COS_COLLECT and _cos_enabled()
    if not (want_cos or keep_file):
        return remote_url, None          # 既不转存也不需要文件，别白下
    fd, path = tempfile.mkstemp(suffix=".mp4", prefix="hqcollect-")
    os.close(fd)
    keep = False
    try:
        if not _download_with_retry(remote_url, rel_key, path):
            return remote_url, None      # 下载失败：回退原链接，ASR 会自己再试一次
        url = store_video_file(path, rel_key, content_type) if want_cos else None
        if want_cos and not url:
            print("[cos] 转存失败，回退会过期的原链接: %s" % rel_key, flush=True)
        keep = bool(keep_file)
        return (url or remote_url), (path if keep_file else None)
    finally:
        if not keep:
            try: os.unlink(path)
            except OSError: pass


def _cos_enabled():
    try:
        from content_domains import cos
        return cos.enabled()
    except Exception:
        return False


def public_url_from_remote(remote_url, rel_key, content_type=None):
    """兼容旧签名：只要 URL，临时文件用完即删。"""
    return fetch_and_store(remote_url, rel_key, content_type, keep_file=False)[0]

def _collect_cos_play_url(platform, vid_id, play_url, keep_file=False):
    """采集视频 play_url → COS 永久直链。图集/无 play_url 跳过、保持原样。
    对象键 collect/<platform>/<id>.mp4。转存失败/未配置回退原 play_url。
    注意：视频号(channels)是加密流，不能走这里直存——用 _collect_channels_play_url 先解密。

    keep_file=True 时一并返回下好的临时文件路径，供 ASR 复用，避免同一个 URL 下两次。
    返回 (url, path 或 None)。path 归调用方删。
    """
    if not play_url:
        return play_url, None
    ident = re.sub(r"[^A-Za-z0-9_.-]", "", str(vid_id or "")) or "v"
    key = "collect/%s/%s.mp4" % ((platform or "x"), ident)
    return fetch_and_store(play_url, key, "video/mp4", keep_file=keep_file)


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

def _auth_points(path, username, amount):
    """调 auth 服务的点数接口（BEGIN IMMEDIATE 事务 + points_audit 流水），与 imggen_api 同一范式。"""
    if not INTERNAL_TOKEN:
        return 500, {"detail": "HQ_INTERNAL_TOKEN 未配置"}
    body = json.dumps({"username": username, "amount": int(amount)}, ensure_ascii=False).encode()
    req = urllib.request.Request(AUTH_BASE + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-HQ-Internal-Token": INTERNAL_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {"detail": "points update failed"}
    except Exception:
        return 500, {"detail": "points update failed"}

def deduct_points(username, amount):
    return _auth_points("/api/auth/points/deduct", username, amount)   # 带 BEGIN IMMEDIATE + points>=amount 原子校验

def refund_points(username, amount):
    return _auth_points("/api/auth/points/refund", username, amount)

def _add_points_direct(username, delta):
    """兜底：直接写 users.db。无事务保护、不进 points_audit —— 只在 auth 不可用时用。

    扣点(delta<0)必须带 points >= 需扣数 的条件，否则 MAX(0, ...) 会把余额不足的用户
    硬扣到 0 且静默成功。返回是否真正生效。
    """
    try:
        with closing(sqlite3.connect(AUTH_DB, timeout=10)) as c:
            if delta < 0:
                cur = c.execute("UPDATE users SET points = points + ? WHERE username=? AND points >= ?",
                                (delta, username, -delta))
            else:
                cur = c.execute("UPDATE users SET points = points + ? WHERE username=?", (delta, username))
            c.commit()
            return cur.rowcount == 1
    except Exception as e:
        print("[leadgen] 直写 users.db 失败 user=%s delta=%s: %s" % (username, delta, e), flush=True)
        return False

def add_points(username, delta):
    """加/减点数。delta>0 退点走 auth 的 /refund，delta<0 扣点走 /deduct。

    ⚠ 这个函数同时被扣点(do_POST 里 -cost / -1)和退点(_refund_once 里 +cost)调用。
    auth 的两个端点都校验 `amount >= 0`，所以必须按符号分流到不同端点、并传绝对值，
    否则扣点会拿到 400 而被误当成「auth 故障」。

    auth 不可用时回退直写 users.db：宁可审计缺一条，也不能把用户的点吞了。
    但「点数不足」(402) 是业务结论而非故障，绝不回退——回退等于绕过余额校验硬扣。
    返回是否真正生效（扣点余额不足时为 False）。
    """
    delta = int(delta or 0)
    if delta == 0:
        return True
    if delta > 0:
        status, data = refund_points(username, delta)
    else:
        status, data = deduct_points(username, -delta)
        if status == 402:
            return False   # 点数不足：auth 的原子校验已经拒绝，不要再直写
    if status == 200:
        return True
    print("[leadgen] auth 点数接口失败(delta=%s status=%s detail=%s)，回退直写 users.db；本次不进 points_audit"
          % (delta, status, (data or {}).get("detail")), flush=True)
    return _add_points_direct(username, delta)

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
    video_path = None
    if platform == "channels":   # 视频号是加密流：先解密再存 COS，否则存下来是打不开的乱码
        play_url = _collect_channels_play_url(det.get("id") or ident, det.get("play_url"), det.get("decode_key"))
    else:
        # 只有真要跑 ASR 时才留文件：视频号不走 ASR，小红书有官方字幕(subtitle_url)就不用下视频。
        need_file = ("transcript" in want
                     and platform != "channels"
                     and not det.get("subtitle_url"))
        play_url, video_path = _collect_cos_play_url(
            platform, det.get("id") or ident, det.get("play_url"), keep_file=need_file)
    try:
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
                # video_path 是上面转存时下好的同一个 play_url；非 None 时 ASR 不再重复下载
                out["transcript"] = tikhub.transcript(det, video_path=video_path)
            except tikhub.TikHubError as e:
                out["transcript"] = {"text": None, "error": str(e)[:120]}
        return out
    finally:
        if video_path:   # 临时文件归本函数删，无论成败
            try: os.unlink(video_path)
            except OSError: pass


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
# 与 content_domains/core.py 的 _set_terminal/_refund_once 同语义：本服务与 content_api 共写
# 同一张 jobs 表，reaper 只在 content_api 里跑。不做 CAS 就会出现「reaper 判超时退了点，
# worker 随后把 error 覆写回 done」——用户既拿到结果又拿回点数(线上 id=1170 实例)。
# CAS 抢终态 / 退点幂等：实现在 content_domains/jobs_store.py，三个共写 jobs 表的服务共用一份。
def _set_terminal(job_id, status, result=None, error=None, from_states=("running",)):
    from content_domains import jobs_store
    return jobs_store.set_terminal(jdb, job_id, status, result, error, from_states)

def _refund_once(job_id, username, cost):
    from content_domains import jobs_store
    # add_points：auth 的 /refund 优先，失败回退直写 users.db；返回 False 时 jobs_store 会回滚 refunded 标记
    return jobs_store.refund_once(jdb, job_id, username, cost, lambda u, c: add_points(u, c))

def run_job(job_id):
    with closing(jdb()) as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r: return
    kind = r["kind"]; payload = json.loads(r["payload"] or "{}")
    try:
        with closing(jdb()) as c:   # CAS 认领：只有 pending 才能被本次执行接管，防同一 job 被跑两遍
            claimed = c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='pending'",
                                (int(time.time()), job_id)); c.commit()
        if claimed.rowcount < 1:
            return  # 已被别的线程接管或已是终态
        result = HANDLERS[kind](payload)
        if not _set_terminal(job_id, "done", result=result):
            # reaper 已把它判超时并退点：不覆写终态。宁可用户重试，也不能既退点又出结果。
            print("[leadgen] job %s 完成时已非 running（reaper 判超时在先），丢弃结果" % job_id, flush=True)
            return
        # 拿到 done 终态后才入资产库；入库是次要副作用，失败不改状态、不退点
        try:
            from content_domains import assets_store
            assets_store.record_asset(job_id, r["username"], kind, result)
        except Exception as e:
            print("[leadgen] 资产入库失败 job=%s: %s" % (job_id, e), flush=True)
    except Exception as e:
        # from_states 含 pending：认领那句 UPDATE 自己抛异常时任务还停在 pending，
        # 只认 running 会导致不退点且 reaper 永远扫不到它（预扣的点永久丢失）
        if _set_terminal(job_id, "error", error=str(e), from_states=("pending", "running")):
            _refund_once(job_id, r["username"], r["cost"])


# ============ HTTP ============
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _token(self):
        return _request_token(self.headers)
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
            # 原来是「先 get_points 查余额，再 add_points 扣」——两步之间有并发超扣窗口。
            # 现在扣点直接走 auth 的 /deduct（BEGIN IMMEDIATE + points>=amount 原子校验），
            # 扣不动就说明余额不足，不建任务。
            if not add_points(user["username"], -cost):
                return self._send(402, {"detail": "点数不足", "need": cost})
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
            if not add_points(user["username"], -1):   # 并发下余额可能已被别的请求扣光
                return self._send(402, {"detail": "点数不足", "need": 1})
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
