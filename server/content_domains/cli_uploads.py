"""Private, short-lived media inputs uploaded through HQ CLI."""

import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time
import uuid


MAX_BYTES = 10 * 1024 * 1024
MAX_USER_BYTES = 96 * 1024 * 1024
MAX_USER_FILES = 20
MIN_FREE_BYTES = 512 * 1024 * 1024

# ---- 顾客素材（数字人一键成片）---------------------------------------------
# 比通用图片口宽得多：图片/视频/音频都收，单文件与整批各 200MB，一批最多 10 个。
# ⚠️ 这组常量与下面的 MIME 表**只**服务于素材口。通用图片口（store_image）
# 保持 10MB / 3 种不动 —— 它同时被视频助手（video_agent）和 CLI 上传
# （cli_gateway）使用，跟着一起放开会波及那两条链路。
MATERIAL_MAX_BYTES = 200 * 1024 * 1024
MATERIAL_MAX_USER_BYTES = 200 * 1024 * 1024
MATERIAL_MAX_USER_FILES = 10
MATERIAL_MIME_EXTENSIONS = {
    # 图片
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/avif": ".avif",
    # 视频（不含 MKV：它和 WebM 共用同一段魔数，只有靠文件头几百字节里的
    # DocType 才能区分，而校验环节只看前 16 字节 —— 认不准的类型宁可不收）
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    # 音频
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
}
# 素材类型白名单的对外文案（报错时直接展示给用户）
MATERIAL_TYPE_HINT = "图片（PNG/JPG/WebP/HEIC/GIF/BMP/TIFF/AVIF）、视频（MP4/MOV/WebM/MKV/AVI）或音频（MP3/WAV/M4A/AAC/OGG/FLAC）"
TTL = max(600, min(24 * 60 * 60, int(os.environ.get("HQ_CLI_IMAGE_UPLOAD_TTL", "3600") or 3600)))
UPLOAD_ROOT = pathlib.Path(os.environ.get(
    "HQ_CLI_UPLOAD_DIR",
    str(pathlib.Path(__file__).resolve().parents[1] / "content_out" / "_cli_uploads"),
))
UPLOAD_ID_RE = re.compile(r"^img_[0-9a-f]{32}$")
MIME_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
VIDEO_MAX_BYTES = 32 * 1024 * 1024
VIDEO_MAX_USER_BYTES = 96 * 1024 * 1024
VIDEO_MAX_USER_FILES = 6
VIDEO_MAX_SECONDS = 15
VIDEO_UPLOAD_ID_RE = re.compile(r"^vid_[0-9a-f]{32}$")
VIDEO_MIME_EXTENSIONS = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm"}
AUDIO_MAX_BYTES = 10 * 1024 * 1024
AUDIO_MAX_USER_BYTES = 96 * 1024 * 1024
AUDIO_MAX_USER_FILES = 20
AUDIO_MAX_SECONDS = 300
AUDIO_UPLOAD_ID_RE = re.compile(r"^aud_[0-9a-f]{32}$")
AUDIO_MIME_EXTENSIONS = {
    "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/ogg": ".ogg",
}
_UPLOAD_LOCK = threading.Lock()


def detect_mime(header):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return ""


def detect_video_mime(header):
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/quicktime" if header[8:12] == b"qt  " else "video/mp4"
    return ""


def detect_audio_mime(header):
    if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0:
        return "audio/aac"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "audio/mp4"
    return ""


def detect_material_mime(header):
    """顾客素材的类型识别：图片 / 视频 / 音频。

    一律**只看文件头** —— 不信任客户端声明的 Content-Type，也不看文件名后缀。
    认得出来才收；认不出来一律拒绝（宁可拒掉冷门格式，也不放行识别不了的字节）。

    ⚠️ 本函数只看传入的前若干字节，调用方给的是前 16 字节，所以**不要**往这里
    加需要更深字节才能区分的格式（例如 MKV —— 它和 WebM 共用魔数，只能靠
    DocType 区分，前 16 字节里没有）。
    """
    if not header:
        return ""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith(b"II*\x00") or header.startswith(b"MM\x00*"):
        return "image/tiff"
    if header.startswith(b"fLaC"):
        return "audio/flac"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    if header.startswith(b"ID3"):
        return "audio/mpeg"
    # RIFF 家族：第 8-12 字节区分 WAVE(音频) / AVI(视频) / WEBP(图片)
    if len(header) >= 12 and header[:4] == b"RIFF":
        kind = header[8:12]
        if kind == b"WEBP":
            return "image/webp"
        if kind == b"WAVE":
            return "audio/wav"
        if kind == b"AVI ":
            return "video/x-msvideo"
        return ""
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    # ISO-BMFF 家族：ftyp 后面的 brand 决定是图片 / 视频 / 音频
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "image/heic"
        if brand == b"qt  ":
            return "video/quicktime"
        if brand[:3] == b"M4A":
            return "audio/mp4"
        return "video/mp4"
    # 帧同步型音频 —— 必须排在 JPEG 之后：JPEG 是 FF D8 FF，
    # 0xD8 & 0xE0 == 0xC0，不会命中下面的掩码，两者不冲突。
    if len(header) >= 2 and header[0] == 0xFF:
        if header[1] & 0xF6 == 0xF0:
            return "audio/aac"
        if header[1] & 0xE0 == 0xE0:
            return "audio/mpeg"
    return ""


def _owner_hash(username):
    return hashlib.sha256(str(username or "").encode("utf-8")).hexdigest()


def _paths(upload_id, extension):
    root = UPLOAD_ROOT.resolve()
    data = (root / (upload_id + extension)).resolve()
    meta = (root / (upload_id + ".json")).resolve()
    data.relative_to(root)
    meta.relative_to(root)
    return data, meta


