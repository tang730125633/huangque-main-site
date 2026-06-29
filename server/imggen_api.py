#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄雀 · 作图后端(nano banana / Gemini 图像)—— 独立服务，不进 content_api.py。
背景：content_api.py 被多人共改反复覆盖。把 nano banana 作图做成独立端口(8101)+独立 systemd 单元，
nginx 用 location = /api/gen/banana 精确路由过来；同事怎么改/重启 content_api 都碰不到这里。

模型(nano banana = Gemini 原生作图能力的总称，三个模型)：
  nb2 → gemini-3.1-flash-image (Nano Banana 2，主力，快+便宜+中文好)
  pro → gemini-3-pro-image     (Nano Banana Pro，精品，4K/Thinking/最强中文)
共用基础设施：content_jobs.db(轮询/历史走 content_api 8096)、users.db 点数、auth(8095)、
content_out/ 出图目录(文件由 content_api 的 /api/gen/file 服务)。

⚠️ 服务器在大陆，Google API 被墙 → 本服务**走环境代理**(content.env 里的 HTTPS_PROXY=mihomo)出墙，
   与 TikHub(强制直连)相反。systemd 加载同一份 content.env。
"""
import os, json, time, base64, threading, sqlite3, pathlib, urllib.request, urllib.error
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT        = int(os.environ.get("IMGGEN_API_PORT", "8101"))
AUTH_BASE   = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")
AUTH_DB     = os.environ.get("AUTH_DB", "/home/ubuntu/auth-service/users.db")
JOB_DB      = os.environ.get("CONTENT_JOB_DB", "/home/ubuntu/content-api/content_jobs.db")
OUT_DIR     = pathlib.Path(os.environ.get("CONTENT_OUT", "/home/ubuntu/content-api/content_out"))
OUT_DIR.mkdir(parents=True, exist_ok=True)
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
GEMINI_BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com").rstrip("/")

MODELS = {"nb2": "gemini-3.1-flash-image", "pro": "gemini-3-pro-image"}
# 质量基价(最终点数=基价×数量) + 清晰度→imageSize(按 model 分档，大写K)
BASE_COST   = {"nb2": {"std": 10, "hd": 14}, "pro": {"std": 18, "hd": 26}}
IMAGE_SIZES = {"nb2": {"std": "1K", "hd": "2K"}, "pro": {"std": "2K", "hd": "4K"}}
RATIOS = {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}


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


# ============ 出图：nano banana / Gemini ============
def _build_banana_body(prompt, ratio, image=None, image_size=None):
    """Gemini generateContent 请求体：有 image=图生图(整图编辑)，无=文生图；image_size=清晰度(1K/2K/4K)。"""
    parts = []
    if image:
        # ponytail: 前端三入口(上传/继续改/结果)均经 canvas 转 PNG，恒为 PNG
        parts.append({"inlineData": {"mimeType": "image/png", "data": image}})
    parts.append({"text": prompt})
    img_cfg = {"aspectRatio": ratio}
    if image_size:
        img_cfg["imageSize"] = image_size  # ⚠️ preview 模型可能忽略，部署后 live 验证
    return {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": img_cfg},
    }

def _banana_one(model, body, idx):
    """单次出图 → 写盘，返回文件名。"""
    req = urllib.request.Request(
        GEMINI_BASE + "/v1beta/models/" + model + ":generateContent",
        data=body, headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:  # 走环境代理(HTTPS_PROXY)出墙到 Google
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ValueError("Gemini %s: %s" % (e.code, e.read()[:160].decode("u8", "ignore")))
    parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    img = next((p.get("inlineData") for p in parts if p.get("inlineData")), None)
    if not img:
        raise ValueError("出图失败：" + str((d.get("error") or {}).get("message") or d)[:140])
    fn = "nb_%d_%d.png" % (int(time.time() * 1000), idx)
    (OUT_DIR / fn).write_bytes(base64.b64decode(img["data"]))
    return fn

def gen_banana(payload):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    mkey = (payload.get("model") or "nb2").strip().lower()
    if mkey not in MODELS:
        mkey = "nb2"
    model = MODELS[mkey]
    ratio = payload.get("ratio") or "1:1"
    if ratio not in RATIOS:
        ratio = "1:1"
    image = payload.get("image")  # base64(无 data: 前缀) 参考图 → 图生图(整图编辑)
    q = "hd" if (payload.get("quality") or "hd") == "hd" else "std"
    image_size = IMAGE_SIZES[mkey][q]
    count = max(1, min(2, int(payload.get("count") or 1)))  # nano banana 循环出图，上限2(防6分钟清道夫)
    if not GEMINI_KEY:
        raise ValueError("GEMINI_API_KEY 未配置")
    body = json.dumps(_build_banana_body(prompt, ratio, image, image_size)).encode()
    files = [_banana_one(model, body, i) for i in range(count)]
    urls = ["/api/gen/file/" + f for f in files]
    return {"type": "image", "mode": ("nanobanana_img2img_" if image else "nanobanana_") + mkey, "model": model,
            "image_size": image_size, "count": count, "file": files[0], "url": urls[0], "files": files, "urls": urls,
            "ratio": ratio, "prompt": prompt}


# ============ worker（失败退点；清道夫由 content_api 统一跑） ============
def run_job(job_id):
    with closing(jdb()) as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        return
    payload = json.loads(r["payload"] or "{}")
    try:
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (int(time.time()), job_id)); c.commit()
        result = gen_banana(payload)
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=?",
                      (json.dumps(result, ensure_ascii=False), int(time.time()), job_id)); c.commit()
    except Exception as e:
        add_points(r["username"], r["cost"])  # 失败退点
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
        if p == "/api/gen/banana":
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录或登录已过期"})
            body = self._json_body()
            mk = (body.get("model") or "nb2").strip().lower()
            if mk not in BASE_COST: mk = "nb2"
            cq = "hd" if (body.get("quality") or "hd") == "hd" else "std"
            cn = max(1, min(2, int(body.get("count") or 1)))
            cost = BASE_COST[mk][cq] * cn  # 质量基价 × 数量
            if get_points(user["username"]) < cost:
                return self._send(402, {"detail": "点数不足", "need": cost})
            add_points(user["username"], -cost)
            now = int(time.time())
            with closing(jdb()) as c:
                cur = c.execute("INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at) VALUES('image',?,?,?,?,?)",
                                (user["username"], cost, json.dumps(body, ensure_ascii=False), now, now))
                c.commit(); jid = cur.lastrowid
            threading.Thread(target=run_job, args=(jid,), daemon=True).start()
            return self._send(200, {"job_id": jid, "cost": cost, "points_left": get_points(user["username"])})
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        if self.path.split("?")[0] == "/api/gen/banana/health":
            return self._send(200, {"ok": True, "service": "huangque-imggen", "models": MODELS, "has_key": bool(GEMINI_KEY)})
        self._send(404, {"detail": "not found"})


def _selftest():
    b = _build_banana_body("画只猫", "1:1", None)
    assert b["contents"][0]["parts"] == [{"text": "画只猫"}], b
    b2 = _build_banana_body("把背景换成红色", "9:16", "QUJD")
    p = b2["contents"][0]["parts"]
    assert p[0]["inlineData"] == {"mimeType": "image/png", "data": "QUJD"}, b2
    assert p[1] == {"text": "把背景换成红色"}, b2
    assert b2["generationConfig"]["imageConfig"]["aspectRatio"] == "9:16", b2
    assert "imageSize" not in b2["generationConfig"]["imageConfig"], b2  # 没传 size 时不带
    b3 = _build_banana_body("x", "1:1", None, "4K")
    assert b3["generationConfig"]["imageConfig"]["imageSize"] == "4K", b3
    assert IMAGE_SIZES["pro"]["hd"] == "4K" and IMAGE_SIZES["nb2"]["std"] == "1K"
    assert BASE_COST["pro"]["hd"] == 26 and BASE_COST["nb2"]["std"] == 10
    print("imggen selftest OK")

if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest(); raise SystemExit(0)
    print("huangque-imggen-api on 127.0.0.1:%d  models=%s" % (PORT, MODELS))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
