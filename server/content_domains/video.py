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
VALID_VIDEO_RESOLUTIONS = {"720p", "1080p"}
VALID_VIDEO_MOTIONS = {"low", "medium", "high"}
VALID_IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
VALID_AUDIO_MIMES = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/mp4", "audio/m4a", "audio/x-m4a"}
VALID_REFERENCE_VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/webm"}
VIDEO_BATCH_MAX = 5
TRYON_MAX_INPUT_SEC = 6   # RunningHub 耗时随输入时长增长，线路一只处理前 6 秒。
XIAOLE_RATIO_SIZES = {
    "9:16": "720x1280",
    "16:9": "1280x720",
    "1:1": "1024x1024",
    "4:5": "1024x1280",
    "5:4": "1280x1024",
}

# 统一视频生成 API（xiaolevideo.cn）：果肉=Grok Video、豆姐=Seedance 2.0
XIAOLEVIDEO_API_KEY = os.environ.get("XIAOLEVIDEO_API_KEY", "")
XIAOLEVIDEO_API_BASE = os.environ.get("XIAOLEVIDEO_API_BASE", "https://api.xiaolevideo.cn").rstrip("/")
XIAOLE_MAX_WAIT = int(os.environ.get("XIAOLEVIDEO_TIMEOUT", "600"))
XIAOLE_POLL_INTERVAL = int(os.environ.get("XIAOLEVIDEO_POLL_INTERVAL", "5"))
_xiaole_429_retries = int(os.environ.get("XIAOLEVIDEO_429_RETRIES", "5"))   # 并发限流(429)退避重试次数
_xiaole_dl_retries = int(os.environ.get("XIAOLEVIDEO_DL_RETRIES", "3"))     # 下载中断重试次数
# 页面渠道 → 模型 id（前端传 channel，后端定 model，避免任意模型注入）
XIAOLE_CHANNEL_MODELS = {
    "grok": "Grok Image Video",   # 果肉视频（Grok Video 1.0：文生/图生视频）
    "micro": "seedance-2.0-fast", # 豆姐视频（Seedance 2.0 Fast：文生/图生视频）
    "omni": "omni-fast",          # 欧米视频（Omni Fast：文生/图生视频，~100s快；文生真人会被上游内容审核拦，图生真人不拦）
}
XIAOLE_IMAGE_CHANNELS = {"grok", "micro", "omni"}  # 支持参考图（图生视频）的渠道
# 文生视频固定时长的渠道（None=用平台默认）。omni-fast 只支持 10 秒(duration_options=[10])，不传会 400。
XIAOLE_CHANNEL_DURATION = {"omni": 10}
XIAOLE_MAX_REF = int(os.environ.get("XIAOLEVIDEO_MAX_REF", "7"))  # Grok 图生视频最多参考图数(实测上游pydantic硬上限7张,超过422)

def _xiaole_build_refs(reference_images):
    # 前端传 dataURL/URL → API 要的 [{type, value}]，最多 XIAOLE_MAX_REF 张。
    # type 合法枚举(实测 422 暴露)：'url' | 'base64' | 'data_url'。
    #  - https 链接    → url（实测：上游 Grok 渠道只有这种稳定出片，data_url/base64 会超时丢弃）
    #  - dataURL/裸base64 → 理论上也合法，但已知会超时；正常流程会先转存 COS 换成 url，这两支只是兜底
    out = []
    for item in (reference_images or [])[:XIAOLE_MAX_REF]:
        s = str(item or "").strip()
        if not s:
            continue
        if s.startswith("http"):
            out.append({"type": "url", "value": s})
        elif s.startswith("data:"):
            out.append({"type": "data_url", "value": s})
        else:
            out.append({"type": "base64", "value": s})
    return out

def _xiaole_ref_to_url(data_url):
    """Grok 参考图实测只有公网 HTTPS URL 能稳定出片(data_url/base64 会超时)。
    本地上传的图先落盘转存 COS 换直链；已经是 http(s) 的直接透传；转存失败就回退原始数据。"""
    s = str(data_url or "").strip()
    if not s or s.startswith("http"):
        return s
    try:
        fn = _save_data_file(s, "grok_ref", [".jpg", ".png", ".webp"])
        if not fn:
            return s
        url = public_url(fn, mimetypes.guess_type(fn)[0])
        return url if url.startswith("http") else s
    except Exception as e:
        print("[video] 参考图转存COS失败，回退原始数据: %s" % e, flush=True)
        return s

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
        decoded = base64.b64decode(encoded, validate=True)
    except Exception:
        return False
    if allowed_mimes == VALID_IMAGE_MIMES:
        return _image_bytes_look_valid(decoded)
    return True

def _image_bytes_look_valid(raw):
    if not raw:
        return False
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if raw.startswith(b"\xff\xd8\xff"):
        return True
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    return False

def _faststart_video_file(rel):
    raw = str(rel or "").strip()
    if not raw.lower().endswith(".mp4"):
        return rel
    src = _out_path(raw)
    if not src.is_file():
        return rel
    tmp = src.with_name(src.stem + ".faststart.tmp.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
             "-map", "0", "-c", "copy", "-movflags", "+faststart", str(tmp)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=600,
        )
        if tmp.is_file() and tmp.stat().st_size > 0:
            tmp.replace(src)
    except FileNotFoundError:
        print("[video] ffmpeg missing, skip faststart for %s" % raw, flush=True)
    except Exception as e:
        print("[video] faststart skipped for %s: %s" % (raw, str(e)[:160]), flush=True)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return rel


def _extract_first_frame_cover(video_rel, ss=1):
    """Extract first frame (-ss ss to skip black) as jpg cover. Returns rel path under video/ or None.
    Graceful if no ffmpeg (for 运维 install step).
    """
    raw = str(video_rel or "").strip()
    if not raw.lower().endswith((".mp4", ".mov", ".webm")):
        return None
    src = _out_path(raw)
    if not src.is_file():
        return None
    stem = src.stem
    cover = src.with_name(f"{stem}_cover.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(ss), "-i", str(src),
             "-vframes", "1", "-q:v", "3", str(cover)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
        )
        if cover.is_file() and cover.stat().st_size > 0:
            # return rel consistent with video_file convention (e.g. "video/xxx_cover.jpg" or just name)
            if "/" in raw:
                d = raw.rsplit("/", 1)[0]
                return f"{d}/{cover.name}"
            return cover.name
    except FileNotFoundError:
        print("[video] ffmpeg missing, skip first frame cover for %s (运维: apt install ffmpeg)" % raw, flush=True)
    except Exception as e:
        print("[video] first frame cover skipped for %s: %s" % (raw, str(e)[:160]), flush=True)
    return None


