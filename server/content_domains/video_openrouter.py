# -*- coding: utf-8 -*-
"""OpenRouter asynchronous video API adapter for the Grok fallback channel."""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import egress


OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_API_BASE = os.environ.get(
    "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"
).rstrip("/")
OPENROUTER_VIDEO_TIMEOUT = int(os.environ.get("OPENROUTER_VIDEO_TIMEOUT", "1200") or 1200)
OPENROUTER_VIDEO_POLL_INTERVAL = int(
    os.environ.get("OPENROUTER_VIDEO_POLL_INTERVAL", "10") or 10
)
OPENROUTER_MODEL_MAP = {
    "grok-imagine-video": "x-ai/grok-imagine-video",
    "grok-imagine-video-1.5": "x-ai/grok-imagine-video-1.5",
}
TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}
TRANSIENT_BACKOFF = (5, 10, 20, 30)


class TransientOpenRouterError(RuntimeError):
    pass


def available():
    return bool(OPENROUTER_API_KEY)


def download_headers():
    if not OPENROUTER_API_KEY:
        raise ValueError("OpenRouter 视频备用渠道未配置（OPENROUTER_API_KEY）")
    return {"Authorization": "Bearer " + OPENROUTER_API_KEY}


def _opener():
    proxy = egress.preferred_proxy()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener()


def _error_detail(exc):
    try:
        raw = exc.read().decode("utf-8", "replace")[:1000]
        body = json.loads(raw)
        if isinstance(body, dict):
            detail = body.get("error") or body.get("message") or body.get("detail") or raw
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("code") or detail
            return str(detail)[:500]
        return raw
    except Exception:
        return str(exc)[:500]


def _safe_url(path):
    url = urllib.parse.urljoin(OPENROUTER_API_BASE + "/", str(path or "").lstrip("/"))
    base = urllib.parse.urlparse(OPENROUTER_API_BASE)
    target = urllib.parse.urlparse(url)
    if target.scheme != "https" or target.netloc != base.netloc:
        raise RuntimeError("OpenRouter 返回了不可信的轮询地址")
    return url


def _request_json(opener, method, path, body=None, timeout=90):
    if not OPENROUTER_API_KEY:
        raise ValueError("OpenRouter 视频备用渠道未配置（OPENROUTER_API_KEY）")
    headers = {
        "Authorization": "Bearer " + OPENROUTER_API_KEY,
        "Accept": "application/json",
        "User-Agent": "huangque-content/1.0",
        "HTTP-Referer": "https://huangquechuanmei.com",
        "X-Title": "Huangque Content",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_safe_url(path), data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code in TRANSIENT_HTTP_CODES:
            raise TransientOpenRouterError(
                "OpenRouter 视频临时不可用: HTTP %s %s" % (exc.code, detail)
            )
        if exc.code in (401, 403):
            raise RuntimeError("OpenRouter 鉴权失败: HTTP %s %s" % (exc.code, detail))
        if exc.code == 402:
            raise RuntimeError("OpenRouter 账户余额不足: %s" % detail)
        raise RuntimeError("OpenRouter 视频接口失败: HTTP %s %s" % (exc.code, detail))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransientOpenRouterError("OpenRouter 视频网络异常: %s" % str(exc)[:300])


def _poll(opener, request_id, model, duration, job_id=None, heartbeat=None, now=None, sleep=None):
    now = now or time.time
    sleep = sleep or time.sleep
    deadline = now() + OPENROUTER_VIDEO_TIMEOUT
    transient_attempt = 0
    last_transient = None
    while now() < deadline:
        try:
            result = _request_json(
                opener, "GET", "/videos/" + urllib.parse.quote(request_id), timeout=60
            )
            transient_attempt = 0
            last_transient = None
        except TransientOpenRouterError as exc:
            last_transient = exc
            if heartbeat:
                heartbeat(job_id, "openrouter_retrying", provider_video_id=request_id,
                          model=model, error=str(exc)[:300])
            delay = TRANSIENT_BACKOFF[min(transient_attempt, len(TRANSIENT_BACKOFF) - 1)]
            transient_attempt += 1
            if now() + delay >= deadline:
                break
            sleep(delay)
            continue

        status = str(result.get("status") or "").strip().lower()
        if heartbeat:
            heartbeat(job_id, "openrouter_" + (status or "pending"),
                      provider_video_id=request_id, model=model, error="")
        if status == "completed":
            urls = result.get("unsigned_urls") or []
            source_url = str(urls[0] or "").strip() if urls else ""
            if not source_url:
                raise RuntimeError("OpenRouter 视频已完成但未返回免鉴权成片地址")
            return {
                "request_id": request_id,
                "model": model,
                "source_video_url": source_url,
                "duration": duration,
                "usage": result.get("usage"),
                "provider": "openrouter",
            }
        if status in {"failed", "cancelled", "expired"}:
            detail = result.get("error") or result.get("message") or status
            raise RuntimeError("OpenRouter 视频生成失败: %s" % str(detail)[:500])
        sleep(OPENROUTER_VIDEO_POLL_INTERVAL)
    if last_transient:
        raise TimeoutError("OpenRouter 视频查询超时: %s" % str(last_transient)[:200])
    raise TimeoutError("OpenRouter 视频生成超时")


def resume(request_id, model, duration, job_id=None, heartbeat=None, now=None, sleep=None):
    if not str(request_id or "").strip():
        raise ValueError("恢复 OpenRouter 视频缺少 request_id")
    return _poll(_opener(), str(request_id).strip(), model, duration,
                 job_id, heartbeat, now, sleep)


def generate(model, prompt, duration, aspect_ratio, resolution, image_urls=None,
             job_id=None, heartbeat=None, now=None, sleep=None):
    openrouter_model = OPENROUTER_MODEL_MAP.get(model)
    if not openrouter_model:
        raise ValueError("OpenRouter 不支持果肉视频模型：%s" % model)
    payload = {
        "model": openrouter_model,
        "prompt": str(prompt or "").strip(),
        "duration": int(duration),
        "resolution": resolution,
    }
    refs = [str(url).strip() for url in (image_urls or []) if str(url).strip()]
    if model == "grok-imagine-video-1.5":
        if refs:
            payload["frame_images"] = [{
                "type": "image_url", "image_url": {"url": refs[0]}, "frame_type": "first_frame",
            }]
    else:
        payload["aspect_ratio"] = aspect_ratio
        if refs:
            payload["input_references"] = [
                {"type": "image_url", "image_url": {"url": url}} for url in refs
            ]

    # An unknown create outcome must never be retried; the provider may have billed it.
    opener = _opener()
    created = _request_json(opener, "POST", "/videos", payload, timeout=120)
    request_id = str(created.get("id") or "").strip()
    if not request_id:
        raise RuntimeError("OpenRouter 视频服务未返回任务 ID")
    if heartbeat:
        heartbeat(job_id, "openrouter_pending", provider_video_id=request_id,
                  model=model, error="")
    return _poll(opener, request_id, model, duration, job_id, heartbeat, now, sleep)
