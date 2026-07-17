# -*- coding: utf-8 -*-
import math
import tempfile

from .core import (
    AUDIO_OUT_DIR, CINEMATIC_GEN_DEADLINE, HEYGEN_API_BASE, HEYGEN_API_KEY, HEYGEN_POLL_INTERVAL,
    HEYGEN_TIMEOUT, OUT_DIR, VIDEO_GEN_DEADLINE, VIDEO_OUT_DIR, _env_positive_int, _file_url, _out_path, _resolve_out_file,
    _user_owns_output_file, adb, base64, closing, jdb, json, mimetypes, os, pathlib, public_url,
    re, subprocess, threading, time, urllib, uuid,
)

import random   # 429 退避重试的抖动：不加抖动，同一批 worker 退避后又会撞在一起

from .audio import gen_audio

VALID_VIDEO_MODES = {"text", "audio"}
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
GROK_VIDEO_PROVIDER = os.environ.get("GROK_VIDEO_PROVIDER", "xai").strip().lower()
XAI_GROK_MODELS = {"grok-imagine-video", "grok-imagine-video-1.5"}
XAI_GROK_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
XAI_GROK_RESOLUTIONS = {"480p", "720p"}

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

def validate_xiaole_video_payload(payload):
    """校验果肉/豆姐/欧米的公共入口；果肉官方线另按 xAI 参数收紧。"""
    if not isinstance(payload, dict):
        raise ValueError("请求体不是合法 JSON")
    cleaned = dict(payload)
    channel = str(cleaned.get("channel") or "grok").strip().lower()
    if channel not in XIAOLE_CHANNEL_MODELS:
        raise ValueError("未知视频渠道：%s" % channel)
    prompt = str(cleaned.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("请输入视频提示词")
    cleaned["channel"] = channel
    cleaned["prompt"] = prompt
    if channel != "grok" or GROK_VIDEO_PROVIDER == "xiaole":
        return cleaned

    operation = str(cleaned.get("operation") or "generate").strip().lower()
    if operation not in {"generate", "edit"}:
        raise ValueError("果肉视频操作类型不支持：%s" % operation)
    cleaned["operation"] = operation
    if operation == "edit":
        source = str(cleaned.get("reference_video_data") or "").strip()
        if not _is_valid_data_url(source, {"video/mp4"}):
            raise ValueError("请上传有效的 MP4 参考视频")
        duration = _probe_data_video_duration(source)
        if duration <= 0 or duration > 8.7:
            raise ValueError("xAI 官方视频编辑仅支持不超过 8.7 秒的参考视频")
        cleaned.update({"model": "grok-imagine-video", "reference_video_data": source,
                        "source_duration": duration, "reference_images": []})
        return cleaned

    model = str(cleaned.get("model") or "grok-imagine-video").strip()
    if model not in XAI_GROK_MODELS:
        raise ValueError("果肉官方模型不支持：%s" % model)
    refs = cleaned.get("reference_images") or []
    if not isinstance(refs, list):
        raise ValueError("reference_images 必须是数组")
    refs = [str(x or "").strip() for x in refs if str(x or "").strip()]
    if len(refs) > XIAOLE_MAX_REF:
        raise ValueError("xAI官方图生视频最多支持%d张参考图" % XIAOLE_MAX_REF)
    if model == "grok-imagine-video-1.5" and not refs:
        raise ValueError("Grok Video 1.5 仅支持图生视频，请先上传参考图")
    ratio = str(cleaned.get("ratio") or "16:9").strip()
    if ratio not in XAI_GROK_RATIOS:
        raise ValueError("果肉官方比例仅支持 " + "、".join(sorted(XAI_GROK_RATIOS)))
    try:
        duration = int(cleaned.get("duration") or 10)
    except (TypeError, ValueError):
        raise ValueError("果肉视频时长必须是1-15秒整数")
    if duration < 1 or duration > 15:
        raise ValueError("果肉视频时长必须是1-15秒整数")
    resolution = str(cleaned.get("resolution") or "720p").strip().lower()
    allowed_resolutions = XAI_GROK_RESOLUTIONS | ({"1080p"} if model == "grok-imagine-video-1.5" else set())
    if resolution not in allowed_resolutions:
        raise ValueError("%s 不支持分辨率 %s" % (model, resolution))
    cleaned.update({
        "model": model, "ratio": ratio, "duration": duration,
        "resolution": resolution, "reference_images": refs,
    })
    return cleaned

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
    return bool(decoded)


def _normalize_audio_file_ref(audio_file, username=None):
    raw = str(audio_file or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("audio_file 不能为空")
    fp = _resolve_out_file(raw)
    if not fp:
        raise ValueError("音频文件不存在：%s" % audio_file)
    ext = fp.suffix.lower()
    if ext not in {".mp3", ".wav", ".m4a"}:
        raise ValueError("audio_file 仅支持 mp3、wav、m4a")
    try:
        rel = fp.resolve().relative_to(OUT_DIR.resolve()).as_posix()
    except Exception:
        raise ValueError("audio_file 必须位于 content_out 目录内")
    if username and not _user_owns_output_file(username, rel):
        raise ValueError("音频文件不存在或不属于当前账号")
    return rel


def _probe_data_video_duration(data_url):
    """用服务端 ffprobe 校验真实媒体时长，不信任浏览器提交的 duration。"""
    encoded = str(data_url).split(",", 1)[1]
    raw = base64.b64decode(encoded, validate=True)
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as fh:
            fh.write(raw)
            path = fh.name
        proc = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ], capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            raise ValueError("参考视频无法解析，请确认是有效的 MP4 文件")
        return float((proc.stdout or "0").strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError("参考视频无法解析，请确认是有效的 MP4 文件")
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
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
        raise ValueError("mode 仅支持 text/audio")

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
        audio_file = (payload.get("audio_file") or "").strip()
        if not audio_data and not audio_file:
            raise ValueError("audio_data 或 audio_file 不能为空")
        if audio_data and not _is_valid_data_url(audio_data, VALID_AUDIO_MIMES):
            raise ValueError("audio_data 不是有效的音频文件")
        if audio_file:
            audio_file = _normalize_audio_file_ref(audio_file, username=username)
    if avatar_id and username:
        get_video_avatar(username, avatar_id)

    ratio = (payload.get("ratio") or "9:16").strip()
    if ratio not in VALID_VIDEO_RATIOS:
        raise ValueError("ratio 仅支持 9:16、16:9、1:1、4:5、5:4")
    resolution = (payload.get("resolution") or "1080p").strip().lower()
    if resolution not in VALID_VIDEO_RESOLUTIONS:
        raise ValueError("resolution 仅支持 720p、1080p")
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
    if mode == "audio":
        cleaned["audio_file"] = audio_file
        cleaned["audio_data"] = audio_data
    cleaned["bgm_data"] = bgm_data
    cleaned["bgm_volume"] = bgm_volume
    cleaned.pop("duration", None)
    cleaned.pop("line", None)   # 动作模仿不再有线路，别把老前端传来的 line 写进 payload 混淆历史记录
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

def get_resumable_xai_request(job_id):
    if not job_id:
        return None
    with closing(adb()) as c:
        row = c.execute(
            """SELECT provider_video_id, model, phase, status
               FROM video_assets WHERE job_id=?""",
            (job_id,),
        ).fetchone()
    if not row or not row["provider_video_id"]:
        return None
    phase = str(row["phase"] or "")
    if not (phase.startswith("xai_") or phase == "downloading"):
        return None
    return {
        "request_id": row["provider_video_id"],
        "model": row["model"] or "grok-imagine-video",
        "phase": phase,
        "status": row["status"],
    }


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
        if e.code == 429:
            # 429 单独成一类：请求被【瞬间拒绝、未被处理、未计费】，可以安全重发。
            # 其余错误(超时/RST/5xx)不行——HeyGen 提交即扣 credit，那些可能已经计费了。
            # Retry-After 是 HeyGen 明确告诉我们该等多久（官方文档：「Check the Retry-After
            # response header for the number of seconds to wait before retrying」）——
            # 听它的，比我们瞎猜指数退避准。
            err = HeyGenRateLimited("HeyGen 限流(429): %s" % detail)
            try:
                err.retry_after = float((e.headers or {}).get("Retry-After") or 0)
            except (TypeError, ValueError):
                err.retry_after = 0.0
            raise err from e
        raise RuntimeError("HeyGen接口失败: HTTP %s %s" % (e.code, detail)) from e
    except OSError as e:
        # URLError / socket.timeout(TimeoutError) / ssl.SSLError / ConnectionError —— 传输层瞬时错误。
        # 归为 HeyGenNetworkError：幂等 GET(轮询/下载)可安全重试；提交 POST 照旧穿透不重发。
        # 注意「read timeout」发生在 r.read() 阶段，是 TimeoutError 而非 URLError，
        # 原来的 `except URLError` 漏了它，会裸抛「The read operation timed out」——正是丢片主因(#605)。
        detail = str(getattr(e, "reason", e))[:300]
        print("[heygen] FAIL %s %s -> network %s" % (method, path, detail), flush=True)
        raise HeyGenNetworkError("HeyGen接口网络失败: %s" % detail) from e
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


# 参考视频上传前压到 720p/2Mbps。用户传的是手机原片（实测 1920×1080 / 15.4 Mbps / 24MB），
# 而 HeyGen 的成片只有 720p / 5~7MB —— 我们推上去的码率是拿回来的 3.5 倍。
#
# 参考视频只用来提取动作：HeyGen 的提示词里写死了「Follow the reference video ONLY for body
# movement, pose, timing, gestures, camera motion… Do NOT copy the reference video person's
# appearance」，人物样貌全部来自 avatar 图。720p/2Mbps 传递姿态绰绰有余（成片本来就只有 720p）。
# 2026-07-11 用同一 avatar、同一段素材做过原片/压缩片对比生成：姿态、身份、画质无差异，
# 压缩片成片无伪影无变形（差异只在表情/构图，那是 cinematic 生成本身的随机性）。
#
# 为什么非压不可 —— 瓶颈是出境隧道，不是 HeyGen（10 路并发实测无 429、生成不降速）：
#   隧道上行 ~1.1 MB/s，上传硬超时 240s
#   23MB × N 路 → 约 21N 秒：10 路要 210s，实测挂了 1/10（撞 240s 超时）
#   3MB  × N 路 → 约  3N 秒：10 路只要 30s
# 不压，motion 的 worker 就被带宽死死卡在 3~4；压完，带宽不再是约束。
MOTION_REF_MAX_LONG_SIDE = int(os.environ.get("MOTION_REF_MAX_LONG_SIDE", "1280") or 1280)
MOTION_REF_BITRATE_K = int(os.environ.get("MOTION_REF_BITRATE_K", "2000") or 2000)
MOTION_REF_SHRINK_MIN_BYTES = int(os.environ.get("MOTION_REF_SHRINK_MIN_BYTES", "6291456") or 6291456)


def _shrink_reference_video(ref_path):
    """参考视频上传前压到 720p/2Mbps。已经够小的原样返回。

    压缩是优化而非正确性前提：ffmpeg 缺失、转码失败、产物为空——一律回退原片上传，
    绝不能因为压不动就让整个任务失败（那是把一个省钱的优化变成新的故障源）。
    """
    path = pathlib.Path(ref_path)
    try:
        if path.stat().st_size <= MOTION_REF_SHRINK_MIN_BYTES:
            return path
    except OSError:
        return path
    out = path.parent / ("motion_ref_small_%d.mp4" % int(time.time() * 1000))
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
        # 长边收到 1280：竖屏 1080×1920 → 720×1280，横屏 1920×1080 → 1280×720
        "-vf", "scale=w=%d:h=%d:force_original_aspect_ratio=decrease" % (
            MOTION_REF_MAX_LONG_SIDE, MOTION_REF_MAX_LONG_SIDE),
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", "%dk" % MOTION_REF_BITRATE_K,
        "-maxrate", "%dk" % int(MOTION_REF_BITRATE_K * 1.2),
        "-bufsize", "%dk" % (MOTION_REF_BITRATE_K * 2),
        "-an",                                    # 动作参考用不到音轨
        str(out),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=180, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not out.exists() or out.stat().st_size <= 0:
            raise RuntimeError("产物为空")
    except Exception as e:
        print("[heygen] 参考视频压缩失败，改用原片上传: %s" % str(e)[:120], flush=True)
        return path
    print("[motion] 参考视频 %.1fMB → %.1fMB" % (
        path.stat().st_size / 1048576.0, out.stat().st_size / 1048576.0), flush=True)
    return out


def _extract_reference_audio(ref_path):
    """把参考视频的原声抽出来，返回音频文件的【相对路径】；没有音轨或抽失败 → None。

    ⚠️ 必须在【剥音轨之前】调用 —— 剥完就没了。

    抽不出来不算错：动作模仿本来就不依赖声音，静音成片仍然是可用的成片。
    """
    fp = _resolve_out_file(ref_path) if not pathlib.Path(str(ref_path)).is_absolute() else pathlib.Path(str(ref_path))
    if not fp or not pathlib.Path(fp).is_file():
        return None
    out_rel = "audio/motion_src_%s.m4a" % uuid.uuid4().hex
    out_fp = _out_path(out_rel)
    out_fp.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(fp),
           "-vn", "-c:a", "aac", "-b:a", "192k", str(out_fp)]
    try:
        subprocess.run(cmd, check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not out_fp.exists() or out_fp.stat().st_size <= 0:
            raise RuntimeError("产物为空")
    except Exception as e:
        # 参考视频本来就没有音轨（很常见）也会走到这里 —— 不是错误
        print("[motion] 参考视频没有可用音轨（或抽取失败），成片将无声: %s" % str(e)[:90], flush=True)
        try:
            out_fp.unlink()
        except Exception:
            pass
        return None
    return out_rel


def _strip_audio(ref_path):
    """把参考视频的音轨剥掉再上传。

    HeyGen 的 cinematic_avatar 【只看画面】—— 它不会用参考视频的声音。音轨对它是纯浪费：
    要经过我们那条 ~1.5 MB/s 的出境隧道推上去。剥掉能省 5~15% 的上传量，而且 100% 无损失。

    ⚠️ 只重封装（-c copy），不重编码 —— 画质一帧不动，几十毫秒的事。
    失败就原样返回：这是优化，不是正确性前提，绝不能因为剥不动就让任务失败。

    ⚠️ 路径必须解析：video_files 里存的是【相对 OUT_DIR 的路径】（如 "video/xxx.mp4"），
    而服务 CWD ≠ OUT_DIR。输入要 _resolve_out_file 解析到绝对路径、输出要 _out_path 落到
    OUT_DIR，返回相对路径 —— 和 _extract_reference_audio / 旧 _shrink_motion_reference 一致。
    （否则 ffmpeg 按 CWD 找不到输入，每次都静默回退，剥音轨形同虚设。）
    """
    fp = _resolve_out_file(ref_path)
    if not fp:
        return ref_path
    out_rel = "video/motion_ref_mute_%s.mp4" % uuid.uuid4().hex
    out_fp = _out_path(out_rel)
    out_fp.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(fp),
           "-an", "-c:v", "copy", "-movflags", "+faststart", str(out_fp)]
    try:
        subprocess.run(cmd, check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not out_fp.exists() or out_fp.stat().st_size <= 0:
            raise RuntimeError("产物为空")
    except Exception as e:
        print("[motion] 参考视频剥音轨失败，原样上传: %s" % str(e)[:100], flush=True)
        try:
            out_fp.unlink()
        except Exception:
            pass
        return ref_path
    try:
        before, after = fp.stat().st_size, out_fp.stat().st_size
        print("[motion] 剥音轨 %.1fMB → %.1fMB（省 %.0f%%）"
              % (before / 1048576.0, after / 1048576.0, 100.0 * (before - after) / max(before, 1)), flush=True)
    except OSError:
        pass
    return out_rel


def _mux_original_audio(video_file, audio_rel):
    """把参考视频的原声合进成片。返回新的相对路径；失败 → 原样返回（成片仍可用，只是无声）。

    HeyGen 的 cinematic 成片【本身没有声音】。用户上传的参考视频是有声的，成片配回原声，
    观感上才是「同一条片子，只是换了个人演」。

    时长对不齐是常态：成片是 4~15 秒（自适应向上取整），原声是参考视频的实际长度。
    -shortest 以短的为准 —— 宁可音频末尾少一点，也不要视频尾巴上挂一段黑屏/静止。
    """
    vfp = _resolve_out_file(video_file)
    afp = _resolve_out_file(audio_rel)
    if not vfp or not afp:
        return video_file
    out_rel = "video/cine_snd_%s.mp4" % uuid.uuid4().hex
    out_fp = _out_path(out_rel)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(vfp), "-i", str(afp),
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy",            # 画面一帧不动
           "-c:a", "aac", "-b:a", "192k",
           "-shortest", "-movflags", "+faststart", str(out_fp)]
    try:
        subprocess.run(cmd, check=True, timeout=180, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if not out_fp.exists() or out_fp.stat().st_size <= 0:
            raise RuntimeError("产物为空")
    except Exception as e:
        print("[motion] 合入原声失败，保留无声成片: %s" % str(e)[:110], flush=True)
        try:
            out_fp.unlink()
        except Exception:
            pass
        return video_file          # ⚠️ 回退：宁可无声，也不能因为配音失败就把成片丢了
    print("[motion] 已合入参考视频的原声", flush=True)
    return out_rel


def _shrink_motion_reference(reference_video_file):
    """落盘后立刻压缩参考视频，返回新的相对路径（压不动就原样返回原路径）。

    放在【线路分发之前】，两条路都受益：
      * HeyGen    ——原始字节要推过隧道(上行仅 ~1.1 MB/s，硬超时 240s)，压缩是解开并发天花板的关键
      * WaveSpeed ——素材先传 COS 再把 URL 给对方自己拉，不占隧道；压缩省的是 COS 上传与流量

    压完删原片：这个文件刚从 payload 写出来，此刻还没有任何东西引用它（video_assets 记的是
    本函数的返回值），删掉是安全的，省下 8 倍磁盘。删失败不算错——留给每日 GC 收拾。
    """
    fp = _resolve_out_file(reference_video_file)
    if not fp:
        return reference_video_file
    small = _shrink_reference_video(fp)
    if small == fp:
        return reference_video_file      # 本来就够小，或压缩失败已回退原片
    try:
        fp.unlink()
    except OSError:
        pass
    return "video/" + small.name

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
# 建形象轮询上限。线上跑通的 photo avatar 都在 12~42s 内就绪，卡住的才会拖满旧的 900s 上限
# —— 那些永远不会成，只是白占 worker、把退点拖到 15 分钟后。收到 120s：成功的有 3 倍余量，
# 真卡住的 2 分钟快速失败并退点。可用 HEYGEN_PHOTO_AVATAR_DEADLINE 覆盖。
HEYGEN_PHOTO_AVATAR_DEADLINE = _env_positive_int("HEYGEN_PHOTO_AVATAR_DEADLINE", 120)

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
    deadline = time.time() + min(HEYGEN_TIMEOUT, HEYGEN_PHOTO_AVATAR_DEADLINE)
    started = time.time()
    last_status = ""
    while time.time() < deadline:
        # 401/配额之类的 HTTP 错误直接上抛，不掩盖(被「继续轮询」吃掉会伪装成超时)。但【瞬时网络
        # 抖动】(隧道 read timeout/SSL)要重试——轮询是幂等 GET、建形象免费，一次抖动不该判死建形象
        # (yuanzhi 的 read timeout 就死在这，#611 只包了上传/创建、漏了这步 poll)。同 #607 的 poll。
        try:
            status, moderation = _heygen_look_status(avatar_item_id, avatar_group_id, direct=direct)
        except HeyGenNetworkError as e:
            print("[heygen] avatar look 轮询网络抖动，%ds 后重试: %s" % (HEYGEN_POLL_INTERVAL, str(e)[:100]), flush=True)
            time.sleep(HEYGEN_POLL_INTERVAL)
            continue
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

# 身份约束。无论用户的创意提示词写什么，都要拼上这段 —— 否则 HeyGen 会把参考视频里那个人
# 的长相抄进成片，用户拿到的就不是"自己"了。用户只负责写创意，身份不由用户把关。
CINEMATIC_IDENTITY_GUARD = (
    " CRITICAL: Keep each avatar person's exact identity, face, hairstyle, body shape, skin tone "
    "and clothing exactly as in their avatar photo. Do NOT copy any person's appearance, body "
    "proportions or outfit from the reference video. Smooth realistic motion, no text, no logo, "
    "no extra people beyond the given avatars."
)

# ============ 动作模仿的固定提示词：照抄线上跑通的那一条 ============
#
# 2026-07-13：HeyGen 的内容审核在拦我们的动作模仿。它的网页上写
#     "Your content was flagged by our moderation system. Please try different images or
#      prompts. No credits charged."
# 而【API 一个字都不给】（v1/video_status.get 的 error 是 null，v3/videos 只有 4 个字段）。
#
# 把「真的被 HeyGen 判失败」和「我们自己的隧道上传超时」分开统计之后，数据很干净：
#
#     玩法             提示词        HeyGen 判失败   成片
#     单人动作模仿      写死的英文          5          1
#     双人动作模仿      写死的英文          2          0
#     开放式生成        用户写的中文        0          5
#     旧版(开放式)      用户写的中文        1         10
#
# 被判失败的【几乎全是写死英文提示词的动作模仿】；用户自己写中文的开放式一条都没被判过失败
# （它们的失败全是参考视频上传撞 240s 硬超时，压根没提交到 HeyGen）。
#
# 英文那两段里的
#     "The output must look like the avatar person ... not the reference person"
#     "Use these two avatars to replace the two people in the reference video"
# 是换脸/深度伪造的教科书措辞。审核模型是英文的 —— 中文对它半透明，英文它读得懂。
#
# 所以这里【原样照抄】线上跑通的 #2173：
#     提示词  「用这个人物形象模仿视频里面的动作」（用户写的，成片 383s）
#     分辨率  1080p        比例 9:16（跟随参考视频）    时长 11s（自适应，参考视频 10.9s）
#     润色    关           参考视频 576x1024 竖版
#
# ⚠️ 身份约束（CINEMATIC_IDENTITY_GUARD）【不要改】：#2173 发出去的是「这句中文 + 那段英文
# 约束」，它带着 "from the reference video" 也照样过了。所以约束不是触发点，正文才是。
# 我一度想连约束一起重写，那是错的 —— 会把唯一一个已知能过的配置也改掉。

# 单人动作模仿的固定提示词（kongli 给的，2026-07-14）。逐字照放。
#
# ⚠️ 它是【自包含】的 —— 自带 "CRITICAL: Keep the avatar person's exact identity..." 和
# "no extra people"。所以【不再拼】CINEMATIC_IDENTITY_GUARD，否则同样的话说两遍。
#
# ⚠️ 风险（已跟 kongli 说清楚，他确认要换）：里面的
#     "Do NOT copy the reference video person's appearance"
#     "not the reference person"
# 正是线上那版英文提示词的措辞 —— 战绩【5 败 1 成】，被 HeyGen 的内容审核拦下
# （网页原话 "Your content was flagged by our moderation system"）。
# 被它换掉的中文版是照抄 #2173 的，那是【唯一验证过能过审核】的配置。真挂了，先看这里。
MOTION_PROMPT_BASE = (
    "Create a realistic cinematic vertical video of the same person from the avatar photo. "
    "Follow the uploaded reference video ONLY for body movement, pose, timing, gestures, "
    "facial expression rhythm, framing and camera motion. CRITICAL: Keep the avatar person's "
    "exact identity, face, hairstyle, body shape, skin tone and clothing. Do NOT copy the "
    "reference video person's appearance, body proportions or outfit. The output must look like "
    "the avatar person performing the reference motion, not the reference person. Smooth "
    "realistic motion, no text, no logo, no extra people."
)
MOTION_PROMPT = MOTION_PROMPT_BASE   # 【不拼】guard —— 它自带

# 双人：同一路子的中文。⚠️ 尚未实测（双人的英文版是 0 成 2 败）。
# 双人（已从前端下掉）。同样做成【自包含】—— 开回来时不该再依赖外部拼接。
DUO_MOTION_PROMPT_BASE = "用这两个人物形象模仿视频里面的动作" + CINEMATIC_IDENTITY_GUARD
DUO_MOTION_PROMPT = DUO_MOTION_PROMPT_BASE


def _heygen_create_cinematic_video(avatar_item_id, reference_asset_id, ratio, resolution, duration,
                                   prompt=None, direct=False, enhance_prompt=False):
    # avatar_id 是 1~3 个 look 的数组 —— 多个 look 会让 HeyGen 在【同一个镜头】里同时出现多个人，
    # 不是生成多条视频。所以 3 个形象仍然只扣 1 条视频的钱。
    ids = [i for i in (avatar_item_id if isinstance(avatar_item_id, (list, tuple)) else [avatar_item_id]) if i]
    payload = {
        "type": "cinematic_avatar",
        "title": "follow_reference_motion",
        "prompt": prompt or MOTION_PROMPT,
        "avatar_id": ids,
        "aspect_ratio": _heygen_cinematic_ratio(ratio),
        "resolution": resolution,
        "duration": duration,
        # 自动润色：HeyGen 把简短提示词扩写成更丰富的描述。默认关——它可能把用户的意图改跑偏。
        "enhance_prompt": bool(enhance_prompt),
    }
    # references 可选、可多个（文档：用来 steer 风格/动作/构图，非必填）。
    # 单个 asset_id 也收，兼容老调用（动作模仿那条路径传的就是单个）。
    refs = reference_asset_id if isinstance(reference_asset_id, (list, tuple)) else (
        [reference_asset_id] if reference_asset_id else [])
    refs = [{"type": "asset_id", "asset_id": a} for a in refs if a]
    if refs:
        payload["references"] = refs
    body = json.dumps(payload, ensure_ascii=False).encode()
    data = _heygen_request_json("POST", "/videos", body, {
        "Content-Type": "application/json",
    }, timeout=90, direct=direct)
    video_id = ((data.get("data") or {}).get("video_id") or "").strip()
    if not video_id:
        raise RuntimeError("HeyGen未返回video_id: %s" % json.dumps(data, ensure_ascii=False)[:500])
    return video_id

class HeyGenRateLimited(RuntimeError):
    """HeyGen 429。请求被【瞬间拒绝、未被处理、未计费】—— 这是唯一可以安全重发的失败。

    .retry_after: HeyGen 在响应头里明确告诉我们该等多久（秒）。没有就是 0。

    2026-07-12 实测（20 路同时提交：10 口播 + 10 剧情视频）：
        7 个 429，全部在 1.1 秒内瞬间返回；错误码是 `rate_limit_exceeded`，
        原文「please reduce the RATE to call this api」—— 这是【速率】墙，不是并发墙。
        而被接受的 13 条【全部成功出片】，说明并发本身没到顶（文档说的 10 并没有拦我们）。

    与之相对：超时 / RST / 5xx 【绝不能】重发 —— HeyGen 提交即扣 credit，
    那些失败发生在请求已经送达之后，视频可能已经在生成、钱已经花了。
    （同一条纪律见 HeyGenBilledError，以及 egress.post_json 的 _pre_delivery_failure。）
    """


class HeyGenNetworkError(RuntimeError):
    """HeyGen 传输层【瞬时】网络错误：连接被拒 / 超时(read timeout) / SSL EOF / RST。

    与 429、与「提交后失败」都不同 —— 这类错误只说明这一次 HTTP 传输没走通，
    【幂等 GET】(轮询查状态、下载成片)可以安全重试：GET 不产生计费、不改状态。

    ⚠️ 但它仍是 RuntimeError 的子类，所以【提交 POST】路径行为不变 —— `_heygen_retry_429`
    只认 HeyGenRateLimited，HeyGenNetworkError 会照旧穿透 → HeyGenBilledError（不重发）。
    提交遇网络错等于「可能已计费」，绝不能因为它长得像瞬时错误就重发。
    只有轮询/下载的调用方会显式 catch 它来重试。

    背景(#605)：egress 隧道一天 flap 5 次，每次抖动撞上一个正在轮询的任务，就把
    「已提交、成片已在 HeyGen 生成好」的任务判死、白烧一次提交费（cinematic 每条约 $7）。
    今日单日 5 条因此丢片（已手动 re-poll 全部挽回）。根因就是轮询/下载对网络错零重试。
    """


# 轮询/下载成片的网络韧性：幂等 GET，瞬时抖动退避重试。不计费、可安全重试，和提交(POST)本质不同。
HEYGEN_NET_RETRIES = int(os.environ.get("HEYGEN_NET_RETRIES", "4") or 4)


def _heygen_retry_net(fn, what=""):
    """对【建形象】的提交重试瞬时网络错误。⚠️ 只能用在建形象上，别往视频提交上抄。

    视频的提交 POST 绝不重发（见 HeyGenBilledError）：HeyGen 提交即计费，重发 = 同一条片子
    付两次 $7。建形象不一样 —— **实测免费**（2026-07-12：连建 6 个形象，plan_credit 和 api
    两个池都是 0 扣减）。所以「万一上一次其实已经送达」的代价只是在 HeyGen 上多留一个孤儿
    形象（我们的库里只记返回的那个 id，用户看不到多出来的），不是钱。

    这就是为什么这里可以不去纠结 egress._pre_delivery_failure 那套「投递前/投递后」的判据：
    那套判据是为非幂等的**计费** POST 定的，宁可失败也不重复扣钱。建形象没有那个代价。

    背景：10 路并发实测，出境隧道扛不住 10 个并发 TLS 握手，1/10 挂在 handshake timeout。
    握手超时意味着请求根本没发出去，用户却看到「建形象失败」并被退了 5 点 —— 什么都没发生。
    """
    last = None
    for i in range(HEYGEN_NET_RETRIES):
        try:
            return fn()
        except HeyGenNetworkError as e:
            last = e
            if i == HEYGEN_NET_RETRIES - 1:
                break
            delay = min(20.0, 2.0 * (2 ** i)) * (0.7 + random.random() * 0.6)   # 必须抖动
            print("[heygen] %s 网络抖动，重试(%d/%d) %.1fs 后: %s"
                  % (what, i + 1, HEYGEN_NET_RETRIES, delay, str(e)[:120]), flush=True)
            time.sleep(delay)
    raise last


def _heygen_read_retry(open_fn, what):
    """打开并读取一个【幂等 GET】(下载成片)，对传输层瞬时网络错误退避重试，返回字节。

    open_fn: 无参、每次调用返回一个新的 response 上下文管理器（每次重试都重新 open，
             不复用可能已半死的连接）。
    只 catch OSError（URLError / socket.timeout(TimeoutError) / ssl.SSLError / ConnectionError
    都是它的子类）—— HTTP 状态错误不在此列（那是上游明确响应，不该盲重）。
    """
    last = None
    for i in range(HEYGEN_NET_RETRIES):
        try:
            with open_fn() as r:
                data = r.read()
            if data:
                return data
            last = RuntimeError("下载内容为空")
        except OSError as e:
            last = e
            print("[heygen] %s 网络抖动，重试(%d/%d): %s"
                  % (what, i + 1, HEYGEN_NET_RETRIES, str(getattr(e, "reason", e))[:120]), flush=True)
        if i < HEYGEN_NET_RETRIES - 1:
            time.sleep(2.0 * (i + 1))
    raise HeyGenNetworkError("%s 多次网络失败: %s" % (what, str(getattr(last, "reason", last))[:150]))


# 429 退避重试。不重试的话，一次突发就把用户的任务判死退点、白等几分钟——
# 而实测 20 路里有 13 路是过的，被拒的那 7 个退避几秒重发几乎必成。
HEYGEN_429_TRIES = int(os.environ.get("HEYGEN_429_TRIES", "6") or 6)
HEYGEN_429_MAX_WAIT = int(os.environ.get("HEYGEN_429_MAX_WAIT", "120") or 120)


def _heygen_retry_429(fn, what=""):
    """只对 429 退避重试；其它异常原样抛出（可能已计费，绝不能重发）。"""
    waited = 0.0
    for i in range(HEYGEN_429_TRIES):
        try:
            return fn()
        except HeyGenRateLimited as e:
            if i == HEYGEN_429_TRIES - 1:
                raise
            # 优先听 HeyGen 的 Retry-After（官方文档明说要读它）；它没给才自己指数退避。
            hinted = getattr(e, "retry_after", 0) or 0
            base = hinted if hinted > 0 else min(20.0, 2.0 * (2 ** i))
            # 抖动：不加的话，同一批被拒的 worker 会在同一刻一起重发——那正是 429 的成因，
            # 等于把突发原样搬到了退避之后。哪怕 Retry-After 给了确切秒数也要抖。
            delay = base * (0.7 + random.random() * 0.6)
            if waited + delay > HEYGEN_429_MAX_WAIT:
                raise
            waited += delay
            print("[heygen] %s 撞 429，退避重试(%d/%d) 等 %.1fs%s"
                  % (what, i + 1, HEYGEN_429_TRIES, delay,
                     "（Retry-After=%.0fs）" % hinted if hinted > 0 else ""), flush=True)
            time.sleep(delay)


# ============ HeyGen 账号级并发闸（削峰用，不是挡并发） ============
# 官方文档（Usage Limits）说 Pay-As-You-Go 的 "Max Concurrent Video Jobs" = 10。
# 【实测证明这不是硬限制】——2026-07-12 跑 20 路并发（10 口播 + 10 剧情视频同时生成）：
#     20/20 全部成功出片，零降速（口播平均 114s，而单条基线是 104s）
#     10 并发 133s / 13 并发 169s / 20 并发 114s —— 前两轮的「降速」是噪声，不是并发导致的
# 所以 HeyGen 的渲染容量远大于 20，那个 10 拦不住我们。
#
# 真正的限制是【提交突发】：20 个 POST 同一瞬间打出去 → 8 个 429（rate_limit_exceeded，
# 「please reduce the RATE to call this api」）。而退避 1.7~2.5 秒重发，一次就全过。
# 兜住它的是 _heygen_retry_429，不是这个信号量。
#
# 那这个信号量还留着干嘛？—— 削峰。它把同时在飞的请求数摊平（21 = 口播10 + 剧情10 + 建形象1），
# 顺带降低撞 429 的概率，是重试之外的一层保险。真要放开，改 env 即可，不用动代码。
#
# 槽只在【生成期间】持有（建视频 → 轮询出片）。上传素材、查 look 状态不占槽。
# 中转(泽龙)转发的是同一个账号，所以中转路径同样要占槽 —— 不占就等于绕过了闸。
HEYGEN_MAX_CONCURRENCY = int(os.environ.get("HEYGEN_MAX_CONCURRENCY", "21") or 21)
_heygen_gen_sem = threading.BoundedSemaphore(HEYGEN_MAX_CONCURRENCY)


class heygen_slot(object):
    """占一个 HeyGen 账号级并发槽。用法： with heygen_slot("口播"): create... poll..."""

    def __init__(self, label=""):
        self.label = label

    def __enter__(self):
        t0 = time.time()
        _heygen_gen_sem.acquire()
        waited = time.time() - t0
        if waited > 1:
            print("[heygen] %s 等并发槽 %.0fs（账号级上限 %d）" % (self.label, waited, HEYGEN_MAX_CONCURRENCY), flush=True)
        return self

    def __exit__(self, *exc):
        _heygen_gen_sem.release()
        return False


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


# 电影化身走 HeyGen 时的轮询死线 —— 20 分钟（kongli 2026-07-14，原来跟全站的 15 分钟走）。
#
# 数就定在 core.CINEMATIC_GEN_DEADLINE，这里【只是引用】—— reaper 对 cinematic 的宽限
# (CINEMATIC_REAPER_GRACE) 是拿它 +300 算出来的。两边各写一个字面量，就会重演「引擎死线比
# reaper 宽限还长 → reaper 先把活着的任务杀了」那个老 bug。
HEYGEN_MOTION_DEADLINE = CINEMATIC_GEN_DEADLINE


def _heygen_poll_video(video_id, direct=False, deadline_s=None):
    deadline = time.time() + (deadline_s or HEYGEN_TIMEOUT)
    last_status = ""
    net_fails = 0
    while time.time() < deadline:
        try:
            data = _heygen_request_json("GET", "/videos/" + urllib.parse.quote(video_id), timeout=90, direct=direct)
        except HeyGenNetworkError as e:
            # 轮询是幂等 GET、不计费——隧道瞬时抖动不该判死任务、白烧提交费(#605)。
            # 等下一轮重试；deadline 仍是总上限，不会无限转。provider 明确 failed 才判失败(见下)。
            net_fails += 1
            print("[heygen] poll video_id=%s 网络抖动(%d)，%ds 后重试: %s"
                  % (video_id, net_fails, HEYGEN_POLL_INTERVAL, str(e)[:120]), flush=True)
            time.sleep(HEYGEN_POLL_INTERVAL)
            continue
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
    # 幂等 GET 下载成片：瞬时网络错误退避重试（不计费、可安全重试，#605）
    data = _heygen_read_retry(lambda: urllib.request.urlopen(req, timeout=360), "成片下载")
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
    # 幂等 GET 下载成片：瞬时网络错误退避重试（不计费、可安全重试，#605）
    data = _heygen_read_retry(lambda: _heygen_direct_opener().open(req, timeout=360), "成片直连下载")
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
    # 素材上传对瞬时网络错误重试：上传不计费(计费在 create-video)，重试安全。隧道抖动一下不该
    # 让整条口播失败(fang 的 cinematic/口播上传 240s 超时同源)。见 _heygen_retry_net。
    image_asset_id = _heygen_retry_net(lambda: _heygen_upload_asset(image_fp, direct=True), "口播传图")
    audio_asset_id = _heygen_retry_net(lambda: _heygen_upload_asset(audio_fp, direct=True), "口播传音")
    with heygen_slot("口播直连"):   # 账号级并发上限 10，三个池共用；超了在本地排队，不让 HeyGen 甩 429
        # 429 退避重试：请求被瞬间拒绝、未计费，是唯一可以安全重发的失败。
        # 不重试的话，一次突发就把用户的任务判死退点、白等几分钟。
        video_id = _heygen_retry_429(
            lambda: _heygen_create_video(image_asset_id, audio_asset_id, resolution, ratio, motion, direct=True),
            "口播直连")
        # ↓ 此刻已计费。之后任何失败都不能回退中转重发（同一账号，会再付一次），见 HeyGenBilledError
        try:
            info = _heygen_poll_video(video_id, direct=True, deadline_s=VIDEO_GEN_DEADLINE)
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
    # 素材上传对瞬时网络错误重试(不计费、安全，同直连)
    image_asset_id = _heygen_retry_net(lambda: _heygen_upload_asset(image_fp), "口播中转传图")
    audio_asset_id = _heygen_retry_net(lambda: _heygen_upload_asset(audio_fp), "口播中转传音")
    # 中转(泽龙)转发的是同一个 HeyGen 账号，一样占账号的并发额度 —— 不占槽就等于绕过了闸
    with heygen_slot("口播中转"):
        video_id = _heygen_retry_429(
            lambda: _heygen_create_video(image_asset_id, audio_asset_id, resolution, ratio, motion), "口播中转")
        # 中转也用同一个死线。原来它回落到 HEYGEN_TIMEOUT(1200s)，比 reaper 对口播的宽限
        # (540s)还长 —— reaper 先把任务判死并退点，worker 却还在轮询，上游照样出片照样收钱。
        info = _heygen_poll_video(video_id, deadline_s=VIDEO_GEN_DEADLINE)
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

MOTION_REF_MAX_SECONDS = 120   # WaveSpeed 的上限。去线路化后只剩这一档（原线路一 HeyGen 是 30 秒）


def _motion_reference_duration(reference_video_file):
    """超长的参考视频要【在本地】明确拒绝，别丢给上游去报一句天书错误。"""
    duration = _probe_video_duration(reference_video_file)
    if duration > MOTION_REF_MAX_SECONDS + 0.05:
        raise ValueError("参考视频 %.1f 秒，超过最长 %d 秒，请先裁剪后重试" % (duration, MOTION_REF_MAX_SECONDS))
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
    if mode not in {"text", "audio"}:
        raise ValueError("生成方式不正确")
    # 口播(text/audio)走 HeyGen。
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
    if mode == "text":
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
        if payload.get("audio_file"):
            audio_file = _normalize_audio_file_ref(payload.get("audio_file"), username=(payload.get("_username") or "").strip() or None)
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

def _xiaole_download_candidates(url, tunnel_proxy):
    """成片下载候选链(GET 幂等，可自由多档尝试，不像出图 POST 有重复计费顾虑)。顺序：
      ① 原始 URL 走 egress 快隧道(Reality VPS/mihomo)—— tunnel_proxy 非空才加，避开拥塞的 heygen 中转
      ② heygen 法兰克福 /cdn/ 中转 —— 兜底(拥塞时慢到分钟级)，走进程默认(NO_PROXY 含 zelong.vip → 直连中转)
      ③ 原始 URL 走进程默认 —— 国内可直连的 CDN(如 seedance update.asiot.top，中转反而 404)兜底
    返回 [(fetch_url, headers, proxy_or_None), ...]；proxy 非空则该档强制走此代理，None 则用进程默认 urlopen。
    未配隧道(tunnel_proxy 为空)时链退化为 [heygen, 直连]，等于改动前老行为。"""
    plain_headers = {"User-Agent": "huangque-content/1.0"}
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    candidates = []
    if tunnel_proxy:
        candidates.append((url, dict(plain_headers), tunnel_proxy))
    relay = os.environ.get("HEYGEN_RELAY_BASE", "").strip().rstrip("/")
    if relay and host and not host.endswith(".cn"):
        fetch = "%s/cdn/%s/%s" % (relay, host, parts.path.lstrip("/"))
        if parts.query:
            fetch += "?" + parts.query
        headers = dict(plain_headers)
        token = os.environ.get("HEYGEN_RELAY_TOKEN", "").strip()
        if token:
            headers["X-Relay-Token"] = token
        candidates.append((fetch, headers, None))
    candidates.append((url, dict(plain_headers), None))
    return candidates


def _download_xiaole_video(url, prefix="xiaole"):
    # 视频 CDN 多在海外(如 vidgen.x.ai)，国内直连不通。成片下载是 GET(幂等)，故可多档尝试：
    # 优先走 egress 快隧道，避开拥塞到分钟级的 heygen 法兰克福老中转(实测 xAI 2 分钟出片、
    # 走老中转下载却要 11~19 分钟，甚至卡死被 reaper 判超时退点)。中转仅作兜底。
    from . import egress
    candidates = _xiaole_download_candidates(url, egress.preferred_proxy())
    # 下载中断(IncompleteRead/网络抖动)自动重试；前一档耗尽后换下一档
    data = None
    last_err = None
    for fetch_url, headers, proxy in candidates:
        if data is not None:
            break
        opener_open = egress._opener(proxy).open if proxy else urllib.request.urlopen
        for attempt in range(_xiaole_dl_retries):
            try:
                req = urllib.request.Request(fetch_url, headers=headers)
                with opener_open(req, timeout=300) as r:
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
    use_xai = channel == "grok" and GROK_VIDEO_PROVIDER != "xiaole"
    model = (payload.get("model") or "grok-imagine-video") if use_xai else XIAOLE_CHANNEL_MODELS.get(channel)
    if not model:
        raise ValueError("未知视频渠道：%s" % channel)
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("请输入视频提示词")
    ratio = (payload.get("ratio") or ("16:9" if use_xai else "9:16")).strip()
    if not use_xai and ratio not in XIAOLE_RATIO_SIZES:
        ratio = "9:16"
    size = _xiaole_size_for_ratio(ratio) if not use_xai else None
    ref_images = None
    if channel in XIAOLE_IMAGE_CHANNELS:
        raw_refs = payload.get("reference_images") or None
        if raw_refs:
            ref_images = [_xiaole_ref_to_url(r) for r in raw_refs]
    label = {"grok": "果肉视频", "micro": "豆姐视频", "omni": "欧米视频"}.get(channel, model)
    existing = get_resumable_xai_request(job_id) if use_xai else None
    if job_id and not existing:
        update_video_asset_phase(job_id, "queued", mode=channel, text=prompt, model=model)
    if use_xai:
        from . import video_xai
        operation = payload.get("operation") or "generate"
        reference_video_file = reference_video_url = None
        if existing:
            xres = video_xai.resume(
                existing["request_id"], existing.get("model") or model,
                payload.get("duration") or 10,
                job_id=job_id, heartbeat=update_video_asset_phase,
            )
        elif operation == "edit":
            reference_video_file = _save_data_file(payload.get("reference_video_data"), "grok_edit_source", [".mp4"])
            if not reference_video_file:
                raise RuntimeError("参考视频保存失败")
            source_public_url = public_url(reference_video_file, "video/mp4")
            if not str(source_public_url).startswith(("http://", "https://")):
                raise RuntimeError("xAI官方视频编辑需要可公网访问的参考视频，COS转存失败")
            reference_video_url = _file_url(reference_video_file)
            xres = video_xai.edit(model="grok-imagine-video", prompt=prompt, video_url=source_public_url,
                                  duration=payload.get("source_duration"), job_id=job_id,
                                  heartbeat=update_video_asset_phase)
        else:
            image_url = ref_images[0] if ref_images else None
            if image_url and not str(image_url).startswith(("http://", "https://")):
                raise RuntimeError("xAI官方图生视频需要可公网访问的参考图，COS转存失败")
            xres = video_xai.generate(
                model=model, prompt=prompt, image_url=image_url,
                duration=payload.get("duration") or 10,
                aspect_ratio=ratio, resolution=payload.get("resolution") or "720p",
                job_id=job_id, heartbeat=update_video_asset_phase,
            )
        source_url = xres["source_video_url"]
        if job_id:
            update_video_asset_phase(job_id, "downloading", source_video_url=source_url,
                                     provider_video_id=xres.get("request_id"), model=xres.get("model") or model)
        video_file = _download_xiaole_video(source_url, "grok_xai")
        cover = _extract_first_frame_cover(video_file)
        result = {
            "video_file": video_file, "video_url": _file_url(video_file),
            "source_video_url": source_url, "model": xres.get("model") or model,
            "request_id": xres.get("request_id"), "duration": xres.get("duration"),
            "image_file": cover,
            "image_url": public_url(cover, "image/jpeg") if cover else None,
            "reference_video_file": reference_video_file,
            "reference_video_url": reference_video_url,
        }
    else:
        result = generate_xiaole_video(model, prompt, reference_images=ref_images, size=size, job_id=job_id, prefix=channel,
                                       duration=XIAOLE_CHANNEL_DURATION.get(channel))
    return {
        "type": "video", "status": "done", "mode": channel, "model": result.get("model") or model, "text": prompt,
        "operation": payload.get("operation") or "generate",
        "ratio": ratio, "resolution": payload.get("resolution") if use_xai and payload.get("operation") != "edit" else None,
        "duration": result.get("duration") or (payload.get("duration") if use_xai else None),
        "provider_video_id": result.get("request_id"),
        "video_file": result.get("video_file"), "video_url": result.get("video_url"),
        "source_video_url": result.get("source_video_url"),
        "reference_video_file": result.get("reference_video_file"),
        "reference_video_url": result.get("reference_video_url"),
        "image_file": result.get("image_file"),
        "image_url": result.get("image_url") or (public_url(result.get("image_file"), "image/jpeg") if result.get("image_file") else None),
        "phase": "done", "message": "%s生成完成" % label,
    }

# ============ 数字人形象：从动作模仿里拆出来的独立一步 ============
# 原来建 avatar 混在动作模仿任务里：用户传的照片如果检测不到人脸，HeyGen 报
# 「No face detected in the image」，整个任务失败 —— 而那 20 点已经扣了（虽然会退，
# 但用户白等了几分钟）。拆出来之后，这类失败在第一步就当场暴露，只花 5 点、25 秒。
# 形象建好可反复使用，是长期资产。

def validate_avatar_payload(body):
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    image_data = (body.get("image_data") or "").strip()
    if not image_data:
        raise ValueError("请先上传人物照片")
    if not _is_valid_data_url(image_data, VALID_IMAGE_MIMES):
        raise ValueError("image_data 不是有效的图片（支持 jpg/png/webp）")
    return {"image_data": image_data, "name": (body.get("name") or "").strip()[:40]}


def gen_avatar(payload):
    """照片 → HeyGen photo avatar → 记进 avatars 表。实测 25 秒（传图 12s + 建 2s + 等就绪 12s）。"""
    username = (payload.get("_username") or "").strip()
    image_file = _save_data_file(payload.get("image_data"), "avatar_src", [".jpg", ".png", ".webp"])
    if not image_file:
        raise ValueError("请先上传人物照片")
    fp = _ensure_heygen_image_jpg(_resolve_out_file(image_file))
    try:
        # 传图和建 look 都对瞬时网络错误重试 —— 建形象免费，重发不会重复计费（见 _heygen_retry_net）。
        # 隧道扛不住 5 路以上的并发 TLS 握手，不重试的话用户会莫名其妙地建形象失败。
        asset_id = _heygen_retry_net(lambda: _heygen_upload_asset(fp, direct=True), "建形象传图")
        item_id, group_id = _heygen_retry_net(
            lambda: _heygen_retry_429(
                lambda: _heygen_create_photo_avatar(asset_id, direct=True), "建形象"),
            "建形象提交")
        _heygen_wait_photo_avatar(item_id, group_id, direct=True)
    except Exception as e:
        # 线上最常见的失败就是这个。原样把 HeyGen 的英文报文抛给用户毫无意义，翻译成人话。
        if "No face detected" in str(e):
            raise ValueError("照片里没有检测到人脸，请换一张正脸清晰、光线充足的照片")
        raise
    row = record_video_avatar(username, image_file, item_id, group_id, payload.get("name")) or {}
    return {
        "avatar_id": row.get("id"), "name": row.get("name"), "status": "ready",
        "image_file": image_file, "image_url": public_url(image_file, "image/jpeg"),
        "provider_avatar_id": item_id, "provider_avatar_group_id": group_id,
        "phase": "done", "message": "形象创建完成，可在剧情视频里反复使用",
    }


# ============ 电影化身：HeyGen cinematic_avatar ============
# 三个玩法共用同一个上游接口（type=cinematic_avatar），差别只在【谁来写提示词】和【几个形象】：
#   motion 单人动作模仿：1 个形象 + 必传参考视频，提示词写死，用户只调分辨率和时长
#   duo   双人动作模仿：2 个形象 + 必传参考视频，提示词写死（另一段）
#   open  开放式生成  ：1~3 个形象，自己写提示词，参考视频选填
CINEMATIC_MAX_AVATARS = 3        # HeyGen 硬上限：avatar_id 是 1~3 个 look 的数组
CINEMATIC_RESOLUTIONS = {"720p", "1080p"}
CINEMATIC_PROMPT_MAX = 2000
CINEMATIC_DURATION_RANGE = (4, 15)   # HeyGen: 4~15 秒
CINEMATIC_AUTO_DURATION = 10         # 选了「自适应」但没传参考视频时的回落值
CINEMATIC_MODES = ("motion", "duo", "open")
# 双人暂不开放：线上 0 成 2 败 —— 被 HeyGen 的内容审核拦的（网页原话 "Your content was
# flagged by our moderation system. Please try different images or prompts. No credits
# charged."，而 API 一个字都不给：v1/video_status.get 的 error 是 null）。
# 嫌疑是它的英文提示词 "Use these two avatars to replace the two people in the reference
# video" 字面就是换脸措辞，而审核模型是英文的。中文版换过，但【一次都没实测】。
# 与其让用户白等十几分钟再看到「生成失败」，先下掉。
# ⚠️ 玩法本身【保留】（CINEMATIC_MODES / CINEMATIC_MODE_AVATARS / 提示词都还在）——
# 实测通过后把 CINEMATIC_DUO_ENABLED=1 打开、前端把页签加回来即可，不用重写任何逻辑。
CINEMATIC_DUO_ENABLED = os.environ.get("CINEMATIC_DUO_ENABLED", "").strip().lower() in ("1", "true", "yes")
CINEMATIC_COMING_SOON = {} if CINEMATIC_DUO_ENABLED else {"duo": "双人动作模仿暂未开放"}
# 动作模仿只给三档：自适应 / 10 秒 / 15 秒（开放式仍可在 4~15 内任选）
# 动作模仿锁死的参数（照抄 #2173 —— 目前唯一已知能过 HeyGen 审核的配置）。
# 用户只能换形象和参考视频；分辨率/时长/润色都不给选，也不认客户端传的值。
CINEMATIC_MOTION_RESOLUTION = "1080p"

# 每秒点数。HeyGen 那边是扁平价（$7/条，与时长无关），我们按时长卖 —— 这是产品定价，不是成本。
# ⚠️ 改这里等于改价：cost_of() 直接乘这个数。
#
# 三个玩法统一 30 点/秒（kongli 2026-07-15 调价；此前 10，更早 motion 3/duo 5/open 5）。
# HeyGen 对三者收一样的钱，这里不分档。保留 per-mode 的表结构和 env 覆盖，以后想再分档不用改代码。

CINEMATIC_RATE_PER_SEC = {
    "motion": _env_positive_int("CINEMATIC_RATE_MOTION", 30),   # 单人动作模仿
    "duo":    _env_positive_int("CINEMATIC_RATE_DUO", 30),      # 双人动作模仿
    "open":   _env_positive_int("CINEMATIC_RATE_OPEN", 30),     # 开放式生成
}
CINEMATIC_RATE_FALLBACK = 30   # 玩法认不出来时按最贵的收，绝不按最便宜的（更不能按 0）


def cinematic_rate(cine_mode):
    return CINEMATIC_RATE_PER_SEC.get(cine_mode, CINEMATIC_RATE_FALLBACK)


# ===== 口播(video kind)按秒计费：10 点/秒 × 输出时长（kongli 2026-07-15）=====
# 口播成片时长 ≈ 音频时长。扣点时机不同：
#   audio 模式：上传/引用音频，扣点前 ffprobe 就能拿到精确时长 → 扣准，跑完无需结算。
#   text 模式：TTS 在 job 里才跑，扣点那刻不知道语音多长 → 按文本长度【偏保守】估算预扣，
#              跑完再按成片真实时长结算（core.run_job 里调 talking_actual_cost，多退少不补）。
TALKING_RATE_PER_SEC = _env_positive_int("TALKING_RATE_PER_SEC", 10)
# 中文口播语速估算：偏保守取 4 字/秒（估长一点→预扣偏高→跑完退差，避免系统性少扣）。
TALKING_CHARS_PER_SEC = float(os.environ.get("TALKING_CHARS_PER_SEC", "4") or 4)
TALKING_FALLBACK_SEC = 10.0   # 音频探不到时长时的兜底估算秒数


def _talking_estimate_seconds(body):
    mode = str(body.get("mode") or "text").lower()
    if mode == "audio":
        af = (body.get("audio_file") or "").strip()
        if af:
            fp = _resolve_out_file(af)
            if fp:
                try:
                    proc = subprocess.run(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", str(fp)],
                        capture_output=True, text=True, timeout=20)
                    if proc.returncode == 0:
                        return max(1.0, float((proc.stdout or "0").strip()))
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
        ad = (body.get("audio_data") or "").strip()
        if ad:
            try:
                return max(1.0, _probe_data_video_duration(ad))   # ffprobe 认内容不认后缀，音频也能探
            except Exception:
                pass
        return TALKING_FALLBACK_SEC
    # text 模式：TTS 还没跑，按文本长度估算
    return max(1.0, len(str(body.get("text") or "")) / TALKING_CHARS_PER_SEC)


def video_cost(body):
    """口播扣点前的预扣(hold)：10 点/秒 × 估算/精确输出秒数。text 模式偏估、跑完结算。"""
    secs = _talking_estimate_seconds(body)
    return max(TALKING_RATE_PER_SEC, int(math.ceil(secs)) * TALKING_RATE_PER_SEC)


def talking_actual_cost(result):
    """口播成片后的真实点数 = 10 点/秒 × 成片真实秒数（HeyGen 返回的 duration）。
    拿不到时长返回 None（不结算，保留预扣）。"""
    secs = (result or {}).get("duration") or (result or {}).get("seconds")
    if not secs:
        return None
    try:
        return max(TALKING_RATE_PER_SEC, int(math.ceil(float(secs))) * TALKING_RATE_PER_SEC)
    except (TypeError, ValueError):
        return None


def cinematic_cost(body):
    """点数 = 成片秒数 × 单价。

    能这么算的前提是：validate_cinematic_payload 已经把「自适应」解析成了确定的整数秒
    （参考视频落盘 + ffprobe），所以扣点时不存在「还不知道多长」的情况，不需要预扣退差。
    """
    refs = body.get("reference_video_files") or []
    duration = _cinematic_duration(body.get("duration"), refs[0] if refs else None)
    return max(1, int(duration) * cinematic_rate(body.get("cine_mode")))


# 动作模仿的提示词是【写死的】，用户不填、也改不了 —— 前端连输入框都不显示，
# 后端也不信任客户端传上来的 prompt（见 validate_cinematic_payload）。
CINEMATIC_FIXED_PROMPTS = {"motion": MOTION_PROMPT_BASE, "duo": DUO_MOTION_PROMPT_BASE}
# 动作模仿需要几个形象：数量必须【正好】等于这个数，多一个少一个都不行 ——
# 双人提示词会去参考视频里找两个人，只给一个形象，HeyGen 会把参考视频里的另一个人抄进来。
CINEMATIC_MODE_AVATARS = {"motion": 1, "duo": 2}

# 媒体预算。官方文档原文：
#   「Avatar looks and references share a combined media budget:
#     at most 3 videos and 9 images total across avatar_id and references.」
# avatar 和参考素材【共用】这份额度，不是各算各的。
#
# ⚠️ 文档没明说「每个 avatar look 算不算一张图」。这里按【算】处理（保守）：
# 选了 3 个 avatar 就只剩 6 张图片额度。宁可少放，也别让 HeyGen 400 ——
# 那时视频已经提交、钱已经扣了（提交即计费），报错对用户就是白扣一次。
CINEMATIC_MAX_MEDIA_VIDEOS = 3
CINEMATIC_MAX_MEDIA_IMAGES = 9


def cinematic_ref_budget(avatar_count):
    """选了 N 个形象之后，还能再放几个参考素材。返回 (可放视频数, 可放图片数)。"""
    n = max(0, int(avatar_count or 0))
    return CINEMATIC_MAX_MEDIA_VIDEOS, max(0, CINEMATIC_MAX_MEDIA_IMAGES - n)


def _cinematic_duration(raw, reference_video_file=None):
    """把 duration 解析成 HeyGen 要的整数秒。

    「自适应」= 跟随参考视频的实际长度。这才是用户的本意：既然给了参考片段，
    成片就该和它一样长，而不是被截断或者硬拖到某个固定秒数。

    没给参考视频时无从跟随（只有提示词，没有时间基准），回落到默认 10 秒。
    探测失败（ffprobe 挂了 / 文件坏了）也回落 —— 时长是个优化项，不该让整个任务失败。

    结果一律夹进 HeyGen 的 4~15 秒；超出范围它会直接 400。
    """
    lo, hi = CINEMATIC_DURATION_RANGE
    if str(raw or "").strip().lower() not in ("", "auto"):
        return max(lo, min(hi, int(raw)))
    if not reference_video_file:
        return CINEMATIC_AUTO_DURATION
    try:
        secs = _probe_video_duration(reference_video_file)
    except Exception as e:
        print("[cinematic] 参考视频时长探测失败，回落 %ds: %s" % (CINEMATIC_AUTO_DURATION, str(e)[:80]), flush=True)
        return CINEMATIC_AUTO_DURATION
    # 向上取整：宁可多一帧，也别把参考片段的末尾截掉
    return max(lo, min(hi, int(secs + 0.999999)))


def validate_cinematic_payload(body, username=None):
    """校验并【落定】payload —— 尤其是时长。

    时长必须在这里就变成一个确定的整数秒，不能留 "auto" 带下去：调用链是
        validate_cinematic_payload → cost_of → 扣点 → 入队 → gen_cinematic
    点数 = 秒数 × 单价，扣点发生在 gen_cinematic 之前。留个 "auto" 给 worker 去解析，
    扣点这一刻就不知道该扣多少 —— 只能预扣上限再退差。所以参考视频在这里就落盘 + 探测。
    """
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")

    cine_mode = (body.get("cine_mode") or "open").strip().lower()
    if cine_mode in CINEMATIC_COMING_SOON:
        # 前端已经把页签删了，但前端【不是】安全边界 —— 直接 POST 一个 cine_mode=duo
        # 进来也得挡住，否则用户照样白等十几分钟再看到失败。
        raise ValueError(CINEMATIC_COMING_SOON[cine_mode])
    if cine_mode not in CINEMATIC_MODES:
        raise ValueError("玩法仅支持 motion（单人动作模仿）、duo（双人动作模仿）、open（开放式生成）")

    raw_ids = body.get("avatar_ids") or ([body["avatar_id"]] if body.get("avatar_id") else [])
    if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
        raise ValueError("请至少选择一个数字人形象")
    ids = []
    for a in raw_ids:
        try:
            v = int(a)
        except Exception:
            raise ValueError("形象不存在")
        if v not in ids:
            ids.append(v)
        # 归属校验：get_video_avatar 只认本人的形象，别人的 id 直接 ValueError
        if username:
            get_video_avatar(username, v)
    need = CINEMATIC_MODE_AVATARS.get(cine_mode)
    if need and len(ids) != need:
        # 「正好 N 个」，不是「最多 N 个」：双人提示词会去参考视频里找两个人，
        # 只给一个形象，HeyGen 就会把参考视频里的另一个人原样抄进成片。
        raise ValueError("%s需要正好 %d 个形象，当前选了 %d 个"
                         % ("双人动作模仿" if cine_mode == "duo" else "动作模仿", need, len(ids)))
    if len(ids) > CINEMATIC_MAX_AVATARS:
        raise ValueError("最多同时选择 %d 个形象" % CINEMATIC_MAX_AVATARS)

    # 参考素材的校验统一放在下面（reference_videos/reference_images，老的单字段会先合进去）。
    # 别在这里按老字段 reference_video_data 再判一次「动作模仿必须传参考视频」——
    # 新前端发的是 reference_videos[]，那样判会把每一条动作模仿都拒掉。
    if cine_mode in CINEMATIC_FIXED_PROMPTS:
        # 提示词写死。客户端传什么都不看 —— 它是计费和成片效果的一部分，不能由前端说了算。
        prompt = CINEMATIC_FIXED_PROMPTS[cine_mode]
    else:
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("请填写画面描述（或选一个模板）")
        if len(prompt) > CINEMATIC_PROMPT_MAX:
            raise ValueError("画面描述不能超过 %d 字" % CINEMATIC_PROMPT_MAX)

    if cine_mode in CINEMATIC_FIXED_PROMPTS:
        # 动作模仿【锁死】成 #2173 那一条的形状 —— 它是目前唯一一个已知能过 HeyGen 审核的配置。
        # 用户只能换两样东西：形象、参考视频。分辨率/时长/润色一律不接受客户端的值。
        #     分辨率 1080p         （#2173 就是 1080p；不再给 720p 的选项）
        #     时长   自适应        （跟随参考视频：#2173 的参考片段 10.9s → 成片 11s）
        #     润色   关            （下面统一置 False）
        #     比例   跟随参考视频   （#2173 的参考是 576x1024 竖版 → 9:16；前端按宽高算好传上来。
        #                           这里仍然校验它是合法值，但不给用户在界面上选）
        resolution = CINEMATIC_MOTION_RESOLUTION
        duration = "auto"
    else:
        resolution = (body.get("resolution") or "720p").strip().lower()
        if resolution not in CINEMATIC_RESOLUTIONS:
            raise ValueError("分辨率仅支持 720p、1080p")

        lo, hi = CINEMATIC_DURATION_RANGE
        raw = str(body.get("duration") or "").strip().lower()
        if raw in ("", "auto"):
            duration = "auto"
        else:
            try:
                duration = int(raw)
            except Exception:
                raise ValueError("时长必须是 %d~%d 之间的整数，或填 auto 跟随参考视频" % (lo, hi))
            if not lo <= duration <= hi:
                raise ValueError("时长仅支持 %d~%d 秒" % (lo, hi))

    ratio = (body.get("ratio") or "9:16").strip()
    if ratio not in _HEYGEN_CINEMATIC_RATIOS:
        raise ValueError("画面比例仅支持 9:16、16:9、1:1")

    # 参考素材。reference_video_data（单个）是老字段，合进 reference_videos 里，别让老前端 400。
    max_videos, max_images = cinematic_ref_budget(len(ids))

    videos = [v for v in (body.get("reference_videos") or []) if str(v or "").strip()]
    legacy = (body.get("reference_video_data") or "").strip()
    if legacy and legacy not in videos:
        videos.insert(0, legacy)
    for v in videos:
        if not _is_valid_data_url(v, VALID_REFERENCE_VIDEO_MIMES):
            raise ValueError("参考视频格式不支持（mp4/mov/webm）")
    if len(videos) > max_videos:
        raise ValueError("参考视频最多 %d 个" % max_videos)

    images = [i for i in (body.get("reference_images") or []) if str(i or "").strip()]
    for i in images:
        if not _is_valid_data_url(i, VALID_IMAGE_MIMES):
            raise ValueError("参考图片格式不支持（jpg/png/webp）")
    if len(images) > max_images:
        # 说清楚为什么只剩这么多 —— 否则用户会以为是 bug（明明文档说 9 张）
        raise ValueError("参考图片最多 %d 张（形象和参考素材共用 %d 张图的额度，你已选 %d 个形象）"
                         % (max_images, CINEMATIC_MAX_MEDIA_IMAGES, len(ids)))

    if cine_mode in CINEMATIC_FIXED_PROMPTS:
        # 动作模仿：正好一个参考视频，不收参考图。
        # 多参考素材（#599）是给开放式生成的 —— 那边用户自己写提示词，可以说清楚每个素材干嘛用。
        # 动作模仿的提示词是写死的「照着参考视频演」，再塞第二个视频或几张图，HeyGen 只会
        # 在它们之间乱抄，用户既控制不了、也无从预期。
        if len(videos) != 1:
            raise ValueError("动作模仿需要正好上传 1 个参考视频")
        if images:
            raise ValueError("动作模仿不支持参考图片，只看参考视频的动作")

    cleaned = dict(body)
    # 参考素材在这里就落盘（原来是留到 worker 里）。两个原因：
    #   1. 【必须】按成片秒数计费，而扣点发生在 worker 之前 —— 「自适应」要在这里探测出秒数，
    #      否则扣点这一刻不知道该扣多少，只能预扣上限再退差。
    #   2. 顺带：payload 里存路径而不是几十 MB 的 base64，jobs.payload 不再被撑爆。
    video_files = [f for f in (_save_data_file(v, "motion_ref", [".mp4", ".mov", ".webm"]) for v in videos) if f]
    image_files = [f for f in (_save_data_file(i, "cine_ref", [".jpg", ".png", ".webp"]) for i in images) if f]
    if video_files:
        _motion_reference_duration(video_files[0])   # 超长（>120s）在这里就明确拒绝
    # 「自适应」跟随第一个参考视频的长度
    duration = _cinematic_duration(duration, video_files[0] if video_files else None)

    cleaned.update({"cine_mode": cine_mode, "avatar_ids": ids, "prompt": prompt,
                    "resolution": resolution, "ratio": ratio, "duration": duration,
                    "reference_video_files": video_files, "reference_image_files": image_files,
                    # 自动润色：HeyGen 把简短提示词扩写成更丰富的描述。默认关 ——
                    # 它可能把用户的意图改跑偏，要不要开由用户决定。
                    # 动作模仿【一律关】：提示词是写死的（含身份约束），让 HeyGen 去改写它，
                    # 等于让它改写「不许抄参考视频里那个人的脸」这句话。
                    "enhance_prompt": False if cine_mode in CINEMATIC_FIXED_PROMPTS
                                      else bool(body.get("enhance_prompt"))})
    cleaned.pop("avatar_id", None)
    # base64 已经落盘成文件了，别在 payload 里再留一份
    for k in ("reference_video_data", "reference_videos", "reference_images"):
        cleaned.pop(k, None)
    return cleaned


def gen_cinematic(payload):
    """选 1~3 个自己的形象 + 提示词（+ 可选参考视频）→ HeyGen cinematic_avatar。

    与动作模仿的区别：形象是【事先建好的】，这里只做「生成」——不再传人物图、不再建 avatar、
    不再等 avatar 就绪。实测这条精简路径 10 路并发无 429、生成不降速。
    """
    username = (payload.get("_username") or "").strip()
    job_id = payload.get("_job_id")
    avatars = [get_video_avatar(username, a) for a in payload["avatar_ids"]]
    look_ids = [a["provider_avatar_id"] for a in avatars]
    if not all(look_ids):
        raise ValueError("所选形象尚未就绪，请重新创建")

    # 参考素材已经在 validate 阶段落盘了（按秒计费，扣点前就得探测出时长）。这里只做压缩：
    # 视频要推字节过隧道，不压的话 10 路并发会撞 240s 上传超时；图片本来就小，原样传。
    #
    # reference_videos/reference_images（base64）是老 payload 的兼容路径 —— 重启恢复在飞任务时，
    # 队列里可能还躺着改版前入队的 job，它们的素材还没落盘。
    video_files = list(payload.get("reference_video_files") or [])
    image_files = list(payload.get("reference_image_files") or [])
    if not video_files and payload.get("reference_videos"):
        video_files = [_save_data_file(v, "motion_ref", [".mp4", ".mov", ".webm"])
                       for v in payload["reference_videos"]]
    if not image_files and payload.get("reference_images"):
        image_files = [_save_data_file(i, "cine_ref", [".jpg", ".png", ".webp"])
                       for i in payload["reference_images"]]
    # ⚠️ 顺序不能换：先【抽原声】，再【剥音轨】—— 剥完就抽不出来了。
    #
    # 为什么剥：HeyGen 的 cinematic_avatar 只看画面，它不会用参考视频的声音。音轨对它是纯浪费，
    #   却要经过我们那条 ~1.5 MB/s 的出境隧道推上去。剥掉能省 5~15% 的上传量，100% 无损失。
    # 为什么抽：HeyGen 的成片【本身没有声音】。把参考视频的原声配回成片，观感上才是
    #   「同一条片子，只是换了个人演」。
    #
    # 原声只取【第一个】参考视频的 —— 它同时也是决定成片时长的那一个（_cinematic_duration）。
    source_audio = _extract_reference_audio(video_files[0]) if video_files else None
    #
    # ⚠️ 【不压缩】（kongli 的决定，2026-07-14）。原来这里会把 >6MB 的参考视频转码成
    # 720p/2Mbps —— 那是重编码，画质有损，而动作模仿的成片质量直接取决于参考视频。
    # 出境隧道换了新节点后带宽是 ~1.5 MB/s，上传超时也放宽到 600s，压缩省的那点时间
    # 不值得拿画质去换。
    #
    # 剥音轨【不是】压缩：-c:v copy 只重封装，画面一帧不动，几十毫秒的事。
    video_files = [_strip_audio(f) for f in video_files if f]
    image_files = [f for f in image_files if f]
    reference_video_file = video_files[0] if video_files else None   # 资产表只存第一个（列是单值）

    # 时长在 validate 里就落定成整数了（= 已经按这个秒数扣过点）。这里再算一次只为兜住老 payload。
    duration = _cinematic_duration(payload.get("duration"), reference_video_file)

    update_video_asset_phase(job_id, "queued", mode="cinematic", text=payload["prompt"],
                             resolution=payload["resolution"], ratio=payload["ratio"])

    reference_asset_ids = []
    if video_files or image_files:
        update_video_asset_phase(job_id, "uploading_reference_asset")
        for f in video_files + image_files:
            if f:
                # 参考素材上传对瞬时网络错误重试：上传不计费，重试安全。fang 的电影化身就死在这——
                # 参考视频上传撞隧道 240s read timeout、压根没提交 HeyGen 就判死(#630 也点名)。
                reference_asset_ids.append(
                    _heygen_retry_net(lambda fp=f: _heygen_upload_asset(_resolve_out_file(fp), direct=True), "剧情视频传素材"))
    reference_asset_id = reference_asset_ids[0] if reference_asset_ids else None   # 资产表用

    update_video_asset_phase(job_id, "creating_cinematic_video", reference_asset_id=reference_asset_id)
    # 账号级并发闸：和口播共用 10 个槽（HeyGen 的上限是账号级的，不是每个功能各 10 个）。
    # 素材上传在闸外——它不产生 async job，不占 HeyGen 的并发额度。
    with heygen_slot("剧情视频"):
        video_id = _heygen_retry_429(lambda: _heygen_create_cinematic_video(
            look_ids, reference_asset_ids, payload["ratio"], payload["resolution"], duration,
            # 开放式生成【不再拼】身份约束（kongli 的决定，2026-07-14）——
            # 用户写什么就发什么，一个字不加。
            #
            # ⚠️ 代价（已经跟 kongli 说清楚）：不拼的话，HeyGen 可能把参考视频里那个人的长相
            # 抄进成片，用户拿到的就不是自己的脸了。用户自己写的中文提示词里通常不会写
            # 「保持我的脸不变」这种话 —— 身份这件事本来就不该交给用户把关。
            # 真出现串脸，先看这里。
            #
            # 动作模仿的新提示词是【自包含】的（自带 CRITICAL 身份约束），再拼一次就是
            # 同样的话说两遍。所以这里【什么都不拼】——
            #     payload 里的 prompt == HeyGen 真正收到的 prompt，一个字不差。
            # 顺带一个好处：排查时不用再脑补「后端还偷偷加了什么」。
            prompt=payload["prompt"], direct=True,
            enhance_prompt=payload.get("enhance_prompt")), "剧情视频")

        # ↓ 此刻已计费。之后任何失败都不能重发（见 HeyGenBilledError）——HeyGen 提交即扣费。
        update_video_asset_phase(job_id, "polling_video", provider_video_id=video_id)
        try:
            info = _heygen_poll_video(video_id, direct=True, deadline_s=HEYGEN_MOTION_DEADLINE)
            update_video_asset_phase(job_id, "downloading_video", source_video_url=info.get("video_url"))
            video_file = _download_video_file_direct(info["video_url"], "cinematic")
            # 把参考视频的原声合回成片（HeyGen 的成片本身是无声的）。
            # 合失败就保留无声成片 —— 宁可无声，也不能因为配音失败把片子丢了。
            if source_audio:
                video_file = _mux_original_audio(video_file, source_audio)
            cover = _extract_first_frame_cover(video_file)   # 封面从【最终】成片抽
        except Exception as e:
            raise HeyGenBilledError("剧情视频已提交 HeyGen(video_id=%s，已扣费)，后续失败: %s"
                                    % (video_id, str(e)[:180])) from e

    ret = {
        # ⚠️ status/mode/type 一个都不能少 —— record_video_asset 从 result 里取它们写进
        # video_assets，而前端读的是那张表。漏了 status，它会写成 "pending"，
        # UPSERT 的 COALESCE 又挡不住非 NULL 值，资产行就永远停在 running，
        # 用户看到的就是「一直显示生成中」——哪怕 jobs 表早就 done 了。
        "type": "video", "status": "done", "mode": "cinematic",
        "video_id": video_id, "video_file": video_file, "video_url": _file_url(video_file),
        "reference_video_file": reference_video_file,
        "avatar_ids": payload["avatar_ids"],
        "avatar_names": [a.get("name") for a in avatars],
        "text": payload["prompt"],   # video_assets 的文案列叫 text，前端卡片也读它
        "prompt": payload["prompt"], "resolution": payload["resolution"], "ratio": payload["ratio"],
        "duration": info.get("duration") or duration,
        "source_video_url": info.get("video_url"), "thumbnail_url": info.get("thumbnail_url"),
        "provider": "heygen_direct", "phase": "done", "message": "剧情视频生成完成",
    }
    if cover:
        ret["image_file"] = cover
        ret["image_url"] = public_url(cover, "image/jpeg")
    return ret


HANDLERS = {"video": gen_video, "tryon": gen_tryon, "xiaole_video": gen_xiaole_video,
            "avatar": gen_avatar, "cinematic": gen_cinematic}
