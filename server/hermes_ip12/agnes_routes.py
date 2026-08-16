from flask import request, jsonify, render_template, send_from_directory
from pathlib import Path
from datetime import datetime
import json
import time
import uuid
import urllib.request
import urllib.error
from werkzeug.utils import secure_filename
from artifact_store import (
    StorageQuotaExceeded,
    atomic_append_bytes,
    atomic_write_bytes,
    reserve_capacity,
)

BASE_URL = "https://apihub.agnes-ai.com/v1"
TEXT_MODEL = "agnes-2.0-flash"
IMAGE_MODEL = "agnes-image-2.0-flash"
VIDEO_MODEL = "agnes-video-v2.0"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_name(prefix):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_')}{uuid.uuid4().hex[:6]}"


def read_agnes_key(project_dir):
    for name in [".agnes_key", "agnes_key.txt"]:
        p = Path(project_dir) / name
        if p.exists():
            v = p.read_text(encoding="utf-8", errors="ignore").strip()
            if v.startswith("sk-"):
                return v
    return ""


def headers(key, json_content=True):
    h = {"Authorization": f"Bearer {key}", "User-Agent": "HermesCockpit/AgnesLab"}
    if json_content:
        h["Content-Type"] = "application/json"
    return h


def http_json(path, key, payload=None, method=None, timeout=90):
    url = BASE_URL + path
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers(key, json_content=payload is not None), method=method or ("POST" if payload is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except Exception:
                body = {"raw": raw}
            return {"ok": True, "status": r.status, "body": body}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw}
        return {"ok": False, "status": e.code, "body": body, "error": raw[:1200]}
    except Exception as e:
        return {"ok": False, "status": 0, "body": {}, "error": str(e)}


def append_run(runs_path, row):
    content = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_append_bytes(runs_path, content)


def extract_text_completion(body):
    try:
        return body["choices"][0]["message"]["content"]
    except Exception:
        return ""


def pick_video_url(body):
    if not isinstance(body, dict):
        return ""
    candidates = [
        body.get("url"),
        body.get("video_url"),
        body.get("output_url"),
        body.get("remixed_from_video_id"),
    ]
    data = body.get("data")
    if isinstance(data, dict):
        candidates += [data.get("url"), data.get("video_url"), data.get("output_url"), data.get("remixed_from_video_id")]
    for u in candidates:
        if isinstance(u, str) and u.startswith("http") and ".mp4" in u:
            return u
    return ""


def download_video_if_ready(body, task_id, video_root):
    remote_url = pick_video_url(body)
    if not remote_url:
        return None
    safe_task = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in (task_id or safe_name("task")))
    fn = f"agnes_video_{safe_task}.mp4"
    path = video_root / fn
    if not path.exists() or path.stat().st_size == 0:
        req = urllib.request.Request(remote_url, headers={"User-Agent": "HermesCockpit/AgnesLab"})
        with urllib.request.urlopen(req, timeout=240) as rr:
            atomic_write_bytes(path, rr.read())
    return {"filename": fn, "url": f"/media/agnes/videos/{fn}", "path": str(path), "remote_url": remote_url, "bytes": path.stat().st_size}


ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def public_url_for_request(local_path):
    # 公网页面必须给 Agnes 一个它能访问的 image_url；本地 127.0.0.1 只适合本机预览，不适合云端生成。
    root = request.url_root.rstrip("/")
    return root + local_path


