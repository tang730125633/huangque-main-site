#!/usr/bin/env python3
"""服务器端 worker（多 worker 并发版）—— 跑在腾讯云服务器上。
搜索：青果住宅代理 + 注入号池 cookie；账号深扒：直连机房IP（深采放行）。
隔离靠 WORKER_ID 派生的独立工作目录(cwd) + 命令行参数传配置（不改 MediaCrawler config 文件，零竞态）。
环境变量：WORKER_ID(默认1) / WORKER_MODES(空=全能, 如 "search,transcribe,download_video"=快车道)
"""
import os
import re
import sys
import json
import time
import glob
import shutil
import signal
import subprocess
import urllib.request
import urllib.parse

WORKER_ID = os.environ.get("WORKER_ID", "1")
WORKER_MODES = os.environ.get("WORKER_MODES", "")   # 空=领所有 mode；非空=只领这些(快慢分道)
MC = "/home/ubuntu/MediaCrawler"                     # 共享只读代码
VENV = MC + "/.venv/bin/python"
MAIN_PY = MC + "/main.py"                             # 绝对脚本路径（cwd 不影响 import）
WORK_DIR = f"/home/ubuntu/worker_{WORKER_ID}"         # 本 worker cwd → 隔离 browser_data
DATA_DIR = f"{WORK_DIR}/data"                         # --save_data_path
JSONL_DIR = f"{DATA_DIR}/douyin/jsonl"               # build_result 从这读
UD = f"{WORK_DIR}/browser_data/dy_user_data_dir"      # prep_login 注入到这
NUMBER_POOL = "/home/ubuntu/number_pool"
COOKIES_JSON = f"{NUMBER_POOL}/cookie_{WORKER_ID}.json"  # 号池：本 worker 绑定的号
QG_KEY = os.environ.get("QG_KEY", "55DC9F7E")
SERVER = os.environ.get("LEADGEN_SERVER", "http://127.0.0.1:8090")
TOKEN = os.environ.get("LEADGEN_WORKER_TOKEN", "worker-secret-2026")
STATIC_PROXY = os.environ.get("STATIC_PROXY", "")   # 长效静态代理 user:pass@host:port；设了则搜索优先用(长命→爬得深)
CMT_LIMIT = os.environ.get("CMT_LIMIT", "100")       # 每视频评论上限
POLL = 8
PREP_LOGIN_TTL = int(os.environ.get("PREP_LOGIN_TTL", "900"))
SEARCH_STATIC_TIMEOUT = int(os.environ.get("SEARCH_STATIC_TIMEOUT", "180"))
SEARCH_PROXY_TIMEOUT = int(os.environ.get("SEARCH_PROXY_TIMEOUT", "120"))
VIDEO_CRAWL_TIMEOUT = int(os.environ.get("VIDEO_CRAWL_TIMEOUT", "180"))
PREP_LOGIN_MARK = f"{WORK_DIR}/.prep_login_ok.json"

# 共享的提取结果目录（按 aweme_id 命名，多 worker 共享不撞）
FILES_DIR = "/home/ubuntu/leadgen-server/files"
TRANS_DIR = "/home/ubuntu/leadgen-server/transcribe_out"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
_WHISPER = None

os.makedirs(JSONL_DIR, exist_ok=True)

sys.path.insert(0, "/home/ubuntu/douyin-leadgen")
from scripts.leads_filter import is_spam, is_high  # noqa


def log(m):
    print(time.strftime("%H:%M:%S"), f"[w{WORKER_ID}]", m, flush=True)


def http_get(path):
    with urllib.request.urlopen(SERVER + path, timeout=20) as r:
        return json.loads(r.read())


def http_post(path, data):
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(urllib.request.Request(SERVER + path, data=body), timeout=30) as r:
        return json.loads(r.read())


def load_jsonl(pattern):
    rows = []
    for p in glob.glob(pattern):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


