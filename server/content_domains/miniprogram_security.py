"""微信小程序内容安全检查。

生成任务在扣点、入队之前调用这里。文本走微信 msg_sec_check，用户上传的
图片走 img_sec_check；违规内容直接拒绝，微信服务异常时不收单，避免绕过审核。

access_token 走稳定版接口（/cgi-bin/stable_token，force_refresh=false 时多实例
共享同一 token、互不挤占）；旧版 token 接口每签发即让其他实例 token 失效，
多实例部署会互打 40001。检测收到 40001/40014/42001 时仅重新获取平台共享
stable token 后重试一次；请求路径绝不强刷 token，避免双机再次互相作废。
"""
import base64
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
    DecompressionBombError = Image.DecompressionBombError
except ImportError:  # Production installs Pillow; keep imports usable in bare dev environments.
    Image = None
    ImageOps = None
    UnidentifiedImageError = OSError
    DecompressionBombError = ValueError


API_BASE = "https://api.weixin.qq.com"
_LOG = logging.getLogger(__name__)
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {"value": "", "expires_at": 0}
_TEXT_KEYS = {
    "prompt", "text", "topic", "selling_points", "style", "title", "name",
    "description", "script", "content", "negative_prompt", "batch_label",
}
_IMAGE_KEY_MARKERS = ("image", "img", "photo", "clothes", "background")
_MAX_TEXT_BYTES = 480 * 1024
_MAX_IMAGES = 12
_MAX_CHECK_IMAGE_BYTES = 900 * 1024
_MAX_CHECK_IMAGE_PIXELS = 40_000_000
_CHECK_IMAGE_EDGES = (2048, 1600, 1280, 1024, 768)
_CHECK_IMAGE_QUALITIES = (88, 80, 72, 64, 56)


class ContentRejected(ValueError):
    pass


class SecurityUnavailable(RuntimeError):
    pass


class _TokenInvalid(Exception):
    """微信侧判定当前 access_token 失效（errcode 40001/40014/42001）。

    典型诱因：多实例各自刷新普通 token 互相挤占、进程缓存的 token 被外部轮换。
    可条件失效旧缓存并获取平台共享 stable token 重试一次。
    """

    def __init__(self, token="", code=0):
        super().__init__("access_token 已失效(errcode=%s)" % int(code or 0))
        self.token = str(token or "")
        self.code = int(code or 0)


_TOKEN_INVALID_CODES = {40001, 40014, 42001}


def configured():
    return bool((os.environ.get("WX_MP_APPID") or "").strip() and
                (os.environ.get("WX_MP_APPSECRET") or "").strip())