def register_agnes_routes(app, project_dir, data_root=None):
    project_dir = Path(project_dir)
    data_root = Path(data_root or project_dir / "data")
    lab_root = data_root / "agnes_lab"
    image_root = lab_root / "images"
    video_root = lab_root / "videos"
    runs_path = lab_root / "runs.jsonl"
    for p in [lab_root, image_root, video_root]:
        p.mkdir(parents=True, exist_ok=True)

    @app.route("/agnes-lab")
    def agnes_lab():
        return render_template("agnes_lab.html")

    @app.route("/media/agnes/<kind>/<path:filename>")
    def media_agnes(kind, filename):
        root = image_root if kind == "images" else video_root if kind == "videos" else None
        if root is None:
            return jsonify({"ok": False, "error": "bad kind"}), 400
        full = (root / filename).resolve()
        if not full.is_relative_to(root.resolve()) or not full.exists():
            return jsonify({"ok": False, "error": "file not found"}), 404
        return send_from_directory(str(full.parent), full.name)

    @app.route("/api/agnes/status")
    def api_agnes_status():
        key = read_agnes_key(project_dir)
        return jsonify({
            "ok": True,
            "has_key": bool(key),
            "base_url": BASE_URL,
            "models": {"text": TEXT_MODEL, "image": IMAGE_MODEL, "video": VIDEO_MODEL},
            "data_root": str(lab_root),
        })

    @app.route("/api/agnes/models")
    def api_agnes_models():
        key = read_agnes_key(project_dir)
        if not key:
            return jsonify({"ok": False, "error": "缺少 Agnes API Key"}), 400
        t0 = time.time()
        out = http_json("/models", key, timeout=30)
        row = {"time": now(), "type": "models", "ok": out.get("ok"), "status": out.get("status"), "elapsed": round(time.time() - t0, 2)}
        append_run(runs_path, row)
        return jsonify({"ok": out.get("ok"), "elapsed": row["elapsed"], "result": out.get("body"), "error": out.get("error", "")}), (200 if out.get("ok") else 502)

    @app.route("/api/agnes/text", methods=["POST"])
    def api_agnes_text():
        key = read_agnes_key(project_dir)
        if not key:
            return jsonify({"ok": False, "error": "缺少 Agnes API Key"}), 400
        data = request.get_json(force=True, silent=True) or {}
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"ok": False, "error": "请输入提示词"}), 400
        model = data.get("model") or TEXT_MODEL
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个中文AI应用测试助手。回答要直接、结构清晰。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": float(data.get("temperature") or 0.4),
        }
        t0 = time.time()
        out = http_json("/chat/completions", key, payload=payload, timeout=120)
        elapsed = round(time.time() - t0, 2)
        text = extract_text_completion(out.get("body") or {}) if out.get("ok") else ""
        row = {"time": now(), "type": "text", "model": model, "prompt": prompt, "ok": out.get("ok"), "status": out.get("status"), "elapsed": elapsed, "text": text, "usage": (out.get("body") or {}).get("usage")}
        append_run(runs_path, row)
        return jsonify({"ok": out.get("ok"), "elapsed": elapsed, "text": text, "usage": row["usage"], "raw": out.get("body"), "error": out.get("error", "")}), (200 if out.get("ok") else 502)

    @app.route("/api/agnes/polish-prompt", methods=["POST"])
    def api_agnes_polish_prompt():
        key = read_agnes_key(project_dir)
        if not key:
            return jsonify({"ok": False, "error": "缺少 Agnes API Key"}), 400
        data = request.get_json(force=True, silent=True) or {}
        raw_prompt = (data.get("prompt") or "").strip()
        target = (data.get("target") or "image").strip()
        if not raw_prompt:
            return jsonify({"ok": False, "error": "请输入原始需求/提示词"}), 400
        model = data.get("model") or TEXT_MODEL
        kind = "视频" if target == "video" else "图片"
        polish_mode = (data.get("polish_mode") or "beauty").strip()
        modes = {
            "beauty": {
                "label": "美业专用",
                "sys": "你是美业AI素材提示词导演。美业=中国美容院/美业老板/直销/私域成交/IP孵化，不是欧美beauty化妆品大片。只输出可直接用于生图/生视频的中文提示词，不要解释。",
                "rules": "1. 真实中国本地美业商业场景。\n2. 明确人物、场景、镜头/构图、光线、用途。\n3. 避免欧美风、机器人、赛博朋克、夸张科幻、文字、水印、logo。\n4. 适合朋友圈配图、短视频封面、课程海报或商业素材。\n5. 如果是视频，动作要简单自然：缓慢推进、查看手机/平板、轻微点头，避免复杂走路和多人互动。",
            },
            "business": {
                "label": "通用商业",
                "sys": "你是通用商业视觉提示词导演。保留用户原始主题，不要强行改成美业。只输出可直接用于生图/生视频的中文提示词，不要解释。",
                "rules": "1. 严格保留用户原始主题、行业和对象，不要擅自换题材。\n2. 补充真实商业场景、人物/产品、构图、光线、氛围、用途。\n3. 适合品牌宣传、朋友圈、短视频封面、文章头图或商业素材。\n4. 避免文字、水印、logo、畸形人物、廉价广告感。\n5. 如果是视频，动作要简单自然，镜头运动清晰可执行。",
            },
            "free": {
                "label": "自由创意",
                "sys": "你是自由创意视觉提示词导演。必须保留用户原始题材，不要强行商业化，不要强行美业化。只输出可直接用于生图/生视频的中文提示词，不要解释。",
                "rules": "1. 保留原始主题和创意方向。\n2. 只优化画面细节、主体、背景、构图、光线、镜头、风格。\n3. 可以更有想象力，但不要改变用户想要的对象。\n4. 避免文字、水印、logo、畸形结构。\n5. 如果是视频，动作和镜头要简单可生成。",
            },
            "poster": {
                "label": "产品海报",
                "sys": "你是产品海报视觉提示词导演。保留用户产品/主题，优化成适合商业海报的生成提示词。只输出提示词，不要解释。",
                "rules": "1. 明确产品主体、背景、材质、光线和高级感。\n2. 保留标题/文案留白区域，但不要让模型直接生成中文大字。\n3. 适合电商、课程、活动、品牌宣传海报。\n4. 避免水印、logo、错误文字、廉价促销感。",
            },
            "cover": {
                "label": "短视频封面",
                "sys": "你是短视频封面视觉提示词导演。保留用户主题，优化成一眼看懂、有冲突、有标题留白的封面提示词。只输出提示词，不要解释。",
                "rules": "1. 主体突出，情绪/冲突明确，一眼看懂。\n2. 背景简洁，预留标题区域，但不要直接生成中文文字。\n3. 构图适合短视频封面和竖屏平台。\n4. 避免水印、logo、错误文字、人物畸形。",
            },
        }
        cfg = modes.get(polish_mode, modes["beauty"])
        sys = cfg["sys"]
        user = f"""把下面原始需求润色成一个{kind}生成提示词。润色模式：{cfg['label']}。

要求：
{cfg['rules']}

原始需求：
{raw_prompt}

直接输出润色后的提示词："""
        payload = {"model": model, "messages": [{"role": "system", "content": sys}, {"role": "user", "content": user}], "temperature": float(data.get("temperature") or 0.35)}
        t0 = time.time()
        out = http_json("/chat/completions", key, payload=payload, timeout=120)
        elapsed = round(time.time() - t0, 2)
        polished = extract_text_completion(out.get("body") or {}) if out.get("ok") else ""
        row = {"time": now(), "type": "polish_prompt", "target": target, "polish_mode": polish_mode, "model": model, "prompt": raw_prompt, "ok": out.get("ok"), "status": out.get("status"), "elapsed": elapsed, "polished": polished}
        append_run(runs_path, row)
        return jsonify({"ok": out.get("ok"), "elapsed": elapsed, "polished": polished, "polish_mode": polish_mode, "raw": out.get("body"), "error": out.get("error", "")}), (200 if out.get("ok") else 502)

    @app.route("/api/agnes/image", methods=["POST"])
    def api_agnes_image():
        key = read_agnes_key(project_dir)
        if not key:
            return jsonify({"ok": False, "error": "缺少 Agnes API Key"}), 400
        data = request.get_json(force=True, silent=True) or {}
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"ok": False, "error": "请输入图片提示词"}), 400
        model = data.get("model") or IMAGE_MODEL
        # Agnes 文档未完全验证前，先按 OpenAI images/generations 兼容格式尝试。
        payload = {"model": model, "prompt": prompt, "n": int(data.get("n") or 1), "size": data.get("size") or "1024x1024"}
        t0 = time.time()
        out = http_json("/images/generations", key, payload=payload, timeout=180)
        elapsed = round(time.time() - t0, 2)
        saved = []
        body = out.get("body") or {}
        if out.get("ok"):
            for i, item in enumerate(body.get("data") or []):
                b64 = item.get("b64_json")
                url = item.get("url")
                if b64:
                    import base64
                    fn = safe_name("agnes_img") + f"_{i}.png"
                    atomic_write_bytes(image_root / fn, base64.b64decode(b64))
                    saved.append({"filename": fn, "url": f"/media/agnes/images/{fn}"})
                elif url:
                    try:
                        req = urllib.request.Request(url, headers={"User-Agent": "HermesCockpit/AgnesLab"})
                        with urllib.request.urlopen(req, timeout=90) as rr:
                            raw_img = rr.read()
                        ext = ".png"
                        fn = safe_name("agnes_img") + f"_{i}{ext}"
                        atomic_write_bytes(image_root / fn, raw_img)
                        saved.append({"filename": fn, "url": f"/media/agnes/images/{fn}", "remote_url": url})
                    except StorageQuotaExceeded:
                        raise
                    except Exception:
                        saved.append({"remote_url": url})
        row = {"time": now(), "type": "image", "model": model, "prompt": prompt, "ok": out.get("ok"), "status": out.get("status"), "elapsed": elapsed, "saved": saved, "raw_keys": list(body.keys()) if isinstance(body, dict) else []}
        append_run(runs_path, row)
        return jsonify({"ok": out.get("ok"), "elapsed": elapsed, "saved": saved, "raw": body, "error": out.get("error", "")}), (200 if out.get("ok") else 502)

    @app.route("/api/agnes/upload-image", methods=["POST"])
    def api_agnes_upload_image():
        files = request.files.getlist("files") or []
        if not files:
            f = request.files.get("file")
            files = [f] if f else []
        if not files:
            return jsonify({"ok": False, "error": "请上传图片"}), 400
        pending = []
        for f in files:
            original = f.filename or "image.png"
            ext = Path(original).suffix.lower() or ".png"
            if ext not in ALLOWED_IMAGE_EXTS:
                return jsonify({"ok": False, "error": f"不支持的图片类型：{ext}"}), 400
            safe = secure_filename(original) or f"image{ext}"
            fn = safe_name("agnes_upload") + "_" + safe
            dest = image_root / fn
            content = f.read()
            pending.append((original, fn, dest, content))
        saved = []
        with reserve_capacity(sum(len(item[3]) for item in pending)) as reservation:
            for original, fn, dest, content in pending:
                atomic_write_bytes(dest, content, reservation=reservation)
                local_url = f"/media/agnes/images/{fn}"
                saved.append({
                    "filename": fn,
                    "url": local_url,
                    "public_url": public_url_for_request(local_url),
                    "path": str(dest),
                    "bytes": len(content),
                })
        append_run(runs_path, {"time": now(), "type": "image_upload", "ok": True, "saved": saved})
        return jsonify({"ok": True, "files": saved})

    @app.route("/api/agnes/image-to-video", methods=["POST"])
    def api_agnes_image_to_video():
        key = read_agnes_key(project_dir)
        if not key:
            return jsonify({"ok": False, "error": "缺少 Agnes API Key"}), 400
        data = request.get_json(force=True, silent=True) or {}
        prompt = (data.get("prompt") or "").strip()
        image_url = (data.get("image_url") or "").strip()
        if not prompt:
            return jsonify({"ok": False, "error": "请输入图生视频动作提示词"}), 400
        if not image_url:
            return jsonify({"ok": False, "error": "请先上传首帧图片，或填写图片URL"}), 400
        model = data.get("model") or VIDEO_MODEL
        seconds = str(int(data.get("duration") or data.get("seconds") or 5))
        payload = {"model": model, "prompt": prompt, "seconds": seconds, "size": data.get("size") or "1280x768", "image_url": image_url}
        t0 = time.time()
        out = http_json("/video/generations", key, payload=payload, timeout=90)
        elapsed = round(time.time() - t0, 2)
        body = out.get("body") or {}
        task_id = body.get("task_id") or body.get("id")
        row = {"time": now(), "type": "image_to_video", "model": model, "prompt": prompt, "image_url": image_url, "ok": out.get("ok"), "status": out.get("status"), "elapsed": elapsed, "task_id": task_id, "raw": body}
        append_run(runs_path, row)
        return jsonify({"ok": out.get("ok"), "elapsed": elapsed, "task_id": task_id, "poll_url": f"/api/agnes/video/{task_id}" if task_id else "", "raw": body, "error": out.get("error", "")}), (200 if out.get("ok") else 502)

    @app.route("/api/agnes/video", methods=["POST"])
    def api_agnes_video():
        key = read_agnes_key(project_dir)
        if not key:
            return jsonify({"ok": False, "error": "缺少 Agnes API Key"}), 400
        data = request.get_json(force=True, silent=True) or {}
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"ok": False, "error": "请输入视频提示词"}), 400
        model = data.get("model") or VIDEO_MODEL
        # Agnes 视频创建端点是 /video/generations，返回异步 task_id；用 /videos/<task_id> 查询进度。
        # Agnes 的 Go 后端要求 seconds 是字符串，不能传数字；否则报：json cannot unmarshal number into ... seconds of type string
        seconds = str(int(data.get("duration") or 5))
        payload = {"model": model, "prompt": prompt, "seconds": seconds, "size": data.get("size") or "1280x768"}
        t0 = time.time()
        out = http_json("/video/generations", key, payload=payload, timeout=60)
        elapsed = round(time.time() - t0, 2)
        body = out.get("body") or {}
        task_id = body.get("task_id") or body.get("id")
        row = {"time": now(), "type": "video", "model": model, "prompt": prompt, "ok": out.get("ok"), "status": out.get("status"), "elapsed": elapsed, "task_id": task_id, "raw": body}
        append_run(runs_path, row)
        return jsonify({"ok": out.get("ok"), "elapsed": elapsed, "task_id": task_id, "poll_url": f"/api/agnes/video/{task_id}" if task_id else "", "raw": body, "error": out.get("error", "")}), (200 if out.get("ok") else 502)

    @app.route("/api/agnes/video/<task_id>")
    def api_agnes_video_status(task_id):
        key = read_agnes_key(project_dir)
        if not key:
            return jsonify({"ok": False, "error": "缺少 Agnes API Key"}), 400
        t0 = time.time()
        out = http_json(f"/videos/{task_id}", key, timeout=60)
        elapsed = round(time.time() - t0, 2)
        body = out.get("body") or {}
        saved = None
        download_error = ""
        if out.get("ok") and body.get("status") == "completed":
            try:
                saved = download_video_if_ready(body, task_id, video_root)
            except StorageQuotaExceeded:
                return jsonify({
                    "ok": False,
                    "error": "Hermes storage quota exceeded",
                }), 507
            except Exception as e:
                download_error = str(e)
        append_run(runs_path, {"time": now(), "type": "video_status", "ok": out.get("ok"), "status": out.get("status"), "elapsed": elapsed, "task_id": task_id, "video_status": body.get("status"), "progress": body.get("progress"), "saved": saved, "download_error": download_error})
        return jsonify({"ok": out.get("ok"), "elapsed": elapsed, "task_id": task_id, "video_status": body.get("status"), "progress": body.get("progress"), "saved": saved, "download_error": download_error, "raw": body, "error": out.get("error", "")}), (200 if out.get("ok") else 502)

    @app.route("/api/agnes/runs")
    def api_agnes_runs():
        items = []
        if runs_path.exists():
            lines = runs_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-80:]
            for line in lines:
                try:
                    items.append(json.loads(line))
                except Exception:
                    pass
        return jsonify({"ok": True, "items": list(reversed(items)), "path": str(runs_path)})