def extract_qg():
    """提青果住宅代理，返回 (gateway_host:port, 出口IP, 地区, isp)。"""
    url = f"https://share.proxy.qg.net/get?key={QG_KEY}&num=1&format=json"
    d = json.loads(urllib.request.urlopen(url, timeout=15).read())
    it = d["data"][0]
    return it["server"], it["proxy_ip"], it["area"], it["isp"]


def _proxy_arg(proxy_server):
    """proxy_server 可为 'host:port' 或 'user:pass@host:port'；账密拆成 playwright 独立字段(不塞进server地址)。"""
    if not proxy_server:
        return None
    if "@" in proxy_server:
        creds, hostport = proxy_server.rsplit("@", 1)
        user, _, pwd = creds.partition(":")
        return {"server": f"http://{hostport}", "username": user, "password": pwd}
    return {"server": f"http://{proxy_server}"}


def _prep_login_cache_valid():
    if PREP_LOGIN_TTL <= 0:
        return False
    if not (os.path.isdir(UD) and os.path.exists(PREP_LOGIN_MARK) and os.path.exists(COOKIES_JSON)):
        return False
    try:
        mark = json.load(open(PREP_LOGIN_MARK, encoding="utf-8"))
        cookie_mtime = os.path.getmtime(COOKIES_JSON)
        return (
            abs(float(mark.get("cookie_mtime", -1)) - cookie_mtime) < 0.001
            and time.time() - float(mark.get("ok_at", 0)) < PREP_LOGIN_TTL
        )
    except Exception:
        return False


def _mark_prep_login_ok():
    os.makedirs(os.path.dirname(PREP_LOGIN_MARK), exist_ok=True)
    with open(PREP_LOGIN_MARK, "w", encoding="utf-8") as f:
        json.dump({"ok_at": time.time(), "cookie_mtime": os.path.getmtime(COOKIES_JSON)}, f)