def mix_video_bgm(video_file, bgm_file, volume=0.18):
    """Loop BGM to the video duration. Keep the source video untouched on failure."""
    video_fp = _resolve_out_file(video_file)
    bgm_fp = _resolve_out_file(bgm_file)
    if not video_fp or not bgm_fp:
        raise ValueError("BGM 素材文件不存在")
    volume = max(0.05, min(0.8, float(volume)))
    out_rel = "video/bgm_%s.mp4" % uuid.uuid4().hex
    out_fp = _out_path(out_rel)
    common = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video_fp),
              "-stream_loop", "-1", "-i", str(bgm_fp)]
    attempts = [
        common + ["-filter_complex", "[0:a]volume=1[voice];[1:a]volume=%s[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[a]" % volume,
                  "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out_fp)],
        common + ["-filter_complex", "[1:a]volume=%s[a]" % volume, "-map", "0:v:0", "-map", "[a]",
                  "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out_fp)],
    ]
    last_error = None
    for cmd in attempts:
        try:
            subprocess.run(cmd, check=True, timeout=600, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if out_fp.is_file() and out_fp.stat().st_size > 0:
                return out_rel
        except Exception as exc:
            last_error = exc
        try:
            if out_fp.exists(): out_fp.unlink()
        except Exception:
            pass
    raise RuntimeError("BGM 混音失败: %s" % str(last_error)[:160])


def validate_video_payload(payload, username=None):
    if not isinstance(payload, dict):
        raise ValueError("请求体不是合法 JSON")
    mode = str(payload.get("mode") or "text").strip().lower()
    if mode not in VALID_VIDEO_MODES:
        raise ValueError("mode 仅支持 text/audio/motion")

    image_data = str(payload.get("image_data") or "").strip()
    avatar_id = str(payload.get("avatar_id") or "").strip()
    if not image_data and not avatar_id:
        raise ValueError("image_data 不能为空")
    if image_data and avatar_id:
        raise ValueError("image_data 与 avatar_id 只能选一个")
    if image_data and not _is_valid_data_url(image_data, VALID_IMAGE_MIMES):
        raise ValueError("image_data 不是有效的人物形象图片")
    line = None
    if mode == "text":
        if not str(payload.get("text") or "").strip():
            raise ValueError("mode=text 时 text 必填")
        if not (payload.get("voice") or "").strip():
            raise ValueError("mode=text 时 voice 必填")
    elif mode == "audio":
        audio_data = (payload.get("audio_data") or "").strip()
        if not audio_data:
            raise ValueError("audio_data 不能为空")
        if not _is_valid_data_url(audio_data, VALID_AUDIO_MIMES):
            raise ValueError("audio_data 不是有效的音频文件")
    elif mode == "motion":
        line = str(payload.get("line") or "2").strip()
        if line not in {"1", "2"}:
            raise ValueError("line 仅支持 1、2")
        reference_video_data = (payload.get("reference_video_data") or "").strip()
        if not reference_video_data:
            raise ValueError("reference_video_data 不能为空")
        if not _is_valid_data_url(reference_video_data, VALID_REFERENCE_VIDEO_MIMES):
            raise ValueError("reference_video_data 不是有效的参考动作视频")
    if avatar_id and username:
        get_video_avatar(username, avatar_id)

    ratio = (payload.get("ratio") or "9:16").strip()
    if ratio not in VALID_VIDEO_RATIOS:
        raise ValueError("ratio 仅支持 9:16、16:9、1:1、4:5、5:4")
    default_resolution = "720p" if mode == "motion" and line == "2" else "1080p"
    resolution = (payload.get("resolution") or default_resolution).strip().lower()
    if resolution not in VALID_VIDEO_RESOLUTIONS:
        raise ValueError("resolution 仅支持 720p、1080p")
    if mode == "motion" and line == "2" and resolution != "720p":
        raise ValueError("影视级模仿线路二固定为 720p，请改用线路一生成 1080p")
    motion = (payload.get("motion") or "medium").strip().lower()
    if motion not in VALID_VIDEO_MOTIONS:
        raise ValueError("motion 仅支持 low、medium、high")
    bgm_data = str(payload.get("bgm_data") or "").strip()
    if bgm_data and not _is_valid_data_url(bgm_data, VALID_AUDIO_MIMES):
        raise ValueError("bgm_data 不是有效的音频文件")
    try:
        bgm_volume = float(payload.get("bgm_volume", 0.18))
    except (TypeError, ValueError):
        raise ValueError("bgm_volume 必须是 0.05-0.8 的数字")
    if not 0.05 <= bgm_volume <= 0.8:
        raise ValueError("bgm_volume 必须是 0.05-0.8 的数字")

    cleaned = dict(payload)
    cleaned["mode"] = mode
    cleaned["ratio"] = ratio
    cleaned["resolution"] = resolution
    cleaned["motion"] = motion
    cleaned["bgm_data"] = bgm_data
    cleaned["bgm_volume"] = bgm_volume
    cleaned.pop("duration", None)
    if mode == "motion":
        cleaned["line"] = line
    return cleaned


def validate_video_batch_payload(payload, username=None, max_items=VIDEO_BATCH_MAX):
    if not isinstance(payload, dict):
        raise ValueError("请求体不是合法 JSON")
    if str(payload.get("mode") or "text").strip().lower() != "text":
        raise ValueError("批量出片仅支持文案配音模式")
    items = payload.get("avatars")
    if not isinstance(items, list) or len(items) < 2:
        raise ValueError("批量出片请至少选择 2 个形象")
    limit = max(1, min(VIDEO_BATCH_MAX, int(max_items or VIDEO_BATCH_MAX)))
    if len(items) > limit:
        raise ValueError("批量出片一次最多选择 %d 个形象" % limit)

    common = dict(payload)
    common.pop("avatars", None)
    common.pop("image_data", None)
    common.pop("avatar_id", None)
    common["mode"] = "text"
    cleaned_items, seen = [], set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError("第 %d 个形象参数不正确" % index)
        image_data = str(item.get("image_data") or "").strip()
        avatar_id = str(item.get("avatar_id") or "").strip()
        if bool(image_data) == bool(avatar_id):
            raise ValueError("第 %d 个形象必须且只能提供 image_data 或 avatar_id" % index)
        identity = "avatar:" + avatar_id if avatar_id else "image:" + image_data
        if identity in seen:
            raise ValueError("批量形象不能重复")
        seen.add(identity)
        one = dict(common)
        one["image_data"] = image_data
        one["avatar_id"] = avatar_id
        one["batch_label"] = str(item.get("label") or ("形象 %d" % index)).strip()[:60] or ("形象 %d" % index)
        one = validate_video_payload(one, username=username)
        one["batch_index"], one["batch_size"] = index, len(items)
        cleaned_items.append(one)
    return cleaned_items


def _tryon_line(payload):
    line = str(payload.get("line") or "").strip()
    if not line:
        line = "2" if ((payload.get("person_image_data") or payload.get("image_data"))
                       and not payload.get("person_video_data")) else "1"
    if line not in {"1", "2"}:
        raise ValueError("line 仅支持 1、2")
    return line


def _tryon_seconds(payload, line):
    raw = payload.get("seconds")
    if raw is None or raw == "":
        raw = 6
    if isinstance(raw, bool):
        raise ValueError("seconds 必须是整数")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        raise ValueError("seconds 必须是整数")
    if str(raw).strip() != str(seconds):
        raise ValueError("seconds 必须是整数")
    if line == "2" and not 5 <= seconds <= 15:
        raise ValueError("换装线路二时长仅支持 5-15 秒")
    if line == "1" and not 1 <= seconds <= TRYON_MAX_INPUT_SEC:
        raise ValueError("换装线路一时长仅支持 1-%d 秒" % TRYON_MAX_INPUT_SEC)
    return seconds


def validate_tryon_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求体不是合法 JSON")
    line = _tryon_line(payload)
    seconds = _tryon_seconds(payload, line)
    clothes_data = str(payload.get("clothes_data") or "").strip()
    background_data = str(payload.get("background_data") or "").strip()

    if line == "2":
        person_image_data = str(payload.get("person_image_data") or payload.get("image_data") or "").strip()
        if not person_image_data:
            raise ValueError("线路二换装请上传人物照片")
        if not _is_valid_data_url(person_image_data, VALID_IMAGE_MIMES):
            raise ValueError("person_image_data 不是有效的人物照片")
        if not clothes_data:
            raise ValueError("请上传衣服图")
        if not _is_valid_data_url(clothes_data, VALID_IMAGE_MIMES):
            raise ValueError("clothes_data 不是有效的衣服图片")
        if background_data:
            raise ValueError("线路二不支持换背景，请改用线路一")
    else:
        person_video_data = str(payload.get("person_video_data") or "").strip()
        if not person_video_data:
            raise ValueError("请上传换装视频")
        if not _is_valid_data_url(person_video_data, VALID_REFERENCE_VIDEO_MIMES):
            raise ValueError("person_video_data 不是有效的换装视频")
        if not clothes_data and not background_data:
            raise ValueError("请至少上传衣服图或背景图")
        if clothes_data and not _is_valid_data_url(clothes_data, VALID_IMAGE_MIMES):
            raise ValueError("clothes_data 不是有效的衣服图片")
        if background_data and not _is_valid_data_url(background_data, VALID_IMAGE_MIMES):
            raise ValueError("background_data 不是有效的背景图片")

    cleaned = dict(payload)
    cleaned["line"] = line
    cleaned["seconds"] = seconds
    return cleaned

def record_video_asset(job_id, username, result):
    now = int(time.time())
    with closing(adb()) as c:
        c.execute("""INSERT INTO video_assets
            (job_id, username, mode, image_file, audio_file, reference_video_file, video_file, video_url, text, voice_key,
             resolution, ratio, motion, phase, image_asset_id, audio_asset_id, reference_asset_id, provider_video_id,
             provider_avatar_id, provider_avatar_group_id, source_video_url, background_file, tryon_mode, model,
             status, error, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                background_file=COALESCE(excluded.background_file, video_assets.background_file),
                tryon_mode=COALESCE(excluded.tryon_mode, video_assets.tryon_mode),
                model=COALESCE(excluded.model, video_assets.model),
                status=COALESCE(excluded.status, video_assets.status),
                error=excluded.error,
                updated_at=excluded.updated_at""",
            (job_id, username, result.get("mode"), result.get("image_file"), result.get("audio_file"),
             result.get("reference_video_file"), result.get("video_file"), result.get("video_url"), result.get("text"), result.get("voice"),
             result.get("resolution"), result.get("ratio"), result.get("motion"), result.get("phase"),
             result.get("image_asset_id"), result.get("audio_asset_id"), result.get("reference_asset_id"),
             result.get("provider_video_id") or result.get("video_id"), result.get("provider_avatar_id") or result.get("avatar_item_id"),
             result.get("provider_avatar_group_id") or result.get("avatar_group_id"), result.get("source_video_url"),
             result.get("background_file"), result.get("tryon_mode"), result.get("model"),
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
        "provider_avatar_group_id", "source_video_url", "background_file", "tryon_mode",
        "model", "status", "error"
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
    is_tryon = bool(payload.get("person_video_data") or payload.get("person_image_data")
                    or payload.get("clothes_data") or payload.get("background_data"))
    mode = "tryon" if is_tryon else (payload.get("mode") or payload.get("channel") or "text")
    is_talking = mode in {"text", "audio"}
    resolution = payload.get("resolution")
    if not resolution and is_talking:
        resolution = "1080p"
    record_video_asset(job_id, username, {
        "mode": mode,
        "text": payload.get("text") or payload.get("prompt") or "",
        "voice": payload.get("voice") or "",
        "resolution": resolution,
        "ratio": payload.get("ratio") or "9:16",
        "motion": (payload.get("motion") or "medium") if is_talking else payload.get("motion"),
        "reference_video_file": payload.get("person_video_file") or None,
        "background_file": payload.get("background_file") or None,
        "model": payload.get("model") or None,
        "phase": "queued",
        "status": "running",
    })

def list_video_assets(username, limit=120):
    limit = max(1, min(120, int(limit or 120)))
    with closing(adb()) as c:
        rows = c.execute("""SELECT id, job_id, username, mode, image_file, audio_file, reference_video_file, video_file, video_url,
                   text, voice_key, resolution, ratio, motion, phase, image_asset_id, audio_asset_id, reference_asset_id,
                   provider_video_id, provider_avatar_id, provider_avatar_group_id, source_video_url,
                   background_file, tryon_mode, model,
                   status, error, created_at, updated_at
            FROM video_assets
            WHERE username=? AND status!='deleted'
            ORDER BY id DESC LIMIT ?""", (username, limit)).fetchall()
    items = [dict(r) for r in rows]
    job_ids = [item.get("job_id") for item in items if item.get("job_id")]
    if job_ids:
        try:
            placeholders = ",".join("?" for _ in job_ids)
            with closing(jdb()) as c:
                jobs = c.execute("SELECT id,payload,result FROM jobs WHERE id IN (%s)" % placeholders,
                                 job_ids).fetchall()
            job_meta = {row["id"]: row for row in jobs}
            for item in items:
                row = job_meta.get(item.get("job_id"))
                if not row:
                    continue
                try:
                    payload = json.loads(row["payload"] or "{}")
                except Exception:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                try:
                    result = json.loads(row["result"] or "{}")
                except Exception:
                    result = {}
                if not isinstance(result, dict):
                    result = {}
                duration = result.get("duration") or result.get("seconds")
                if duration is None and item.get("mode") == "tryon":
                    duration = payload.get("seconds")
                try:
                    duration = float(duration)
                except (TypeError, ValueError):
                    duration = None
                if duration and duration > 0:
                    item["duration"] = duration
                if str(payload.get("line") or "") in {"1", "2"}:
                    item["line"] = str(payload["line"])
                for key in ("batch_id", "batch_label", "batch_index", "batch_size"):
                    if payload.get(key) is not None:
                        item[key] = payload[key]
        except Exception:
            pass
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

def _heygen_request_json(method, path, body=None, headers=None, timeout=180, direct=False):
    # direct=True 时同一套 v3 API 打 HeyGen 真身（泽龙即 v3 转发，路径同构），走 mihomo 代理出境
    if not HEYGEN_API_KEY:
        raise ValueError("视频生成服务未配置")
    h = {"x-api-key": HEYGEN_API_KEY}
    if not direct and _heygen_relay_token():
        h["X-Relay-Token"] = _heygen_relay_token()
    if headers:
        h.update(headers)
    base = (_HEYGEN_DIRECT_API + "/v3") if direct else HEYGEN_API_BASE
    req = urllib.request.Request(base + path, data=body, headers=h, method=method)
    open_fn = _heygen_direct_opener().open if direct else urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as r:
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

def _heygen_upload_asset(file_path, direct=False):
    path = pathlib.Path(file_path)
    if not path.is_file():
        raise ValueError("视频素材文件不存在")
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if direct:
        # HeyGen 素材上传端点收「raw 文件字节 + 文件 mime」(同口播直连 #405 的 /v1/asset)；
        # 发 multipart/form-data 会被 HeyGen 判 "Content type not supported application/octet-stream" 400。
        d = _heygen_direct_req("POST", _HEYGEN_DIRECT_UPLOAD + "/v1/asset", path.read_bytes(), mime, timeout=240)
        node = d.get("data") or {}
        asset_id = str(node.get("asset_id") or node.get("id") or "").strip()
        if not asset_id:
            raise RuntimeError("HeyGen直连素材上传未返回asset_id: %s" % json.dumps(d, ensure_ascii=False)[:300])
        return asset_id
    # ponytail: 中转(泽龙 relay)仍走 v3 /assets multipart——已知同样被 HeyGen 判 octet-stream 400。
    # motion 直连优先(_HEYGEN_DIRECT 默认开)，此 multipart 分支仅在直连被禁用时用；中转上传修复待换渠道或单独排查 relay 端点。
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
    }, timeout=240, direct=direct)
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

def _heygen_create_video(image_asset_id, audio_asset_id, resolution, ratio, motion, direct=False):
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
    }, timeout=90, direct=direct)
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

def _heygen_create_photo_avatar(image_asset_id, direct=False):
    body = json.dumps({
        "type": "photo",
        "name": "huangque_photo_avatar_%d" % int(time.time()),
        "file": {"type": "asset_id", "asset_id": image_asset_id},
    }, ensure_ascii=False).encode()
    data = _heygen_request_json("POST", "/avatars", body, {
        "Content-Type": "application/json",
    }, timeout=90, direct=direct)
    root = data.get("data") or {}
    avatar_item_id = (((root.get("avatar_item") or {}).get("id")) or "").strip()
    avatar_group_id = (((root.get("avatar_group") or {}).get("id")) or "").strip()
    if not avatar_item_id:
        raise RuntimeError("HeyGen未返回avatar_item_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return avatar_item_id, avatar_group_id

_HEYGEN_AVATAR_READY = {"completed", "ready", "success"}
_HEYGEN_AVATAR_FAILED = {"failed", "error", "rejected"}
# 中转（泽龙）只转发 v3，拿不到 look 级状态。盲等这么久后放行，交给 create 的 400 重试兜底。
HEYGEN_AVATAR_UNKNOWN_GRACE = int(os.environ.get("HEYGEN_AVATAR_UNKNOWN_GRACE", "60") or 60)

def _heygen_look_status(avatar_item_id, avatar_group_id="", direct=False):
    """取 photo avatar **look** 的真实状态，返回 (status, moderation_msg)。

    ⚠ 这里踩过一个大坑：`/v3/avatars` 与 `/v3/avatars/{group}` 返回的是 **avatar 组**，
    而组的 `preview_image_url` 在 look 仍是 `pending` 时就已经有值，且那个 URL 恰好长这样：
        https://files2.heygen.ai/talking_photo/<look_id>/xxx.WEBP
    ——里面正好含 look_id。老代码据此模糊匹配、又把「有 preview_image_url」当作就绪，
    于是 wait 立刻返回，随后提交生成就被 HeyGen 400：
        "Avatar look <id> is not ready (status: pending)"
    更要命的是：靠重试等到不再 400 也没用 —— avatar 没训练完，生成任务照样静默 failed
    且 `error: null`。线上 HeyGen 动作模仿约 26% 的成功率，就是这个竞态的产物。

    look 级状态只在 v2：`GET /v2/photo_avatar/{look_id}` → `status`（pending / completed / failed）。
    """
    if direct:
        d = _heygen_direct_req("GET", _HEYGEN_DIRECT_API + "/v2/photo_avatar/" + urllib.parse.quote(avatar_item_id),
                               body=None, ctype=None, timeout=20)
        node = d.get("data") or {}
        return str(node.get("status") or "").lower(), str(node.get("moderation_msg") or "")
    # 中转（泽龙）只转发 v3，拿不到 look 级状态；退而查组，但**仍然要发请求** ——
    # 否则鉴权失败之类的错误会被「继续轮询」掩盖成超时（见 test_avatar_poll_does_not_hide_request_error）。
    path = ("/avatars/" + urllib.parse.quote(avatar_group_id)) if avatar_group_id else "/avatars"
    data = _heygen_request_json("GET", path, timeout=20, direct=False)
    wanted = {i for i in (avatar_item_id, avatar_group_id) if i}
    item = _find_nested_dict(data, lambda d: str(d.get("id") or "") in wanted)
    if not item:
        return "", ""
    return str(item.get("status") or item.get("state") or "").lower(), ""

def _heygen_wait_photo_avatar(avatar_item_id, avatar_group_id="", direct=False):
    """等到 look 真正 completed 才返回。绝不把「有预览图」当就绪。"""
    deadline = time.time() + min(HEYGEN_TIMEOUT, 900)
    started = time.time()
    last_status = ""
    while time.time() < deadline:
        # 查询异常直接上抛，不掩盖：401/配额之类的错误若被「继续轮询」吃掉，会伪装成超时。
        status, moderation = _heygen_look_status(avatar_item_id, avatar_group_id, direct=direct)
        if status and status != last_status:
            print("[heygen] avatar look=%s status=%s" % (avatar_item_id, status), flush=True)
            last_status = status
        if status in _HEYGEN_AVATAR_READY:
            return True
        if status in _HEYGEN_AVATAR_FAILED:
            raise RuntimeError("HeyGen Photo Avatar 处理失败: %s" % (moderation or status))
        if not status and not direct and (time.time() - started) > HEYGEN_AVATAR_UNKNOWN_GRACE:
            # 中转拿不到 look 状态，只能盲等一段再放行；create 侧仍有 400 "not ready" 重试兜底
            print("[heygen] 中转无法获取 look 状态，盲等 %ds 后放行" % HEYGEN_AVATAR_UNKNOWN_GRACE, flush=True)
            return True
        time.sleep(HEYGEN_POLL_INTERVAL)
    raise TimeoutError("HeyGen Photo Avatar处理超时")

# HeyGen cinematic_avatar 只接受 16:9/9:16/1:1，其它比例(如前端曾放的 4:5/5:4)必报 invalid_parameter 400。
# 兜底映射到最近的合法朝向(竖版→9:16、横版→16:9)，未知比例默认 9:16(motion 主打竖屏出片)。
_HEYGEN_CINEMATIC_RATIOS = {"16:9", "9:16", "1:1"}
def _heygen_cinematic_ratio(ratio):
    r = (ratio or "").strip()
    if r in _HEYGEN_CINEMATIC_RATIOS:
        return r
    return {"4:5": "9:16", "5:4": "16:9"}.get(r, "9:16")

def _heygen_create_cinematic_video(avatar_item_id, reference_asset_id, ratio, resolution, duration, direct=False):
    prompt = (
        "Create a realistic cinematic vertical video of the same person from the avatar photo. "
        "Follow the uploaded reference video ONLY for body movement, pose, timing, gestures, "
        "facial expression rhythm, framing and camera motion. CRITICAL: Keep the avatar person's "
        "exact identity, face, hairstyle, body shape, skin tone and clothing. Do NOT copy the "
        "reference video person's appearance, body proportions or outfit. The output must look "
        "like the avatar person performing the reference motion, not the reference person. "
        "Smooth realistic motion, no text, no logo, no extra people."
    )
    body = json.dumps({
        "type": "cinematic_avatar",
        "title": "follow_reference_motion",
        "prompt": prompt,
        "avatar_id": [avatar_item_id],
        "references": [{"type": "asset_id", "asset_id": reference_asset_id}],
        "aspect_ratio": _heygen_cinematic_ratio(ratio),
        "resolution": resolution,
        "duration": duration,
        "enhance_prompt": False,
    }, ensure_ascii=False).encode()
    data = _heygen_request_json("POST", "/videos", body, {
        "Content-Type": "application/json",
    }, timeout=90, direct=direct)
    video_id = ((data.get("data") or {}).get("video_id") or "").strip()
    if not video_id:
        raise RuntimeError("HeyGen未返回video_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return video_id

class HeyGenBilledError(RuntimeError):
    """视频已在 HeyGen 提交成功（= 已计费）之后才失败。绝不能回退中转重发。

    HeyGen 在「提交」那一刻就扣费，不是出片时（2026-07-11 用生成前后读钱包实测：
    cinematic 提交即扣 $7，钱包 15.15→8.15）。而泽龙中转转发的是同一个 HeyGen 账号
    （见 generate_heygen_motion_video 的注释），所以「回退泽龙」不是换供应商，
    是拿同一份素材再提交一次 —— 同一条视频付两次钱。

    原来两处 fallback 都是 `except Exception` 一把抓，不区分失败发生在提交前还是提交后：
    轮询超时/下载失败/网络抖动，全都会触发重发。这与 egress.post_json 里早已立下的
    非幂等纪律（_pre_delivery_failure：只有「投递前」的失败才可以换通道重试）是同一条，
    这里漏了。

    提交前失败（上传、建 avatar、建视频本身）不属于本异常，仍可安全回退。
    """


# 直连轮询死线。motion 实测生成 392~511s(10 路并发下)，原值 510 是照着早已废弃的
# reaper 600s 算的（reaper 的 motion 宽限现在是 2400s），擦线甚至越线 → 触发回退重发。
HEYGEN_MOTION_DEADLINE = int(os.environ.get("HEYGEN_MOTION_DEADLINE", "1500") or 1500)


def _heygen_poll_video(video_id, direct=False, deadline_s=None):
    deadline = time.time() + (deadline_s or HEYGEN_TIMEOUT)
    last_status = ""
    while time.time() < deadline:
        data = _heygen_request_json("GET", "/videos/" + urllib.parse.quote(video_id), timeout=90, direct=direct)
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
    return _faststart_video_file(fn)

# ==================== HeyGen 直连(数字人口播,绕开泽龙中转,走 mihomo 代理) ====================
# 泽龙共享账号排队让口播动辄超 6 分钟；直连 HeyGen 真身实测约 1 分钟(kongli决策)。直连失败自动回退泽龙。
_HEYGEN_DIRECT = os.environ.get("HEYGEN_DIRECT", "1").strip().lower() not in ("0", "false", "no")
# 出境通道：显式 HEYGEN_DIRECT_PROXY 覆盖一切；否则 VPS 隧道优先、mihomo 备选（见 egress.preferred_proxy）。
# 通道在发请求前选定：create-video 是非幂等的，换通道重发会让 HeyGen 出两条片、计两次费。
_HEYGEN_DIRECT_PROXY = (os.environ.get("HEYGEN_DIRECT_PROXY") or "").strip()
_HEYGEN_PROXY_FALLBACK = (os.environ.get("EGRESS_PROXY_FALLBACK") or os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7897").strip()


def _heygen_proxy():
    if _HEYGEN_DIRECT_PROXY:
        return _HEYGEN_DIRECT_PROXY
    from . import egress
    return egress.preferred_proxy(_HEYGEN_PROXY_FALLBACK)
_HEYGEN_DIRECT_API = "https://api.heygen.com"
_HEYGEN_DIRECT_UPLOAD = "https://upload.heygen.com"

def _heygen_direct_opener():
    p = _heygen_proxy()
    if p:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": p, "https": p}))
    return urllib.request.build_opener()

def _heygen_direct_req(method, url, body=None, ctype="application/json", timeout=120):
    if not HEYGEN_API_KEY:
        raise ValueError("视频生成服务未配置")
    h = {"X-Api-Key": HEYGEN_API_KEY}
    if ctype:
        h["Content-Type"] = ctype
    data = body if isinstance(body, (bytes, bytearray)) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with _heygen_direct_opener().open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").replace("\n", " ")[:400]
        raise RuntimeError("HeyGen直连失败: HTTP %s %s" % (e.code, detail)) from e

def _download_video_file_direct(url, prefix="vid"):
    if not url:
        raise RuntimeError("直连未返回视频地址")
    req = urllib.request.Request(url, headers={"User-Agent": "huangque-content/1.0"})
    with _heygen_direct_opener().open(req, timeout=360) as r:
        data = r.read()
    if not data:
        raise RuntimeError("视频下载失败")
    fn = "video/%s_%s.mp4" % (prefix, uuid.uuid4().hex)  # 不可猜键(#185)
    _out_path(fn).write_bytes(data)
    return _faststart_video_file(fn)

def generate_heygen_video_direct(image_file, audio_file, resolution, ratio, motion):
    """数字人口播直连 HeyGen v3(type=image + expressiveness)：与泽龙中转同一套 API/参数(honor resolution+expressiveness)，
    只是 direct=True 走 api.heygen.com 出境。原 v2 talking_photo 直连丢了 expressiveness 且忽略 resolution
    →出片效果不同+被硬编码720p(用户反馈"效果不一样")；改回 v3 image 后实测 1080×1920 104s。"""
    image_fp = _resolve_out_file(image_file)
    audio_fp = _resolve_out_file(audio_file)
    if not image_fp or not audio_fp:
        raise ValueError("视频素材文件不存在")
    image_fp = _ensure_heygen_image_jpg(image_fp)
    audio_fp = _ensure_heygen_audio_mp3(audio_fp)
    image_asset_id = _heygen_upload_asset(image_fp, direct=True)
    audio_asset_id = _heygen_upload_asset(audio_fp, direct=True)
    video_id = _heygen_create_video(image_asset_id, audio_asset_id, resolution, ratio, motion, direct=True)
    # ↓ 此刻已计费。之后任何失败都不能回退中转重发（同一账号，会再付一次），见 HeyGenBilledError
    try:
        info = _heygen_poll_video(video_id, direct=True, deadline_s=450)  # 直连轮询死线450s，配套 reaper 口播 540s
        video_file = _download_video_file_direct(info["video_url"], "heygen")
        cover = _extract_first_frame_cover(video_file)
    except Exception as e:
        raise HeyGenBilledError("口播已提交 HeyGen(video_id=%s，已计费)，后续失败: %s"
                                % (video_id, str(e)[:180])) from e
    ret = {
        "video_id": video_id, "video_file": video_file, "video_url": _file_url(video_file),
        "image_asset_id": image_asset_id, "audio_asset_id": audio_asset_id,
        "source_video_url": info.get("video_url"), "thumbnail_url": info.get("thumbnail_url"),
        "duration": info.get("duration"), "provider": "heygen_direct",
    }
    if cover:
        ret["image_file"] = cover
        ret["image_url"] = public_url(cover, "image/jpeg")
    return ret

def generate_heygen_video(image_file, audio_file, resolution, ratio, motion):
    if _HEYGEN_DIRECT and HEYGEN_API_KEY:
        try:
            return generate_heygen_video_direct(image_file, audio_file, resolution, ratio, motion)
        except HeyGenBilledError:
            raise   # 已提交=已计费，重发就是再付一次钱（泽龙转发同一账号）
        except Exception as e:
            print("[heygen] 直连失败(提交前),回退泽龙中转: %s" % str(e)[:200], flush=True)
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
    cover = _extract_first_frame_cover(video_file)
    ret = {
        "video_id": video_id,
        "image_asset_id": image_asset_id,
        "audio_asset_id": audio_asset_id,
        "video_file": video_file,
        "video_url": _file_url(video_file),
        "source_video_url": info.get("video_url"),
        "thumbnail_url": info.get("thumbnail_url"),
        "duration": info.get("duration"),
    }
    if cover:
        ret["image_file"] = cover
        ret["image_url"] = public_url(cover, "image/jpeg")
    return ret

def generate_heygen_motion_video(image_file, reference_video_file, resolution, ratio, duration, job_id=None, avatar=None):
    # 直连优先（同 #405 口播）：泽龙转发同一账号，直连省掉排队；「提交前」失败才回退泽龙。
    # 提交后失败绝不回退：cinematic 提交即扣 $7，泽龙是同一账号，重发 = 同一条视频付两次。
    if _HEYGEN_DIRECT and HEYGEN_API_KEY:
        try:
            return _generate_heygen_motion_video_impl(image_file, reference_video_file, resolution, ratio,
                                                      duration, job_id, avatar, direct=True)
        except HeyGenBilledError:
            raise
        except Exception as e:
            print("[heygen] motion直连失败(提交前),回退泽龙中转: %s" % str(e)[:200], flush=True)
    return _generate_heygen_motion_video_impl(image_file, reference_video_file, resolution, ratio,
                                              duration, job_id, avatar, direct=False)

def _generate_heygen_motion_video_impl(image_file, reference_video_file, resolution, ratio, duration, job_id=None, avatar=None, direct=False):
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
        image_asset_id = _heygen_upload_asset(image_fp, direct=direct)
    update_video_asset_phase(job_id, "uploading_reference_asset", image_asset_id=image_asset_id,
                             provider_avatar_id=avatar_item_id or None,
                             provider_avatar_group_id=avatar_group_id or None)
    reference_asset_id = _heygen_upload_asset(reference_fp, direct=direct)
    if not avatar_item_id:
        update_video_asset_phase(job_id, "creating_photo_avatar", image_asset_id=image_asset_id,
                                 reference_asset_id=reference_asset_id)
        avatar_item_id, avatar_group_id = _heygen_create_photo_avatar(image_asset_id, direct=direct)
        update_video_asset_phase(job_id, "waiting_photo_avatar", image_asset_id=image_asset_id,
                                 reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                                 provider_avatar_group_id=avatar_group_id)
        _heygen_wait_photo_avatar(avatar_item_id, avatar_group_id, direct=direct)
    update_video_asset_phase(job_id, "creating_cinematic_video", image_asset_id=image_asset_id,
                             reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                             provider_avatar_group_id=avatar_group_id)
    video_id = None
    last_create_error = None
    rebuilt_avatar = False
    for attempt in range(1, 7):
        try:
            video_id = _heygen_create_cinematic_video(avatar_item_id, reference_asset_id, ratio, resolution, duration, direct=direct)
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
                    image_asset_id = _heygen_upload_asset(image_fp, direct=direct)
                avatar_item_id, avatar_group_id = _heygen_create_photo_avatar(image_asset_id, direct=direct)
                if avatar.get("username"):
                    record_video_avatar(avatar.get("username"), image_file, avatar_item_id, avatar_group_id, avatar.get("name"))
                update_video_asset_phase(job_id, "waiting_rebuilt_photo_avatar", image_asset_id=image_asset_id,
                                         reference_asset_id=reference_asset_id, provider_avatar_id=avatar_item_id,
                                         provider_avatar_group_id=avatar_group_id)
                _heygen_wait_photo_avatar(avatar_item_id, avatar_group_id, direct=direct)
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
    # ↓ 此刻已计费($7)。之后任何失败都不能回退中转重发（同一账号），见 HeyGenBilledError。
    # 死线 1500s：实测 HeyGen 生成 392~511s（10 路并发下），原值 510 擦线甚至越线；
    # 上传(≤240s)+生成(≤1500s)+下载 仍在 reaper 的 motion 宽限 2400s 内，不会被误判超时。
    try:
        info = _heygen_poll_video(video_id, direct=direct,
                                  deadline_s=HEYGEN_MOTION_DEADLINE if direct else None)
        update_video_asset_phase(job_id, "downloading_video", provider_video_id=video_id,
                                 source_video_url=info.get("video_url"))
        video_file = (_download_video_file_direct if direct else _download_video_file)(info["video_url"], "cinematic")
        cover = _extract_first_frame_cover(video_file)
    except Exception as e:
        raise HeyGenBilledError("动作模仿已提交 HeyGen(video_id=%s，已扣费)，后续失败: %s"
                                % (video_id, str(e)[:180])) from e
    ret = {
        "video_id": video_id,
        "provider": "heygen_direct" if direct else "heygen_zelong",
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
    if cover:
        ret["image_file"] = cover
        ret["image_url"] = public_url(cover, "image/jpeg")
    return ret

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
# 字幕位置5档 → (ASS Alignment, MarginV系数)。底部/偏下用底锚(Align2,离底);顶部/偏上用顶锚(Align8,离顶);
# 中央垂直居中(Align5)。bottom 的 mv=None 沿用样式自带值,保持旧默认行为(向后兼容)。
_SUB_POSITIONS = {
    "bottom": (2, None),   # 底部(默认)
    "lower":  (2, 0.20),   # 偏下
    "center": (5, 0.00),   # 中央
    "upper":  (8, 0.20),   # 偏上
    "top":    (8, 0.06),   # 顶部
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

def _probe_video_duration(video_file):
    fp = _resolve_out_file(video_file)
    if not fp:
        raise ValueError("参考动作视频文件不存在")
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(fp)],
            check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode("utf-8", "replace").strip()
        duration = float(out)
        if duration <= 0:
            raise ValueError
        return duration
    except Exception as e:
        raise ValueError("无法读取参考视频时长，请重新导出为 MP4 后上传") from e

def _motion_reference_duration(reference_video_file, line):
    duration = _probe_video_duration(reference_video_file)
    line = "2" if str(line or "1").strip() == "2" else "1"
    max_duration = 120 if line == "2" else 30
    if duration > max_duration + 0.05:
        provider = "线路二 WaveSpeed" if line == "2" else "线路一 HeyGen"
        raise ValueError("参考视频 %.1f 秒，超过%s最长 %d 秒，请先裁剪后重试" % (
            duration, provider, max_duration,
        ))
    if line == "1" and duration < 5:
        raise ValueError("参考视频 %.1f 秒，线路一 HeyGen 最短支持 5 秒" % duration)
    return duration

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
    # 多行折行，最多 4 行；每行都 ≤max_chars，不把剩余整段塞进最后一行(防超长尾行横向溢出)。
    while len(cur) > max_chars and len(lines) < 4:
        cut = cur.rfind(" ", 0, max_chars + 1)   # 停顿已转空格，优先在空格处断
        if cut < max_chars * 0.5:
            cut = max_chars
        lines.append(cur[:cut].strip())
        cur = cur[cut:].strip()
    if cur:
        lines.append(cur[:max_chars] if len(cur) > max_chars else cur)  # 兜底截断,宁可少字也不溢出
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

def _build_ass(segs, style_key, w, h, position="bottom"):
    st = _SUB_STYLES.get(style_key) or _SUB_STYLES["white"]
    align, mvf = _SUB_POSITIONS.get(position) or _SUB_POSITIONS["bottom"]
    fs = max(18, int(h * st["fs"]))
    mv = max(10, int(h * (st["mv"] if mvf is None else mvf)))  # bottom 沿用样式 mv，其余用档位系数
    mlr = max(10, int(w * 0.06))
    # 单行最大字数按「可用宽度(减左右边距) ÷ 单字宽」算。中文全角字宽≈字号(1em)，取 1.05 留安全余量，
    # 防长句超出画面边界。原来用全宽 w + 0.62 系数会算出约 1.6 倍字数→溢出。
    max_chars = max(6, int((w - 2 * mlr) / (fs * 1.05)))
    head = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: %d" % w, "PlayResY: %d" % h,
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,%s,%d,%s,&H000000FF,%s,%s,-1,0,0,0,100,100,0,0,%d,%.1f,%d,%d,%d,%d,%d,1" % (
            SUBTITLE_FONT, fs, st["primary"], st["outline"], st["back"], st["border"], st["ow"], st["shadow"], align, mlr, mlr, mv),
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

def burn_subtitle(video_file, known_text=None, style_key="white", job_id=None, position="bottom"):
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
        ass.write_text(_build_ass(segs, (style_key or "white"), w, h, position or "bottom"), encoding="utf-8")
        update_video_asset_phase(job_id, "burning_subtitle")  # 心跳：开始烧录
        # cwd=视频目录 + ass 用文件名，避免 filtergraph 路径转义问题
        _sub_ffmpeg(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                     "-vf", "ass=" + ass.name, "-c:v", "libx264", "-preset", "veryfast",
                     "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy", str(out_fp)],
                    timeout=600, cwd=str(VIDEO_OUT_DIR))
        if not out_fp.exists() or out_fp.stat().st_size <= 0:
            raise ValueError("字幕烧录输出为空")
        return _faststart_video_file(out_rel)
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
    if avatar_id:
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
    bgm_file = (_save_data_file(payload.get("bgm_data"), "video_bgm", [".mp3", ".wav", ".m4a"])
                if payload.get("bgm_data") else None)
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
    if resolution not in {"720p", "1080p"}:
        resolution = "1080p"
    if ratio not in {"9:16", "16:9", "1:1", "4:5", "5:4"}:
        ratio = "9:16"
    if motion not in {"low", "medium", "high"}:
        motion = "medium"
    created_avatar = None
    if mode == "motion":
        line = "1" if str(payload.get("line") or "2").strip() == "1" else "2"  # 默认线路二(WaveSpeed),仅显式line=1走线路一
        reference_duration = _motion_reference_duration(reference_video_file, line)
        # HeyGen 只收整数秒，向上取整避免截掉参考片段末尾；WaveSpeed 直接跟随驱动视频，不收 duration。
        duration = max(5, min(30, int(reference_duration + 0.999999)))
        if line == "2" and resolution != "720p":
            raise ValueError("影视级模仿线路二固定为 720p，请改用线路一生成 1080p")
        update_video_asset_phase(job_id, "motion_parameters_ready", resolution=resolution,
                                 ratio=ratio, motion=motion)
        if line == "2":
            # 默认线路二(WaveSpeed)：动作模仿两线路输入相同(人物图+驱动视频)，线路二成功率远高于线路一(HeyGen)，只有显式 line=1 才走 HeyGen
            # 线路二 WaveSpeed：人物图 + 驱动视频 → animate，直接出片，不走 HeyGen 的建 avatar 流程
            from . import wavespeed
            if not wavespeed.available():
                raise ValueError("线路二(WaveSpeed)未配置，请用线路一或联系管理员")
            video_result = wavespeed.generate_motion(image_file, reference_video_file, resolution, job_id=job_id)
        else:
            video_result = generate_heygen_motion_video(image_file, reference_video_file, resolution, ratio, duration, job_id, avatar=avatar)
            if not avatar:
                created_avatar = record_video_avatar((payload.get("_username") or "").strip(), image_file,
                                                      video_result.get("avatar_item_id"), video_result.get("avatar_group_id"))
        video_result.setdefault("duration", round(reference_duration, 2))
    else:
        video_result = generate_heygen_video(image_file, audio_file, resolution, ratio, motion)
    bgm_error = None
    if bgm_file and video_result.get("video_file"):
        try:
            update_video_asset_phase(job_id, "mixing_bgm")
            video_result["plain_video_file"] = video_result.get("video_file")
            mixed = mix_video_bgm(video_result["video_file"], bgm_file, payload.get("bgm_volume", 0.18))
            video_result["video_file"] = mixed
            video_result["video_url"] = _file_url(mixed)
        except Exception as e:
            bgm_error = str(e)[:200]
    # F4：口播模式（text/audio）可选自动字幕；失败不影响已生成的视频（保留原片 + 记录错误）
    subtitle_on = False
    subtitle_error = None
    subtitle_style = (payload.get("subtitle_style") or "white").strip()
    if subtitle_style not in _SUB_STYLES:
        subtitle_style = "white"
    subtitle_position = (payload.get("subtitle_position") or "bottom").strip()
    if subtitle_position not in _SUB_POSITIONS:
        subtitle_position = "bottom"
    if payload.get("subtitle") and mode in {"text", "audio"} and video_result.get("video_file"):
        try:
            update_video_asset_phase(job_id, "burning_subtitle")
            known = text if mode == "text" else None
            subtitled = burn_subtitle(video_result["video_file"], known_text=known, style_key=subtitle_style, job_id=job_id, position=subtitle_position)
            video_result["plain_video_file"] = video_result.get("video_file")
            video_result["video_file"] = subtitled
            video_result["video_url"] = _file_url(subtitled)
            subtitle_on = True
        except Exception as e:
            subtitle_error = str(e)[:200]
    return {
        "type": "video", "status": "done", "mode": mode,
        "image_file": video_result.get("image_file") or image_file,
        "image_url": video_result.get("image_url") or _file_url(video_result.get("image_file") or image_file),
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
        "subtitle_position": subtitle_position if subtitle_on else None,
        "subtitle_error": subtitle_error,
        "bgm_file": bgm_file,
        "bgm_volume": payload.get("bgm_volume", 0.18) if bgm_file else None,
        "bgm_error": bgm_error,
        "plain_video_file": video_result.get("plain_video_file"),
        "batch_id": payload.get("batch_id"), "batch_label": payload.get("batch_label"),
        "batch_index": payload.get("batch_index"), "batch_size": payload.get("batch_size"),
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
    return _faststart_video_file(fn)

def _cap_tryon_input(person_fp):
    """输入视频超 TRYON_MAX_INPUT_SEC 秒则截取前段(保证 5 分钟内出片)。返回 (路径, 原时长秒 or None)。
    -c copy 直接复制流不重编码(实测重编码会让 RunningHub 换装失败)；截取失败则退回原视频(宁慢不坏)。"""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(person_fp)], capture_output=True, text=True, timeout=30)
        dur = float((out.stdout or "0").strip() or 0)
    except Exception:
        return person_fp, None
    if dur <= 0 or dur <= TRYON_MAX_INPUT_SEC + 0.5:
        return person_fp, dur or None
    capped = pathlib.Path(str(person_fp).rsplit(".", 1)[0] + "_cap%ds.mp4" % TRYON_MAX_INPUT_SEC)
    try:
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(person_fp),
                        "-t", str(TRYON_MAX_INPUT_SEC), "-c", "copy", str(capped)], check=True, timeout=120)
    except Exception:
        return person_fp, dur
    return (capped if capped.is_file() and capped.stat().st_size > 0 else person_fp), dur


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
    person_fp, _orig_dur = _cap_tryon_input(person_fp)  # 超 10s 截取,保证 5 分钟内出片
    if _orig_dur and _orig_dur > TRYON_MAX_INPUT_SEC + 0.5:
        print("[tryon] 输入视频 %.1fs 超上限,截取前 %ds 保证时效" % (_orig_dur, TRYON_MAX_INPUT_SEC), flush=True)
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
    cover = _extract_first_frame_cover(video_file)
    ret = {
        "video_file": video_file,
        "video_url": video_url,
        "duration": seconds,
    }
    if cover:
        ret["image_file"] = cover
        ret["image_url"] = public_url(cover, "image/jpeg")
    return ret