def _video_paths(upload_id, extension):
    root = UPLOAD_ROOT.resolve()
    data = (root / (upload_id + extension)).resolve()
    meta = (root / (upload_id + ".json")).resolve()
    data.relative_to(root)
    meta.relative_to(root)
    return data, meta


def _delete_upload(upload_id):
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        return
    # 素材口（store_material）与图片口共用 img_ 前缀，扩展名却宽得多
    # （.heic/.mp4/.mp3…）。这里必须把两张表的扩展名都覆盖，
    # 否则过期素材只会被删掉 .json，数据文件留在盘上变成垃圾。
    suffixes = set(MIME_EXTENSIONS.values()) | set(MATERIAL_MIME_EXTENSIONS.values())
    for suffix in tuple(suffixes) + (".json",):
        try:
            (UPLOAD_ROOT / (upload_id + suffix)).unlink(missing_ok=True)
        except OSError:
            pass


def _delete_video_upload(upload_id):
    if not VIDEO_UPLOAD_ID_RE.fullmatch(upload_id):
        return
    for suffix in tuple(VIDEO_MIME_EXTENSIONS.values()) + (".json",):
        try:
            (UPLOAD_ROOT / (upload_id + suffix)).unlink(missing_ok=True)
        except OSError:
            pass


def _delete_audio_upload(upload_id):
    if not AUDIO_UPLOAD_ID_RE.fullmatch(upload_id):
        return
    for suffix in tuple(set(AUDIO_MIME_EXTENSIONS.values())) + (".json",):
        try:
            (UPLOAD_ROOT / (upload_id + suffix)).unlink(missing_ok=True)
        except OSError:
            pass


