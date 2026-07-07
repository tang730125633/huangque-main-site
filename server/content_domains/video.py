# -*- coding: utf-8 -*-
from .core import (
    AUDIO_OUT_DIR, HEYGEN_API_BASE, HEYGEN_API_KEY, HEYGEN_POLL_INTERVAL,
    HEYGEN_TIMEOUT, VIDEO_OUT_DIR, _file_url, _out_path, _resolve_out_file,
    adb, base64, closing, jdb, json, mimetypes, os, pathlib, public_url,
    re, subprocess, threading, time, urllib, uuid,
)

from .audio import gen_audio

VALID_VIDEO_MODES = {"text", "audio", "motion"}
VALID_VIDEO_RATIOS = {"9:16", "16:9", "1:1", "4:5", "5:4"}
VALID_VIDEO_RESOLUTIONS = {"720p", "1080p", "4k"}
VALID_VIDEO_MOTIONS = {"low", "medium", "high"}
VALID_IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
VALID_AUDIO_MIMES = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/mp4", "audio/m4a", "audio/x-m4a"}
VALID_REFERENCE_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm"}

# 统一视频生成 API（xiaolevideo.cn）：果肉=Grok Video、微衣=Seedance 2.0
XIAOLEVIDEO_API_KEY = os.environ.get("XIAOLEVIDEO_API_KEY", "")
XIAOLEVIDEO_API_BASE = os.environ.get("XIAOLEVIDEO_API_BASE", "https://api.xiaolevideo.cn").rstrip("/")
XIAOLE_MAX_WAIT = int(os.environ.get("XIAOLEVIDEO_TIMEOUT", "600"))
XIAOLE_POLL_INTERVAL = int(os.environ.get("XIAOLEVIDEO_POLL_INTERVAL", "5"))
_xiaole_429_retries = int(os.environ.get("XIAOLEVIDEO_429_RETRIES", "5"))   # 并发限流(429)退避重试次数
_xiaole_dl_retries = int(os.environ.get("XIAOLEVIDEO_DL_RETRIES", "3"))     # 下载中断重试次数
# 页面渠道 → 模型 id（前端传 channel，后端定 model，避免任意模型注入）
XIAOLE_CHANNEL_MODELS = {
    "grok": "Grok Image Video",   # 果肉视频（Grok Video 1.0：文生/图生视频）
    "micro": "seedance-2.0-fast", # 微衣视频（Seedance 2.0 Fast：文生视频；高配seedance-2.0账户额度不足）
}
XIAOLE_IMAGE_CHANNELS = {"grok"}  # 支持参考图（图生视频）的渠道

def _is_valid_data_url(value, allowed_mimes):
    raw = (value or "").strip()
    if not raw.startswith("data:") or "," not in raw:
        return False
    meta, encoded = raw.split(",", 1)
    if ";base64" not in meta.lower():
        return False
    mime = meta.split(";", 1)[0].replace("data:", "", 1).lower()
    if mime not in allowed_mimes:
        return False
    try:
        base64.b64decode(encoded, validate=True)
        return True
    except Exception:
        return False

def validate_video_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求体不是合法 JSON")
    mode = (payload.get("mode") or "text").strip().lower()
    if mode not in VALID_VIDEO_MODES:
        raise ValueError("mode 仅支持 text/audio/motion")

    image_data = (payload.get("image_data") or "").strip()
    avatar_id = str(payload.get("avatar_id") or "").strip()
    if not image_data and not (mode == "motion" and avatar_id):
        raise ValueError("image_data 不能为空")
    if image_data and not _is_valid_data_url(image_data, VALID_IMAGE_MIMES):
        raise ValueError("image_data 不是有效的人物形象图片")

    if mode == "text":
        if not (payload.get("voice") or "").strip():
            raise ValueError("mode=text 时 voice 必填")
    elif mode == "audio":
        audio_data = (payload.get("audio_data") or "").strip()
        if not audio_data:
            raise ValueError("audio_data 不能为空")
        if not _is_valid_data_url(audio_data, VALID_AUDIO_MIMES):
            raise ValueError("audio_data 不是有效的音频文件")
    elif mode == "motion":
        reference_video_data = (payload.get("reference_video_data") or "").strip()
        if not reference_video_data:
            raise ValueError("reference_video_data 不能为空")
        if not _is_valid_data_url(reference_video_data, VALID_REFERENCE_VIDEO_MIMES):
            raise ValueError("reference_video_data 不是有效的参考动作视频")

    ratio = (payload.get("ratio") or "9:16").strip()
    if ratio not in VALID_VIDEO_RATIOS:
        raise ValueError("ratio 仅支持 9:16、16:9、1:1、4:5、5:4")
    resolution = (payload.get("resolution") or "1080p").strip().lower()
    if resolution not in VALID_VIDEO_RESOLUTIONS:
        raise ValueError("resolution 仅支持 720p、1080p、4k")
    motion = (payload.get("motion") or "medium").strip().lower()
    if motion not in VALID_VIDEO_MOTIONS:
        raise ValueError("motion 仅支持 low、medium、high")

    cleaned = dict(payload)
    cleaned["mode"] = mode
    cleaned["ratio"] = ratio
    cleaned["resolution"] = resolution
    cleaned["motion"] = motion
    return cleaned

def record_video_asset(job_id, username, result):
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("""INSERT INTO video_assets
            (job_id, username, mode, image_file, audio_file, reference_video_file, video_file, video_url, text, voice_key,
             resolution, ratio, motion, phase, image_asset_id, audio_asset_id, reference_asset_id, provider_video_id,
             provider_avatar_id, provider_avatar_group_id, source_video_url, status, error, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                mode=COALESCE(excluded.mode, video_assets.mode),
                image_file=COALESCE(excluded.image_file, video_assets.image_file),
                audio_file=COALESCE(excluded.audio_file, video_assets.audio_file),
                reference_video_file=COALESCE(excluded.reference_video_file, video_assets.reference_video_file),
                video_file=COALESCE(excluded.video_file, video_assets.video_file),
                video_url=COALESCE(excluded.video_url, video_assets.video_url),
                text=COALESCE(excluded.text, video_assets.text),
                voice_key=COALESCE(excluded.voice_key, video_assets.voice_key),
                resolution=COALESCE(excluded.resolution, video_assets.resolution),
                ratio=COALESCE(excluded.ratio, video_assets.ratio),
                motion=COALESCE(excluded.motion, video_assets.motion),
                phase=COALESCE(excluded.phase, video_assets.phase),
                image_asset_id=COALESCE(excluded.image_asset_id, video_assets.image_asset_id),
                audio_asset_id=COALESCE(excluded.audio_asset_id, video_assets.audio_asset_id),
                reference_asset_id=COALESCE(excluded.reference_asset_id, video_assets.reference_asset_id),
                provider_video_id=COALESCE(excluded.provider_video_id, video_assets.provider_video_id),
                provider_avatar_id=COALESCE(excluded.provider_avatar_id, video_assets.provider_avatar_id),
                provider_avatar_group_id=COALESCE(excluded.provider_avatar_group_id, video_assets.provider_avatar_group_id),
                source_video_url=COALESCE(excluded.source_video_url, video_assets.source_video_url),
                status=COALESCE(excluded.status, video_assets.status),
                error=excluded.error,
                updated_at=excluded.updated_at""",
            (job_id, username, result.get("mode"), result.get("image_file"), result.get("audio_file"),
             result.get("reference_video_file"), result.get("video_file"), result.get("video_url"), result.get("text"), result.get("voice"),
             result.get("resolution"), result.get("ratio"), result.get("motion"), result.get("phase"),
             result.get("image_asset_id"), result.get("audio_asset_id"), result.get("reference_asset_id"),
             result.get("provider_video_id") or result.get("video_id"), result.get("provider_avatar_id") or result.get("avatar_item_id"),
             result.get("provider_avatar_group_id") or result.get("avatar_group_id"), result.get("source_video_url"),
             result.get("status") or "pending", result.get("error"), now, now))
        c.commit()

