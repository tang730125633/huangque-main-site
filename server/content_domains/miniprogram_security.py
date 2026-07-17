"""微信小程序内容安全检查。

生成任务在扣点、入队之前调用这里。文本走微信 msg_sec_check，用户上传的
图片走 img_sec_check；违规内容直接拒绝，微信服务异常时不收单，避免绕过审核。
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


API_BASE = "https://api.weixin.qq.com"
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE = {"value": "", "expires_at": 0}
_TEXT_KEYS = {
    "prompt", "text", "topic", "selling_points", "style", "title", "name",
    "description", "script", "content", "negative_prompt", "batch_label",
}
_IMAGE_KEY_MARKERS = ("image", "img", "photo", "clothes", "background")
_MAX_TEXT_BYTES = 480 * 1024
_MAX_IMAGES = 12


class ContentRejected(ValueError):
    pass


class SecurityUnavailable(RuntimeError):
    pass


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


def access_token():
    now = int(time.time())
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["value"] and _TOKEN_CACHE["expires_at"] > now + 60:
            return _TOKEN_CACHE["value"]
        appid = (os.environ.get("WX_MP_APPID") or "").strip()
        secret = (os.environ.get("WX_MP_APPSECRET") or "").strip()
        if not appid or not secret:
            raise SecurityUnavailable("内容安全服务尚未配置")
        query = urllib.parse.urlencode({"grant_type": "client_credential", "appid": appid, "secret": secret})
        result = _json_request(API_BASE + "/cgi-bin/token?" + query)
        if result.get("errcode") or not result.get("access_token"):
            raise SecurityUnavailable("内容安全服务暂时不可用，请稍后重试")
        _TOKEN_CACHE["value"] = result["access_token"]
        _TOKEN_CACHE["expires_at"] = now + int(result.get("expires_in") or 7200)
        return _TOKEN_CACHE["value"]


def _check_result(result):
    code = int(result.get("errcode") or 0)
    if code == 0:
        return
    if code == 87014:
        raise ContentRejected("内容可能违反平台规范，请修改后再提交")
    raise SecurityUnavailable("内容安全服务暂时不可用，请稍后重试")


def check_text(text):
    text = str(text or "").strip()
    if not text:
        return
    raw = text.encode("utf-8")
    if len(raw) > _MAX_TEXT_BYTES:
        raise ContentRejected("文本内容过长，请精简后再提交")
    token = urllib.parse.quote(access_token(), safe="")
    result = _json_request(API_BASE + "/wxa/msg_sec_check?access_token=" + token, {"content": text})
    _check_result(result)


def check_image(raw, filename="upload.jpg", content_type="image/jpeg"):
    if not raw:
        return
    token = urllib.parse.quote(access_token(), safe="")
    boundary = "----huangque" + uuid.uuid4().hex
    head = ("--%s\r\nContent-Disposition: form-data; name=\"media\"; filename=\"%s\"\r\n"
            "Content-Type: %s\r\n\r\n" % (boundary, filename, content_type)).encode("utf-8")
    body = head + raw + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + "/wxa/img_sec_check?access_token=" + token,
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SecurityUnavailable("图片安全检测暂时不可用，请稍后重试") from exc
    _check_result(result)


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