def gen_tryon(payload):
    job_id = payload.get("_job_id")
    username = (payload.get("_username") or "").strip()
    # 无显式 line 时智能默认：有人物图(且无人物视频)→线路二(WaveSpeed,更稳)；有人物视频→线路一(RunningHub,给视频换装保动作)
    _tline = _tryon_line(payload)
    seconds = _tryon_seconds(payload, _tline)
    if _tline == "2":
        # 线路二 WaveSpeed：人物图 + 衣服图 → 换装展示视频（区别于线路一"给人物视频换装保留原动作"）
        from . import wavespeed
        if not wavespeed.available():
            raise ValueError("线路二(WaveSpeed)未配置，请用线路一或联系管理员")
        person_image_file = _save_data_file(payload.get("person_image_data") or payload.get("image_data"),
                                            "tryon_person_img", [".jpg", ".jpeg", ".png", ".webp"])
        if not person_image_file:
            raise ValueError("线路二换装请上传人物照片")
        clothes2 = _save_data_file(payload.get("clothes_data"), "tryon_cloth", [".jpg", ".jpeg", ".png", ".webp"])
        if not clothes2:
            raise ValueError("请上传衣服图")
        update_video_asset_phase(job_id, "queued", mode="tryon", text="换装",
                                 image_file=person_image_file, tryon_mode="clothes_only")
        wres = wavespeed.generate_tryon(person_image_file, clothes2, seconds, job_id=job_id)
        return {
            "type": "video", "status": "done", "mode": "tryon", "tryon_mode": "clothes_only",
            "person_image_file": person_image_file, "clothes_file": clothes2,
            "image_file": person_image_file, "image_url": _file_url(person_image_file),
            "video_file": wres.get("video_file"), "video_url": wres.get("video_url"),
            "provider": "wavespeed", "text": "换装", "duration": seconds, "seconds": seconds,
            "message": "换装完成",
        }
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
    text = (payload.get("text") or "").strip() or "换装换背景"
    cover_file = clothes_file or background_file
    update_video_asset_phase(job_id, "queued", mode="tryon", text=text,
                             reference_video_file=person_video_file, image_file=cover_file,
                             background_file=background_file, tryon_mode=tryon_mode)
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
        "image_file": video_result.get("image_file") or cover_file,
        "image_url": video_result.get("image_url") or (_file_url(video_result.get("image_file")) if video_result.get("image_file") else (_file_url(cover_file) if cover_file else None)),
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
                print("[video] 429 并发限流，%ds 后重试(%d/%d)" % (wait, attempt + 1, _xiaole_429_retries), flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError("视频接口失败: HTTP %s %s" % (e.code, detail))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # 瞬时网络抖动(SSL握手超时等)自动重试
            if attempt < _xiaole_429_retries:
                wait = min(30, 5 * (attempt + 1))
                print("[video] 网络异常，%ds 后重试(%d/%d): %s" % (wait, attempt + 1, _xiaole_429_retries, str(e)[:80]), flush=True)
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
    # 视频 CDN 多在海外(如 vidgen.x.ai)，国内服务器直连不通 → 复用法兰克福中转 /cdn/。
    # 但部分 CDN(如 seedance 的 update.asiot.top)国内直连可通、中转反而 404 → 中转失败后兜底直连原始 URL。
    plain_headers = {"User-Agent": "huangque-content/1.0"}
    candidates = [(url, plain_headers)]
    relay = os.environ.get("HEYGEN_RELAY_BASE", "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if relay and host and not host.endswith(".cn"):
        fetch = "%s/cdn/%s/%s" % (relay, host, parts.path.lstrip("/"))
        if parts.query:
            fetch += "?" + parts.query
        headers = dict(plain_headers)
        token = os.environ.get("HEYGEN_RELAY_TOKEN", "").strip()
        if token:
            headers["X-Relay-Token"] = token
        candidates.insert(0, (fetch, headers))
    # 下载中断(IncompleteRead/网络抖动)自动重试；中转候选耗尽后换直连候选
    data = None
    last_err = None
    for fetch_url, headers in candidates:
        if data is not None:
            break
        for attempt in range(_xiaole_dl_retries):
            try:
                req = urllib.request.Request(fetch_url, headers=headers)
                with urllib.request.urlopen(req, timeout=300) as r:
                    buf = r.read()
                if buf:
                    data = buf
                    break
                last_err = RuntimeError("下载为空")
            except Exception as e:
                last_err = e
                print("[video] 下载失败重试(%d/%d): %s" % (attempt + 1, _xiaole_dl_retries, str(e)[:100]), flush=True)
                time.sleep(3 * (attempt + 1))
    if data is None:
        raise RuntimeError("视频下载失败: %s" % (str(last_err)[:120] if last_err else "未知"))
    if not data:
        raise RuntimeError("视频下载失败")
    fn = "video/%s_%s.mp4" % (prefix, uuid.uuid4().hex)  # 不可猜键：防枚举
    _out_path(fn).write_bytes(data)
    return _faststart_video_file(fn)

def _xiaole_size_for_ratio(ratio):
    return XIAOLE_RATIO_SIZES.get(str(ratio or "").strip(), XIAOLE_RATIO_SIZES["9:16"])

def _is_xiaole_ratio_channel_error(msg):
    s = str(msg or "")
    return (("无可用渠道" in s) or ("当前模型暂无" in s) or ("暂无支持该视频参数的可用渠道" in s)
            or ("渠道不支持当前视频尺寸" in s))

def generate_xiaole_video(model, prompt, reference_images=None, size="720x1280", job_id=None, prefix="xiaole", duration=None):
    """统一 generations API：创建 → 轮询 → 下载。Grok(果肉)/Seedance(豆姐)/Omni(欧米) 共用。"""
    input_d = {"prompt": (prompt or "").strip(), "size": size or XIAOLE_RATIO_SIZES["9:16"]}   # 果肉/Grok 视频收 size，不收 aspect_ratio(#367)
    refs = _xiaole_build_refs(reference_images)
    if refs:
        input_d["mode"] = "image_to_video"   # 有参考图 → 图生视频
        input_d["reference_images"] = refs
        # 官方文档：图生视频建议 duration_seconds ≤10，否则超部分上游上限(疑之前 502 主因)。
        # 不传时 API 默认 15s（探针实测），Grok 图生示例即用 10s。
        input_d["duration_seconds"] = 10
    elif duration:
        # 文生视频固定时长渠道(如 omni-fast 只支持10s)：不传会 400"不支持该时长"。
        input_d["mode"] = "text_to_video"
        input_d["duration_seconds"] = duration
    try:
        create = _xiaole_request("POST", "/api/v1/generations", {"model": model, "input": input_d})
    except RuntimeError as e:
        m = str(e)
        if _is_xiaole_ratio_channel_error(m):
            raise RuntimeError("该视频渠道当前仅部分比例可用，请优先尝试 16:9（横屏）")
        if ("insufficient_user_quota" in m) or ("额度" in m) or ("媒体任务过多" in m):
            raise RuntimeError("该视频渠道暂时繁忙或维护中，请稍后再试")
        raise
    if create.get("code") not in (200, 0, None):
        msg = str(create.get("message") or create)[:200]
        if _is_xiaole_ratio_channel_error(msg):
            raise RuntimeError("该视频渠道当前仅部分比例可用，请优先尝试 16:9（横屏）")
        if ("额度" in msg) or ("任务过多" in msg):
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
            print("[video] %s model=%s status=%s" % (rid, model, status), flush=True)
            if job_id:
                update_video_asset_phase(job_id, "xiaole_" + (status or "running"))
            last = status
        vurl = _xiaole_pick_video_url(sdata.get("output"))
        if vurl:
            if job_id:
                update_video_asset_phase(job_id, "downloading", source_video_url=vurl)
            video_file = _download_xiaole_video(vurl, prefix)
            cover = _extract_first_frame_cover(video_file)
            ret = {"video_file": video_file, "video_url": _file_url(video_file),
                    "source_video_url": vurl, "model": model, "request_id": rid}
            if cover:
                ret["image_file"] = cover
                ret["image_url"] = public_url(cover, "image/jpeg")
            return ret
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
    ratio = (payload.get("ratio") or "9:16").strip()
    if ratio not in XIAOLE_RATIO_SIZES:
        ratio = "9:16"
    size = _xiaole_size_for_ratio(ratio)
    ref_images = None
    if channel in XIAOLE_IMAGE_CHANNELS:
        raw_refs = payload.get("reference_images") or None
        if raw_refs:
            ref_images = [_xiaole_ref_to_url(r) for r in raw_refs]
    label = {"grok": "果肉视频", "micro": "豆姐视频", "omni": "欧米视频"}.get(channel, model)
    if job_id:
        update_video_asset_phase(job_id, "queued", mode=channel, text=prompt, model=model)
    result = generate_xiaole_video(model, prompt, reference_images=ref_images, size=size, job_id=job_id, prefix=channel,
                                   duration=XIAOLE_CHANNEL_DURATION.get(channel))
    return {
        "type": "video", "status": "done", "mode": channel, "model": model, "text": prompt,
        "ratio": ratio,
        "video_file": result.get("video_file"), "video_url": result.get("video_url"),
        "source_video_url": result.get("source_video_url"),
        "image_file": result.get("image_file"),
        "image_url": result.get("image_url") or (public_url(result.get("image_file"), "image/jpeg") if result.get("image_file") else None),
        "phase": "done", "message": "%s生成完成" % label,
    }

HANDLERS = {"video": gen_video, "tryon": gen_tryon, "xiaole_video": gen_xiaole_video}