def update_video_asset_phase(job_id, phase, **fields):
    if not job_id:
        return
    now = int(time.time())
    allowed = {
        "mode", "image_file", "audio_file", "reference_video_file", "video_file", "video_url",
        "text", "voice_key", "resolution", "ratio", "motion", "image_asset_id",
        "audio_asset_id", "reference_asset_id", "provider_video_id", "provider_avatar_id",
        "provider_avatar_group_id", "source_video_url", "status", "error"
    }
    if "voice" in fields and "voice_key" not in fields:
        fields["voice_key"] = fields.pop("voice")
    updates = {"phase": phase, "status": fields.pop("status", "running")}
    if "error" in fields:
        updates["error"] = fields.pop("error")
    for k, v in fields.items():
        if k in allowed and v is not None:
            updates[k] = v
    sets = ", ".join("%s=?" % k for k in updates)
    vals = list(updates.values()) + [now, job_id]
    try:
        with closing(adb()) as c:
            c.execute("UPDATE video_assets SET %s, updated_at=? WHERE job_id=?" % sets, vals)
            c.commit()
    except Exception:
        pass
    try:
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET updated_at=? WHERE id=? AND status='running'", (now, job_id))
            c.commit()
    except Exception:
        pass

def record_video_pending_asset(job_id, username, payload):
    # 换装/换背景(tryon)与常规视频共用 video_assets 表；tryon 没有 mode/voice 等字段，兜底为空即可
    is_tryon = bool(payload.get("person_video_data") or payload.get("clothes_data") or payload.get("background_data"))
    record_video_asset(job_id, username, {
        "mode": ("tryon" if is_tryon else (payload.get("mode") or "text")),
        "text": payload.get("text") or "",
        "voice": payload.get("voice") or "",
        "resolution": payload.get("resolution") or "1080p",
        "ratio": payload.get("ratio") or "9:16",
        "motion": payload.get("motion") or "medium",
        "phase": "queued",
        "status": "running",
    })

def list_video_assets(username, limit=120):
    limit = max(1, min(120, int(limit or 120)))
    with closing(adb()) as c:
        rows = c.execute("""SELECT id, job_id, username, mode, image_file, audio_file, reference_video_file, video_file, video_url,
                   text, voice_key, resolution, ratio, motion, phase, image_asset_id, audio_asset_id, reference_asset_id,
                   provider_video_id, provider_avatar_id, provider_avatar_group_id, source_video_url,
                   status, error, created_at, updated_at
            FROM video_assets
            WHERE username=? AND status!='deleted'
            ORDER BY id DESC LIMIT ?""", (username, limit)).fetchall()
    items = [dict(r) for r in rows]
    try:
        from . import cos
        if cos.enabled():
            for item in items:
                if item.get("video_file") and str(item.get("video_url") or "").startswith("http"):
                    item["video_url"] = cos.object_url(item["video_file"], private=True)
    except Exception as e:
        print("[video-assets] COS 签名刷新失败: %s" % e, flush=True)
    return items

def get_video_job_phase(job_id):
    try:
        with closing(adb()) as c:
            row = c.execute("SELECT phase FROM video_assets WHERE job_id=?", (job_id,)).fetchone()
        return row["phase"] if row else None
    except Exception:
        return None

def _avatar_display_name(username):
    with closing(adb()) as c:
        row = c.execute("SELECT COUNT(*) AS n FROM avatars WHERE username=?", (username,)).fetchone()
    return "形象 %d" % ((row["n"] if row else 0) + 1)

def record_video_avatar(username, image_file, provider_avatar_id, provider_avatar_group_id=None, name=None):
    username = (username or "").strip()
    provider_avatar_id = (provider_avatar_id or "").strip()
    image_file = (image_file or "").strip()
    if not username or not provider_avatar_id or not image_file:
        return None
    now = int(time.time())
    name = (name or _avatar_display_name(username)).strip()[:40] or _avatar_display_name(username)
    with closing(adb()) as c:
        c.execute("""INSERT INTO avatars
            (username, name, image_file, provider_avatar_id, provider_avatar_group_id, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(username, provider_avatar_id) DO UPDATE SET
                image_file=COALESCE(excluded.image_file, avatars.image_file),
                provider_avatar_group_id=COALESCE(excluded.provider_avatar_group_id, avatars.provider_avatar_group_id),
                status=COALESCE(excluded.status, avatars.status),
                updated_at=excluded.updated_at""",
            (username, name, image_file, provider_avatar_id, provider_avatar_group_id, "ready", now, now))
        c.commit()
        row = c.execute("""SELECT id, username, name, image_file, provider_avatar_id, provider_avatar_group_id,
                   status, created_at, updated_at
            FROM avatars WHERE username=? AND provider_avatar_id=?""", (username, provider_avatar_id)).fetchone()
    return dict(row) if row else None