def _json_request(url, payload=None, headers=None, timeout=15):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers or {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return json.loads(raw or "{}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SecurityUnavailable("内容安全服务暂时不可用，请稍后重试") from exc


def _fetch_token_locked(force_refresh=False):
    """Fetch and cache a stable token. Caller must hold ``_TOKEN_LOCK``."""
    appid = (os.environ.get("WX_MP_APPID") or "").strip()
    secret = (os.environ.get("WX_MP_APPSECRET") or "").strip()
    if not appid or not secret:
        raise SecurityUnavailable("内容安全服务尚未配置")
    result = _json_request(API_BASE + "/cgi-bin/stable_token", {
        "grant_type": "client_credential",
        "appid": appid,
        "secret": secret,
        "force_refresh": bool(force_refresh),
    })
    if result.get("errcode") or not result.get("access_token"):
        raise SecurityUnavailable("内容安全服务暂时不可用，请稍后重试")
    _TOKEN_CACHE["value"] = result["access_token"]
    _TOKEN_CACHE["expires_at"] = int(time.time()) + int(result.get("expires_in") or 7200)
    return _TOKEN_CACHE["value"]


def access_token(force_refresh=False):
    now = int(time.time())
    with _TOKEN_LOCK:
        if not force_refresh and _TOKEN_CACHE["value"] and _TOKEN_CACHE["expires_at"] > now + 60:
            return _TOKEN_CACHE["value"]
        # 稳定版 token（getStableAccessToken）：force_refresh=false 时多实例共享同一个
        # 有效 token、互不挤占；旧版 token 接口每签发一个新 token 就让其他实例的
        # token 在几分钟后失效——双机/多实例部署互打 40001 的根源。
        return _fetch_token_locked(force_refresh)


def _invalidate_token_cache(token=""):
    with _TOKEN_LOCK:
        if token and _TOKEN_CACHE["value"] != token:
            return False
        _TOKEN_CACHE["value"] = ""
        _TOKEN_CACHE["expires_at"] = 0
        return True


def _refresh_invalid_token(bad_token):
    """Reuse the platform-wide stable token without cross-instance invalidation."""
    with _TOKEN_LOCK:
        now = int(time.time())
        cached = _TOKEN_CACHE["value"]
        if cached and cached != bad_token and _TOKEN_CACHE["expires_at"] > now + 60:
            return cached
        if cached == bad_token:
            _TOKEN_CACHE["value"] = ""
            _TOKEN_CACHE["expires_at"] = 0

        # First ask for the platform-wide shared stable token.  This lets
        # another instance's completed refresh win without invalidating it.
        shared = _fetch_token_locked(False)
        if shared != bad_token:
            return shared

        # Never force-refresh from a request handler.  Two servers cannot share
        # this process lock, and simultaneous force_refresh=true calls would
        # invalidate each other's freshly issued tokens.  Drop the rejected
        # value so a later request checks the shared stable token again.
        _TOKEN_CACHE["value"] = ""
        _TOKEN_CACHE["expires_at"] = 0
        raise SecurityUnavailable("内容安全服务暂时不可用，请稍后重试")


def _with_token_retry(fn):
    """执行一次带 token 的微信调用；失效时获取共享 stable token 重试一次。

    fn(token) 使用传入的 token 调微信接口。
    重试仍失效才报不可用，避免坏缓存导致长时间 503。
    """
    token = access_token()
    try:
        return fn(token)
    except _TokenInvalid as invalid:
        bad_token = invalid.token or token
        retry_token = _refresh_invalid_token(bad_token)
        try:
            return fn(retry_token)
        except _TokenInvalid as exc:
            raise SecurityUnavailable("内容安全服务暂时不可用，请稍后重试") from exc


def _check_result(result, image=False, token=""):
    code = int(result.get("errcode") or 0)
    if code == 0:
        return
    _LOG.warning("WeChat content check failed: errcode=%s errmsg=%s",
                 code, str(result.get("errmsg") or "")[:300])
    if code == 87014:
        raise ContentRejected("内容可能违反平台规范，请修改后再提交")
    if image and code == 40006:
        raise ContentRejected("图片无法完成安全检测，请重新导出为 JPG 或 PNG 后上传")
    if code in _TOKEN_INVALID_CODES:
        raise _TokenInvalid(token, code)
    raise SecurityUnavailable("内容安全服务暂时不可用，请稍后重试")


def check_text(text):
    text = str(text or "").strip()
    if not text:
        return
    raw = text.encode("utf-8")
    if len(raw) > _MAX_TEXT_BYTES:
        raise ContentRejected("文本内容过长，请精简后再提交")

    def _do(token):
        encoded = urllib.parse.quote(token, safe="")
        result = _json_request(API_BASE + "/wxa/msg_sec_check?access_token=" + encoded, {"content": text})
        _check_result(result, token=token)

    _with_token_retry(_do)


def _prepare_image_for_security(raw, content_type):
    """Return a bounded review copy without changing the original upload."""
    if len(raw) <= _MAX_CHECK_IMAGE_BYTES:
        return raw, content_type
    if Image is None or ImageOps is None:
        raise SecurityUnavailable("图片安全检测预处理组件不可用，请稍后重试")

    try:
        with Image.open(io.BytesIO(raw)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > _MAX_CHECK_IMAGE_PIXELS:
                raise ContentRejected("图片分辨率过大，请缩小后再上传")
            source.seek(0)
            image = ImageOps.exif_transpose(source)
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.load()
    except ContentRejected:
        raise
    except (UnidentifiedImageError, DecompressionBombError, OSError, ValueError) as exc:
        raise ContentRejected("上传图片无法读取，请重新导出为 JPG 或 PNG 后上传") from exc

    resampling = getattr(Image, "Resampling", Image).LANCZOS
    source_edge = max(image.size)
    edges = []
    for edge in _CHECK_IMAGE_EDGES:
        bounded = min(source_edge, edge)
        if bounded not in edges:
            edges.append(bounded)

    for edge in edges:
        candidate = image.copy()
        candidate.thumbnail((edge, edge), resampling)
        for quality in _CHECK_IMAGE_QUALITIES:
            try:
                output = io.BytesIO()
                candidate.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
            except (OSError, ValueError) as exc:
                raise ContentRejected("上传图片无法处理，请重新导出为 JPG 或 PNG 后上传") from exc
            review = output.getvalue()
            if len(review) <= _MAX_CHECK_IMAGE_BYTES:
                return review, "image/jpeg"

    raise ContentRejected("图片复杂度过高，请缩小后再上传")


def check_image(raw, filename="upload.jpg", content_type="image/jpeg"):
    if not raw:
        return
    review, review_content_type = _prepare_image_for_security(raw, content_type)
    if review_content_type != content_type:
        filename = os.path.splitext(filename)[0] + ".jpg"

    def _do(token):
        encoded = urllib.parse.quote(token, safe="")
        boundary = "----huangque" + uuid.uuid4().hex
        head = ("--%s\r\nContent-Disposition: form-data; name=\"media\"; filename=\"%s\"\r\n"
                "Content-Type: %s\r\n\r\n" % (boundary, filename, review_content_type)).encode("utf-8")
        body = head + review + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        req = urllib.request.Request(
            API_BASE + "/wxa/img_sec_check?access_token=" + encoded,
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8", "replace") or "{}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise SecurityUnavailable("图片安全检测暂时不可用，请稍后重试") from exc
        _check_result(result, image=True, token=token)

    _with_token_retry(_do)


def _walk(value, key=""):
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _walk(child, str(child_key).lower())
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child, key)
    else:
        yield key, value


def _decode_data_image(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("data:image/") or ";base64," not in text[:128]:
        return None
    header, encoded = text.split(",", 1)
    content_type = header[5:].split(";", 1)[0].lower()
    try:
        return base64.b64decode(encoded, validate=True), content_type
    except Exception as exc:
        raise ContentRejected("上传图片格式无效") from exc


def check_payload(payload):
    """检查用户可控文本与 data:image 上传；未配置凭证的开发环境跳过。"""
    if not configured() or not isinstance(payload, dict):
        return
    texts, images = [], []
    for key, value in _walk(payload):
        if key in _TEXT_KEYS and isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text and text not in texts:
                texts.append(text)
        if any(marker in key for marker in _IMAGE_KEY_MARKERS):
            decoded = _decode_data_image(value)
            if decoded:
                images.append(decoded)
    if texts:
        check_text("\n".join(texts))
    for index, (raw, content_type) in enumerate(images[:_MAX_IMAGES]):
        ext = content_type.split("/", 1)[-1].replace("jpeg", "jpg")
        check_image(raw, "upload-%d.%s" % (index + 1, ext), content_type)
