# -*- coding: utf-8 -*-
"""MetaSo MiniMax-H3 asynchronous video adapter."""

import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import provider_keys


MODEL = "MiniMax-H3"
ORIGIN_METASO = "metaso"
ORIGIN_LEGACY = "legacy"
METASO_API_BASE = "https://metaso.cn/api/minimax"
LEGACY_API_BASE = "https://api.minimaxi.com"
ORIGIN_API_BASES = {
    ORIGIN_METASO: METASO_API_BASE,
    ORIGIN_LEGACY: LEGACY_API_BASE,
}
# Compatibility alias for callers which submit new tasks. Recovery must use
# api_base_for_origin() and never a mutable environment value.
API_BASE = METASO_API_BASE
API_KEY = os.environ.get("MINIMAX_API_KEY", "").strip()
TIMEOUT = max(120, int(os.environ.get("MINIMAX_H3_TIMEOUT", "1800") or 1800))
POLL_INTERVAL = max(5, int(os.environ.get("MINIMAX_H3_POLL_INTERVAL", "10") or 10))
TRANSIENT_CODES = {408, 429} | set(range(500, 600))
RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"}
MAX_REFERENCE_IMAGES = 5
MAX_IMAGE_BYTES = 30 * 1024 * 1024
DEFAULT_RESOLUTION = "2K"
RESOLUTIONS = {DEFAULT_RESOLUTION}
RESULT_HOSTS = {
    "cdn.hailuoai.com",
    "cdn.minimax.chat",
    "file.cdn.minimax.io",
    "filecdn.minimax.chat",
}
RESULT_MAX_BYTES = 250 * 1024 * 1024


class CreateOutcomeUnknown(RuntimeError):
    pass


class MiniMaxRejected(RuntimeError):
    pass


class MiniMaxCredentialRejected(MiniMaxRejected):
    pass


class MiniMaxProviderFailed(RuntimeError):
    pass


class TransientMiniMaxError(RuntimeError):
    pass


class MiniMaxOriginUnknown(ValueError):
    pass


def available():
    return provider_keys.has_candidate("minimax")


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _safe(value, api_key=None, limit=500):
    text = str(value or "")
    for secret in (api_key, API_KEY):
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:limit]


def _base_error(payload):
    base = (payload or {}).get("base_resp") or {}
    try:
        code = int(base.get("status_code") or 0)
    except (TypeError, ValueError):
        code = -1
    return code, str(base.get("status_msg") or "").strip()


def _human_error(code, detail):
    low = str(detail or "").lower()
    if code in {401, 403, 1004} or "login fail" in low or "invalid api key" in low:
        return "MetaSo MiniMax 密钥无效，请重新创建并配置 API Key"
    if code == 402 or any(x in low for x in ("balance", "insufficient", "余额", "欠费")):
        return "MetaSo MiniMax 余额不足，请充值后重试"
    if any(x in low for x in ("moderation", "sensitive", "risk", "审核", "敏感")):
        return "视频内容未通过安全审核，请调整提示词或首帧图片"
    if "media metadata is invalid" in low or "2013" in low:
        return "麦克视频请求参数或参考图无法识别，请检查参数及 JPG/PNG 图片"
    if code == 429:
        return "MiniMax 当前并发繁忙，请稍后重试"
    return "MetaSo MiniMax 接口失败：%s" % (detail or code)


def _api_base(value=None):
    base = str(value or API_BASE).strip().rstrip("/")
    if base not in {API_BASE, LEGACY_API_BASE}:
        raise ValueError("麦克视频任务来源无效，已停止自动恢复")
    return base


def api_base_for_origin(origin):
    value = str(origin or ORIGIN_LEGACY).strip().lower()
    try:
        return ORIGIN_API_BASES[value]
    except KeyError as exc:
        raise ValueError("麦克视频任务来源无效，已停止自动恢复") from exc


def new_task_origin():
    """Single source of truth for every new paid MiniMax submission."""
    return ORIGIN_METASO


def new_task_api_base():
    return api_base_for_origin(new_task_origin())


def historical_origin_from_environment():
    """Infer a pre-marker task only from the endpoint used by the old release."""
    base = str(os.environ.get("MINIMAX_API_BASE") or "").strip().rstrip("/")
    if not base:
        return ORIGIN_LEGACY
    for origin, canonical_base in ORIGIN_API_BASES.items():
        if base == canonical_base:
            return origin
    raise MiniMaxOriginUnknown(
        "旧麦克视频任务来源无法安全判定，请人工确认原提交端点后恢复"
    )


