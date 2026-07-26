# -*- coding: utf-8 -*-
"""Google Gemini Omni Flash 官方视频适配器。

创建请求可能计费，只提交一次；网络结果不明时绝不重发。后续文件状态查询与下载
都是幂等 GET，可以安全重试。本模块只访问 Google 官方域名，不回退旧中转站。
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from . import egress, provider_keys


MODEL = "gemini-omni-flash-preview"
OFFICIAL_API_BASE = "https://generativelanguage.googleapis.com"
API_BASE = (
    os.environ.get("GEMINI_OMNI_BASE", "").strip()
    or os.environ.get("GEMINI_BASE", "").strip()
    or OFFICIAL_API_BASE
).rstrip("/")
API_REVISION = "2026-05-20"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
TIMEOUT = max(60, int(os.environ.get("GEMINI_OMNI_TIMEOUT", "600") or 600))
POLL_INTERVAL = max(1, int(os.environ.get("GEMINI_OMNI_POLL_INTERVAL", "5") or 5))
MAX_VIDEO_BYTES = max(
    1024 * 1024,
    int(os.environ.get("GEMINI_OMNI_MAX_BYTES", str(128 * 1024 * 1024))
        or (128 * 1024 * 1024)),
)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_REFERENCE_IMAGES = 3
RATIOS = {"9:16", "16:9"}
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
TRANSIENT_GET_CODES = {408, 429} | set(range(500, 600))


class GeminiOmniCreateOutcomeUnknown(RuntimeError):
    """提交可能已被 Google 接收，禁止自动重发。"""


class GeminiOmniRejected(RuntimeError):
    """Google 明确拒绝了请求，没有可恢复的上游任务。"""


class GeminiOmniCredentialRejected(GeminiOmniRejected):
    """当前密钥在任务创建前被明确拒绝，可安全尝试下一条线路。"""


class GeminiOmniProviderFailed(RuntimeError):
    """已取得 interaction id，但 Google 返回明确失败终态。"""


class GeminiOmniTransientRead(RuntimeError):
    """查询或下载的幂等 GET 暂时失败，可以重试。"""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.HTTPError(newurl, 403, "拒绝非 HTTPS 下载跳转", headers, fp)
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(redirected.full_url)
        if old.netloc.lower() != new.netloc.lower():
            sensitive = {"authorization", "proxy-authorization", "x-goog-api-key", "cookie"}
            for bucket in (redirected.headers, redirected.unredirected_hdrs):
                for name in list(bucket):
                    if name.lower() in sensitive:
                        bucket.pop(name, None)
        return redirected


def available():
    return provider_keys.has_candidate("omni")


def _opener():
    proxy = egress.preferred_proxy()
    handlers = [_SafeRedirectHandler()]
    if proxy:
        handlers.insert(
            0, urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener(*handlers)


def _redact(value, api_key=None):
    text = str(value or "")
    for secret in (api_key, GEMINI_API_KEY):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return re.sub(r"(?i)(key=|x-goog-api-key[\"'=:\s]+)[^&\s\",}]+", r"\1[已隐藏]", text)[:500]


def _error_detail(exc, api_key=None):
    try:
        return _redact(exc.read().decode("utf-8", "replace"), api_key)
    except Exception:
        return _redact(exc, api_key)


def _raise_http_error(exc, method, api_key=None):
    detail = _error_detail(exc, api_key)
    if method == "GET" and exc.code in TRANSIENT_GET_CODES:
        raise GeminiOmniTransientRead(
            "Gemini Omni 查询暂时失败：HTTP %s %s" % (exc.code, detail)
        ) from exc
    if method == "GET":
        # 已拿到 interaction id 后，查询失败不等于生成失败；保留已计费任务供人工恢复。
        raise RuntimeError(
            "Gemini Omni 查询无法继续：HTTP %s %s" % (exc.code, detail)
        ) from exc
    if method == "POST" and (exc.code == 408 or 500 <= exc.code <= 599):
        raise GeminiOmniCreateOutcomeUnknown(
            "Gemini Omni 提交结果未知：HTTP %s %s；已禁止自动重发"
            % (exc.code, detail)
        ) from exc
    if exc.code == 400:
        message = "Gemini Omni 参数或内容未通过校验"
    elif exc.code in (401, 403):
        message = "Gemini Omni 鉴权或权限失败，请检查付费项目、API Key 和调用地区"
    elif exc.code == 429:
        message = "Gemini Omni 当前额度或并发已达上限，请稍后重试"
    elif exc.code in (500, 502, 503, 504):
        message = "Gemini Omni 官方服务暂时不可用"
    else:
        message = "Gemini Omni 官方接口失败（HTTP %s）" % exc.code
    rejected = (
        GeminiOmniCredentialRejected
        if method == "POST" and exc.code in {401, 402, 403, 429}
        else GeminiOmniRejected
    )
    raise rejected("%s：%s" % (message, detail)) from exc


def _request(opener, method, url, body=None, timeout=90, api_key=None):
    api_key = GEMINI_API_KEY if api_key is None else str(api_key).strip()
    if not api_key:
        raise ValueError("Gemini Omni 未配置（GEMINI_API_KEY）")
    headers = {
        "Accept": "application/json",
        "Api-Revision": API_REVISION,
        "User-Agent": "huangque-content/1.0",
        "x-goog-api-key": api_key,
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        _raise_http_error(exc, method, api_key)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        message = "Gemini Omni 网络异常：%s" % _redact(exc, api_key)
        if method == "POST":
            raise GeminiOmniCreateOutcomeUnknown(
                message + "；提交结果未知，已禁止自动重发"
            ) from exc
        raise GeminiOmniTransientRead(message) from exc


def _request_json(opener, method, url, body=None, timeout=90, api_key=None):
    with _request(opener, method, url, body, timeout, api_key) as response:
        raw = response.read()
    try:
        value = json.loads(raw.decode("utf-8", "replace") or "{}")
    except (UnicodeError, ValueError) as exc:
        if method == "POST":
            raise GeminiOmniCreateOutcomeUnknown(
                "Gemini Omni 提交返回无法确认，已禁止自动重发"
            ) from exc
        raise GeminiOmniTransientRead("Gemini Omni 查询返回格式异常") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Gemini Omni 返回格式异常")
    return value


def _image_part(image):
    if isinstance(image, dict):
        mime = str(image.get("mime_type") or image.get("mimeType") or "").lower()
        encoded = str(image.get("data") or "")
    else:
        match = re.fullmatch(
            r"data:(image/(?:jpeg|jpg|png|webp));base64,([A-Za-z0-9+/=\s]+)",
            str(image or "").strip(),
            re.IGNORECASE,
        )
        if not match:
            raise ValueError("Gemini Omni 参考图必须是 JPEG、PNG 或 WebP")
        mime, encoded = match.group(1).lower(), match.group(2)
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in IMAGE_MIMES:
        raise ValueError("Gemini Omni 参考图必须是 JPEG、PNG 或 WebP")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", encoded), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Gemini Omni 参考图数据无效") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Gemini Omni 单张参考图必须小于 8MB")
    return {"type": "image", "data": base64.b64encode(raw).decode("ascii"),
            "mime_type": mime}


def _duration_prompt(prompt, seconds):
    # duration 是硬参数；时间线用于约束镜头内容在所选秒数内完整收束。
    return (
        "%s\n\n[0-%ss] Complete the video within this timeline and end the "
        "final shot at %s seconds." % (prompt, seconds, seconds)
    )


def build_request(prompt, reference_images=None, aspect_ratio="16:9",
                  duration=6, delivery="uri"):
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("请输入 Gemini Omni 视频提示词")
    if aspect_ratio not in RATIOS:
        raise ValueError("Gemini Omni 比例仅支持 9:16、16:9")
    if isinstance(duration, bool):
        raise ValueError("Gemini Omni 目标时长必须是 3-10 秒整数")
    try:
        duration = int(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("Gemini Omni 目标时长必须是 3-10 秒整数") from exc
    if duration < 3 or duration > 10:
        raise ValueError("Gemini Omni 目标时长必须是 3-10 秒整数")
    images = list(reference_images or [])
    if len(images) > MAX_REFERENCE_IMAGES:
        raise ValueError("Gemini Omni 最多支持 3 张参考图")
    parts = [_image_part(image) for image in images]
    effective_prompt = _duration_prompt(prompt, duration)
    if parts:
        parts.append({"type": "text", "text": effective_prompt})
        input_value = parts
        task = "image_to_video" if len(images) == 1 else "reference_to_video"
    else:
        input_value = effective_prompt
        task = "text_to_video"
    response_format = {
        "type": "video",
        "aspect_ratio": aspect_ratio,
        "duration": "%ds" % duration,
    }
    if delivery == "uri":
        response_format["delivery"] = "uri"
    elif delivery != "inline":
        raise ValueError("Gemini Omni 输出方式仅支持 uri 或 inline")
    return {
        "model": MODEL,
        "input": input_value,
        "response_format": response_format,
        "generation_config": {"video_config": {"task": task}},
        "background": True,
        "store": True,
        "stream": False,
    }


def _extract_video(response):
    for step in response.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for item in step.get("content") or []:
            if isinstance(item, dict) and (
                    item.get("type") == "video"
                    or str(item.get("mime_type") or "").startswith("video/")):
                return item
    output = response.get("output_video")
    if isinstance(output, dict):
        return output
    raise GeminiOmniProviderFailed("Gemini Omni 已完成但没有返回视频")


def _file_name(uri):
    parsed = urllib.parse.urlsplit(uri)
    trusted_hosts = {
        urllib.parse.urlsplit(OFFICIAL_API_BASE).netloc.lower(),
        urllib.parse.urlsplit(API_BASE).netloc.lower(),
    }
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() not in trusted_hosts:
        raise RuntimeError("Gemini Omni 返回了非官方视频地址，已拒绝下载")
    match = re.search(r"/(files/[^/:?]+)", parsed.path)
    if not match:
        raise RuntimeError("Gemini Omni 返回的视频文件地址无效")
    return match.group(1)


def _file_request_url(uri):
    _file_name(uri)
    parsed = urllib.parse.urlsplit(uri)
    base = urllib.parse.urlsplit(API_BASE)
    if parsed.netloc.lower() == base.netloc.lower():
        return uri
    return urllib.parse.urlunsplit((
        base.scheme,
        base.netloc,
        base.path.rstrip("/") + parsed.path,
        parsed.query,
        "",
    ))


def _poll_file(opener, uri, now=None, sleep=None, heartbeat=None,
               job_id=None, interaction_id=None, api_key=None,
               provider_key_id=None):
    now, sleep = now or time.monotonic, sleep or time.sleep
    name = _file_name(uri)
    status_url = API_BASE + "/v1beta/" + name
    deadline = now() + TIMEOUT
    last_error = None
    while now() < deadline:
        try:
            info = _request_json(
                opener, "GET", status_url, timeout=60, api_key=api_key
            )
            last_error = None
        except GeminiOmniTransientRead as exc:
            last_error = exc
            sleep(POLL_INTERVAL)
            continue
        state = str(info.get("state") or "").upper()
        if heartbeat:
            heartbeat(job_id, "omni_file_" + (state.lower() or "processing"),
                      provider_video_id=interaction_id,
                      provider_key_id=provider_key_id, model=MODEL)
        if state == "ACTIVE":
            return
        if state == "FAILED":
            raise GeminiOmniProviderFailed(
                "Gemini Omni 视频处理失败：%s"
                % _redact(
                    info.get("error") or info.get("message") or state,
                    api_key,
                )
            )
        sleep(POLL_INTERVAL)
    if last_error:
        raise TimeoutError("Gemini Omni 视频文件查询超时：%s" % last_error)
    raise TimeoutError("Gemini Omni 视频文件处理超时")


def _read_limited(response):
    declared = response.headers.get("Content-Length")
    if declared and int(declared) > MAX_VIDEO_BYTES:
        raise RuntimeError("Gemini Omni 成片超过下载大小上限")
    chunks, total = [], 0
    while True:
        chunk = response.read(min(1024 * 1024, MAX_VIDEO_BYTES - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_VIDEO_BYTES:
            raise RuntimeError("Gemini Omni 成片超过下载大小上限")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise RuntimeError("Gemini Omni 成片下载为空")
    return data


def _download_uri(opener, uri, sleep=None, api_key=None):
    request_url = _file_request_url(uri)
    sleep = sleep or time.sleep
    last = None
    for attempt, delay in enumerate((0, 2, 5, 10)):
        if delay:
            sleep(delay)
        try:
            with _request(
                opener, "GET", request_url, timeout=300, api_key=api_key
            ) as response:
                return _read_limited(response)
        except GeminiOmniTransientRead as exc:
            last = exc
            if attempt == 3:
                break
    raise GeminiOmniTransientRead("Gemini Omni 成片下载失败：%s" % last)


def _decode_inline(item):
    try:
        data = base64.b64decode(str(item.get("data") or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Gemini Omni 返回的视频数据无效") from exc
    if not data or len(data) > MAX_VIDEO_BYTES:
        raise RuntimeError("Gemini Omni 返回的视频为空或超过大小上限")
    return data


def _probe_duration(data):
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4") as temp:
            temp.write(data)
            temp.flush()
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", temp.name],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=10, check=False,
            )
        value = float((result.stdout or "").strip())
        return round(value, 3) if value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _poll_interaction(opener, interaction_id, initial=None, now=None, sleep=None,
                      heartbeat=None, job_id=None, api_key=None,
                      provider_key_id=None):
    """只用 GET 恢复后台任务；取得 id 后绝不再次创建。"""
    now, sleep = now or time.monotonic, sleep or time.sleep
    deadline = now() + TIMEOUT
    current = initial
    last_error = None
    url = API_BASE + "/v1beta/interactions/" + urllib.parse.quote(
        str(interaction_id), safe=""
    )
    while now() < deadline:
        if current is None:
            try:
                current = _request_json(
                    opener, "GET", url, timeout=60, api_key=api_key
                )
                last_error = None
            except GeminiOmniTransientRead as exc:
                last_error = exc
                sleep(POLL_INTERVAL)
                continue
        status = str(current.get("status") or "").strip().lower()
        if not status and any(
                isinstance(step, dict) and step.get("type") == "model_output"
                for step in (current.get("steps") or [])):
            status = "completed"
        if heartbeat:
            heartbeat(
                job_id,
                "omni_" + (status or "in_progress"),
                provider_video_id=interaction_id,
                provider_key_id=provider_key_id,
                model=MODEL,
            )
        if status == "completed":
            return current
        if status in {
            "failed", "cancelled", "canceled", "incomplete", "budget_exceeded",
            "requires_action",
        }:
            detail = current.get("error") or current.get("message") or status
            raise GeminiOmniProviderFailed(
                "Gemini Omni 视频生成失败：%s" % _redact(detail, api_key)
            )
        if status not in {"", "queued", "in_progress"}:
            raise GeminiOmniProviderFailed(
                "Gemini Omni 返回未知任务状态：%s" % _redact(status, api_key)
            )
        sleep(POLL_INTERVAL)
        current = None
    if last_error:
        raise TimeoutError("Gemini Omni 任务查询超时：%s" % last_error)
    raise TimeoutError("Gemini Omni 视频生成超时")


def _finish_response(opener, response, interaction_id, duration,
                     aspect_ratio, job_id=None, heartbeat=None,
                     now=None, sleep=None, api_key=None,
                     provider_key_id=None):
    item = _extract_video(response)
    uri = str(item.get("uri") or "").strip()
    if uri:
        _poll_file(
            opener, uri, now, sleep, heartbeat, job_id, interaction_id,
            api_key, provider_key_id,
        )
        video = _download_uri(opener, uri, sleep, api_key)
    else:
        video = _decode_inline(item)
    actual_duration = _probe_duration(video)
    return {
        "provider": "google_gemini_omni",
        "model": MODEL,
        "interaction_id": interaction_id,
        "request_id": interaction_id,
        "source_video_url": uri or None,
        "mime_type": str(item.get("mime_type") or "video/mp4"),
        "video_bytes": video,
        "aspect_ratio": aspect_ratio,
        "resolution": "720p",
        "requested_duration": int(duration),
        "duration": actual_duration or int(duration),
        "duration_is_measured": actual_duration is not None,
        "provider_key_id": provider_key_id,
    }


def generate(prompt, reference_images=None, aspect_ratio="16:9", duration=6,
             delivery="uri", job_id=None, heartbeat=None, now=None, sleep=None,
             api_key=None, provider_key_id=None):
    """生成一次官方 Omni 视频并返回成片字节；付费 POST 永不自动重试。"""
    body = build_request(prompt, reference_images, aspect_ratio, duration, delivery)
    opener = _opener()
    response = _request_json(
        opener, "POST", API_BASE + "/v1beta/interactions", body,
        timeout=TIMEOUT, api_key=api_key,
    )
    interaction_id = str(response.get("id") or "").strip()
    if not interaction_id:
        raise GeminiOmniCreateOutcomeUnknown(
            "Gemini Omni 提交结果未知，未返回 interaction id；已禁止自动重发"
        )
    completed = _poll_interaction(
        opener, interaction_id, response, now, sleep, heartbeat, job_id,
        api_key, provider_key_id,
    )
    return _finish_response(
        opener, completed, interaction_id, duration, aspect_ratio,
        job_id, heartbeat, now, sleep, api_key, provider_key_id,
    )


def resume(interaction_id, duration=6, aspect_ratio="16:9", job_id=None,
           heartbeat=None, now=None, sleep=None, api_key=None,
           provider_key_id=None):
    """恢复已持久化的后台任务，只执行幂等 GET。"""
    interaction_id = str(interaction_id or "").strip()
    if not interaction_id:
        raise ValueError("恢复 Gemini Omni 视频缺少 interaction id")
    if aspect_ratio not in RATIOS:
        raise ValueError("Gemini Omni 比例仅支持 9:16、16:9")
    opener = _opener()
    completed = _poll_interaction(
        opener, interaction_id, None, now, sleep, heartbeat, job_id,
        api_key, provider_key_id,
    )
    return _finish_response(
        opener, completed, interaction_id, duration, aspect_ratio,
        job_id, heartbeat, now, sleep, api_key, provider_key_id,
    )