def list_video_avatars(username, limit=120):
    limit = max(1, min(120, int(limit or 120)))
    with closing(adb()) as c:
        rows = c.execute("""SELECT id, username, name, image_file, provider_avatar_id, provider_avatar_group_id,
                   status, created_at, updated_at
            FROM avatars WHERE username=? AND status!='deleted' ORDER BY id DESC LIMIT ?""", (username, limit)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["image_url"] = _file_url(d["image_file"]) if d.get("image_file") else None
        items.append(d)
    return items

def get_video_avatar(username, avatar_id):
    try:
        avatar_id = int(avatar_id)
    except Exception:
        raise ValueError("形象不存在")
    with closing(adb()) as c:
        row = c.execute("""SELECT id, username, name, image_file, provider_avatar_id, provider_avatar_group_id,
                   status, created_at, updated_at
            FROM avatars WHERE id=? AND username=? AND status!='deleted'""", (avatar_id, username)).fetchone()
    if not row:
        raise ValueError("形象不存在")
    return dict(row)

def rename_video_avatar(username, avatar_id, name):
    avatar = get_video_avatar(username, avatar_id)
    name = (name or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    name = name[:40]
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("UPDATE avatars SET name=?, updated_at=? WHERE id=? AND username=?",
                  (name, now, avatar["id"], username))
        c.commit()
    avatar["name"] = name
    avatar["updated_at"] = now
    avatar["image_url"] = _file_url(avatar["image_file"]) if avatar.get("image_file") else None
    return avatar

def delete_video_avatar(username, avatar_id):
    avatar = get_video_avatar(username, avatar_id)
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("UPDATE avatars SET status='deleted', updated_at=? WHERE id=? AND username=?",
                  (now, avatar["id"], username))
        c.commit()
    return {"id": avatar["id"], "status": "deleted"}

def _save_data_file(data_url, prefix, allowed_ext):
    raw = (data_url or "").strip()
    if not raw:
        return None
    if "," in raw and raw.lower().startswith("data:"):
        meta, raw = raw.split(",", 1)
        mime = meta.split(";", 1)[0].replace("data:", "").lower()
    else:
        mime = ""
    ext = ""
    for k, v in {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
        "audio/x-wav": ".wav", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"
    }.items():
        if mime == k:
            ext = v
            break
    if not ext:
        ext = allowed_ext[0]
    if ext not in allowed_ext:
        raise ValueError("不支持的文件格式")
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        raise ValueError("文件内容解析失败")
    max_size = (250 if ext in {".mp4", ".mov", ".webm"} else 35) * 1024 * 1024
    if len(data) > max_size:
        raise ValueError("文件过大，请压缩后再上传")
    folder = "audio/" if ext in {".mp3", ".wav", ".m4a"} else ("video/" if ext in {".mp4", ".mov", ".webm"} else "")
    fn = "%s%s_%s%s" % (folder, prefix, uuid.uuid4().hex, ext)  # 不可猜键(#185)：上传的真人素材防猜测
    _out_path(fn).write_bytes(data)
    return fn

def _heygen_relay_token():
    return os.environ.get("HEYGEN_RELAY_TOKEN", "").strip()

def _heygen_request_json(method, path, body=None, headers=None, timeout=180):
    if not HEYGEN_API_KEY:
        raise ValueError("视频生成服务未配置")
    h = {"x-api-key": HEYGEN_API_KEY}
    if _heygen_relay_token():
        h["X-Relay-Token"] = _heygen_relay_token()
    if headers:
        h.update(headers)
    req = urllib.request.Request(HEYGEN_API_BASE + path, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").replace("\n", " ")[:600]
        print("[heygen] FAIL %s %s -> HTTP %s %s" % (method, path, e.code, detail), flush=True)
        raise RuntimeError("HeyGen接口失败: HTTP %s %s" % (e.code, detail)) from e
    except urllib.error.URLError as e:
        detail = str(e.reason)[:300]
        print("[heygen] FAIL %s %s -> network %s" % (method, path, detail), flush=True)
        raise RuntimeError("HeyGen接口网络失败: %s" % detail) from e
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("HeyGen返回解析失败: %s" % raw[:300].decode("utf-8", "replace"))

def _heygen_upload_asset(file_path):
    path = pathlib.Path(file_path)
    if not path.is_file():
        raise ValueError("视频素材文件不存在")
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    boundary = "----huangque-heygen-%d" % int(time.time() * 1000)
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="file"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n"
    ) % (boundary, path.name.replace('"', ''), mime)
    body = head.encode() + path.read_bytes() + ("\r\n--%s--\r\n" % boundary).encode()
    data = _heygen_request_json("POST", "/assets", body, {
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "Content-Length": str(len(body)),
    }, timeout=240)
    asset_id = ((data.get("data") or {}).get("asset_id") or "").strip()
    if not asset_id:
        raise RuntimeError("HeyGen素材上传未返回asset_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return asset_id

def _ensure_heygen_audio_mp3(audio_path):
    path = pathlib.Path(audio_path)
    if path.suffix.lower() == ".mp3":
        return path
    out = AUDIO_OUT_DIR / ("heygen_audio_%d.mp3" % int(time.time() * 1000))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-vn", "-acodec", "libmp3lame", "-ar", "24000", "-ac", "1", "-b:a", "128k",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=180, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise ValueError("服务器未安装 ffmpeg，无法转换上传音频格式")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode("utf-8", "replace")[:220]
        raise ValueError("音频格式转换失败，请重新上传 mp3 音频" + (": " + detail if detail else ""))
    except subprocess.TimeoutExpired:
        raise ValueError("音频格式转换超时，请重新上传更短的 mp3 音频")
    if not out.exists() or out.stat().st_size <= 0:
        raise ValueError("音频格式转换失败，请重新上传 mp3 音频")
    return out

HEYGEN_IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

def _ensure_heygen_image_jpg(image_path):
    # HeyGen 素材接口只收 jpg/png；webp 等格式原样上传必然 400（invalid_parameter）
    path = pathlib.Path(image_path)
    if path.suffix.lower() in HEYGEN_IMAGE_EXTS:
        return path
    out = path.parent / ("heygen_img_%d.jpg" % int(time.time() * 1000))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-frames:v", "1", "-q:v", "2",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise ValueError("服务器未安装 ffmpeg，无法转换图片格式")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode("utf-8", "replace")[:220]
        raise ValueError("图片格式转换失败，请上传 jpg/png 格式的人物形象图" + (": " + detail if detail else ""))
    except subprocess.TimeoutExpired:
        raise ValueError("图片格式转换超时，请上传 jpg/png 格式的人物形象图")
    if not out.exists() or out.stat().st_size <= 0:
        raise ValueError("图片格式转换失败，请上传 jpg/png 格式的人物形象图")
    return out

def _heygen_create_video(image_asset_id, audio_asset_id, resolution, ratio, motion):
    title = "huangque video %d" % int(time.time())
    body = json.dumps({
        "title": title,
        "type": "image",
        "image": {"type": "asset_id", "asset_id": image_asset_id},
        "audio_asset_id": audio_asset_id,
        "resolution": resolution,
        "aspect_ratio": ratio,
        "fit": "cover",
        "expressiveness": motion,
        "output_format": "mp4",
    }, ensure_ascii=False).encode()
    data = _heygen_request_json("POST", "/videos", body, {
        "Content-Type": "application/json",
    }, timeout=90)
    video_id = ((data.get("data") or {}).get("video_id") or "").strip()
    if not video_id:
        raise RuntimeError("HeyGen未返回video_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return video_id

def _find_nested_dict(obj, pred):
    if isinstance(obj, dict):
        if pred(obj):
            return obj
        for v in obj.values():
            got = _find_nested_dict(v, pred)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_nested_dict(v, pred)
            if got:
                return got
    return None

def _heygen_create_photo_avatar(image_asset_id):
    body = json.dumps({
        "type": "photo",
        "name": "huangque_photo_avatar_%d" % int(time.time()),
        "file": {"type": "asset_id", "asset_id": image_asset_id},
    }, ensure_ascii=False).encode()
    data = _heygen_request_json("POST", "/avatars", body, {
        "Content-Type": "application/json",
    }, timeout=90)
    root = data.get("data") or {}
    avatar_item_id = (((root.get("avatar_item") or {}).get("id")) or "").strip()
    avatar_group_id = (((root.get("avatar_group") or {}).get("id")) or "").strip()
    if not avatar_item_id:
        raise RuntimeError("HeyGen未返回avatar_item_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return avatar_item_id, avatar_group_id

def _avatar_ready_from_payload(data, avatar_item_id, avatar_group_id=""):
    def is_avatar(d):
        current_id = str(d.get("id") or "")
        preview_url = str(d.get("preview_image_url") or "")
        return (
            current_id == avatar_item_id
            or bool(avatar_group_id and current_id == avatar_group_id)
            or bool(avatar_item_id and avatar_item_id in preview_url)
        )
    item = _find_nested_dict(data, is_avatar)
    if not item:
        return False
    status = str(item.get("status") or item.get("state") or "").lower()
    return bool(item.get("preview_image_url") or status in {"completed", "ready", "success"})

def _heygen_wait_photo_avatar(avatar_item_id, avatar_group_id=""):
    deadline = time.time() + min(HEYGEN_TIMEOUT, 900)
    last_status = ""
    while time.time() < deadline:
        payloads = []
        errors = []
        if avatar_group_id:
            try:
                payloads.append(_heygen_request_json("GET", "/avatars/" + urllib.parse.quote(avatar_group_id), timeout=20))
            except Exception as e:
                errors.append(e)
                last_status = str(e)[:120]
        try:
            payloads.append(_heygen_request_json("GET", "/avatars", timeout=20))
        except Exception as e:
            errors.append(e)
            last_status = str(e)[:120]
        if not payloads and errors:
            raise errors[-1]
        for data in payloads:
            if _avatar_ready_from_payload(data, avatar_item_id, avatar_group_id):
                return True
            item = _find_nested_dict(data, lambda d: str(d.get("id") or "") in {avatar_item_id, avatar_group_id})
            if item:
                status = str(item.get("status") or item.get("state") or "processing")
                if status != last_status:
                    print("[heygen] avatar_id=%s status=%s" % (avatar_item_id, status), flush=True)
                    last_status = status
        time.sleep(HEYGEN_POLL_INTERVAL)
    raise TimeoutError("HeyGen Photo Avatar处理超时")

def _heygen_create_cinematic_video(avatar_item_id, reference_asset_id, ratio, resolution, duration):
    prompt = (
        "Create a realistic cinematic vertical video of the same person from the avatar photo. "
        "Follow the uploaded reference video closely for body movement, pose, timing, gestures, "
        "facial expression, framing and camera motion. Keep the person's identity, face, hairstyle, "
        "body proportions and outfit consistent. Smooth realistic motion, no text, no logo, no extra people."
    )
    body = json.dumps({
        "type": "cinematic_avatar",
        "title": "follow_reference_motion",
        "prompt": prompt,
        "avatar_id": [avatar_item_id],
        "references": [{"type": "asset_id", "asset_id": reference_asset_id}],
        "aspect_ratio": ratio,
        "resolution": resolution,
        "duration": duration,
        "enhance_prompt": False,
    }, ensure_ascii=False).encode()
    data = _heygen_request_json("POST", "/videos", body, {
        "Content-Type": "application/json",
    }, timeout=90)
    video_id = ((data.get("data") or {}).get("video_id") or "").strip()
    if not video_id:
        raise RuntimeError("HeyGen未返回video_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return video_id

def _heygen_poll_video(video_id):
    deadline = time.time() + HEYGEN_TIMEOUT
    last_status = ""
    while time.time() < deadline:
        data = _heygen_request_json("GET", "/videos/" + urllib.parse.quote(video_id), timeout=90)
        info = data.get("data") or {}
        status = str(info.get("status") or "").lower()
        if status != last_status:
            print("[heygen] video_id=%s status=%s" % (video_id, status), flush=True)
            last_status = status
        if status == "completed":
            if not info.get("video_url"):
                raise RuntimeError("HeyGen完成但未返回video_url")
            return info
        if status in {"failed", "error"}:
            detail = json.dumps(info, ensure_ascii=False)[:500]
            print("[heygen] FAIL GET /videos/%s -> provider %s" % (video_id, detail), flush=True)
            raise RuntimeError("HeyGen视频生成失败: %s" % detail)
        time.sleep(HEYGEN_POLL_INTERVAL)
    raise TimeoutError("HeyGen视频生成超时")

def _download_video_file(url, prefix="vid"):
    headers = {"User-Agent": "huangque-content/1.0"}
    relay = os.environ.get("HEYGEN_RELAY_BASE", "").strip().rstrip("/")
    if relay:
        # 出境中转：HeyGen 成片/素材 CDN 域名改走法兰克福反代，绕开代理链路
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if host.endswith(".heygen.ai") or host.endswith(".heygen.com"):
            url = "%s/cdn/%s/%s" % (relay, host, parts.path.lstrip("/"))
            if parts.query:
                url += "?" + parts.query
            if _heygen_relay_token():
                headers["X-Relay-Token"] = _heygen_relay_token()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=360) as r:
        data = r.read()
    if not data:
        raise RuntimeError("视频下载失败")
    fn = "video/%s_%s.mp4" % (prefix, uuid.uuid4().hex)  # 不可猜键(#185)：真人视频防猜测枚举
    _out_path(fn).write_bytes(data)
    return fn

def generate_heygen_video(image_file, audio_file, resolution, ratio, motion):
    image_fp = _resolve_out_file(image_file)
    audio_fp = _resolve_out_file(audio_file)
    if not image_fp or not audio_fp:
        raise ValueError("视频素材文件不存在")
    image_fp = _ensure_heygen_image_jpg(image_fp)
    audio_fp = _ensure_heygen_audio_mp3(audio_fp)
    image_asset_id = _heygen_upload_asset(image_fp)
    audio_asset_id = _heygen_upload_asset(audio_fp)
    video_id = _heygen_create_video(image_asset_id, audio_asset_id, resolution, ratio, motion)
    info = _heygen_poll_video(video_id)
    video_file = _download_video_file(info["video_url"], "heygen")
    return {
        "video_id": video_id,
        "image_asset_id": image_asset_id,
        "audio_asset_id": audio_asset_id,
        "video_file": video_file,
        "video_url": _file_url(video_file),
        "source_video_url": info.get("video_url"),
        "thumbnail_url": info.get("thumbnail_url"),
        "duration": info.get("duration"),
    }

def generate_heygen_motion_video(image_file, reference_video_file, resolution, ratio, duration, job_id=None, avatar=None):
    image_fp = _resolve_out_file(image_file)
    reference_fp = _resolve_out_file(reference_video_file)
    if not image_fp or not reference_fp:
        raise ValueError("动作模仿素材文件不存在")
    image_fp = _ensure_heygen_image_jpg(image_fp)
    image_asset_id = None
    avatar_item_id = ""
    avatar_group_id = ""
    if avatar:
        avatar_item_id = (avatar.get("provider_avatar_id") or "").strip()
        avatar_group_id = (avatar.get("provider_avatar_group_id") or "").strip()
        if not avatar_item_id:
            raise ValueError("avatar provider id missing")
        update_video_asset_phase(job_id, "reusing_photo_avatar", provider_avatar_id=avatar_item_id,
                                 provider_avatar_group_id=avatar_group_id)
    else:
        update_video_asset_phase(job_id, "uploading_image_asset")
        image_asset_id = _heygen_upload_asset(image_fp)
    update_video_asset_phase(job_id, "uploading_reference_asset", image_asset_id=image_asset_id,
                             provider_avatar_id=avatar_item_id or None,
                             provider_avatar_group_id=avatar_group_id or None)
    reference_asset_id = _heygen_upload_asset(reference_fp)
    if not avatar_item_id:
        update_video_asset_phase(job_id, "creating_photo_avatar", image_asset_id=image_asset_id,
                                 reference_asset_id=reference_asset_id)
        avatar_item_id, avatar_group_id = _heygen_create_photo_avatar(image_asset_id)
        update_video_asset_phase(job_id, "waiting_photo_avatar", image_asset_id=image_asset_id,
                                 reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                                 provider_avatar_group_id=avatar_group_id)
        _heygen_wait_photo_avatar(avatar_item_id, avatar_group_id)
    update_video_asset_phase(job_id, "creating_cinematic_video", image_asset_id=image_asset_id,
                             reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                             provider_avatar_group_id=avatar_group_id)
    video_id = None
    last_create_error = None
    rebuilt_avatar = False
    for attempt in range(1, 7):
        try:
            video_id = _heygen_create_cinematic_video(avatar_item_id, reference_asset_id, ratio, resolution, duration)
            break
        except RuntimeError as e:
            last_create_error = str(e)
            lowered = last_create_error.lower()
            invalid_avatar = avatar and (not rebuilt_avatar) and "avatar" in lowered and (
                "not found" in lowered or "does not exist" in lowered or "invalid" in lowered
            )
            if invalid_avatar:
                update_video_asset_phase(job_id, "rebuilding_photo_avatar", image_asset_id=image_asset_id,
                                         reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                                         provider_avatar_group_id=avatar_group_id,
                                         error=last_create_error[:180])
                if not image_asset_id:
                    image_asset_id = _heygen_upload_asset(image_fp)
                avatar_item_id, avatar_group_id = _heygen_create_photo_avatar(image_asset_id)
                if avatar.get("username"):
                    record_video_avatar(avatar.get("username"), image_file, avatar_item_id, avatar_group_id, avatar.get("name"))
                update_video_asset_phase(job_id, "waiting_rebuilt_photo_avatar", image_asset_id=image_asset_id,
                                         reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                                         provider_avatar_group_id=avatar_group_id)
                _heygen_wait_photo_avatar(avatar_item_id, avatar_group_id)
                rebuilt_avatar = True
                continue
            retryable = "not ready" in lowered or "status: pending" in lowered
            if not retryable or attempt >= 6:
                raise
            update_video_asset_phase(job_id, "waiting_avatar_look", image_asset_id=image_asset_id,
                                     reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                                     provider_avatar_group_id=avatar_group_id,
                                     error=("avatar look pending, retry %d/6" % attempt))
            time.sleep(20)
    if not video_id:
        raise RuntimeError(last_create_error or "HeyGen未返回video_id")
    update_video_asset_phase(job_id, "polling_video", image_asset_id=image_asset_id,
                             reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                             provider_avatar_group_id=avatar_group_id, provider_video_id=video_id)
    info = _heygen_poll_video(video_id)
    update_video_asset_phase(job_id, "downloading_video", provider_video_id=video_id,
                             source_video_url=info.get("video_url"))
    video_file = _download_video_file(info["video_url"], "cinematic")
    return {
        "video_id": video_id,
        "image_asset_id": image_asset_id,
        "reference_asset_id": reference_asset_id,
        "avatar_item_id": avatar_item_id,
        "avatar_group_id": avatar_group_id,
        "video_file": video_file,
        "video_url": _file_url(video_file),
        "source_video_url": info.get("video_url"),
        "thumbnail_url": info.get("thumbnail_url"),
        "duration": info.get("duration") or duration,
    }

# ============ F4 · 口播视频自动字幕（whisper 时间轴 + libass 烧录） ============
# 仅 text/audio 口播模式生效；motion 动作模仿不做字幕（多无语音，价值低）。
# whisper 吃 CPU，用信号量把同时转写数限到 WHISPER_MAX_CONCURRENCY（默认 1），避免打满核。
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
_whisper_sem = threading.BoundedSemaphore(max(1, int(os.environ.get("WHISPER_MAX_CONCURRENCY", "1") or "1")))
_whisper_model = None
_whisper_model_lock = threading.Lock()
SUBTITLE_FONT = os.environ.get("SUBTITLE_FONT", "Noto Sans SC")  # 服务器已装，libass 可用
# 三个预设样式；数值是相对视频高度的比例。ASS 颜色为 &HAABBGGRR。
_SUB_STYLES = {
    "white":   {"fs": 0.052, "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H00000000", "border": 1, "ow": 3.0, "shadow": 1, "mv": 0.060},
    "variety": {"fs": 0.066, "primary": "&H0000E5FF", "outline": "&H00202020", "back": "&H00000000", "border": 1, "ow": 4.0, "shadow": 1, "mv": 0.072},
    "bar":     {"fs": 0.050, "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H80101010", "border": 3, "ow": 8.0, "shadow": 0, "mv": 0.050},
}

def _sub_ffmpeg(cmd, timeout, cwd=None):
    try:
        subprocess.run(cmd, check=True, timeout=timeout, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise ValueError("服务器未安装 ffmpeg，无法烧录字幕")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode("utf-8", "replace")[-220:]
        raise ValueError("字幕处理失败" + (": " + detail if detail else ""))
    except subprocess.TimeoutExpired:
        raise ValueError("字幕处理超时")

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_model_lock:
            if _whisper_model is None:
                # whisper 用本地缓存模型、无需联网；但服务继承了全局 SOCKS 代理(ALL_PROXY)，
                # huggingface_hub 的 httpx 会因缺 socksio 而报错。加载期间临时清代理即可
                # （一次性 + 已加锁，窗口极小；模型走缓存不发请求）。
                _proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                               "http_proxy", "https_proxy", "all_proxy")
                _saved = {k: os.environ.pop(k) for k in _proxy_keys if k in os.environ}
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                try:
                    from faster_whisper import WhisperModel  # 服务器已装；本地/CI 不触发 import
                    _whisper_model = WhisperModel(WHISPER_MODEL_NAME, device="cpu", compute_type="int8")
                finally:
                    os.environ.update(_saved)
    return _whisper_model

def _probe_video_size(fp):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=s=x:p=0", str(fp)],
            check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).stdout.decode("utf-8", "replace").strip()
        w, h = out.split("x")[:2]
        return max(16, int(w)), max(16, int(h))
    except Exception:
        return 1080, 1920  # 兜底按 9:16 竖屏

def _ass_time(sec):
    cs = max(0, int(round(float(sec) * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return "%d:%02d:%02d.%02d" % (h, m, s, cs)

def _ass_escape(t):
    t = (t or "").replace("\\", "\\\\").replace("{", "(").replace("}", ")")  # 防 ASS 覆盖块注入
    return t.replace("\r", " ").replace("\n", "\\N").strip()

def _wrap_cn(text, max_chars):
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    lines, cur = [], text
    while len(cur) > max_chars and len(lines) < 2:
        cut = cur.rfind(" ", 0, max_chars + 1)   # 停顿已转空格，优先在空格处断
        if cut < max_chars * 0.5:
            cut = max_chars
        lines.append(cur[:cut].strip())
        cur = cur[cut:].strip()
    lines.append(cur)
    return "\\N".join(l for l in lines if l)


# 字幕文本清洗 + 短卡片切分（短视频风格：不显示句末标点、停顿转空格、单卡不过长）
_SENT_PUNCT = "。.!！?？,，、;；:：…"

def _clean_sub_text(t):
    t = (t or "").strip()
    t = re.sub(r"[。.!！?？…]+", "", t)      # 去句末标点（短视频不显示）
    t = re.sub(r"[，,、;；:：]+", " ", t)      # 停顿标点 → 空格
    t = re.sub(r"\s+", " ", t)
    return t.strip()

def _split_to_cards(segs, max_chars):
    """把每个 whisper 段按标点切成 ≤max_chars 的短卡片，时间按（清洗后）字数比例分。"""
    cap = max(6, int(max_chars))
    cards = []
    for (start, end, text) in segs:
        try:
            start = float(start); end = float(end)
        except Exception:
            continue
        text = (text or "").strip()
        if not text:
            continue
        phrases = re.findall(r"[^。.!！?？,，、;；:：…]+[。.!！?？,，、;；:：…]?", text)
        phrases = [p for p in phrases if p.strip()] or [text]
        pieces, buf = [], ""
        for ph in phrases:
            if buf and len(_clean_sub_text(buf)) + len(_clean_sub_text(ph)) > cap:
                pieces.append(buf); buf = ph
            else:
                buf += ph
        if buf:
            pieces.append(buf)
        cleaned = [c for c in (_clean_sub_text(p) for p in pieces) if c]
        if not cleaned:
            continue
        tot = sum(len(c) for c in cleaned) or 1
        pos = start
        for k, c in enumerate(cleaned):
            e = end if k == len(cleaned) - 1 else pos + (end - start) * (len(c) / tot)
            if e <= pos:
                e = pos + 0.4
            cards.append((pos, e, c))
            pos = e
    return cards

def _redistribute_known_text(known_text, segs):
    # text 模式：保留 whisper 时间轴，用已知文案替换识别文本（按各段识别字数比例切分，减少错字）
    kt = re.sub(r"\s+", "", known_text or "")
    if not kt or not segs:
        return segs
    total = sum(max(1, len(s[2])) for s in segs)
    out, pos, n = [], 0, len(segs)
    for i, (st, en, rec) in enumerate(segs):
        if i == n - 1:
            chunk = kt[pos:]
        else:
            take = max(1, int(round(len(rec) / total * len(kt))))
            end = pos + take
            lo, hi = max(pos + 1, end - 6), min(len(kt), end + 6)   # 切点吸附到最近标点，别切半个词
            best = -1
            for j in range(lo, hi + 1):
                if 0 < j <= len(kt) and kt[j - 1] in _SENT_PUNCT:
                    if best < 0 or abs(j - end) < abs(best - end):
                        best = j
            if best > 0:
                end = best
            chunk = kt[pos:end]
            pos = end
        out.append((st, en, chunk or rec))
    return out

def _build_ass(segs, style_key, w, h):
    st = _SUB_STYLES.get(style_key) or _SUB_STYLES["white"]
    fs = max(18, int(h * st["fs"]))
    mv = max(10, int(h * st["mv"]))
    mlr = max(10, int(w * 0.06))
    max_chars = max(8, int(w / (fs * 0.62)))
    head = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: %d" % w, "PlayResY: %d" % h,
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,%s,%d,%s,&H000000FF,%s,%s,-1,0,0,0,100,100,0,0,%d,%.1f,%d,2,%d,%d,%d,1" % (
            SUBTITLE_FONT, fs, st["primary"], st["outline"], st["back"], st["border"], st["ow"], st["shadow"], mlr, mlr, mv),
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text",
    ]
    body = []
    for (start, end, text) in _split_to_cards(segs, max_chars):  # 先按标点切成短卡片(去标点/分时间)
        try:
            start = float(start); end = float(end)
        except Exception:
            continue
        if end <= start:
            end = start + 1.2
        line = _wrap_cn(_ass_escape(text), max_chars)  # 先转义再断行：否则 \N 的反斜杠会被二次转义成 \\N，画面出现多余反斜杠
        if line:
            body.append("Dialogue: 0,%s,%s,Default,,0,0,,%s" % (_ass_time(start), _ass_time(end), line))
    return "\n".join(head + body) + "\n"

def burn_subtitle(video_file, known_text=None, style_key="white", job_id=None):
    """把 video_file 抽音频→whisper 转写→生成 .ass→ffmpeg 烧录，返回带字幕视频的相对路径。"""
    src = _resolve_out_file(video_file)
    if not src:
        raise ValueError("字幕烧录：视频文件不存在")
    tok = "%d_%s" % (int(time.time() * 1000), uuid.uuid4().hex[:8])  # 唯一，防同毫秒并发撞名/互相覆盖
    wav = VIDEO_OUT_DIR / ("sub_%s.wav" % tok)
    ass = VIDEO_OUT_DIR / ("sub_%s.ass" % tok)
    out_rel = "video/subtitled_%s.mp4" % tok
    out_fp = _out_path(out_rel)
    try:
        _sub_ffmpeg(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                     "-vn", "-ar", "16000", "-ac", "1", str(wav)], timeout=300)
        with _whisper_sem:  # 限制并发转写，避免多任务把 CPU 打满
            update_video_asset_phase(job_id, "burning_subtitle")  # 心跳：拿到信号量、开始转写，刷新 updated_at 防 reaper 误杀
            model = _get_whisper_model()
            seg_iter, _info = model.transcribe(str(wav), language="zh", vad_filter=True)
            segs = [(s.start, s.end, (s.text or "").strip()) for s in seg_iter if (s.text or "").strip()]
        if not segs:
            raise ValueError("字幕识别结果为空")
        if known_text:  # text 模式：用已知文案替换识别文本，时间轴仍用 whisper
            try:
                segs = _redistribute_known_text(known_text, segs)
            except Exception:
                pass
        w, h = _probe_video_size(src)
        ass.write_text(_build_ass(segs, (style_key or "white"), w, h), encoding="utf-8")
        update_video_asset_phase(job_id, "burning_subtitle")  # 心跳：开始烧录
        # cwd=视频目录 + ass 用文件名，避免 filtergraph 路径转义问题
        _sub_ffmpeg(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                     "-vf", "ass=" + ass.name, "-c:v", "libx264", "-preset", "veryfast",
                     "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy", str(out_fp)],
                    timeout=600, cwd=str(VIDEO_OUT_DIR))
        if not out_fp.exists() or out_fp.stat().st_size <= 0:
            raise ValueError("字幕烧录输出为空")
        return out_rel
    finally:
        for tmp in (wav, ass):
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

def gen_video(payload):
    job_id = payload.get("_job_id")
    mode = (payload.get("mode") or "text").strip()
    if mode not in {"text", "audio", "motion"}:
        raise ValueError("生成方式不正确")
    if not HEYGEN_API_KEY:
        raise ValueError("视频生成服务未配置")
    avatar = None
    avatar_id = payload.get("avatar_id")
    if mode == "motion" and avatar_id:
        avatar = get_video_avatar((payload.get("_username") or "").strip(), avatar_id)
        image_file = avatar.get("image_file")
    else:
        image_file = _save_data_file(payload.get("image_data"), "vid_img", [".jpg", ".png", ".webp"])
    if not image_file:
        raise ValueError("请先上传人物形象图片")
    text = (payload.get("text") or "").strip()
    voice = (payload.get("voice") or "").strip()
    audio_file = None
    audio_url = None
    reference_video_file = None
    if mode == "motion":
        reference_video_file = _save_data_file(payload.get("reference_video_data"), "motion_ref", [".mp4", ".mov", ".webm"])
        if not reference_video_file:
            raise ValueError("请先上传参考动作视频")
        text = text or "动作模仿"
        update_video_asset_phase(job_id, "files_saved", mode=mode, image_file=image_file,
                                 reference_video_file=reference_video_file, text=text,
                                 voice=voice)
    elif mode == "text":
        if not text:
            raise ValueError("请先输入口播文案")
        if not voice:
            raise ValueError("请先选择音色")
        audio_result = gen_audio({
            "_username": (payload.get("_username") or "").strip(),
            "text": text,
            "voice": voice,
            "speed": payload.get("speed", 1.0),
            "pitch": payload.get("pitch", 0),
            "volume": payload.get("volume", 0),
        })
        audio_file = audio_result.get("file")
        audio_url = audio_result.get("url")
        if not audio_file:
            raise ValueError("口播音频生成失败")
    else:
        audio_file = _save_data_file(payload.get("audio_data"), "vid_aud", [".mp3", ".wav", ".m4a"])
        if not audio_file:
            raise ValueError("请先选择口播音频")
        audio_url = _file_url(audio_file)
    resolution = (payload.get("resolution") or "1080p").strip()
    ratio = (payload.get("ratio") or "9:16").strip()
    motion = (payload.get("motion") or "medium").strip()
    if resolution not in {"720p", "1080p", "4k"}:
        resolution = "1080p"
    if ratio not in {"9:16", "16:9", "1:1", "4:5", "5:4"}:
        ratio = "9:16"
    if motion not in {"low", "medium", "high"}:
        motion = "medium"
    try:
        duration = int(payload.get("duration") or 10)
    except Exception:
        duration = 10
    duration = max(5, min(30, duration))
    created_avatar = None
    if mode == "motion":
        if resolution not in {"720p", "1080p"}:
            resolution = "720p"
        update_video_asset_phase(job_id, "motion_parameters_ready", resolution=resolution,
                                 ratio=ratio, motion=motion)
        video_result = generate_heygen_motion_video(image_file, reference_video_file, resolution, ratio, duration, job_id, avatar=avatar)
        if not avatar:
            created_avatar = record_video_avatar((payload.get("_username") or "").strip(), image_file,
                                                 video_result.get("avatar_item_id"), video_result.get("avatar_group_id"))
    else:
        video_result = generate_heygen_video(image_file, audio_file, resolution, ratio, motion)
    # F4：口播模式（text/audio）可选自动字幕；失败不影响已生成的视频（保留原片 + 记录错误）
    subtitle_on = False
    subtitle_error = None
    subtitle_style = (payload.get("subtitle_style") or "white").strip()
    if subtitle_style not in _SUB_STYLES:
        subtitle_style = "white"
    if payload.get("subtitle") and mode in {"text", "audio"} and video_result.get("video_file"):
        try:
            update_video_asset_phase(job_id, "burning_subtitle")
            known = text if mode == "text" else None
            subtitled = burn_subtitle(video_result["video_file"], known_text=known, style_key=subtitle_style, job_id=job_id)
            video_result["plain_video_file"] = video_result.get("video_file")
            video_result["video_file"] = subtitled
            video_result["video_url"] = _file_url(subtitled)
            subtitle_on = True
        except Exception as e:
            subtitle_error = str(e)[:200]
    return {
        "type": "video", "status": "done", "mode": mode,
        "image_file": image_file, "image_url": _file_url(image_file),
        "audio_file": audio_file, "audio_url": audio_url,
        "reference_video_file": reference_video_file,
        "reference_video_url": _file_url(reference_video_file) if reference_video_file else None,
        "text": text, "voice": voice,
        "video_file": video_result.get("video_file"), "video_url": public_url(video_result.get("video_file"), "video/mp4", private=True),
        "provider_video_id": video_result.get("video_id"),
        "provider_avatar_id": video_result.get("avatar_item_id"),
        "provider_avatar_group_id": video_result.get("avatar_group_id"),
        "avatar_id": (avatar.get("id") if avatar else (created_avatar or {}).get("id")),
        "image_asset_id": video_result.get("image_asset_id"),
        "audio_asset_id": video_result.get("audio_asset_id"),
        "reference_asset_id": video_result.get("reference_asset_id"),
        "source_video_url": video_result.get("source_video_url"),
        "thumbnail_url": video_result.get("thumbnail_url"), "duration": video_result.get("duration"),
        "resolution": resolution, "ratio": ratio, "motion": motion,
        "phase": "done",
        "subtitle": subtitle_on,
        "subtitle_style": subtitle_style if subtitle_on else None,
        "subtitle_error": subtitle_error,
        "plain_video_file": video_result.get("plain_video_file"),
        "message": "视频生成完成"
    }

# ============ F8 · 视频换装 / 换背景（RunningHub 两段式 AI App） ============
# 两段：换装(Wan2.2 Animate) → 换背景(VideoRefusion)。按有无衣服图/背景图裁剪阶段。
# clothes+bg → both；仅 clothes → 只换装；仅 bg → 只换背景。
TRYON_WEBAPP_ID = "1969605116187844610"   # 换装 AI App
BG_WEBAPP_ID    = "1986353521488523266"   # 换背景 AI App
TRYON_MAX_WAIT  = 40 * 60                  # 单段最长等待(秒)，超时判失败退点

def _rh_uploaded_name(upload_response):
    """RunningHub upload_file 返回体里取文件名（不同版本字段名不一）。"""
    for attr in ("fileName", "file_name", "file", "url", "key", "objectName", "object_name"):
        value = getattr(upload_response, attr, None)
        if value:
            return value
    if isinstance(upload_response, dict):
        for attr in ("fileName", "file_name", "file", "url", "key", "objectName", "object_name"):
            value = upload_response.get(attr)
            if value:
                return value
    raise RuntimeError("RunningHub 上传响应解析失败: %r" % (upload_response,))

def _rh_task_id(response):
    return (
        getattr(response, "task_id", None)
        or getattr(response, "taskId", None)
        or (response.get("taskId") if isinstance(response, dict) else None)
        or str(response)
    )

def _rh_wait_success(client, task_id, job_id, phase, fail_msg):
    """轮询 RunningHub 任务；每轮发一次 phase 心跳刷新 updated_at，防 reaper 误杀。"""
    deadline = time.time() + TRYON_MAX_WAIT
    while True:
        status = client.get_status(task_id)
        s = str(status)
        if s.endswith("SUCCESS"):
            return
        if s.endswith("FAILED"):
            raise RuntimeError(fail_msg)
        if time.time() > deadline:
            raise TimeoutError(fail_msg + "(超时)")
        update_video_asset_phase(job_id, phase)  # 心跳
        time.sleep(20)

def _store_tryon_video(local_path, prefix="tryon"):
    """把 RunningHub 下载到本地工作目录的成片，复制进内容输出库，返回相对路径(video/...)。"""
    src = pathlib.Path(local_path)
    if not src.is_file():
        raise RuntimeError("换装成片文件不存在")
    ext = src.suffix.lower() or ".mp4"
    if ext not in {".mp4", ".mov", ".webm"}:
        ext = ".mp4"
    fn = "video/%s_%d%s" % (prefix, int(time.time() * 1000), ext)
    _out_path(fn).write_bytes(src.read_bytes())
    return fn

def generate_tryon_video(person_video_file, clothes_file, background_file, seconds, job_id=None, username=None):
    """RunningHub 两段式换装/换背景驱动。返回 {video_file, video_url, ...}。"""
    try:
        from runninghub_sdk import RunningHubClient  # 服务器 pip 装；本地/CI 不触发 import
    except ImportError:
        raise RuntimeError("服务器未安装 runninghub_sdk")
    API_KEY = os.environ.get("RUNNINGHUB_API_KEY", "")
    if not API_KEY:
        raise RuntimeError("未配置 RUNNINGHUB_API_KEY")
    client = RunningHubClient(API_KEY, base_url="https://www.runninghub.cn", timeout=120)

    person_fp = _resolve_out_file(person_video_file)
    if not person_fp:
        raise ValueError("换装视频文件不存在")
    clothes_fp = _resolve_out_file(clothes_file) if clothes_file else None
    background_fp = _resolve_out_file(background_file) if background_file else None
    if clothes_file and not clothes_fp:
        raise ValueError("衣服图文件不存在")
    if background_file and not background_fp:
        raise ValueError("背景图文件不存在")

    work_dir = VIDEO_OUT_DIR / ("tryon_work_%d" % int(time.time() * 1000))
    work_dir.mkdir(parents=True, exist_ok=True)

    # ---- Stage 1 换装（仅有衣服图时执行） ----
    if clothes_fp:
        update_video_asset_phase(job_id, "uploading")
        src = _rh_uploaded_name(client.upload_file(str(person_fp)))
        cloth = _rh_uploaded_name(client.upload_file(str(clothes_fp)))
        update_video_asset_phase(job_id, "tryon_running")
        nodes = [
            {"nodeId": "363", "fieldName": "video", "fieldValue": src},
            {"nodeId": "373", "fieldName": "image", "fieldValue": cloth},
            {"nodeId": "362", "fieldName": "value", "fieldValue": str(seconds)},
            {"nodeId": "358", "fieldName": "value", "fieldValue": "576"},
            {"nodeId": "359", "fieldName": "value", "fieldValue": "1024"},
            {"nodeId": "372", "fieldName": "text", "fieldValue": "Clothes"},
        ]
        resp = client.run_ai_app(TRYON_WEBAPP_ID, node_info_list=nodes)
        task_id = _rh_task_id(resp)
        _rh_wait_success(client, task_id, job_id, "tryon_running", "换装失败")
        outputs = client.get_outputs(task_id)
        paths = client.download_outputs(outputs, work_dir, overwrite=True)
        if not paths:
            raise RuntimeError("换装未产出视频")
        working_video = str(paths[0])
    else:
        working_video = str(person_fp)

    # ---- Stage 2 换背景（仅有背景图时执行） ----
    if background_fp:
        update_video_asset_phase(job_id, "bg_running")
        vid = _rh_uploaded_name(client.upload_file(working_video))
        bg = _rh_uploaded_name(client.upload_file(str(background_fp)))
        nodes = [
            {"nodeId": "352", "fieldName": "video", "fieldValue": vid},
            {"nodeId": "318", "fieldName": "image", "fieldValue": bg},
            {"nodeId": "339", "fieldName": "int", "fieldValue": str(seconds)},
        ]
        resp = client.run_ai_app(BG_WEBAPP_ID, node_info_list=nodes)
        task_id = _rh_task_id(resp)
        _rh_wait_success(client, task_id, job_id, "bg_running", "换背景失败")
        outputs = client.get_outputs(task_id)
        paths = client.download_outputs(outputs, work_dir, overwrite=True)
        if not paths:
            raise RuntimeError("换背景未产出视频")
        final_video = str(paths[0])
    else:
        final_video = working_video

    # ---- 收尾：成片入库 ----
    update_video_asset_phase(job_id, "downloading")
    video_file = _store_tryon_video(final_video, "tryon")
    try:
        for tmp in work_dir.glob("*"):
            try: tmp.unlink()
            except Exception: pass
        work_dir.rmdir()
    except Exception:
        pass
    # 成片对外链接：优先上传 COS 用直链；未配置或失败则回退本地 /api/gen/file/ 链接
    video_url = _file_url(video_file)
    try:
        from . import cos
        if cos.enabled():
            video_url = cos.upload(_out_path(video_file), video_file, "video/mp4", private=True)
            if cos.delete_local_after_upload():
                try: _out_path(video_file).unlink()
                except Exception: pass
    except Exception as _cos_ex:
        print("[tryon] COS 上传失败，回退本地链接: %s" % _cos_ex, flush=True)
        video_url = _file_url(video_file)
    return {
        "video_file": video_file,
        "video_url": video_url,
        "duration": seconds,
    }

def gen_tryon(payload):
    job_id = payload.get("_job_id")
    username = (payload.get("_username") or "").strip()
    person_video_file = _save_data_file(payload.get("person_video_data"), "tryon_person", [".mp4", ".mov", ".webm"])
    if not person_video_file:
        raise ValueError("请上传换装视频")
    clothes_file = _save_data_file(payload.get("clothes_data"), "tryon_cloth", [".jpg", ".jpeg", ".png", ".webp"])
    background_file = _save_data_file(payload.get("background_data"), "tryon_bg", [".jpg", ".jpeg", ".png", ".webp"])
    if not clothes_file and not background_file:
        raise ValueError("请至少上传衣服图或背景图")
    if clothes_file and background_file:
        tryon_mode = "both"          # 换装 + 换背景
    elif clothes_file:
        tryon_mode = "clothes_only"  # 只换装
    else:
        tryon_mode = "bg_only"       # 只换背景
    try:
        seconds = int(payload.get("seconds") or 6)
    except Exception:
        seconds = 6
    seconds = max(1, min(15, seconds))
    text = (payload.get("text") or "").strip() or "换装换背景"
    update_video_asset_phase(job_id, "queued", mode="tryon", text=text,
                             reference_video_file=person_video_file, image_file=clothes_file)
    video_result = generate_tryon_video(person_video_file, clothes_file, background_file, seconds,
                                        job_id=job_id, username=username)
    return {
        "type": "video", "status": "done", "mode": "tryon",
        "tryon_mode": tryon_mode,
        "person_video_file": person_video_file,
        "reference_video_file": person_video_file,
        "reference_video_url": _file_url(person_video_file),
        "clothes_file": clothes_file,
        "background_file": background_file,
        "image_file": clothes_file,
        "image_url": _file_url(clothes_file) if clothes_file else None,
        "text": text,
        "video_file": video_result.get("video_file"), "video_url": video_result.get("video_url"),
        "source_video_url": video_result.get("video_url"),
        "duration": video_result.get("duration"),
        "seconds": seconds,
        "phase": "done",
        "message": "换装换背景视频生成完成"
    }

def _xiaole_request(method, path, body=None, timeout=90):
    if not XIAOLEVIDEO_API_KEY:
        raise ValueError("视频生成服务未配置（XIAOLEVIDEO_API_KEY）")
    url = path if path.startswith("http") else (XIAOLEVIDEO_API_BASE + path)
    headers = {"Authorization": "Bearer " + XIAOLEVIDEO_API_KEY, "User-Agent": "huangque-content/1.0"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    # 429（API Key 媒体任务过多）自动退避重试，扛并发限流
    for attempt in range(_xiaole_429_retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 429 and attempt < _xiaole_429_retries:
                wait = min(45, 8 * (attempt + 1))
                print("[xiaolevideo] 429 并发限流，%ds 后重试(%d/%d)" % (wait, attempt + 1, _xiaole_429_retries), flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError("视频接口失败: HTTP %s %s" % (e.code, detail))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # 瞬时网络抖动(SSL握手超时等)自动重试
            if attempt < _xiaole_429_retries:
                wait = min(30, 5 * (attempt + 1))
                print("[xiaolevideo] 网络异常，%ds 后重试(%d/%d): %s" % (wait, attempt + 1, _xiaole_429_retries, str(e)[:80]), flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError("视频接口网络异常: %s" % str(e)[:120])

def _xiaole_pick_video_url(output):
    for v in ((output or {}).get("videos") or []):
        if isinstance(v, dict):
            u = v.get("url") or v.get("video_url") or v.get("src") or v.get("download_url")
            if u:
                return u
        elif isinstance(v, str) and v:
            return v
    return None

def _download_xiaole_video(url, prefix="xiaole"):
    # 视频 CDN 多在海外(如 vidgen.x.ai)，国内服务器直连不通 → 复用法兰克福中转 /cdn/
    headers = {"User-Agent": "huangque-content/1.0"}
    relay = os.environ.get("HEYGEN_RELAY_BASE", "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if relay and host and not host.endswith(".cn"):
        fetch = "%s/cdn/%s/%s" % (relay, host, parts.path.lstrip("/"))
        if parts.query:
            fetch += "?" + parts.query
        token = os.environ.get("HEYGEN_RELAY_TOKEN", "").strip()
        if token:
            headers["X-Relay-Token"] = token
        url = fetch
    # 下载中断(IncompleteRead/网络抖动)自动重试
    data = None
    last_err = None
    for attempt in range(_xiaole_dl_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as r:
                buf = r.read()
            if buf:
                data = buf
                break
            last_err = RuntimeError("下载为空")
        except Exception as e:
            last_err = e
            print("[xiaolevideo] 下载失败重试(%d/%d): %s" % (attempt + 1, _xiaole_dl_retries, str(e)[:100]), flush=True)
            time.sleep(3 * (attempt + 1))
    if data is None:
        raise RuntimeError("视频下载失败: %s" % (str(last_err)[:120] if last_err else "未知"))
    if not data:
        raise RuntimeError("视频下载失败")
    fn = "video/%s_%s.mp4" % (prefix, uuid.uuid4().hex)  # 不可猜键：防枚举
    _out_path(fn).write_bytes(data)
    return fn

def generate_xiaole_video(model, prompt, reference_images=None, job_id=None, prefix="xiaole"):
    """统一 generations API：创建 → 轮询 → 下载。Grok(果肉)/Seedance(微衣) 共用。"""
    input_d = {"prompt": (prompt or "").strip()}
    if reference_images:
        input_d["reference_images"] = reference_images
    try:
        create = _xiaole_request("POST", "/api/v1/generations", {"model": model, "input": input_d})
    except RuntimeError as e:
        m = str(e)
        if ("无可用渠道" in m) or ("insufficient_user_quota" in m) or ("额度" in m) or ("媒体任务过多" in m):
            raise RuntimeError("该视频渠道暂时繁忙或维护中，请稍后再试")
        raise
    if create.get("code") not in (200, 0, None):
        msg = str(create.get("message") or create)[:200]
        if ("无可用渠道" in msg) or ("额度" in msg) or ("任务过多" in msg):
            raise RuntimeError("该视频渠道暂时繁忙或维护中，请稍后再试")
        raise RuntimeError("视频创建失败: %s" % msg)
    data = create.get("data") or {}
    rid = data.get("request_id") or data.get("task_id")
    status_url = data.get("status_url") or (("/api/v1/generations/" + str(rid)) if rid else "")
    if not status_url:
        raise RuntimeError("视频服务未返回任务ID: %s" % str(create)[:300])
    deadline = time.time() + XIAOLE_MAX_WAIT
    last = ""
    while time.time() < deadline:
        st = _xiaole_request("GET", status_url, timeout=30)
        sdata = st.get("data") or {}
        status = str(sdata.get("status") or "").lower()
        if status != last:
            print("[xiaolevideo] %s model=%s status=%s" % (rid, model, status), flush=True)
            if job_id:
                update_video_asset_phase(job_id, "xiaole_" + (status or "running"))
            last = status
        vurl = _xiaole_pick_video_url(sdata.get("output"))
        if vurl:
            if job_id:
                update_video_asset_phase(job_id, "downloading", source_video_url=vurl)
            video_file = _download_xiaole_video(vurl, prefix)
            return {"video_file": video_file, "video_url": _file_url(video_file),
                    "source_video_url": vurl, "model": model, "request_id": rid}
        if status in ("failed", "error", "cancelled", "canceled"):
            err = sdata.get("error") or {}
            msg = (err.get("message") if isinstance(err, dict) else None) or str(err) or status
            raise RuntimeError("视频生成失败: %s" % msg)
        time.sleep(XIAOLE_POLL_INTERVAL)
    raise TimeoutError("视频生成超时")

def gen_xiaole_video(payload):
    job_id = payload.get("_job_id")
    channel = (payload.get("channel") or "grok").strip()
    model = XIAOLE_CHANNEL_MODELS.get(channel)
    if not model:
        raise ValueError("未知视频渠道：%s" % channel)
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("请输入视频提示词")
    ref_images = None
    if channel in XIAOLE_IMAGE_CHANNELS:
        ref_images = payload.get("reference_images") or None
    label = {"grok": "果肉视频", "micro": "微衣视频"}.get(channel, model)
    if job_id:
        update_video_asset_phase(job_id, "queued", mode=channel, text=prompt, model=model)
    result = generate_xiaole_video(model, prompt, reference_images=ref_images, job_id=job_id, prefix=channel)
    return {
        "type": "video", "status": "done", "mode": channel, "model": model, "text": prompt,
        "video_file": result.get("video_file"), "video_url": result.get("video_url"),
        "source_video_url": result.get("source_video_url"),
        "phase": "done", "message": "%s生成完成" % label,
    }

HANDLERS = {"video": gen_video, "tryon": gen_tryon, "xiaole_video": gen_xiaole_video}
