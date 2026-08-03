# -*- coding: utf-8 -*-
"""火山方舟官方 Seedance 2.0 异步视频适配器。"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import provider_keys


ARK_API_KEY = os.environ.get("ARK_API_KEY", "").strip()
ARK_BASE = os.environ.get(
    "ARK_BASE", "https://ark.cn-beijing.volces.com/api/v3"
).rstrip("/")
SEEDANCE_MODEL = os.environ.get(
    "ARK_SEEDANCE_MODEL", "doubao-seedance-2-0-260128"
).strip()
SEEDANCE_FAST_MODEL = os.environ.get(
    "ARK_SEEDANCE_FAST_MODEL", "doubao-seedance-2-0-fast-260128"
).strip()
SEEDANCE_TIMEOUT = int(os.environ.get("ARK_SEEDANCE_TIMEOUT", "1200") or 1200)
SEEDANCE_POLL_INTERVAL = int(
    os.environ.get("ARK_SEEDANCE_POLL_INTERVAL", "10") or 10
)

MODELS = {SEEDANCE_MODEL, SEEDANCE_FAST_MODEL}
RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "adaptive"}
RESOLUTIONS = {
    SEEDANCE_MODEL: {"480p", "720p", "1080p"},
    SEEDANCE_FAST_MODEL: {"480p", "720p"},
}
TRANSIENT_HTTP_CODES = {408, 429} | set(range(500, 600))
TRANSIENT_BACKOFF = (5, 10, 20, 30)


class CreateOutcomeUnknown(RuntimeError):
    """创建请求可能已被接受；调用方不得自动重发。"""


class SeedanceRejected(RuntimeError):
    """创建请求被官方明确拒绝，没有可恢复的 task id。"""


class SeedanceCredentialRejected(SeedanceRejected):
    """当前密钥在任务创建前被明确拒绝，可安全尝试下一条线路。"""


class SeedanceProviderFailed(RuntimeError):
    """已取得 task id，但官方返回明确失败终态。"""


class TransientSeedanceError(RuntimeError):
    """仅供已知 task id 的幂等查询重试。"""


def available():
    return provider_keys.has_candidate("seedance")


def _opener():
    # 方舟在国内，显式忽略进程级海外代理。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _safe_text(value, limit=500, api_key=None):
    text = str(value or "")
    for secret in (api_key, ARK_API_KEY):
        if secret:
            text = re.sub(re.escape(secret), "***", text, flags=re.IGNORECASE)
    # Signed object URLs are credentials. Redact the complete query instead
    # of trying to enumerate provider-specific signature parameter names.
    text = re.sub(
        r"(?i)(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]+",
        lambda match: match.group(1) + "?[REDACTED]",
        text,
    )
    return text[:limit]


def _payload_detail(payload, api_key=None):
    if not isinstance(payload, dict):
        return _safe_text(payload, api_key=api_key)
    detail = payload.get("error") or payload.get("message") or payload.get("detail")
    if isinstance(detail, dict):
        detail = (
            detail.get("message")
            or detail.get("detail")
            or detail.get("code")
            or detail
        )
    return _safe_text(detail or payload, api_key=api_key)


def _error_detail(exc, api_key=None):
    try:
        raw = exc.read().decode("utf-8", "replace")[:2000]
        return _payload_detail(json.loads(raw or "{}"), api_key)
    except Exception:
        return _safe_text(exc, api_key=api_key)


def _credential_rejected(code, detail):
    """Only explicit auth failures may quarantine a pooled API key."""
    text = _safe_text(detail).lower()
    if code == 401:
        return True
    if code != 403:
        return False
    return any(phrase in text for phrase in (
        "invalid api key", "invalid_api_key", "api key invalid",
        "invalid credential", "authentication failed", "signature invalid",
    ))


def _human_error(code, detail, api_key=None):
    text = _safe_text(detail, api_key=api_key)
    low = text.lower()
    summary = "（上游摘要：%s）" % text if text else ""
    if _credential_rejected(code, text):
        return "Seedance 官方视频鉴权失败，请检查 ARK_API_KEY" + summary
    if code == 402 or any(
        word in low for word in ("insufficient", "balance", "arrears", "余额", "欠费")
    ):
        return "Seedance 官方视频账户余额不足，请先充值" + summary
    if any(
        word in low
        for word in (
            "sensitivecontent",
            "moderation",
            "content policy",
            "安全审核",
            "敏感",
        )
    ):
        return "Seedance 内容未通过安全审核，请调整提示词或参考图" + summary
    if code == 429 or any(word in low for word in ("rate limit", "too many", "限流")):
        return "Seedance 官方视频并发繁忙，请稍后重试" + summary
    if any(
        word in low
        for word in ("modelnotfound", "permission", "not activated", "未开通", "无权限")
    ):
        return "Seedance 官方模型未开通或当前账号无权限" + summary
    return "Seedance 官方视频接口失败: HTTP %s %s" % (code, text)


def _request_json(opener, method, path, body=None, timeout=90, api_key=None):
    api_key = ARK_API_KEY if api_key is None else str(api_key).strip()
    if not api_key:
        raise ValueError("Seedance 官方视频未配置（ARK_API_KEY）")
    url = ARK_BASE + "/" + str(path or "").lstrip("/")
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
        url, data=data, headers=headers, method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc, api_key)
        if method == "POST" and exc.code in TRANSIENT_HTTP_CODES - {429}:
            raise CreateOutcomeUnknown(
                "Seedance 提交结果未知，请勿重复提交: HTTP %s %s"
                % (exc.code, detail)
            ) from exc
        if method == "GET" and exc.code in TRANSIENT_HTTP_CODES:
            raise TransientSeedanceError(_human_error(exc.code, detail, api_key)) from exc
        if method == "POST":
            if _credential_rejected(exc.code, detail):
                raise SeedanceCredentialRejected(
                    _human_error(exc.code, detail, api_key)
                ) from exc
            raise SeedanceRejected(_human_error(exc.code, detail, api_key)) from exc
        raise RuntimeError(_human_error(exc.code, detail, api_key)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        message = "Seedance 官方视频网络异常: %s" % _safe_text(
            exc, 300, api_key
        )
        if method == "POST":
            raise CreateOutcomeUnknown(
                "Seedance 提交结果未知，请勿重复提交: " + message
            ) from exc
        raise TransientSeedanceError(message) from exc

    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except (UnicodeError, ValueError) as exc:
        if method == "POST":
            raise CreateOutcomeUnknown(
                "Seedance 提交结果未知，请勿重复提交：返回内容无法解析"
            ) from exc
        raise TransientSeedanceError("Seedance 查询返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        if method == "POST":
            raise CreateOutcomeUnknown(
                "Seedance 提交结果未知，请勿重复提交：返回格式异常"
            )
        raise TransientSeedanceError("Seedance 查询返回格式异常")
    return payload


def _reference_item(url):
    url = str(url or "").strip()
    parsed = urllib.parse.urlsplit(url)
    valid_http = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                  and not parsed.username and not parsed.password)
    valid_asset = (parsed.scheme == "asset" and bool(re.fullmatch(
        r"asset://asset-[A-Za-z0-9._-]{1,240}", url)))
    if not (valid_http or valid_asset):
        raise ValueError("Seedance 参考图必须是公网 URL 或已授权 asset:// 素材")
    return {
        "type": "image_url",
        "image_url": {"url": url},
        "role": "reference_image",
    }


def _build_payload(
    model,
    prompt,
    duration,
    ratio,
    resolution,
    generate_audio,
    reference_images=None,
):
    model = str(model or "").strip()
    if model not in MODELS:
        raise ValueError("不支持的 Seedance 官方模型")
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("请输入 Seedance 视频提示词")
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        raise ValueError("Seedance 视频时长必须为 4～15 秒") from None
    if duration < 4 or duration > 15:
        raise ValueError("Seedance 视频时长必须为 4～15 秒")
    ratio = str(ratio or "").strip()
    if ratio not in RATIOS:
        raise ValueError("Seedance 不支持该画面比例")
    resolution = str(resolution or "").strip().lower()
    if resolution not in RESOLUTIONS[model]:
        if model == SEEDANCE_FAST_MODEL and resolution == "1080p":
            raise ValueError("Seedance 2.0 Fast 仅支持 480p 或 720p")
        raise ValueError("Seedance 不支持该分辨率")
    if not isinstance(generate_audio, bool):
        raise ValueError("Seedance 声音选项必须为布尔值")
    refs = list(reference_images or [])
    if len(refs) > 9:
        raise ValueError("Seedance 最多支持 9 张参考图")
    content = [{"type": "text", "text": prompt}]
    content.extend(_reference_item(url) for url in refs)
    return {
        "model": model,
        "content": content,
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "generate_audio": generate_audio,
        "watermark": False,
    }


def _poll(
    opener,
    task_id,
    model,
    duration,
    ratio,
    resolution,
    generate_audio,
    job_id=None,
    heartbeat=None,
    now=None,
    sleep=None,
    api_key=None,
    provider_key_id=None,
):
    now = now or time.time
    sleep = sleep or time.sleep
    deadline = now() + SEEDANCE_TIMEOUT
    transient_attempt = 0
    last_transient = None

    while now() < deadline:
        try:
            payload = _request_json(
                opener,
                "GET",
                "/contents/generations/tasks/"
                + urllib.parse.quote(str(task_id), safe=""),
                timeout=60,
                api_key=api_key,
            )
            transient_attempt = 0
            last_transient = None
        except TransientSeedanceError as exc:
            last_transient = exc
            if heartbeat:
                heartbeat(
                    job_id,
                    "seedance_retrying",
                    provider_video_id=task_id,
                    provider_key_id=provider_key_id,
                    model=model,
                    error=_safe_text(exc, 300, api_key),
                )
            delay = TRANSIENT_BACKOFF[
                min(transient_attempt, len(TRANSIENT_BACKOFF) - 1)
            ]
            transient_attempt += 1
            if now() + delay >= deadline:
                break
            sleep(delay)
            continue

        status = str(payload.get("status") or "").strip().lower()
        if heartbeat:
            heartbeat(
                job_id,
                "seedance_" + (status or "unknown"),
                provider_video_id=task_id,
                provider_key_id=provider_key_id,
                model=str(payload.get("model") or model),
                error="",
            )
        if status == "succeeded":
            content = payload.get("content") or {}
            video_url = (
                str(content.get("video_url") or "").strip()
                if isinstance(content, dict)
                else ""
            )
            if not video_url:
                raise RuntimeError("Seedance 视频已完成但未返回成片 URL")
            usage = payload.get("usage") or {}
            return {
                "request_id": task_id,
                "model": str(payload.get("model") or model),
                "source_video_url": video_url,
                "duration": payload.get("duration") or duration,
                "ratio": str(payload.get("ratio") or ratio),
                "resolution": str(payload.get("resolution") or resolution),
                "generate_audio": (
                    payload.get("generate_audio")
                    if isinstance(payload.get("generate_audio"), bool)
                    else generate_audio
                ),
                "completion_tokens": (
                    usage.get("completion_tokens")
                    if isinstance(usage, dict)
                    else None
                ),
            }
        if status in {"failed", "expired", "cancelled", "canceled"}:
            detail = _payload_detail(payload, api_key)
            message = _human_error(400, detail, api_key)
            if message.startswith("Seedance 官方视频接口失败"):
                message = "Seedance 视频生成失败: " + detail
            raise SeedanceProviderFailed(message)
        if status not in {"queued", "running"}:
            raise SeedanceProviderFailed(
                "Seedance 官方视频返回未知状态: " + (status or "空")
            )
        sleep(SEEDANCE_POLL_INTERVAL)

    if last_transient:
        raise TimeoutError(
            "Seedance 视频查询超时: " + _safe_text(last_transient, 240, api_key)
        )
    raise TimeoutError("Seedance 视频生成超时")


def generate(
    model=SEEDANCE_MODEL,
    prompt="",
    duration=5,
    ratio="9:16",
    resolution="720p",
    generate_audio=True,
    reference_images=None,
    job_id=None,
    heartbeat=None,
    now=None,
    sleep=None,
    api_key=None,
    provider_key_id=None,
):
    """只创建一次付费任务；取得 id 后才进入可安全重试的 GET 轮询。"""
    payload = _build_payload(
        model,
        prompt,
        duration,
        ratio,
        resolution,
        generate_audio,
        reference_images,
    )
    opener = _opener()
    try:
        created = _request_json(
            opener,
            "POST",
            "/contents/generations/tasks",
            payload,
            timeout=120,
            api_key=api_key,
        )
    except CreateOutcomeUnknown:
        raise
    except Exception:
        raise
    task_id = str(created.get("id") or "").strip()
    if not task_id:
        raise CreateOutcomeUnknown(
            "Seedance 提交结果未知，请勿重复提交：未返回 task id"
        )
    if heartbeat:
        heartbeat(
            job_id,
            "seedance_" + str(created.get("status") or "queued").lower(),
            provider_video_id=task_id,
            provider_key_id=provider_key_id,
            model=model,
            error="",
        )
    return _poll(
        opener,
        task_id,
        model,
        payload["duration"],
        payload["ratio"],
        payload["resolution"],
        payload["generate_audio"],
        job_id=job_id,
        heartbeat=heartbeat,
        now=now,
        sleep=sleep,
        api_key=api_key,
        provider_key_id=provider_key_id,
    )


def resume(
    task_id,
    model=SEEDANCE_MODEL,
    duration=5,
    ratio="9:16",
    resolution="720p",
    generate_audio=True,
    job_id=None,
    heartbeat=None,
    now=None,
    sleep=None,
    api_key=None,
    provider_key_id=None,
):
    """仅查询已有任务，不会发起新的生成。"""
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("恢复 Seedance 视频缺少 task id")
    return _poll(
        _opener(),
        task_id,
        model,
        duration,
        ratio,
        resolution,
        generate_audio,
        job_id=job_id,
        heartbeat=heartbeat,
        now=now,
        sleep=sleep,
        api_key=api_key,
        provider_key_id=provider_key_id,
    )
