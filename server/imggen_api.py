#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
榛勯泙 路 浣滃浘鍚庣(nano banana / Gemini 鍥惧儚)鈥斺€?鐙珛鏈嶅姟锛屼笉杩?content_api.py銆?
鑳屾櫙锛歝ontent_api.py 琚浜哄叡鏀瑰弽澶嶈鐩栥€傛妸 nano banana 浣滃浘鍋氭垚鐙珛绔彛(8101)+鐙珛 systemd 鍗曞厓锛?
nginx 鐢?location = /api/gen/banana 绮剧‘璺敱杩囨潵锛涘悓浜嬫€庝箞鏀?閲嶅惎 content_api 閮界涓嶅埌杩欓噷銆?

妯″瀷(nano banana = Gemini 鍘熺敓浣滃浘鑳藉姏鐨勬€荤О锛屼笁涓ā鍨?锛?
  nb2 鈫?gemini-3.1-flash-image (Nano Banana 2锛屼富鍔涳紝蹇?渚垮疁+涓枃濂?
  pro 鈫?gemini-3-pro-image     (Nano Banana Pro锛岀簿鍝侊紝4K/Thinking/鏈€寮轰腑鏂?
鍏辩敤鍩虹璁炬柦锛歝ontent_jobs.db(杞/鍘嗗彶璧?content_api 8096)銆乽sers.db 鐐规暟銆乤uth(8095)銆?
content_out/ 鍑哄浘鐩綍(鏂囦欢鐢?content_api 鐨?/api/gen/file 鏈嶅姟)銆?

鈿狅笍 鏈嶅姟鍣ㄥ湪澶ч檰锛孏oogle API 琚 鈫?鏈湇鍔?*璧扮幆澧冧唬鐞?*(content.env 閲岀殑 HTTPS_PROXY=mihomo)鍑哄锛?
   涓?TikHub(寮哄埗鐩磋繛)鐩稿弽銆俿ystemd 鍔犺浇鍚屼竴浠?content.env銆?
"""
import os, json, time, base64, threading, queue, sqlite3, pathlib, urllib.request, urllib.error, io
from contextlib import closing
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from content_domains import feature_flags
except ImportError:
    feature_flags = None

AUTH_COOKIE_NAME = os.environ.get("HQ_AUTH_COOKIE_NAME", "hq_session")

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

try:
    from PIL import Image
except Exception:
    Image = None

PORT        = int(os.environ.get("IMGGEN_API_PORT", "8101"))
AUTH_BASE   = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")
INTERNAL_TOKEN = os.environ.get("HQ_INTERNAL_TOKEN", "")
JOB_DB      = os.environ.get("CONTENT_JOB_DB", "/home/ubuntu/content-api/content_jobs.db")
SERVICE_OWNER = "imggen"   # 写进 jobs.owner，让 content 的 pending 重排/孤儿回收扫描认出这不是它的活(#511)

# ---- 生图任务池（与 content 8096 的生图池对齐）----
# 改动前这里是 threading.Thread(target=run_job).start()：无池、无队列、无闸——单个用户
# 想开几个开几个(线程数=在飞任务数)，还能把 Gemini 打到 429。
# 闸数的是 jobs 全表 kind='image'，content(gpt/seedream/果肉/泽龙2) 也写这张表，
# 所以「每人最多 3 个生图在跑」是跨两个服务统一的，不是各算各的 3 个。
JOB_WORKERS  = max(1, int(os.environ.get("IMGGEN_JOB_WORKERS", "10") or 10))
JOB_QUEUE_MAX = max(1, int(os.environ.get("IMGGEN_JOB_QUEUE_MAX", "32") or 32))
MAX_USER_RUNNING_IMAGE = max(1, int(os.environ.get("MAX_USER_RUNNING_IMAGE", "3") or 3))   # 与 content 同名同默认值
MAX_USER_ACTIVE_JOBS = max(1, int(os.environ.get("MAX_USER_ACTIVE_JOBS", "5") or 5))       # pending+running 提交闸，防单用户占满队列

OUT_DIR     = pathlib.Path(os.environ.get("CONTENT_OUT", "/home/ubuntu/content-api/content_out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com").rstrip("/")   # 兜底档：线上=heygen 中转
# 出境优先级链见 content_domains/egress.py。官方 Gemini 直连地址，走 VPS隧道/mihomo 代理时用。
GEMINI_OFFICIAL_BASE = os.environ.get("GEMINI_OFFICIAL_BASE", "https://generativelanguage.googleapis.com").rstrip("/")
# ---- 提示词反推（图→文，多模态文本模型）----
REVERSE_MODEL = os.environ.get("REVERSE_MODEL", "gemini-2.5-flash")   # 反推用的多模态文本模型，可 env 覆盖
REVERSE_COST  = int(os.environ.get("REVERSE_COST", "2"))              # 反推点数
REVERSE_INSTRUCTION = ("你是资深美业广告视觉分析师。仔细看这张图，反推出一条可直接用于文生图的中文提示词，"
    "用来生成同风格但全新原创的图（不是逐字描述这张图，而是能复现其风格气质、可换主体细节，版权安全）。"
    "需覆盖：主体、构图/机位、场景与材质道具、光影与色调、留白位置、画面内中文文案（若有）。"
    "约 60-120 字，直接输出这条提示词本身，不要任何解释、前后缀或引号。")
# 反推并发闸：同步调 Gemini 会占住 HTTP 线程，限并发防打爆上游/线程池（可 env 覆盖）
_reverse_sem = threading.BoundedSemaphore(max(1, int(os.environ.get("REVERSE_MAX_CONCURRENCY", "2") or "2")))

# ============ COS 出图存储（可选，与 content_api 共用同一个 content.env 的 COS_* 环境变量）============
# 配置齐全且文件存在 → 上传 COS 返回直链；未配置/失败 → 回退本地 /api/gen/file/，零影响。密钥仅走环境变量。
_COS_ID     = os.environ.get("COS_SECRET_ID", "").strip()
_COS_KEY    = os.environ.get("COS_SECRET_KEY", "").strip()
_COS_REGION = os.environ.get("COS_REGION", "").strip()
_COS_BUCKET = os.environ.get("COS_BUCKET", "").strip()
_COS_PREFIX = os.environ.get("COS_PREFIX", "").strip().strip("/")
_COS_DOMAIN = os.environ.get("COS_DOMAIN", "").strip().rstrip("/")
_cos_client = None

def _cos_enabled():
    return bool(_COS_ID and _COS_KEY and _COS_REGION and _COS_BUCKET)

def _cos_get_client():
    global _cos_client
    if _cos_client is None:
        from qcloud_cos import CosConfig, CosS3Client  # 服务器已装；懒加载，本地/CI 不触发
        _cos_client = CosS3Client(CosConfig(Region=_COS_REGION, SecretId=_COS_ID, SecretKey=_COS_KEY, Scheme="https"))
    return _cos_client

def _public_url(rel, content_type=None):
    local = "/api/gen/file/" + str(rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return local
    try:
        if _cos_enabled():
            fp = OUT_DIR / rel
            if fp.is_file():
                key = (_COS_PREFIX + "/" + str(rel).lstrip("/")) if _COS_PREFIX else str(rel).lstrip("/")
                with open(fp, "rb") as f:
                    kw = {"Bucket": _COS_BUCKET, "Key": key, "Body": f}
                    if content_type:
                        kw["ContentType"] = content_type
                    _cos_get_client().put_object(**kw)
                if _COS_DOMAIN:
                    return _COS_DOMAIN + "/" + key
                return "https://%s.cos.%s.myqcloud.com/%s" % (_COS_BUCKET, _COS_REGION, key)
    except Exception as e:
        print("[imggen] COS 上传失败，回退本地: %s -> %s" % (rel, e), flush=True)
    return local

MODELS = {"nb2": "gemini-3.1-flash-image", "pro": "gemini-3-pro-image"}
# 璐ㄩ噺鍩轰环(鏈€缁堢偣鏁?鍩轰环脳鏁伴噺) + 娓呮櫚搴︹啋imageSize(鎸?model 鍒嗘。锛屽ぇ鍐橩)
BASE_COST   = {"nb2": {"std": 15, "hd": 25}, "pro": {"std": 25, "hd": 30}}
IMAGE_SIZES = {"nb2": {"std": "1K", "hd": "2K"}, "pro": {"std": "2K", "hd": "4K"}}
RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024

def _clean_b64(value):
    raw = (value or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    return "".join(raw.split())

def _validate_b64_image(body, field):
    raw = _clean_b64(body.get(field))
    if not raw:
        return
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        raise ValueError("%s 必须是合法 base64" % field)
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError("图片太大，请压缩到 10MB 以内后重试")
    body[field] = raw

def validate_banana_payload(body):
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    if len(prompt) > 2000:
        raise ValueError("提示词不能超过 2000 字")
    mkey = (body.get("model") or "nb2").strip().lower()
    if mkey not in MODELS:
        raise ValueError("model 仅支持 nb2/pro")
    ratio = (body.get("ratio") or "1:1").strip()
    if ratio not in RATIOS:
        raise ValueError("ratio 仅支持: " + ", ".join(sorted(RATIOS)))
    q = (body.get("quality") or "std").strip().lower()
    if q not in {"std", "hd"}:
        raise ValueError("quality 仅支持 std/hd")
    try:
        count = int(body.get("count") or 1)
    except Exception:
        raise ValueError("count 必须是 1 或 2")
    if count not in {1, 2}:
        raise ValueError("count 必须是 1 或 2")
    _validate_b64_image(body, "image")
    body["prompt"] = prompt
    body["model"] = mkey
    body["ratio"] = ratio
    body["quality"] = q
    body["count"] = count
    return body

def _parse_ratio(ratio):
    try:
        w, h = (int(x) for x in str(ratio).split(":", 1))
        if w > 0 and h > 0:
            return w / h
    except Exception:
        pass
    return None

def _normalize_image_ratio(raw, ratio):
    target_ratio = _parse_ratio(ratio)
    if not target_ratio or Image is None:
        return raw, None
    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        mode = "RGBA" if im.mode in ("RGBA", "LA") else "RGB"
        im = im.convert(mode)
        sw, sh = im.size
        src_ratio = sw / sh
        if abs(src_ratio - target_ratio) > 0.001:
            if src_ratio > target_ratio:
                nw = max(1, int(sh * target_ratio))
                left = max(0, (sw - nw) // 2)
                im = im.crop((left, 0, left + nw, sh))
            else:
                nh = max(1, int(sw / target_ratio))
                top = max(0, (sh - nh) // 2)
                im = im.crop((0, top, sw, top + nh))
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue(), {"width": im.size[0], "height": im.size[1]}


# ============ 鍏变韩绠￠亾锛氫换鍔″簱 / 鐐规暟 / 閴存潈 ============
def jdb():
    c = sqlite3.connect(JOB_DB, timeout=10); c.row_factory = sqlite3.Row; return c

def _auth_points(path, username, amount, reason="", transaction_key=""):
    if not INTERNAL_TOKEN:
        return 500, {"detail": "HQ_INTERNAL_TOKEN 未配置"}
    payload = {"username": username, "amount": int(amount), "reason": reason}
    if transaction_key:
        payload["transaction_key"] = str(transaction_key)
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        AUTH_BASE + path,
        data=body,
        headers={"Content-Type": "application/json", "X-HQ-Internal-Token": INTERNAL_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read() or b"{}")
        except Exception:
            data = {"detail": "points update failed"}
        return e.code, data
    except Exception:
        return 500, {"detail": "points update failed"}

def deduct_points(username, amount, reason=""):
    return _auth_points("/api/auth/points/deduct", username, amount, reason)

def refund_points(username, amount, reason="", transaction_key=""):
    if transaction_key:
        return _auth_points("/api/auth/points/refund", username, amount, reason, transaction_key)
    return _auth_points("/api/auth/points/refund", username, amount, reason)


def _deduct_paid_job(username, amount, reason):
    from content_domains import jobs_store
    status, data = deduct_points(username, amount, reason)
    if status != 200:
        raise jobs_store.PaidJobDeductError(status, (data or {}).get("detail") or "点数扣除失败")
    return int((data or {}).get("points") or 0)

def verify(token):
    if not token: return None
    try:
        req = urllib.request.Request(AUTH_BASE + "/api/auth/me", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read()).get("user")
    except Exception:
        return None


# ============ Nano Banana / Gemini image generation ============
def _build_banana_body(prompt, ratio, image=None, image_size=None):
    """Build Gemini generateContent request body."""
    parts = []
    if image:
        # Frontend sends uploaded/reference/result images as PNG base64.
        parts.append({"inlineData": {"mimeType": "image/png", "data": image}})
    parts.append({"text": prompt})
    img_cfg = {"aspectRatio": ratio}
    if image_size:
        img_cfg["imageSize"] = image_size
    return {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": img_cfg},
    }

def _banana_one(model, body, idx, ratio=None):
    """Generate one image, save it, and return filename plus dimensions."""
    # 出境优先级：VPS 隧道 → mihomo → heygen（见 egress.py）。前档超时/报错自动降级；
    # 未配 EGRESS_* 时只走 heygen，行为与改动前一致。
    from content_domains import egress
    d = egress.post_json(
        GEMINI_OFFICIAL_BASE, GEMINI_BASE,
        "/v1beta/models/" + model + ":generateContent", body,
        {"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
        log=lambda m: print(m, flush=True))
    parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    img = next((p.get("inlineData") for p in parts if p.get("inlineData")), None)
    if not img:
        raise ValueError("出图失败：" + str((d.get("error") or {}).get("message") or d)[:140])
    raw = base64.b64decode(img["data"])
    raw, dim = _normalize_image_ratio(raw, ratio)
    fn = "nb_%d_%d.png" % (int(time.time() * 1000), idx)
    (OUT_DIR / fn).write_bytes(raw)
    return fn, dim

# ============ worker锛堝け璐ラ€€鐐癸紱娓呴亾澶敱 content_api 缁熶竴璺戯級 ============
def gen_banana(payload):
    payload = validate_banana_payload(payload)
    prompt = payload["prompt"]
    mkey = payload["model"]
    model = MODELS[mkey]
    ratio = payload["ratio"]
    image = payload.get("image")
    q = payload["quality"]
    image_size = IMAGE_SIZES[mkey][q]
    count = payload["count"]
    if not GEMINI_KEY:
        raise ValueError("GEMINI_API_KEY 未配置")
    body = json.dumps(_build_banana_body(prompt, ratio, image, image_size)).encode()
    items = [_banana_one(model, body, i, ratio) for i in range(count)]
    files = [fn for fn, _ in items]
    dimensions = [dim for _, dim in items if dim]
    urls = [_public_url(f, "image/png") for f in files]
    result = {"type": "image", "mode": ("nanobanana_img2img_" if image else "nanobanana_") + mkey, "model": model,
            "image_size": image_size, "quality": q, "count": count, "file": files[0], "url": urls[0],
            "files": files, "urls": urls, "ratio": ratio, "prompt": prompt}
    if dimensions:
        result["width"] = dimensions[0]["width"]
        result["height"] = dimensions[0]["height"]
        result["dimensions"] = dimensions
    return result

# 与 content_domains/core.py 的 _set_terminal/_refund_once 同语义：本服务与 content_api 共写
# 同一张 jobs 表，reaper 只在 content_api 里跑。不做 CAS 就会「reaper 判超时退了点，
# worker 随后把 error 覆写回 done」——用户既拿到图又拿回点数(线上 image 有 10 条这种记录)。
# CAS 抢终态 / 退点幂等：实现在 content_domains/jobs_store.py，三个共写 jobs 表的服务共用一份。
def _set_terminal(job_id, status, result=None, error=None, from_states=("running",)):
    from content_domains import jobs_store
    return jobs_store.set_terminal(jdb, job_id, status, result, error, from_states)

def _refund_via_auth(username, cost, reason="", transaction_key=""):
    """本服务没有直写 users.db 的兜底：Auth 未确认就保持退款待确认态。"""
    status, data = refund_points(username, cost, reason, transaction_key)
    if status == 200:
        return True
    print("imggen refund pending user=%s status=%s detail=%s（保留待确认，稍后重试）" % (
        username, status, (data or {}).get("detail")), flush=True)
    return False

def _refund_once(job_id, username, cost):
    from content_domains import jobs_store
    transaction_key = jobs_store.refund_transaction_key(job_id, username)
    return jobs_store.refund_once(jdb, job_id, username, cost,
                                  lambda u, c: _refund_via_auth(
                                      u, c, "job#%d" % job_id, transaction_key))

def run_job(job_id):
    with closing(jdb()) as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        return
    payload = json.loads(r["payload"] or "{}")
    started = False   # 只有真跑起来的任务，结束时才去重排队列——被闸挡回的不重排，否则 worker 自旋
    try:
        # 单用户生图运行闸 + 原子抢 running：count 与 claim 必须同锁内完成，
        # 否则多个 worker 同时数到 2 会一起放行，冲破上限。整段放进 try：抢 running
        # 那句 UPDATE 自己抛异常时任务还停在 pending，下面 except 的 from_states 含
        # pending 才能把它判死退点——否则预扣的点永久丢失(reaper 只扫 running)。
        with _run_gate_lock:
            if _user_running_image_count(r["username"]) >= MAX_USER_RUNNING_IMAGE:
                return  # 超闸→不启动，留 pending 排队；等同用户任务完成或 30s 扫描器重排
            with closing(jdb()) as c:   # CAS 认领：只有 pending 才能被本次执行接管，防同一 job 被跑两遍
                claimed = c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='pending'",
                                    (int(time.time()), job_id)); c.commit()
            if claimed.rowcount < 1:
                return  # 已被别的线程接管或已是终态
            started = True
        result = gen_banana(payload)
        if not _set_terminal(job_id, "done", result=result):
            # reaper 已把它判超时并退点：不覆写终态。宁可用户重试，也不能既退点又出图。
            print("[imggen] job %s 完成时已非 running（reaper 判超时在先），丢弃结果" % job_id, flush=True)
            return
        # 出图产物不入统一 assets 表：图片走 jobs.result → /api/gen/history，
        # 那才是 assets.html 图片分类读的数据源。见 assets_store.KIND_STAGE 的注释。
    except Exception as e:
        # from_states 含 pending：认领那句 UPDATE 自己抛异常时任务还停在 pending，
        # 只认 running 会导致不退点且 reaper 永远扫不到它
        if _set_terminal(job_id, "error", error=str(e), from_states=("pending", "running")):
            _refund_once(job_id, r["username"], r["cost"])
    finally:
        if started:
            _recover_pending_jobs()   # 腾出一个运行槽 → 立刻重排排队中的(+30s 扫描兜底)


# ============ 生图任务池：有界 worker + 单用户并发闸 ============
_job_queue = queue.Queue(maxsize=JOB_QUEUE_MAX)
_queued_job_ids = set()          # 防同一 job 被重复入队(提交入队 与 扫描器重排 会撞)
_queue_lock = threading.Lock()
_run_gate_lock = threading.Lock()      # 运行闸：count+抢running 原子，防多 worker 同时过闸超发
_submission_lock = threading.Lock()    # 提交闸：活跃数检查+扣点+入队串行，防并发提交一起冲破上限


def enqueue_job(job_id):
    """入队。队列满返回 False，调用方须把该 pending 任务判死并退点，别让它烂在库里。"""
    with _queue_lock:
        if job_id in _queued_job_ids:
            return True
        try:
            _job_queue.put_nowait(job_id)
        except queue.Full:
            return False
        _queued_job_ids.add(job_id)
        return True


def _user_running_image_count(username):
    """该用户「运行中」的生图条数。数的是全表 kind='image'——含 content 那边的 gpt/seedream，
    这正是让「每人 3 个生图」跨两个服务统一生效的关键，否则会变成各自 3 个 = 实际 6 个。"""
    if not username:
        return 0
    with closing(jdb()) as c:
        row = c.execute("SELECT COUNT(*) AS n FROM jobs WHERE username=? AND status='running' AND kind='image'",
                        (username,)).fetchone()
    return int(row["n"] if row else 0)


def _user_active_job_count(username):
    """该用户 pending+running 的本服务任务数：提交闸，防单用户把 32 个队列位占满饿死别人。"""
    with closing(jdb()) as c:
        row = c.execute("""SELECT COUNT(*) AS n FROM jobs
                           WHERE username=? AND status IN ('pending','running') AND owner=?""",
                        (username, SERVICE_OWNER)).fetchone()
    return int(row["n"] if row else 0)


def _reject_pending_job(job_id, username, cost, reason):
    """入队失败(队列满)：把刚插入的 pending 任务判死并退点。"""
    if _set_terminal(job_id, "error", error=reason, from_states=("pending",)):
        _refund_once(job_id, username, cost)


def _job_worker():
    while True:
        job_id = _job_queue.get()
        try:
            run_job(job_id)
        except Exception as e:
            print("[imggen] worker 异常 job=%s: %s" % (job_id, e), flush=True)
        finally:
            with _queue_lock:
                _queued_job_ids.discard(job_id)
            _job_queue.task_done()


def _recover_pending_jobs(limit=None):
    """重排本服务名下仍是 pending 的任务：被闸挡回的 + 上次重启时还没轮到的。
    只捞 owner=本服务的，别去动 content 的活（对称地，content 也不会来动我们的）。"""
    limit = int(limit or JOB_QUEUE_MAX)
    try:
        with closing(jdb()) as c:
            rows = c.execute("""SELECT id FROM jobs
                                WHERE status='pending' AND owner=?
                                ORDER BY id ASC LIMIT ?""", (SERVICE_OWNER, limit)).fetchall()
    except Exception:
        return 0
    n = 0
    for r in rows:
        if not enqueue_job(r["id"]):
            break   # 队列满，剩下的留给下一轮扫描
        n += 1
    return n


def _pending_job_scanner():
    while True:
        try:
            _recover_pending_jobs()
        except Exception:
            pass
        time.sleep(30)


def start_job_workers():
    for i in range(JOB_WORKERS):
        threading.Thread(target=_job_worker, name="imggen-image-worker-%d" % i, daemon=True).start()
    threading.Thread(target=_pending_job_scanner, name="imggen-job-recover", daemon=True).start()
    _recover_pending_jobs()


# ============ 提示词反推：图 → Gemini 多模态 → 文生图提示词（同步，不建 job） ============
def gen_reverse(image):
    if not GEMINI_KEY:
        raise ValueError("GEMINI_API_KEY 未配置")
    if not image:
        raise ValueError("缺少图片")
    body = json.dumps({
        "contents": [{"parts": [
            {"inlineData": {"mimeType": "image/png", "data": image}},
            {"text": REVERSE_INSTRUCTION},
        ]}],
        "generationConfig": {"responseModalities": ["TEXT"], "temperature": 0.7, "maxOutputTokens": 500},
    }).encode()
    req = urllib.request.Request(
        GEMINI_BASE + "/v1beta/models/" + REVERSE_MODEL + ":generateContent",
        data=body, headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ValueError("Gemini %s: %s" % (e.code, e.read()[:160].decode("u8", "ignore")))
    parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = " ".join(p.get("text", "") for p in parts if p.get("text")).strip().strip('"“”')
    if not text:
        raise ValueError("反推失败：" + str((d.get("error") or {}).get("message") or d)[:140])
    return text[:600]


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
        if p == "/api/gen/banana":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录或登录已过期"})
            if user.get("must_change"):
                return self._send(403, {"detail": "请先修改初始密码后再使用"})
            if feature_flags is not None:
                try:
                    feature_flags.require_enabled("banana")
                except feature_flags.FeatureDisabled as e:
                    return self._send(503, {"detail": str(e)})
            body = self._json_body()
            try:
                body = validate_banana_payload(body)
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            mk = body["model"]
            cq = body["quality"]
            cn = body["count"]
            cost = BASE_COST[mk][cq] * cn  # 璐ㄩ噺鍩轰环 脳 鏁伴噺
            with _submission_lock:
                active_jobs = _user_active_job_count(user["username"])
                if active_jobs >= MAX_USER_ACTIVE_JOBS:
                    return self._send(429, {"detail": "您有 %d 个生图任务正在排队/生成，完成后再提交" % active_jobs,
                                            "code": "active_job_cap", "active_jobs": active_jobs,
                                            "max_active_jobs": MAX_USER_ACTIVE_JOBS,
                                            "retry_after_ms": 4000, "need": cost})
                try:
                    from content_domains import jobs_store
                    jid, points_left = jobs_store.create_paid_job(
                        jdb, _deduct_paid_job, _refund_via_auth, "image", user["username"],
                        cost, body, SERVICE_OWNER)
                except jobs_store.PaidJobDeductError as e:
                    return self._send(402 if e.status == 402 else 500,
                                      {"detail": e.detail, "need": cost})
                except jobs_store.PaidJobInsertError as e:
                    return self._send(500, {"detail": {"refunded": "任务创建失败，点数已退回",
                        "queued": "任务创建失败，退款正在自动确认"}.get(e.compensation,
                        "任务创建失败，退款需人工核对"), "submission_ref": e.submission_ref})
                # 入队，不再裸起线程：有界 worker 池 + 单用户运行闸(见 run_job)。
                # 队列满就当场判死退点——静默丢任务等于白扣用户的点。
                if not enqueue_job(jid):
                    _reject_pending_job(jid, user["username"], cost, "任务队列已满，请稍后再试")
                    return self._send(429, {"detail": "任务队列已满，请稍后再试", "retry_after_ms": 5000})
            return self._send(200, {"job_id": jid, "cost": cost, "points_left": points_left})
        if p == "/api/gen/reverse":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录或登录已过期"})
            if user.get("must_change"):
                return self._send(403, {"detail": "请先修改初始密码后再使用"})
            body = self._json_body()
            image = (body.get("image") or "").strip()
            if image.startswith("data:") and "," in image:
                image = image.split(",", 1)[1]  # 去掉 data URL 前缀，只留 base64
            if not image:
                return self._send(400, {"detail": "请先上传或粘贴一张图片"})
            if len(image) > 8 * 1024 * 1024:     # base64 ~8MB ≈ 原图 6MB
                return self._send(400, {"detail": "图片太大，请压缩后再试"})
            cost = REVERSE_COST
            deduct_status, deduct_data = deduct_points(user["username"], cost, "reverse")
            if deduct_status == 402:
                return self._send(402, {"detail": "点数不足", "need": cost})
            if deduct_status != 200:
                return self._send(500, {"detail": (deduct_data or {}).get("detail") or "点数扣除失败"})
            points_left = (deduct_data.get("points") if isinstance(deduct_data, dict) else None)
            try:
                with _reverse_sem:                       # 限并发，防同步调用打爆上游/线程池
                    prompt = gen_reverse(image)
            except Exception as e:
                refund_points(user["username"], cost, "reverse:refund")   # 失败退点
                return self._send(502, {"detail": "反推失败：" + str(e)[:160]})
            return self._send(200, {"prompt": prompt, "cost": cost, "points_left": points_left})
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        if self.path.split("?")[0] == "/api/gen/banana/health":
            return self._send(200, {"ok": True, "service": "huangque-imggen", "models": MODELS, "has_key": bool(GEMINI_KEY)})
        self._send(404, {"detail": "not found"})


def _selftest():
    b = _build_banana_body("draw a cat", "1:1", None)
    assert b["contents"][0]["parts"] == [{"text": "draw a cat"}], b
    b2 = _build_banana_body("change background to red", "9:16", "QUJD")
    p = b2["contents"][0]["parts"]
    assert p[0]["inlineData"] == {"mimeType": "image/png", "data": "QUJD"}, b2
    assert p[1] == {"text": "change background to red"}, b2
    assert b2["generationConfig"]["imageConfig"]["aspectRatio"] == "9:16", b2
    assert "imageSize" not in b2["generationConfig"]["imageConfig"], b2
    b3 = _build_banana_body("x", "1:1", None, "4K")
    assert b3["generationConfig"]["imageConfig"]["imageSize"] == "4K", b3
    assert IMAGE_SIZES["pro"]["hd"] == "4K" and IMAGE_SIZES["nb2"]["std"] == "1K"
    assert BASE_COST["pro"]["hd"] == 26 and BASE_COST["nb2"]["std"] == 10
    if Image is not None:
        buf = io.BytesIO()
        Image.new("RGB", (1, 1), (255, 255, 255)).save(buf, format="PNG")
        raw = buf.getvalue()
        encoded = base64.b64encode(raw).decode("ascii")
        checked = validate_banana_payload({"prompt": "use reference", "image": "data:image/png;base64," + encoded})
        assert checked["image"] == encoded, checked
        normalized, dim = _normalize_image_ratio(raw, "1:1")
        assert normalized and dim == {"width": 1, "height": 1}, dim
    print("imggen selftest OK")

if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); raise SystemExit(0)
    if feature_flags is not None:
        feature_flags.init_db()
    from content_domains import jobs_store
    jobs_store.ensure_owner_column(jdb)   # 必须在 start_job_workers 之前：重排扫描按 owner 过滤
    start_job_workers()
    print("huangque-imggen-api on 127.0.0.1:%d  models=%s workers=%d 单用户生图并发上限=%d"
          % (PORT, MODELS, JOB_WORKERS, MAX_USER_RUNNING_IMAGE))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