def origin_from_payload(payload):
    payload = payload or {}
    origin = str(payload.get("_minimax_origin") or "").strip().lower()
    if origin:
        api_base_for_origin(origin)
        return origin
    legacy_base = str(payload.get("_minimax_api_base") or "").strip().rstrip("/")
    if legacy_base:
        for candidate_origin, candidate_base in ORIGIN_API_BASES.items():
            if legacy_base == candidate_base:
                return candidate_origin
        raise ValueError("麦克视频任务来源无效，已停止自动恢复")
    raise MiniMaxOriginUnknown(
        "旧麦克视频任务缺少来源标记，请先完成来源回填"
    )


def _request_json(
    opener, method, path, body=None, timeout=90, api_key=None, api_base=None,
):
    api_key = API_KEY if api_key is None else str(api_key).strip()
    if not api_key:
        raise MiniMaxCredentialRejected("尚未配置 MetaSo MiniMax API Key")
    headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "User-Agent": "huangque-content/1.0",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _api_base(api_base) + "/" + path.lstrip("/"),
        data=data, headers=headers, method=method,
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:1000]
        except Exception:
            detail = str(exc)
        if method == "POST" and (exc.code == 408 or 500 <= exc.code <= 599):
            raise CreateOutcomeUnknown("MiniMax 提交结果未知，已禁止自动重发") from exc
        if method == "GET" and exc.code in TRANSIENT_CODES:
            raise TransientMiniMaxError(_human_error(exc.code, detail)) from exc
        error_type = MiniMaxCredentialRejected if exc.code in {401, 403} else MiniMaxRejected
        raise error_type(_human_error(exc.code, detail)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if method == "POST":
            raise CreateOutcomeUnknown("MiniMax 网络异常，提交结果未知，已禁止自动重发") from exc
        raise TransientMiniMaxError("MiniMax 网络异常：" + _safe(exc, api_key, 240)) from exc
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except (UnicodeError, ValueError) as exc:
        if method == "POST":
            raise CreateOutcomeUnknown("MiniMax 提交响应格式异常，已禁止自动重发") from exc
        raise TransientMiniMaxError("MiniMax 查询响应格式异常") from exc
    if not isinstance(payload, dict):
        raise MiniMaxRejected("MiniMax 返回格式异常")
    code, detail = _base_error(payload)
    if code:
        error_type = MiniMaxCredentialRejected if code == 1004 else MiniMaxRejected
        raise error_type(_human_error(code, detail))
    return payload


def check_credentials(api_key, opener=None):
    """Side-effect-free authentication check used before any point deduction."""
    # Keep this probe identical to the accepted admin-console check.  The old
    # dummy-task lookup could return MiniMax base code 1004 for a missing task
    # and incorrectly quarantine an otherwise usable key before submission.
    _request_json(
        opener or _opener(), "GET",
        "/v2/query/video_generation?page_num=1&page_size=1",
        timeout=30, api_key=api_key,
    )
    return True


def _image_item(value):
    value = str(value or "").strip()
    match = re.fullmatch(
        r"data:(image/(?:jpeg|jpg|png|webp));base64,([A-Za-z0-9+/=\s]+)",
        value,
        re.IGNORECASE,
    )
    if match:
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("麦克视频参考图数据无效") from exc
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("麦克视频单张参考图必须小于 30MB")
        try:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                expected = (
                    "JPEG"
                    if match.group(1).lower() in {"image/jpeg", "image/jpg"}
                    else match.group(1)[6:].upper()
                )
                if image.format != expected:
                    raise ValueError("麦克视频参考图格式与图片内容不一致")
                if not 256 <= image.width <= 5760 or not 256 <= image.height <= 5760:
                    raise ValueError("麦克视频参考图宽高必须为 256～5760 像素")
                image.load()
                if image.format != "PNG" or image.mode not in {"RGB", "RGBA"}:
                    clean = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    output = io.BytesIO()
                    clean.save(output, "PNG", optimize=True)
                    raw = output.getvalue()
                    value = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        except ValueError:
            raise
        except Exception:
            raise ValueError(
                "麦克视频参考图无法识别，请重新上传 JPG 或 PNG 图片"
            ) from None
    else:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("麦克视频参考图必须是图片数据或公网 URL")
    return {
        "type": "image_url",
        "image_url": {"url": value},
        "role": "reference_image",
    }


def build_request(
    prompt, reference_images=None, ratio="9:16", duration=5,
    resolution=DEFAULT_RESOLUTION, allow_legacy_resolution=False,
):
    prompt = str(prompt or "").strip()
    if not prompt or len(prompt) > 7000:
        raise ValueError("麦克视频提示词必须为 1～7000 个字符")
    refs = list(reference_images or [])
    if len(refs) > MAX_REFERENCE_IMAGES:
        raise ValueError("麦克视频最多使用 5 张参考图")
    if isinstance(duration, bool):
        raise ValueError("麦克视频时长必须为 4～15 秒整数")
    try:
        duration = int(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("麦克视频时长必须为 4～15 秒整数") from exc
    if duration < 4 or duration > 15:
        raise ValueError("麦克视频时长必须为 4～15 秒整数")
    ratio = str(ratio or "").strip()
    if ratio not in RATIOS:
        raise ValueError("麦克视频不支持该画面比例")
    resolution = str(resolution or DEFAULT_RESOLUTION).strip().upper()
    allowed_resolutions = (
        RESOLUTIONS | {"768P"} if allow_legacy_resolution else RESOLUTIONS
    )
    if resolution not in allowed_resolutions:
        raise ValueError("麦克视频分辨率仅支持 2K；旧任务可继续使用 768P")
    return {
        "model": MODEL,
        "content": [{"type": "text", "text": prompt}] + [_image_item(x) for x in refs],
        "duration": duration,
        "resolution": resolution,
        "ratio": ratio,
    }


def query_task(task_id, api_key, opener=None, api_base=None):
    return _request_json(
        opener or _opener(), "GET",
        "/v2/query/video_generation/" + urllib.parse.quote(str(task_id), safe=""),
        timeout=60, api_key=api_key, api_base=api_base,
    )


def _poll(opener, task_id, duration, ratio, resolution, job_id=None, heartbeat=None,
          now=None, sleep=None, api_key=None, provider_key_id=None, api_base=None):
    now, sleep = now or time.time, sleep or time.sleep
    deadline = now() + TIMEOUT
    last_error = None
    while now() < deadline:
        try:
            payload = query_task(task_id, api_key, opener, api_base=api_base)
            last_error = None
        except TransientMiniMaxError as exc:
            last_error = exc
            if heartbeat:
                heartbeat(
                    job_id, "minimax_retrying", provider_video_id=task_id,
                    provider_key_id=provider_key_id, model=MODEL,
                    error=_safe(exc, api_key=api_key, limit=300),
                )
            sleep(POLL_INTERVAL)
            continue
        task = payload.get("task") or {}
        status = str(task.get("status") or "").strip().lower()
        if heartbeat:
            heartbeat(job_id, "minimax_" + (status or "unknown"), provider_video_id=task_id,
                      provider_key_id=provider_key_id, model=MODEL, error="")
        if status == "succeeded":
            content = task.get("content") or {}
            url = str(content.get("url") or "").strip() if isinstance(content, dict) else ""
            if not url:
                raise MiniMaxProviderFailed("麦克视频已完成但没有返回成片地址")
            resolved = str(task.get("resolution") or resolution).strip().lower()
            return {"request_id": task_id, "source_video_url": url, "model": MODEL,
                    "duration": task.get("duration") or duration,
                    "ratio": task.get("ratio") or ratio, "resolution": resolved,
                    "provider": "minimax_h3_cn"}
        if status in {"failed", "cancelled", "canceled"}:
            raise MiniMaxProviderFailed(
                "麦克视频生成失败：" + _safe(task.get("error") or payload, api_key=api_key)
            )
        if status not in {"preparing", "queueing", "queued", "processing", "running"}:
            raise MiniMaxProviderFailed("麦克视频返回未知状态：" + (status or "空"))
        sleep(POLL_INTERVAL)
    if last_error:
        raise TimeoutError("麦克视频查询超时：" + _safe(last_error, api_key=api_key, limit=240))
    raise TimeoutError("麦克视频生成超时")


def generate(prompt, reference_images=None, ratio="9:16", duration=5,
             resolution=DEFAULT_RESOLUTION,
             job_id=None, heartbeat=None, now=None, sleep=None, api_key=None,
             provider_key_id=None, api_base=None):
    body = build_request(prompt, reference_images, ratio, duration, resolution)
    opener = _opener()
    created = _request_json(opener, "POST", "/v2/video_generation", body,
                            timeout=120, api_key=api_key, api_base=api_base)
    task_id = str(created.get("task_id") or "").strip()
    if not task_id:
        raise CreateOutcomeUnknown("麦克视频提交结果未知：未返回任务编号")
    if heartbeat:
        heartbeat(job_id, "minimax_queued", provider_video_id=task_id,
                  provider_key_id=provider_key_id, model=MODEL, error="")
    return _poll(
        opener, task_id, body["duration"], body["ratio"], body["resolution"],
        job_id, heartbeat, now, sleep, api_key, provider_key_id,
        api_base,
    )


def resume(task_id, duration=5, ratio="9:16", job_id=None, heartbeat=None,
           now=None, sleep=None, api_key=None, provider_key_id=None,
           resolution=DEFAULT_RESOLUTION, api_base=None):
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("恢复麦克视频缺少任务编号")
    return _poll(
        _opener(), task_id, int(duration), ratio, resolution, job_id, heartbeat,
        now, sleep, api_key, provider_key_id, api_base,
    )