def _cleanup(now):
    # ponytail: 试点量小，上传时扫目录；文件量上千后再换 SQLite 索引。
    try:
        for temp_path in UPLOAD_ROOT.glob(".*.tmp"):
            try:
                if temp_path.stat().st_mtime < now - 600:
                    temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        entries = list(UPLOAD_ROOT.glob("img_*.json"))
    except OSError:
        return
    for meta_path in entries:
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                raise ValueError("metadata too large")
            expires_at = int(json.loads(raw_meta).get("expires_at") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            expires_at = 0
        if expires_at <= now:
            _delete_upload(meta_path.stem)
    for meta_path in UPLOAD_ROOT.glob("vid_*.json"):
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                raise ValueError("metadata too large")
            expires_at = int(json.loads(raw_meta).get("expires_at") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            expires_at = 0
        if expires_at <= now:
            _delete_video_upload(meta_path.stem)
    for meta_path in UPLOAD_ROOT.glob("aud_*.json"):
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                raise ValueError("metadata too large")
            expires_at = int(json.loads(raw_meta).get("expires_at") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            expires_at = 0
        if expires_at <= now:
            _delete_audio_upload(meta_path.stem)


def _active_usage(owner_hash, now):
    count = total = 0
    for meta_path in UPLOAD_ROOT.glob("img_*.json"):
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                continue
            meta = json.loads(raw_meta)
            if int(meta.get("expires_at") or 0) > now and hmac.compare_digest(
                    str(meta.get("owner_hash") or ""), owner_hash):
                count += 1
                total += int(meta.get("bytes") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return count, total


def _active_video_usage(owner_hash, now):
    count = total = 0
    for meta_path in UPLOAD_ROOT.glob("vid_*.json"):
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                continue
            meta = json.loads(raw_meta)
            if int(meta.get("expires_at") or 0) > now and hmac.compare_digest(
                    str(meta.get("owner_hash") or ""), owner_hash):
                count += 1
                total += int(meta.get("bytes") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return count, total


def _active_audio_usage(owner_hash, now):
    count = total = 0
    for meta_path in UPLOAD_ROOT.glob("aud_*.json"):
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                continue
            meta = json.loads(raw_meta)
            if int(meta.get("expires_at") or 0) > now and hmac.compare_digest(
                    str(meta.get("owner_hash") or ""), owner_hash):
                count += 1
                total += int(meta.get("bytes") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return count, total


def store_image(stream, length, username, content_type, expected_sha256, now=None):
    now = int(time.time() if now is None else now)
    if not username:
        raise ValueError("缺少上传账号")
    if content_type not in MIME_EXTENSIONS:
        raise ValueError("只支持 PNG / JPG / WebP")
    if not isinstance(length, int) or length <= 0 or length > MAX_BYTES:
        raise ValueError("图片大小必须在 1B 到 10MB 之间")
    expected_sha256 = str(expected_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("缺少有效的图片摘要")

    with _UPLOAD_LOCK:
        return _store_image(stream, length, username, content_type, expected_sha256, now)


def _store_image(stream, length, username, content_type, expected_sha256, now):
    return _store_blob(
        stream, length, username, content_type, expected_sha256, now,
        mime_table=MIME_EXTENSIONS, sniff=detect_mime,
        max_bytes=MAX_BYTES, max_files=MAX_USER_FILES,
        max_user_bytes=MAX_USER_BYTES,
        too_big="图片大小必须在 1B 到 %s 之间" % _human_size(MAX_BYTES),
        quota_full="当前账号的临时图片已达上限，请等待过期后重试",
        incomplete="图片上传不完整",
        changed="图片上传过程中发生变化，请重新上传",
        mismatch="图片内容与声明格式不一致",
        no_space="图片临时空间不足",
    )


def store_material(stream, length, username, content_type, expected_sha256, now=None):
    """顾客素材上传（数字人一键成片）：比通用图片口宽得多。

    与 store_image 的差别**只有**：允许的类型（图片/视频/音频）与限额
    （单文件 200MB、整批 200MB、一批 10 个）。落盘、摘要、原子替换、
    目录权限这些安全措施完全沿用同一条实现。

    ⚠️ 这里**不看**客户端声明的 content_type 是否在表内就放行 ——
    声明值只用来做「和文件头是否一致」的比对，真正的判据是文件头。
    """
    now = int(time.time() if now is None else now)
    if not username:
        raise ValueError("缺少上传账号")
    if content_type and content_type not in MATERIAL_MIME_EXTENSIONS:
        raise ValueError("暂不支持该文件类型，仅支持%s" % MATERIAL_TYPE_HINT)
    if not isinstance(length, int) or length <= 0 or length > MATERIAL_MAX_BYTES:
        raise ValueError(
            "单个素材大小必须在 1B 到 %s 之间" % _human_size(MATERIAL_MAX_BYTES))
    expected_sha256 = str(expected_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("缺少有效的素材摘要")

    with _UPLOAD_LOCK:
        return _store_blob(
            stream, length, username, content_type, expected_sha256, now,
            mime_table=MATERIAL_MIME_EXTENSIONS, sniff=detect_material_mime,
            max_bytes=MATERIAL_MAX_BYTES, max_files=MATERIAL_MAX_USER_FILES,
            max_user_bytes=MATERIAL_MAX_USER_BYTES,
            too_big="单个素材大小必须在 1B 到 %s 之间" % _human_size(MATERIAL_MAX_BYTES),
            quota_full=(
                "本批素材已达上限（最多 %d 个、合计 %s），请先移除部分文件或等待过期后重试"
                % (MATERIAL_MAX_USER_FILES, _human_size(MATERIAL_MAX_USER_BYTES))
            ),
            incomplete="素材上传不完整",
            changed="素材上传过程中发生变化，请重新上传",
            mismatch="素材内容与声明格式不一致，或属于暂不支持的类型（仅支持%s）" % MATERIAL_TYPE_HINT,
            no_space="素材临时空间不足",
        )


def _human_size(num_bytes):
    return "%dMB" % (int(num_bytes) // (1024 * 1024))


def _store_blob(stream, length, username, content_type, expected_sha256, now,
                *, mime_table, sniff, max_bytes, max_files, max_user_bytes,
                too_big, quota_full, incomplete, changed, mismatch, no_space):
    """上传落盘的唯一实现 —— 图片口与素材口共用，差别只在传进来的参数。

    安全措施（0600 文件 / 0700 目录 / 边收边算摘要 / 临时文件 + 原子替换 /
    出错即清理）对所有调用方一视同仁，**不要**因为「素材口更宽」而在
    这里开口子。
    """
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(UPLOAD_ROOT, 0o700)
    _cleanup(now)
    count, total = _active_usage(_owner_hash(username), now)
    if count >= max_files or total + length > max_user_bytes:
        raise ValueError(quota_full)
    if shutil.disk_usage(UPLOAD_ROOT).free - length < MIN_FREE_BYTES:
        raise OSError(no_space)
    upload_id = "img_" + uuid.uuid4().hex
    extension = mime_table.get(content_type) or ".bin"
    data_path, meta_path = _paths(upload_id, extension)
    temp_data = UPLOAD_ROOT / ("." + upload_id + ".tmp")
    temp_meta = UPLOAD_ROOT / ("." + upload_id + ".json.tmp")
    digest = hashlib.sha256()
    header = b""
    remaining = length
    descriptor = os.open(str(temp_data), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    raise ValueError(incomplete)
                if len(header) < 16:
                    header += chunk[:16 - len(header)]
                handle.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        actual_sha256 = digest.hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ValueError(changed)
        # 以文件头为准判定真实类型；声明值只是用来交叉核对。
        detected = sniff(header)
        if not detected or detected not in mime_table:
            raise ValueError(mismatch)
        if content_type and detected != content_type:
            raise ValueError(mismatch)
        extension = mime_table[detected]
        data_path, meta_path = _paths(upload_id, extension)
        meta = {
            "version": 1,
            "owner_hash": _owner_hash(username),
            "mime": detected,
            "extension": extension,
            "bytes": length,
            "sha256": actual_sha256,
            "expires_at": now + TTL,
        }
        temp_meta.write_text(json.dumps(meta, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.chmod(temp_meta, 0o600)
        os.replace(temp_data, data_path)
        os.replace(temp_meta, meta_path)
    except Exception:
        temp_data.unlink(missing_ok=True)
        temp_meta.unlink(missing_ok=True)
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise
    return {
        "upload_id": upload_id,
        "mime": detected,
        "bytes": length,
        "sha256": expected_sha256,
        "expires_at": now + TTL,
        "expires_in": TTL,
    }


def _probe_video_duration(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=index,codec_type:format=duration",
             "-of", "json", str(path)],
            check=True, timeout=20, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        probe = json.loads(result.stdout)
        streams = probe.get("streams") or []
        if not any(stream.get("codec_type") == "video" for stream in streams):
            raise ValueError("missing video stream")
        duration = float((probe.get("format") or {}).get("duration"))
    except (FileNotFoundError, subprocess.SubprocessError, TypeError, ValueError,
            json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("无法读取视频时长，请换用完整的 MP4 / MOV / WebM") from exc
    if not 0 < duration <= VIDEO_MAX_SECONDS:
        raise ValueError("视频时长必须在 0-%d 秒之间" % VIDEO_MAX_SECONDS)
    return round(duration, 3)


def store_video(stream, length, username, content_type, expected_sha256, now=None):
    now = int(time.time() if now is None else now)
    if not username:
        raise ValueError("缺少上传账号")
    if content_type not in VIDEO_MIME_EXTENSIONS:
        raise ValueError("只支持 MP4 / MOV / WebM")
    if not isinstance(length, int) or length <= 0 or length > VIDEO_MAX_BYTES:
        raise ValueError("视频大小必须在 1B 到 32MB 之间")
    expected_sha256 = str(expected_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("缺少有效的视频摘要")

    with _UPLOAD_LOCK:
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(UPLOAD_ROOT, 0o700)
        _cleanup(now)
        count, total = _active_video_usage(_owner_hash(username), now)
        if count >= VIDEO_MAX_USER_FILES or total + length > VIDEO_MAX_USER_BYTES:
            raise ValueError("当前账号的临时视频已达上限，请等待过期后重试")
        if shutil.disk_usage(UPLOAD_ROOT).free - length < MIN_FREE_BYTES:
            raise OSError("视频临时空间不足")

        upload_id = "vid_" + uuid.uuid4().hex
        extension = VIDEO_MIME_EXTENSIONS[content_type]
        data_path, meta_path = _video_paths(upload_id, extension)
        temp_data = UPLOAD_ROOT / ("." + upload_id + ".tmp")
        temp_meta = UPLOAD_ROOT / ("." + upload_id + ".json.tmp")
        digest = hashlib.sha256()
        header = b""
        remaining = length
        descriptor = os.open(str(temp_data), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                while remaining:
                    chunk = stream.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise ValueError("视频上传不完整")
                    if len(header) < 32:
                        header += chunk[:32 - len(header)]
                    handle.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            actual_sha256 = digest.hexdigest()
            if not hmac.compare_digest(actual_sha256, expected_sha256):
                raise ValueError("视频上传过程中发生变化，请重新上传")
            if detect_video_mime(header) != content_type:
                raise ValueError("视频内容与声明格式不一致")
            duration = _probe_video_duration(temp_data)
            meta = {
                "version": 1,
                "owner_hash": _owner_hash(username),
                "mime": content_type,
                "extension": extension,
                "bytes": length,
                "sha256": actual_sha256,
                "duration": duration,
                "expires_at": now + TTL,
            }
            temp_meta.write_text(json.dumps(meta, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.chmod(temp_meta, 0o600)
            os.replace(temp_data, data_path)
            os.replace(temp_meta, meta_path)
        except Exception:
            temp_data.unlink(missing_ok=True)
            temp_meta.unlink(missing_ok=True)
            data_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise
    return {
        "upload_id": upload_id,
        "mime": content_type,
        "bytes": length,
        "sha256": expected_sha256,
        "duration": duration,
        "expires_at": now + TTL,
        "expires_in": TTL,
    }


def _probe_audio_duration(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index,codec_type:format=duration",
             "-of", "json", str(path)],
            check=True, timeout=20, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        probe = json.loads(result.stdout)
        streams = probe.get("streams") or []
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            raise ValueError("missing audio stream")
        duration = float((probe.get("format") or {}).get("duration"))
    except (FileNotFoundError, subprocess.SubprocessError, TypeError, ValueError,
            json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("无法读取音频，请换用完整的 MP3 / WAV / M4A / AAC / OGG") from exc
    if not 0 < duration <= AUDIO_MAX_SECONDS:
        raise ValueError("音频时长必须在 0-%d 秒之间" % AUDIO_MAX_SECONDS)
    return round(duration, 3)


def store_audio(stream, length, username, content_type, expected_sha256, now=None):
    now = int(time.time() if now is None else now)
    if not username:
        raise ValueError("缺少上传账号")
    if content_type not in AUDIO_MIME_EXTENSIONS:
        raise ValueError("只支持 MP3 / WAV / M4A / AAC / OGG")
    if not isinstance(length, int) or length <= 0 or length > AUDIO_MAX_BYTES:
        raise ValueError("音频大小必须在 1B 到 10MB 之间")
    expected_sha256 = str(expected_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("缺少有效的音频摘要")

    with _UPLOAD_LOCK:
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(UPLOAD_ROOT, 0o700)
        _cleanup(now)
        count, total = _active_audio_usage(_owner_hash(username), now)
        if count >= AUDIO_MAX_USER_FILES or total + length > AUDIO_MAX_USER_BYTES:
            raise ValueError("当前账号的临时音频已达上限，请等待过期后重试")
        if shutil.disk_usage(UPLOAD_ROOT).free - length < MIN_FREE_BYTES:
            raise OSError("音频临时空间不足")

        upload_id = "aud_" + uuid.uuid4().hex
        extension = AUDIO_MIME_EXTENSIONS[content_type]
        data_path, meta_path = _paths(upload_id, extension)
        temp_data = UPLOAD_ROOT / ("." + upload_id + ".tmp")
        temp_meta = UPLOAD_ROOT / ("." + upload_id + ".json.tmp")
        digest = hashlib.sha256()
        header = b""
        remaining = length
        descriptor = os.open(str(temp_data), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                while remaining:
                    chunk = stream.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise ValueError("音频上传不完整")
                    if len(header) < 32:
                        header += chunk[:32 - len(header)]
                    handle.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            actual_sha256 = digest.hexdigest()
            if not hmac.compare_digest(actual_sha256, expected_sha256):
                raise ValueError("音频上传过程中发生变化，请重新上传")
            detected = detect_audio_mime(header)
            if detected != content_type and not (
                detected == "audio/wav" and content_type == "audio/x-wav"
            ) and not (detected == "audio/mp4" and content_type == "audio/x-m4a"):
                raise ValueError("音频内容与声明格式不一致")
            duration = _probe_audio_duration(temp_data)
            meta = {
                "version": 1, "owner_hash": _owner_hash(username), "mime": content_type,
                "extension": extension, "bytes": length, "sha256": actual_sha256,
                "duration": duration, "expires_at": now + TTL,
            }
            temp_meta.write_text(json.dumps(meta, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.chmod(temp_meta, 0o600)
            os.replace(temp_data, data_path)
            os.replace(temp_meta, meta_path)
        except Exception:
            temp_data.unlink(missing_ok=True)
            temp_meta.unlink(missing_ok=True)
            data_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise
    return {
        "upload_id": upload_id, "mime": content_type, "bytes": length,
        "sha256": expected_sha256, "duration": duration,
        "expires_at": now + TTL, "expires_in": TTL,
    }


def _load_image(upload_id, username, now, *,
                mime_table=MIME_EXTENSIONS, max_bytes=MAX_BYTES,
                sniff=detect_mime, label="图片"):
    upload_id = str(upload_id or "").strip().lower()
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        raise ValueError("%s upload_id 格式不合法" % label)
    _, meta_path = _paths(upload_id, ".png")
    try:
        raw_meta = meta_path.read_bytes()
        if len(raw_meta) > 4096:
            raise ValueError("%s upload_id 元数据异常" % label)
        meta = json.loads(raw_meta)
        extension = str(meta.get("extension") or "")
        data_path, _ = _paths(upload_id, extension)
        data = data_path.read_bytes()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("%s upload_id 不存在或已失效" % label)
    if meta.get("version") != 1 or extension not in mime_table.values():
        raise ValueError("%s upload_id 元数据异常" % label)
    if not hmac.compare_digest(str(meta.get("owner_hash") or ""), _owner_hash(username)):
        raise ValueError("%s upload_id 不存在或已失效" % label)
    if int(meta.get("expires_at") or 0) <= now:
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise ValueError("%s upload_id 已过期，请重新上传" % label)
    if not 0 < len(data) <= max_bytes or len(data) != int(meta.get("bytes") or -1):
        raise ValueError("%s upload_id 文件异常" % label)
    if sniff(data[:16]) != meta.get("mime"):
        raise ValueError("%s upload_id 文件格式异常" % label)
    digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(digest, str(meta.get("sha256") or "")):
        raise ValueError("%s upload_id 文件校验失败" % label)
    return base64.b64encode(data).decode("ascii"), meta


def load_image_data_url(upload_id, username, now=None):
    data, meta = _load_image(
        upload_id, username, int(time.time() if now is None else now))
    return "data:%s;base64,%s" % (meta["mime"], data)


def inspect_image(upload_id, username, now=None):
    """Return owner-scoped image metadata without exposing the stored path."""
    _data, meta = _load_image(
        upload_id, username, int(time.time() if now is None else now))
    return dict(meta)


def read_image_bytes(upload_id, username, now=None):
    """Return verified bytes for an owner-scoped temporary image."""
    data, meta = _load_image(
        upload_id, username, int(time.time() if now is None else now))
    return base64.b64decode(data), dict(meta)


def _load_material(upload_id, username, now):
    """顾客素材的读取/校验：与图片口同一套实现，只换类型表与限额。"""
    return _load_image(
        upload_id, username, now,
        mime_table=MATERIAL_MIME_EXTENSIONS,
        max_bytes=MATERIAL_MAX_BYTES,
        sniff=detect_material_mime,
        label="素材",
    )


def inspect_material(upload_id, username, now=None):
    """Return owner-scoped material metadata without exposing the stored path."""
    _data, meta = _load_material(
        upload_id, username, int(time.time() if now is None else now))
    return dict(meta)


def read_material_bytes(upload_id, username, now=None):
    """Return verified bytes for an owner-scoped material.

    ⚠️ 会把整个文件读进内存 —— 目前只有图片安全检测用它（检测接口只吃原始字节）。
    视频/音频**不要**走这里，用 open_material() 流式读。
    """
    data, meta = _load_material(
        upload_id, username, int(time.time() if now is None else now))
    return base64.b64decode(data), dict(meta)


def discard_material(upload_id, username, now=None):
    """Delete one verified owner-scoped material upload."""
    _data, meta = _load_material(
        upload_id, username, int(time.time() if now is None else now))
    upload_id = str(upload_id).strip().lower()
    data_path, meta_path = _paths(upload_id, meta["extension"])
    data_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return True


def approve_image(upload_id, username, purpose, lease_seconds, now=None):
    """Bind a temporary image to one server-known purpose for a short lease."""
    now = int(time.time() if now is None else now)
    _data, meta = _load_image(upload_id, username, now)
    purpose = str(purpose or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", purpose):
        raise ValueError("图片授权用途无效")
    lease_seconds = int(lease_seconds or 0)
    if not 60 <= lease_seconds <= TTL:
        raise ValueError("图片授权期限无效")
    upload_id = str(upload_id).strip().lower()
    _data_path, meta_path = _paths(upload_id, meta["extension"])
    approved = dict(meta)
    approved["approved_for"] = purpose
    approved["approved_until"] = min(
        int(meta["expires_at"]), now + lease_seconds,
    )
    temp_path = meta_path.with_name("." + meta_path.name + ".approve.tmp")
    temp_path.write_text(
        json.dumps(approved, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, meta_path)
    return approved


def approve_material(upload_id, username, purpose, lease_seconds, now=None):
    """把一份临时素材绑定到某个服务端已知用途（短租约）。"""
    now = int(time.time() if now is None else now)
    _data, meta = _load_material(upload_id, username, now)
    purpose = str(purpose or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", purpose):
        raise ValueError("素材授权用途无效")
    lease_seconds = int(lease_seconds or 0)
    if not 60 <= lease_seconds <= TTL:
        raise ValueError("素材授权期限无效")
    upload_id = str(upload_id).strip().lower()
    _data_path, meta_path = _paths(upload_id, meta["extension"])
    approved = dict(meta)
    approved["approved_for"] = purpose
    approved["approved_until"] = min(
        int(meta["expires_at"]), now + lease_seconds,
    )
    temp_path = meta_path.with_name("." + meta_path.name + ".approve.tmp")
    temp_path.write_text(
        json.dumps(approved, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, meta_path)
    return approved


def discard_image(upload_id, username, now=None):
    """Delete one verified owner-scoped image upload."""
    _data, meta = _load_image(
        upload_id, username, int(time.time() if now is None else now))
    upload_id = str(upload_id).strip().lower()
    data_path, meta_path = _paths(upload_id, meta["extension"])
    data_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return True


def _load_video(upload_id, username, now):
    data, meta = _load_video_bytes(upload_id, username, now)
    return "data:%s;base64,%s" % (meta["mime"], base64.b64encode(data).decode("ascii")), meta


def _load_video_bytes(upload_id, username, now):
    upload_id = str(upload_id or "").strip().lower()
    if not VIDEO_UPLOAD_ID_RE.fullmatch(upload_id):
        raise ValueError("视频 upload_id 格式不合法")
    _, meta_path = _video_paths(upload_id, ".mp4")
    try:
        raw_meta = meta_path.read_bytes()
        if len(raw_meta) > 4096:
            raise ValueError("视频 upload_id 元数据异常")
        meta = json.loads(raw_meta)
        extension = str(meta.get("extension") or "")
        data_path, _ = _video_paths(upload_id, extension)
        data = data_path.read_bytes()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("视频 upload_id 不存在或已失效")
    if meta.get("version") != 1 or extension not in VIDEO_MIME_EXTENSIONS.values():
        raise ValueError("视频 upload_id 元数据异常")
    if not hmac.compare_digest(str(meta.get("owner_hash") or ""), _owner_hash(username)):
        raise ValueError("视频 upload_id 不存在或已失效")
    if int(meta.get("expires_at") or 0) <= now:
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise ValueError("视频 upload_id 已过期，请重新上传")
    if not 0 < len(data) <= VIDEO_MAX_BYTES or len(data) != int(meta.get("bytes") or -1):
        raise ValueError("视频 upload_id 文件异常")
    if detect_video_mime(data[:32]) != meta.get("mime"):
        raise ValueError("视频 upload_id 文件格式异常")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), str(meta.get("sha256") or "")):
        raise ValueError("视频 upload_id 文件校验失败")
    return data, meta


def load_preview(kind, upload_id, username, now=None):
    """Return verified private upload bytes without exposing its filesystem path."""
    now = int(time.time() if now is None else now)
    if kind == "image":
        data, meta = read_image_bytes(upload_id, username, now)
    elif kind == "video":
        data, meta = _load_video_bytes(upload_id, username, now)
    else:
        raise ValueError("素材类型不支持预览")
    return data, str(meta["mime"])


def verify_upload(kind, upload_id, username, now=None):
    """Verify an account-owned upload before it becomes planning evidence.

    The check streams the payload to bind size, MIME and SHA-256 to its stored
    metadata.  It never materializes the upload in memory, and any mismatch
    fails closed so a stale or replaced file cannot make a plan executable.
    """
    now = int(time.time() if now is None else now)
    upload_id = str(upload_id or "").strip().lower()
    if kind == "image":
        if not UPLOAD_ID_RE.fullmatch(upload_id):
            return False
        _, meta_path = _paths(upload_id, ".png")
    elif kind == "video":
        if not VIDEO_UPLOAD_ID_RE.fullmatch(upload_id):
            return False
        _, meta_path = _video_paths(upload_id, ".mp4")
    else:
        return False
    try:
        raw_meta = meta_path.read_bytes()
        if len(raw_meta) > 4096:
            return False
        meta = json.loads(raw_meta)
        extension = str(meta.get("extension") or "")
        if kind == "image":
            data_path, _ = _paths(upload_id, extension)
            valid_extension = extension in MIME_EXTENSIONS.values()
            max_bytes = MAX_BYTES
            sniff_fn = detect_mime
        else:
            data_path, _ = _video_paths(upload_id, extension)
            valid_extension = extension in VIDEO_MIME_EXTENSIONS.values()
            max_bytes = VIDEO_MAX_BYTES
            sniff_fn = detect_video_mime
        if meta.get("version") != 1 or not valid_extension:
            return False
        if not hmac.compare_digest(str(meta.get("owner_hash") or ""), _owner_hash(username)):
            return False
        if int(meta.get("expires_at") or 0) <= now:
            return False
        _verify_file_stream(data_path, meta, max_bytes, sniff_fn)
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _verify_file_stream(path, meta, max_bytes, sniff_fn):
    """Streaming size / mime / sha256 verification for file-backed previews."""
    try:
        size = path.stat().st_size
    except OSError:
        raise ValueError("素材文件不存在或已失效")
    if not 0 < size <= max_bytes or size != int(meta.get("bytes") or -1):
        raise ValueError("素材文件异常")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        head = handle.read(32)
        if sniff_fn(head) != meta.get("mime"):
            raise ValueError("素材文件格式异常")
        digest.update(head)
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), str(meta.get("sha256") or "")):
        raise ValueError("素材文件校验失败")
    return size


def open_preview(kind, upload_id, username, now=None):
    """Return ``(binary file handle, size, mime)`` for streaming preview.

    Owner / expiry / size / mime / sha256 checks are identical to the
    whole-read paths; the caller streams from the returned handle (64KB
    chunks, Range aware) instead of materializing up to 32MB in memory.
    """
    now = int(time.time() if now is None else now)
    if kind == "image":
        upload_id = str(upload_id or "").strip().lower()
        if not UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("图片 upload_id 格式不合法")
        _, meta_path = _paths(upload_id, ".png")
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                raise ValueError("图片 upload_id 元数据异常")
            meta = json.loads(raw_meta)
            extension = str(meta.get("extension") or "")
            data_path, _ = _paths(upload_id, extension)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise ValueError("图片 upload_id 不存在或已失效")
        if meta.get("version") != 1 or extension not in MIME_EXTENSIONS.values():
            raise ValueError("图片 upload_id 元数据异常")
        if not hmac.compare_digest(str(meta.get("owner_hash") or ""), _owner_hash(username)):
            raise ValueError("图片 upload_id 不存在或已失效")
        if int(meta.get("expires_at") or 0) <= now:
            data_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise ValueError("图片 upload_id 已过期，请重新上传")
        size = _verify_file_stream(data_path, meta, MAX_BYTES, detect_mime)
        return open(data_path, "rb"), size, str(meta["mime"])
    if kind == "video":
        upload_id = str(upload_id or "").strip().lower()
        if not VIDEO_UPLOAD_ID_RE.fullmatch(upload_id):
            raise ValueError("视频 upload_id 格式不合法")
        _, meta_path = _video_paths(upload_id, ".mp4")
        try:
            raw_meta = meta_path.read_bytes()
            if len(raw_meta) > 4096:
                raise ValueError("视频 upload_id 元数据异常")
            meta = json.loads(raw_meta)
            extension = str(meta.get("extension") or "")
            data_path, _ = _video_paths(upload_id, extension)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise ValueError("视频 upload_id 不存在或已失效")
        if meta.get("version") != 1 or extension not in VIDEO_MIME_EXTENSIONS.values():
            raise ValueError("视频 upload_id 元数据异常")
        if not hmac.compare_digest(str(meta.get("owner_hash") or ""), _owner_hash(username)):
            raise ValueError("视频 upload_id 不存在或已失效")
        if int(meta.get("expires_at") or 0) <= now:
            data_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise ValueError("视频 upload_id 已过期，请重新上传")
        size = _verify_file_stream(data_path, meta, VIDEO_MAX_BYTES, detect_video_mime)
        return open(data_path, "rb"), size, str(meta["mime"])
    raise ValueError("素材类型不支持预览")


def _load_audio(upload_id, username, now):
    upload_id = str(upload_id or "").strip().lower()
    if not AUDIO_UPLOAD_ID_RE.fullmatch(upload_id):
        raise ValueError("音频 upload_id 格式不合法")
    _, meta_path = _paths(upload_id, ".mp3")
    try:
        raw_meta = meta_path.read_bytes()
        if len(raw_meta) > 4096:
            raise ValueError("音频 upload_id 元数据异常")
        meta = json.loads(raw_meta)
        extension = str(meta.get("extension") or "")
        data_path, _ = _paths(upload_id, extension)
        data = data_path.read_bytes()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("音频 upload_id 不存在或已失效")
    if meta.get("version") != 1 or extension not in AUDIO_MIME_EXTENSIONS.values():
        raise ValueError("音频 upload_id 元数据异常")
    if not hmac.compare_digest(str(meta.get("owner_hash") or ""), _owner_hash(username)):
        raise ValueError("音频 upload_id 不存在或已失效")
    if int(meta.get("expires_at") or 0) <= now:
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise ValueError("音频 upload_id 已过期，请重新上传")
    if not 0 < len(data) <= AUDIO_MAX_BYTES or len(data) != int(meta.get("bytes") or -1):
        raise ValueError("音频 upload_id 文件异常")
    detected = detect_audio_mime(data[:32])
    if detected != meta.get("mime") and not (
        detected == "audio/wav" and meta.get("mime") == "audio/x-wav"
    ) and not (detected == "audio/mp4" and meta.get("mime") == "audio/x-m4a"):
        raise ValueError("音频 upload_id 文件格式异常")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), str(meta.get("sha256") or "")):
        raise ValueError("音频 upload_id 文件校验失败")
    return "data:%s;base64,%s" % (meta["mime"], base64.b64encode(data).decode("ascii")), meta


def expand_voice_clone_payload(payload, username, now=None):
    if not isinstance(payload, dict) or set(payload) != {"slot_id", "name", "audio_upload_id"}:
        raise ValueError("声音克隆只接受 slot_id、name 和 audio_upload_id")
    now = int(time.time() if now is None else now)
    audio, meta = _load_audio(payload["audio_upload_id"], username, now)
    audio_format = str(meta.get("extension") or ".mp3").lower().lstrip(".")
    if audio_format == "wave":
        audio_format = "wav"
    return {
        "slot_id": str(payload["slot_id"] or "").strip(),
        "name": str(payload["name"] or "").strip(),
        "audio": audio,
        "audio_format": audio_format,
    }


def expand_image_payload(payload, username, now=None):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    body = dict(payload)
    image_id = body.pop("image_upload_id", None)
    mask_id = body.pop("mask_upload_id", None)
    reference_ids = body.pop("reference_upload_ids", None)
    if image_id is None and mask_id is None and reference_ids is None:
        return body
    if body.get("image") or body.get("mask") or body.get("reference_images"):
        raise ValueError("upload_id 不能与 base64 图片字段同时使用")
    if image_id and reference_ids:
        raise ValueError("单参考图和多参考图不能同时使用")
    if mask_id and not image_id:
        raise ValueError("蒙版必须同时提供原图 upload_id")
    provider = str(body.get("provider") or "openai").strip().lower()
    if reference_ids is not None:
        limits = {"openai": 16, "seedream": 10, "xiaole": 4, "banana": 14,
                  "grok": 7, "micro": 9, "omni": 6, "minimax": 5, "sora": 1}
        target = str(body.get("channel") or provider).strip().lower()
        limit = limits.get(target, 1)
        if not isinstance(reference_ids, list) or not 1 <= len(reference_ids) <= limit:
            raise ValueError("reference_upload_ids 必须包含 1-%d 项" % limit)
    if mask_id and provider != "openai":
        raise ValueError("蒙版局部修改仅支持 OpenAI 图片引擎")

    now = int(time.time() if now is None else now)
    if image_id:
        body["image"] = _load_image(image_id, username, now)[0]
    if mask_id:
        mask, meta = _load_image(mask_id, username, now)
        if meta.get("mime") != "image/png":
            raise ValueError("蒙版必须是 PNG 图片")
        body["mask"] = mask
    if reference_ids is not None:
        loaded = [_load_image(item, username, now) for item in reference_ids]
        target = str(body.get("channel") or provider).strip().lower()
        digital_human_bound = str(
            body.get("digital_human_pipeline") or ""
        ).strip().lower().startswith("digital_human")
        if target == "banana" and not digital_human_bound:
            body["images"] = [
                {"data": data, "mime_type": meta["mime"]}
                for data, meta in loaded
            ]
        elif target in {"minimax", "omni", "sora"}:
            body["reference_images"] = [
                "data:%s;base64,%s" % (meta["mime"], data)
                for data, meta in loaded
            ]
        else:
            body["reference_images"] = [data for data, _meta in loaded]
    return body


def expand_avatar_payload(payload, username, now=None):
    """Expand one owner-bound Agent image upload for avatar creation."""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    body = dict(payload)
    upload_id = body.pop("image_upload_id", None)
    if upload_id is None:
        return body
    if body.get("image_data"):
        raise ValueError("image_upload_id 不能与 image_data 同时使用")
    now = int(time.time() if now is None else now)
    data, meta = _load_image(upload_id, username, now)
    body["image_data"] = "data:%s;base64,%s" % (meta["mime"], data)
    return body


def expand_role_media_payload(payload, username, now=None):
    """Expand owner-bound upload IDs into the exact media roles existing generators accept."""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    body = dict(payload)
    now = int(time.time() if now is None else now)
    image_roles = {
        "person_image_upload_id": "person_image_data",
        "clothes_upload_id": "clothes_data",
        "background_upload_id": "background_data",
    }
    video_roles = {
        "person_video_upload_id": "person_video_data",
    }
    for upload_key, data_key in image_roles.items():
        upload_id = body.pop(upload_key, None)
        if upload_id is None:
            continue
        if body.get(data_key):
            raise ValueError("%s 不能与 %s 同时使用" % (upload_key, data_key))
        data, meta = _load_image(upload_id, username, now)
        body[data_key] = "data:%s;base64,%s" % (meta["mime"], data)
    for upload_key, data_key in video_roles.items():
        upload_id = body.pop(upload_key, None)
        if upload_id is None:
            continue
        if body.get(data_key):
            raise ValueError("%s 不能与 %s 同时使用" % (upload_key, data_key))
        data, meta = _load_video(upload_id, username, now)
        if float(meta.get("duration") or 0) > 6:
            raise ValueError("经典换装视频不能超过 6 秒")
        body[data_key] = data

    image_ids = body.pop("reference_image_upload_ids", None)
    if image_ids is not None:
        if body.get("reference_images"):
            raise ValueError("reference_image_upload_ids 不能与 reference_images 同时使用")
        raw_avatar_ids = body.get("avatar_ids")
        if isinstance(raw_avatar_ids, (list, tuple)) and raw_avatar_ids:
            avatar_count = len(raw_avatar_ids)
        elif raw_avatar_ids is None and body.get("avatar_id") is not None:
            avatar_count = 1
        else:
            avatar_count = 3
        image_limit = max(0, 9 - avatar_count)
        if not isinstance(image_ids, list) or not 1 <= len(image_ids) <= image_limit:
            raise ValueError("reference_image_upload_ids 超出额度（与形象共用 9 张，当前最多 %d 项）" % image_limit)
        body["reference_images"] = []
        for upload_id in image_ids:
            data, meta = _load_image(upload_id, username, now)
            body["reference_images"].append("data:%s;base64,%s" % (meta["mime"], data))

    video_ids = body.pop("reference_video_upload_ids", None)
    if video_ids is not None:
        if body.get("reference_videos") or body.get("reference_video_data"):
            raise ValueError("reference_video_upload_ids 不能与视频 base64 字段同时使用")
        if not isinstance(video_ids, list) or not 1 <= len(video_ids) <= 3:
            raise ValueError("reference_video_upload_ids 必须包含 1-3 项")
        body["reference_videos"] = [_load_video(item, username, now)[0] for item in video_ids]
    return body


def expand_talking_media_payload(payload, username, now=None):
    """Expand one-off IP12 portrait/audio uploads into the existing talking-video payload."""
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    body = dict(payload)
    now = int(time.time() if now is None else now)
    image_id = body.pop("image_upload_id", None)
    audio_id = body.pop("audio_upload_id", None)
    if image_id is not None:
        if body.get("image_data") or body.get("avatar_id"):
            raise ValueError("上传人物照片不能与已有形象同时使用")
        data, meta = _load_image(image_id, username, now)
        body["image_data"] = "data:%s;base64,%s" % (meta["mime"], data)
    if audio_id is not None:
        if body.get("audio_data") or body.get("audio_file"):
            raise ValueError("上传口播音频不能与已有音频同时使用")
        body["audio_data"] = _load_audio(audio_id, username, now)[0]
    return body