def prep_login(proxy_server=None):
    """全新档案：注入本 worker 的号 cookie + localStorage HasUserLogin=1，使 pong 跳过登录。
    proxy_server=None 时直连（机房IP，深采接口放行）。"""
    import asyncio
    from playwright.async_api import async_playwright
    if _prep_login_cache_valid():
        log("复用热登录态缓存，跳过 Chromium 登录预热")
        return True
    cookies = json.load(open(COOKIES_JSON, encoding="utf-8"))
    for c in cookies:
        if c.get("sameSite") not in ("Strict", "Lax", "None"):
            c["sameSite"] = "Lax"
    if os.path.isdir(UD):
        shutil.rmtree(UD)
    proxy_arg = _proxy_arg(proxy_server)

    async def _run():
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                UD, headless=True, proxy=proxy_arg,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
            await page.evaluate("() => window.localStorage.setItem('HasUserLogin','1')")
            await page.wait_for_timeout(2500)
            await page.goto("https://www.douyin.com/user/self", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
            body = await page.evaluate("() => document.body.innerText")
            ok = bool(re.search(r"抖音号[:：]", body))
            await ctx.close()
            return ok
    ok = asyncio.run(_run())
    if ok:
        _mark_prep_login_ok()
    return ok


def clear_jsonl():
    os.makedirs(JSONL_DIR, exist_ok=True)
    for f in glob.glob(os.path.join(JSONL_DIR, "*.jsonl")):
        os.remove(f)


def run_mediacrawler(cmd, timeout):
    p = subprocess.Popen(cmd, cwd=WORK_DIR,
                         env={**os.environ, "NO_PROXY": "localhost,127.0.0.1"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            p.kill()
        try:
            p.wait(timeout=15)
        except Exception:
            pass
        raise
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


def run_crawl(keyword, count, proxy_server):
    """搜索爬取：cwd=本worker目录(隔离browser_data) + 全 CLI 传配置(不改config文件)。"""
    clear_jsonl()
    cmd = [VENV, MAIN_PY, "--platform", "dy", "--lt", "qrcode", "--type", "search",
           "--keywords", keyword,
           "--crawler_max_notes_count", str(count),
           "--get_comment", "yes", "--get_sub_comment", "no",
           "--max_comments_count_singlenotes", CMT_LIMIT,
           "--save_data_option", "jsonl", "--save_data_path", DATA_DIR,
           "--headless", "yes",
           "--enable_ip_proxy", "yes", "--ip_proxy_provider_name", "static",
           "--static_proxy_url", f"http://{proxy_server}"]
    timeout = SEARCH_STATIC_TIMEOUT if (STATIC_PROXY and proxy_server == STATIC_PROXY) else SEARCH_PROXY_TIMEOUT
    run_mediacrawler(cmd, timeout)
    return JSONL_DIR


def build_result(jdir, content_prefix="search", comment_prefix="search"):
    comments = load_jsonl(os.path.join(jdir, f"{comment_prefix}_comments_*.jsonl"))
    titles, hashtags, videos, vid_seen = {}, {}, [], set()
    for d in load_jsonl(os.path.join(jdir, f"{content_prefix}_contents_*.jsonl")):
        aid = d.get("aweme_id")
        title = d.get("title") or ""
        if aid:
            titles[aid] = title[:24]
        for tag in re.findall(r"#([^#\s\n]+)", title):
            tag = tag.strip()
            if 1 < len(tag) <= 12:
                hashtags[tag] = hashtags.get(tag, 0) + 1
        if aid and aid not in vid_seen:
            vid_seen.add(aid)
            videos.append({
                "aweme_id": aid, "title": title, "nickname": d.get("nickname") or "",
                "likes": d.get("liked_count", 0), "comment_count": d.get("comment_count", 0),
                "url": d.get("aweme_url") or f"https://www.douyin.com/video/{aid}",
                "cover": d.get("cover_url") or "", "ip": d.get("ip_location") or "",
                "download_url": d.get("video_download_url") or "", "leads_count": 0,
            })
    related = [t for t, _ in sorted(hashtags.items(), key=lambda x: -x[1])][:24]
    leads, spam, chat, seen = [], 0, 0, set()
    for c in comments:
        t = (c.get("content") or "").strip()
        if not t:
            continue
        if is_spam(t):
            spam += 1; continue
        if len(re.sub(r"\[[^\]]+\]", "", t).strip()) < 2:
            chat += 1; continue
        if is_high(t):
            key = (c.get("user_id"), t)
            if key in seen:
                continue
            seen.add(key)
            sec = c.get("sec_uid") or ""
            aid = c.get("aweme_id") or ""
            leads.append({
                "nickname": c.get("nickname"), "douyin_id": c.get("user_unique_id") or "",
                "sec_uid": sec, "profile": f"https://www.douyin.com/user/{sec}" if sec else "",
                "ip": c.get("ip_location"), "content": c.get("content"),
                "source": titles.get(aid, ""), "aweme_id": aid, "like": c.get("like_count", 0),
            })
        else:
            chat += 1
    leads.sort(key=lambda x: (len(x["content"]), x["like"]), reverse=True)
    lc = {}
    for l in leads:
        if l["aweme_id"]:
            lc[l["aweme_id"]] = lc.get(l["aweme_id"], 0) + 1
    for v in videos:
        v["leads_count"] = lc.get(v["aweme_id"], 0)
    videos.sort(key=lambda v: v["leads_count"], reverse=True)
    return {"total": len(comments), "leads_count": len(leads), "spam": spam, "chat": chat,
            "leads": leads, "videos": videos, "related_keywords": related}


def fetch_xiaotan_video_meta(aweme_id):
    try:
        api = "http://127.0.0.1:8501/api/douyin/web/fetch_one_video?aweme_id=" + urllib.parse.quote(str(aweme_id))
        d = json.loads(urllib.request.urlopen(api, timeout=15).read().decode("utf-8"))
        detail = ((d.get("data") or {}).get("aweme_detail") or {})
        if not detail:
            return None
        author = detail.get("author") or {}
        stats = detail.get("statistics") or {}
        video = detail.get("video") or {}
        cover = ""
        for key in ("cover", "origin_cover", "dynamic_cover"):
            obj = video.get(key) or {}
            urls = obj.get("url_list") or []
            if urls:
                cover = urls[-1]
                break
        play_url = ""
        play = video.get("play_addr") or {}
        urls = play.get("url_list") or []
        if urls:
            play_url = urls[-1]
        return {
            "aweme_id": str(detail.get("aweme_id") or aweme_id),
            "title": detail.get("desc") or "",
            "nickname": author.get("nickname") or "",
            "likes": stats.get("digg_count") or detail.get("digg_count") or 0,
            "comment_count": stats.get("comment_count") or detail.get("comment_count") or 0,
            "url": "https://www.douyin.com/video/%s" % (detail.get("aweme_id") or aweme_id),
            "cover": cover,
            "ip": "",
            "download_url": play_url,
            "leads_count": 0,
        }
    except Exception as e:
        log(f"xiaotan video meta fallback failed: {str(e)[:80]}")
        return None


def infer_aweme_id_from_result(res, link):
    text = str(link or "")
    if text.isdigit():
        return text
    m = re.search(r"(?:modal_id=|/video/)(\d{8,})", text)
    if m:
        return m.group(1)
    for lead in res.get("leads") or []:
        if lead.get("aweme_id"):
            return str(lead.get("aweme_id"))
    for video in res.get("videos") or []:
        if video.get("aweme_id"):
            return str(video.get("aweme_id"))
    return ""


def run_video_crawl(link, comment_limit, proxy_server=None):
    clear_jsonl()
    cmd = [VENV, MAIN_PY, "--platform", "dy", "--lt", "qrcode", "--type", "detail",
           "--specified_id", str(link),
           "--get_comment", "yes", "--get_sub_comment", "no",
           "--max_comments_count_singlenotes", str(comment_limit),
           "--save_data_option", "jsonl", "--save_data_path", DATA_DIR,
           "--headless", "yes"]
    if proxy_server:
        cmd += ["--enable_ip_proxy", "yes", "--ip_proxy_provider_name", "static",
                "--static_proxy_url", f"http://{proxy_server}"]
    else:
        cmd += ["--enable_ip_proxy", "no"]
    run_mediacrawler(cmd, VIDEO_CRAWL_TIMEOUT)
    return JSONL_DIR


def build_video_result(jdir, link):
    res = build_result(jdir, content_prefix="detail", comment_prefix="detail")
    if not res.get("videos") and not res.get("total"):
        alt = build_result(jdir, content_prefix="search", comment_prefix="search")
        if alt.get("videos") or alt.get("total"):
            res = alt
    if not res.get("videos"):
        aid = infer_aweme_id_from_result(res, link)
        meta = fetch_xiaotan_video_meta(aid) if aid else None
        if meta:
            lc = {}
            for lead in res.get("leads") or []:
                if lead.get("aweme_id"):
                    lc[lead["aweme_id"]] = lc.get(lead["aweme_id"], 0) + 1
            meta["leads_count"] = lc.get(meta.get("aweme_id"), 0)
            res["videos"] = [meta]
    res["type"] = "video"
    res["input"] = link
    return res


def handle_video(job):
    jid = job["id"]
    pl = json.loads(job.get("payload") or "{}")
    link = pl.get("link") or job.get("keyword")
    limit = int(pl.get("comment_limit") or job.get("count") or CMT_LIMIT or 100)
    limit = max(20, min(limit, 500))
    for attempt in range(1, 3):
        try:
            if STATIC_PROXY and attempt == 1:
                server = STATIC_PROXY
                log(f"[job {jid}] video mode attempt {attempt} static proxy {STATIC_PROXY.rsplit('@', 1)[-1]}")
            else:
                server, pxip, area, isp = extract_qg()
                log(f"[job {jid}] video mode attempt {attempt} short proxy {pxip} {area}{isp}")
            if not prep_login(server):
                log(f"[job {jid}] login prep failed, retry")
                continue
            log(f"[job {jid}] crawl specified video {str(link)[:80]} comment_limit={limit}")
            crawl_ok = True
            try:
                run_video_crawl(link, limit, server)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                crawl_ok = False
            res = build_video_result(JSONL_DIR, link)
            got = res.get("total", 0) > 0 or len(res.get("videos") or []) > 0
            if not got and attempt < 2:
                log(f"[job {jid}] video mode got no data, retry")
                continue
            if not crawl_ok and got:
                log(f"[job {jid}] video crawler exited nonzero but partial data is usable")
            log(f"[job {jid}] video done leads={res.get('leads_count', 0)} comments={res.get('total', 0)} videos={len(res.get('videos') or [])}")
            http_post("/api/complete", {"token": TOKEN, "job_id": jid,
                                        "result": json.dumps(res, ensure_ascii=False)})
            return
        except Exception as e:
            log(f"[job {jid}] video attempt {attempt} error {str(e)[:90]}")
            if attempt >= 2:
                raise
    raise RuntimeError("specified video crawl failed")


def handle_search(job):
    jid, kw, cnt = job["id"], job["keyword"], job.get("count") or 10
    for attempt in range(1, 4):  # 优先长效静态IP(爬得深)，失败回退青果短效轮换
        if STATIC_PROXY and attempt == 1:
            server = STATIC_PROXY
            log(f"[job {jid}] 第{attempt}次 长效静态IP {STATIC_PROXY.rsplit('@', 1)[-1]}")
        else:
            server, pxip, area, isp = extract_qg()
            log(f"[job {jid}] 第{attempt}次 短效代理 {pxip} {area}{isp}")
        try:
            if not prep_login(server):
                log(f"[job {jid}] 登录态注入失败，换IP重试"); continue
            log(f"[job {jid}] 登录态就绪，开爬「{kw}」x{cnt}")
            crawl_ok = True
            try:
                run_crawl(kw, cnt, server)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                crawl_ok = False  # 中途失败，看有没有抓到部分数据
            res = build_result(JSONL_DIR)
            got = res["leads_count"] > 0 or len(res["videos"]) > 0
            if not got and attempt < 3:
                log(f"[job {jid}] 爬取{'中断' if not crawl_ok else ''}且无数据，换IP重试 {attempt}/3")
                continue
            if not crawl_ok and got:
                log(f"[job {jid}] 爬取中断但已抓到部分数据，采用")
            log(f"[job {jid}] ✅ {res['leads_count']} 客户 / {len(res['videos'])} 视频")
            http_post("/api/complete", {"token": TOKEN, "job_id": jid,
                                        "result": json.dumps(res, ensure_ascii=False)})
            return
        except Exception as e:
            log(f"[job {jid}] 第{attempt}次异常 {str(e)[:90]}，换IP重试")
            continue
    raise RuntimeError("搜索失败（青果代理连续不稳，3次都没成，稍后重试或检查青果余额）")


def whisper_model():
    global _WHISPER
    if _WHISPER is None:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from faster_whisper import WhisperModel
        _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
    return _WHISPER


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def download_file(url, out_path, proxy=None, retries=4):
    MAX = 300 * 1024 * 1024  # 300MB 上限，防内存/磁盘炸
    if os.path.exists(out_path) and os.path.getsize(out_path) > 100_000:
        return True
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
    handler = (urllib.request.ProxyHandler({"http": f"http://{proxy}", "https": f"http://{proxy}"})
               if proxy else urllib.request.ProxyHandler({}))   # proxy=None 时不走系统代理
    opener = urllib.request.build_opener(handler)
    for _ in range(retries):
        try:
            with opener.open(req, timeout=120) as r:
                cl = r.headers.get("Content-Length")
                if cl and int(cl) > MAX:
                    log("  文件过大跳过"); return False
                size = 0
                with open(out_path, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX:
                            log("  文件超限中止")
                            f.close()
                            if os.path.exists(out_path):
                                os.remove(out_path)
                            return False
                        f.write(chunk)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 50_000:
                return True
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception as e:
            log(f"  下载重试: {str(e)[:80]}")
            time.sleep(2)
    return False


def fetch_video(aid, url, out_path):
    """先直连下载，失败走青果代理。"""
    if download_file(url, out_path):
        return True
    log("  直连失败 → 走青果代理")
    server, *_ = extract_qg()
    return download_file(url, out_path, proxy=server)


def handle_download(job):
    jid = job["id"]
    pl = json.loads(job.get("payload") or "{}")
    aid, url = pl.get("aweme_id"), pl.get("url")
    os.makedirs(FILES_DIR, exist_ok=True)
    out = os.path.join(FILES_DIR, f"{aid}.mp4")
    log(f"[job {jid}] 提取视频 {aid}")
    if not fetch_video(aid, url, out):
        raise RuntimeError("视频下载失败（链接可能已过期）")
    size = os.path.getsize(out)
    http_post("/api/complete", {"token": TOKEN, "job_id": jid, "result": json.dumps(
        {"type": "download", "file": f"files/{aid}.mp4", "size": size, "aweme_id": aid},
        ensure_ascii=False)})
    log(f"[job {jid}] ✅ 视频 {size // 1024}KB")


def transcribe_mp4(jid, aid, mp4, remove_mp4=False):
    os.makedirs(TRANS_DIR, exist_ok=True)
    wav = os.path.join(TRANS_DIR, f"{aid}.{WORKER_ID}.wav")
    log(f"[job {jid}] 提取音频")
    subprocess.run([ffmpeg_exe(), "-y", "-i", mp4, "-ar", "16000", "-ac", "1", "-vn", wav],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"[job {jid}] 语音识别(ASR)…")
    segments, info = whisper_model().transcribe(wav, language="zh", vad_filter=True)
    text = "".join(s.text for s in segments).strip()
    cleanup = [wav]
    if remove_mp4:
        cleanup.append(mp4)
    for f in cleanup:
        try:
            os.remove(f)
        except Exception:
            pass
    http_post("/api/complete", {"token": TOKEN, "job_id": jid, "result": json.dumps(
        {"type": "transcribe", "text": text, "char_count": len(text), "aweme_id": aid},
        ensure_ascii=False)})
    log(f"[job {jid}] ✅ 文案 {len(text)} 字")


def handle_transcribe(job):
    jid = job["id"]
    pl = json.loads(job.get("payload") or "{}")
    aid, url = pl.get("aweme_id"), pl.get("url")
    os.makedirs(TRANS_DIR, exist_ok=True)
    # 临时文件加 worker 后缀，防多 worker 同 aweme_id 撞名
    mp4 = os.path.join(TRANS_DIR, f"{aid}.{WORKER_ID}.mp4")
    log(f"[job {jid}] 提取文案 {aid}：下载视频")
    if not fetch_video(aid, url, mp4):
        raise RuntimeError("视频下载失败（链接可能已过期）")
    transcribe_mp4(jid, aid, mp4, remove_mp4=True)


def handle_transcribe_file(job):
    jid = job["id"]
    pl = json.loads(job.get("payload") or "{}")
    aid = pl.get("aweme_id") or f"job_{jid}"
    rel = pl.get("file") or ""
    fname = os.path.basename(rel)
    mp4 = os.path.join(FILES_DIR, fname)
    if not fname or not mp4.startswith(FILES_DIR + os.sep):
        raise RuntimeError("本地视频路径不合法")
    if not os.path.exists(mp4) or os.path.getsize(mp4) < 50_000:
        raise RuntimeError("本地视频不存在或文件过小")
    log(f"[job {jid}] 视频号本地文件转写 {fname}")
    transcribe_mp4(jid, aid, mp4, remove_mp4=False)


def build_account_result(jdir):
    creators = load_jsonl(os.path.join(jdir, "creator_creators_*.jsonl"))
    works_all = load_jsonl(os.path.join(jdir, "creator_contents_*.jsonl"))
    comments = load_jsonl(os.path.join(jdir, "creator_comments_*.jsonl"))
    c = creators[0] if creators else {}
    sec = works_all[0].get("sec_uid") if works_all else ""
    works, seen = [], set()
    for w in works_all:
        if sec and w.get("sec_uid") != sec:
            continue
        aid = w.get("aweme_id")
        if aid in seen:
            continue
        seen.add(aid)
        works.append({
            "aweme_id": aid, "title": w.get("title") or w.get("desc") or "",
            "cover": w.get("cover_url") or "", "likes": w.get("liked_count"),
            "comments": w.get("comment_count"), "shares": w.get("share_count"),
            "collects": w.get("collected_count"), "create_time": w.get("create_time"),
            "url": w.get("aweme_url") or "", "download_url": w.get("video_download_url") or "",
        })
    return {
        "type": "account", "nickname": c.get("nickname"),
        "douyin_id": works_all[0].get("user_unique_id") if works_all else "",
        "fans": c.get("fans"), "likes": c.get("interaction"),
        "works_count": len(works), "comments_count": len(comments),
        "ip": (c.get("ip_location") or "").replace("IP属地：", ""),
        "desc": c.get("desc") or "", "avatar": c.get("avatar") or "",
        "profile": f"https://www.douyin.com/user/{sec}" if sec else "",
        "works": works,
    }


def handle_account(job):
    """账号深扒：直连机房IP（深采放行）+ --creator_id 传账号（不改 dy_config 文件）。"""
    jid, target = job["id"], job["keyword"]
    log(f"[job {jid}] 账号深扒（直连机房IP，深采接口放行）{target[:46]}")
    if not prep_login():          # 不走代理：深采放行机房IP；注入本 worker 的号
        raise RuntimeError("登录态注入失败（cookie 可能过期）")
    clear_jsonl()
    cmd = [VENV, MAIN_PY, "--platform", "dy", "--lt", "qrcode", "--type", "creator",
           "--creator_id", target,
           "--get_comment", "yes", "--get_sub_comment", "no",
           "--max_comments_count_singlenotes", "100",
           "--crawler_max_notes_count", "100",
           "--save_data_option", "jsonl", "--save_data_path", DATA_DIR,
           "--headless", "yes", "--enable_ip_proxy", "no"]
    subprocess.run(cmd, cwd=WORK_DIR, timeout=1800, check=True,
                   env={**os.environ, "NO_PROXY": "localhost,127.0.0.1"},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res = build_account_result(JSONL_DIR)
    log(f"[job {jid}] ✅ 账号「{res.get('nickname')}」作品{res.get('works_count')}/评论{res.get('comments_count')}")
    http_post("/api/complete", {"token": TOKEN, "job_id": jid,
                                "result": json.dumps(res, ensure_ascii=False)})


def main():
    modes_q = "&modes=" + urllib.parse.quote(WORKER_MODES) if WORKER_MODES else ""
    log(f"server_worker 启动 (WORKER_ID={WORKER_ID}, modes={WORKER_MODES or '全能'}, cwd={WORK_DIR})")
    while True:
        try:
            r = http_get(f"/api/claim?token={TOKEN}{modes_q}")
            job = r.get("job")
            if not job:
                time.sleep(POLL); continue
            mode = job.get("mode") or "search"
            log(f"领到任务 #{job['id']} mode={mode} kw={job.get('keyword')}")
            try:
                if mode == "transcribe":
                    handle_transcribe(job)
                elif mode == "transcribe_file":
                    handle_transcribe_file(job)
                elif mode == "download_video":
                    handle_download(job)
                elif mode == "account":
                    handle_account(job)
                elif mode == "video":
                    handle_video(job)
                else:
                    handle_search(job)
            except subprocess.TimeoutExpired:
                http_post("/api/fail", {"token": TOKEN, "job_id": job["id"], "error": "爬取超时(代理IP可能过期，重试)"})
            except Exception as e:
                log(f"[job {job['id']}] ❌ {e}")
                http_post("/api/fail", {"token": TOKEN, "job_id": job["id"], "error": str(e)[:300]})
        except Exception as e:
            log(f"轮询错误: {e}")
            time.sleep(POLL)


if __name__ == "__main__":
    main()
