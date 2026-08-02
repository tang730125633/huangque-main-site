"""Private, short-lived image inputs uploaded through HQ CLI."""

import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import shutil
import threading
import time
import uuid


MAX_BYTES = 10 * 1024 * 1024
MAX_USER_BYTES = 96 * 1024 * 1024
MAX_USER_FILES = 20
MIN_FREE_BYTES = 512 * 1024 * 1024
TTL = max(600, min(24 * 60 * 60, int(os.environ.get("HQ_CLI_IMAGE_UPLOAD_TTL", "3600") or 3600)))
UPLOAD_ROOT = pathlib.Path(os.environ.get(
    "HQ_CLI_UPLOAD_DIR",
    str(pathlib.Path(__file__).resolve().parents[1] / "content_out" / "_cli_uploads"),
))
UPLOAD_ID_RE = re.compile(r"^img_[0-9a-f]{32}$")
MIME_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_UPLOAD_LOCK = threading.Lock()


def detect_mime(header):
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
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


def _delete_upload(upload_id):
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        return
    for suffix in tuple(MIME_EXTENSIONS.values()) + (".json",):
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
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(UPLOAD_ROOT, 0o700)
    _cleanup(now)
    count, total = _active_usage(_owner_hash(username), now)
    if count >= MAX_USER_FILES or total + length > MAX_USER_BYTES:
        raise ValueError("当前账号的临时图片已达上限，请等待过期后重试")
    if shutil.disk_usage(UPLOAD_ROOT).free - length < MIN_FREE_BYTES:
        raise OSError("图片临时空间不足")
    upload_id = "img_" + uuid.uuid4().hex
    extension = MIME_EXTENSIONS[content_type]
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
                    raise ValueError("图片上传不完整")
                if len(header) < 16:
                    header += chunk[:16 - len(header)]
                handle.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        actual_sha256 = digest.hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ValueError("图片上传过程中发生变化，请重新上传")
        if detect_mime(header) != content_type:
            raise ValueError("图片内容与声明格式不一致")
        meta = {
            "version": 1,
            "owner_hash": _owner_hash(username),
            "mime": content_type,
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
        "mime": content_type,
        "bytes": length,
        "sha256": expected_sha256,
        "expires_at": now + TTL,
        "expires_in": TTL,
    }


def _load_image(upload_id, username, now):
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
        data = data_path.read_bytes()
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
    if not 0 < len(data) <= MAX_BYTES or len(data) != int(meta.get("bytes") or -1):
        raise ValueError("图片 upload_id 文件异常")
    if detect_mime(data[:16]) != meta.get("mime"):
        raise ValueError("图片 upload_id 文件格式异常")
    digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(digest, str(meta.get("sha256") or "")):
        raise ValueError("图片 upload_id 文件校验失败")
    return base64.b64encode(data).decode("ascii"), meta


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
        limits = {"openai": 16, "seedream": 10, "xiaole": 4,
                  "grok": 7, "micro": 9, "omni": 6}
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
        body["reference_images"] = [_load_image(item, username, now)[0] for item in reference_ids]
    return body
